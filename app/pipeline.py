"""A reconciliation run: detect, then route. Each step is idempotent and runs in
its own transaction under the per-tenant reconciliation lock, so

* two concurrent runs serialise instead of interleaving,
* no import or conflict resolution can commit while a step is reading: each
  step also takes the *shared* form of every dataset's import lock, which the
  importers take exclusively, so detection never sees half of a new file (a
  receipt feed that looks fresh while its receipts are not yet visible), and
* if routing fails after detection committed, the next run creates the missing
  tasks (it ensures "every open exception has an active task"; it does not
  replay events).

REPEATABLE READ would also give one snapshot, but its snapshot starts with the
first statement (the lock wait), so it would miss what the previous lock holder
committed and turn ordinary contention into serialization failures.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.config import Settings
from app.db.models import ReconciliationRun
from app.db.session import advisory_xact_lock, advisory_xact_lock_shared
from app.detection.rules import RULE_VERSION
from app.detection.service import ShortfallDetector
from app.enums import RunStatus
from app.errors import RunFailed
from app.ingestion.datasets import DATASETS, DatasetRegistry
from app.ingestion.service import describe_failure
from app.observability import log_event
from app.routing.service import TaskRouter

logger = logging.getLogger("opsib.pipeline")


class ReconciliationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        tenant_id: str,
        clock: Clock,
        settings: Settings,
        registry: DatasetRegistry,
    ):
        self._sessions = session_factory
        self._tenant = tenant_id
        self._clock = clock
        self._detector = ShortfallDetector(tenant_id, settings, registry)
        self._router = TaskRouter(tenant_id, settings)
        self._settings = settings

    def run(self) -> ReconciliationRun:
        as_of = self._clock.now()
        with self._sessions() as session, session.begin():
            run = ReconciliationRun(
                id=uuid.uuid4(),
                tenant_id=self._tenant,
                evaluated_as_of=as_of,
                rule_version=RULE_VERSION,
                routing_policy_version=self._router.policy.version,
                params={
                    **self._detector.params.as_dict(),
                    "availability_max_age_minutes": self._settings.availability_max_age_minutes,
                    "task_due_hours": self._settings.task_due_hours,
                },
                status=RunStatus.RUNNING,
            )
            session.add(run)
        run_id = run.id

        summary: dict = {}
        try:
            with self._sessions() as session, session.begin():
                self._lock_for_reading(session)
                run = session.get_one(ReconciliationRun, run_id, with_for_update=True)
                summary["detection"] = self._detector.detect(session, run)
            with self._sessions() as session, session.begin():
                self._lock_for_reading(session)
                run = session.get_one(ReconciliationRun, run_id, with_for_update=True)
                summary["routing"] = self._router.ensure_tasks(session, run)
                run.status = RunStatus.COMPLETED
                run.summary = summary
                run.completed_at = datetime.now(UTC)
        except Exception as exc:
            reason = describe_failure(exc)
            logger.exception("reconciliation_failed", extra={"fields": {"run_id": str(run_id), "reason": reason}})
            with self._sessions() as session, session.begin():
                failed = session.get_one(ReconciliationRun, run_id, with_for_update=True)
                failed.status = RunStatus.FAILED
                failed.failure_reason = reason
                failed.summary = summary
                failed.completed_at = datetime.now(UTC)
            raise RunFailed(
                "reconciliation run failed; completed steps are kept and the next run will finish the work",
                details={"run_id": str(run_id), "reason": reason},
            ) from exc

        log_event(
            logger,
            "reconciliation_finished",
            run_id=str(run.id),
            evaluated_as_of=as_of.isoformat(),
            mapping_version=run.mapping_version,
            outcomes=summary["detection"]["outcomes"],
            exceptions=summary["detection"]["exceptions"],
        )
        return run

    def _lock_for_reading(self, session: Session) -> None:
        advisory_xact_lock(session, self._tenant, "reconcile")
        for dataset in sorted(DATASETS):
            advisory_xact_lock_shared(session, self._tenant, "import", dataset)
