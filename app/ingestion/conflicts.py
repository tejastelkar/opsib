"""Review decisions on source data: conflicting re-sends and rejected rows.

Conflict policy for source records re-sent with different content.

The fixture exports carry no version or last-modified field, so the service
cannot tell a legitimate update from a bad re-send. Policy:

* identical content under the same key  -> idempotent no-op (UNCHANGED)
* different content under the same key  -> the stored fact is kept unchanged,
  a SourceConflict is opened and the fact is marked CONFLICT. The detector
  treats a CONFLICT fact as "cannot evaluate" (a data-quality issue), never
  as a business exception.
* a data steward resolves it: ACCEPT_INCOMING makes the incoming content the
  new version (version + 1); KEEP_EXISTING leaves the fact as it is.
* other open conflicts on the same record are never closed on anyone's behalf.
  After KEEP_EXISTING they still compare against the stored version, so they
  stay open as they are. After ACCEPT_INCOMING they are re-based onto the new
  stored version and stay open, unless their content *is* the new version
  (closed as SUPERSEDED: nothing left to decide).
* the same re-send arriving again is treated as already reviewed only if a
  steward rejected that exact change (KEEP_EXISTING) against the record's
  current version; after any later change to the record it is a new question.

With a source-supplied change sequence, the policy would become "accept a
strictly newer version automatically, flag equal-version-different-content".
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ImportBatch, ImportRow, RejectedRowDismissal, SourceConflict
from app.db.session import advisory_xact_lock
from app.enums import ConflictResolution, ConflictStatus, ReviewStatus, RowOutcome
from app.errors import InvalidRequest, NotFound, PermissionDenied, StateConflict
from app.ingestion.datasets import DatasetRegistry
from app.ingestion.service import fact_payload_json
from app.mapping.repository import load_org_directory
from app.routing.policy import employment_blockers

# Who may decide which version of a source record is true. In production this is
# a permission from the identity provider (a data-steward role), not an HRMS job title.
DATA_STEWARD_ROLES = frozenset({"store_manager", "regional_operations"})
REVIEWER_DECISIONS = frozenset({ConflictResolution.KEEP_EXISTING, ConflictResolution.ACCEPT_INCOMING})


class ConflictService:
    def __init__(self, session_factory: sessionmaker[Session], tenant_id: str, clock: Clock, registry: DatasetRegistry):
        self._sessions = session_factory
        self._tenant = tenant_id
        self._clock = clock
        self._registry = registry

    def resolve(
        self, conflict_id: uuid.UUID, resolution: ConflictResolution, actor_id: str, reason: str
    ) -> SourceConflict:
        if resolution not in REVIEWER_DECISIONS:
            raise InvalidRequest("resolution must be KEEP_EXISTING or ACCEPT_INCOMING", code="INVALID_RESOLUTION")
        with self._sessions() as session, session.begin():
            self._require_steward(session, actor_id)
            conflict = session.scalar(
                select(SourceConflict).where(SourceConflict.id == conflict_id, SourceConflict.tenant_id == self._tenant)
            )
            if conflict is None:
                raise NotFound("conflict not found", details={"conflict_id": str(conflict_id)})
            # Same lock as imports of this dataset, so a resolution and an import
            # of the same records cannot interleave.
            advisory_xact_lock(session, self._tenant, "import", conflict.dataset)
            session.refresh(conflict, with_for_update=True)
            if conflict.status != ConflictStatus.OPEN:
                raise StateConflict(
                    "conflict is already resolved",
                    code="CONFLICT_ALREADY_RESOLVED",
                    details={"resolution": conflict.resolution, "resolved_by": conflict.resolved_by},
                )

            spec = self._registry.dataset(conflict.dataset)
            fact_model = spec.fact_model
            fact = session.scalar(
                select(fact_model)
                .where(
                    fact_model.tenant_id == self._tenant,
                    fact_model.source_system == conflict.source_system,
                    fact_model.source_record_key == conflict.source_record_key,
                )
                .with_for_update()
            )
            if fact is None or fact.content_hash != conflict.existing_content_hash:
                raise StateConflict("the stored record changed after this conflict was raised", code="CONFLICT_STALE")

            now = self._clock.now()
            conflict.status = ConflictStatus.RESOLVED
            conflict.resolution = resolution
            conflict.resolved_by = actor_id
            conflict.resolution_reason = reason
            conflict.resolved_at = now
            conflict.decided_fact_version = fact.version
            session.flush()  # free its slot in the open-conflict unique index before re-basing others

            others = list(
                session.scalars(
                    select(SourceConflict)
                    .where(
                        SourceConflict.tenant_id == self._tenant,
                        SourceConflict.dataset == conflict.dataset,
                        SourceConflict.source_record_key == conflict.source_record_key,
                        SourceConflict.status == ConflictStatus.OPEN,
                    )
                    .order_by(SourceConflict.created_at)
                    .with_for_update()
                )
            )
            if resolution == ConflictResolution.ACCEPT_INCOMING:
                incoming = spec.payload_model.model_validate(conflict.incoming_payload)
                for column, value in spec.fact_columns(incoming).items():
                    setattr(fact, column, value)
                fact.content_hash = conflict.incoming_content_hash
                fact.version += 1
                fact.last_import_id = conflict.import_id
                current_payload = fact_payload_json(spec, fact)
                open_changes: set[str] = set()
                for other in others:
                    if other.incoming_content_hash == fact.content_hash or other.incoming_content_hash in open_changes:
                        other.status = ConflictStatus.RESOLVED
                        other.resolution = ConflictResolution.SUPERSEDED
                        other.resolved_by = actor_id
                        other.resolution_reason = f"its content became the stored version via conflict {conflict.id}"
                        other.resolved_at = now
                        other.decided_fact_version = fact.version
                    else:
                        # Still an open question, now against the new stored version.
                        other.existing_content_hash = fact.content_hash
                        other.existing_payload = current_payload
                        open_changes.add(other.incoming_content_hash)
                still_open = bool(open_changes)
            else:
                still_open = bool(others)

            fact.review_status = ReviewStatus.CONFLICT if still_open else ReviewStatus.OK
        return conflict

    def dismiss_rejected_row(self, row_id: int, actor_id: str, reason: str) -> RejectedRowDismissal:
        """Record that a rejected row needs no correction, so it stops blocking."""
        with self._sessions() as session, session.begin():
            self._require_steward(session, actor_id)
            found = session.execute(
                select(ImportRow, ImportBatch)
                .join(ImportBatch, ImportBatch.id == ImportRow.import_id)
                .where(ImportRow.id == row_id, ImportBatch.tenant_id == self._tenant)
            ).first()
            if found is None:
                raise NotFound("import row not found", details={"row_id": row_id})
            row, batch = found
            if row.outcome != RowOutcome.REJECTED:
                raise InvalidRequest("only rejected rows can be dismissed", code="NOT_A_REJECTED_ROW")
            advisory_xact_lock(session, self._tenant, "import", batch.dataset)
            if session.scalar(select(RejectedRowDismissal).where(RejectedRowDismissal.import_row_id == row_id)):
                raise StateConflict("row is already dismissed", code="ALREADY_DISMISSED")
            dismissal = RejectedRowDismissal(
                tenant_id=self._tenant,
                import_row_id=row_id,
                dismissed_by=actor_id,
                reason=reason,
                dismissed_at=self._clock.now(),
            )
            session.add(dismissal)
            session.flush()
        return dismissal

    def _require_steward(self, session: Session, actor_id: str) -> None:
        directory = load_org_directory(session, self._tenant)
        actor = directory.employees.get(actor_id)
        if actor is None:
            raise PermissionDenied("actor is not in the HRMS roster", code="UNKNOWN_ACTOR")
        if employment_blockers(actor):
            raise PermissionDenied("actor is not an active employee", code="ACTOR_NOT_ACTIVE")
        if actor.role not in DATA_STEWARD_ROLES:
            raise PermissionDenied(
                "only a data steward (store manager or regional operations) can decide a source conflict",
                details={"role": actor.role, "allowed_roles": sorted(DATA_STEWARD_ROLES)},
            )
