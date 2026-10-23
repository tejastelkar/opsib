"""Task actions. Each request is one transaction:

    claim Idempotency-Key (optional) -> lock exception row, then task row -> natural-replay check
    -> optimistic version check -> permission check -> state machine
    -> mutate + append events -> store response under the key -> commit

Retry safety (the "client timed out after we committed" case)
* With an Idempotency-Key: the key, the request fingerprint and the response
  are written in the same transaction as the state change. A retry with the
  same key and same request returns the stored response (Idempotent-Replay:
  true) without touching the task. The same key with a different request is
  refused. A concurrent duplicate blocks on the key's unique index until the
  first commits, then replays it.
* Without a key the server can only recognise an *immediate* retry: if the
  actor's last action on the task was this exact request and nothing has
  happened since, it is acknowledged as a replay (no new event, no version
  bump). Anything else goes through the state machine and the business rules
  again: a completion retried after verification gets 409 INVALID_TRANSITION,
  and one retried after a rejection is refused because it repeats the rejected
  submission. Exactly-once needs the key; clients should always send one.
* Only successful outcomes are stored; a refused request can be retried.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ExceptionCase, IdempotencyRecord, Task, TaskEvent
from app.enums import ACTIVE_TASK_STATUSES, ExceptionStatus, ResolutionCode, TaskStatus
from app.errors import IdempotencyKeyReused, InvalidRequest, NotFound, PermissionDenied, StateConflict
from app.hashing import content_hash
from app.mapping.model import CanonicalEmployee
from app.mapping.repository import OrgDirectory, load_org_directory
from app.routing.policy import (
    availability_blockers,
    employment_blockers,
    latest_sample,
    manual_assignment_blockers,
    supervises,
)
from app.workflows.records import SYSTEM_WORKFLOW, add_exception_event, add_task_event, record_assignment
from app.workflows.state_machine import TaskAction, next_status
from app.workflows.views import task_view

EVENT_FOR = {
    TaskAction.ACKNOWLEDGE: "ACKNOWLEDGED",
    TaskAction.START: "STARTED",
    TaskAction.BLOCK: "BLOCKED",
    TaskAction.UNBLOCK: "UNBLOCKED",
    TaskAction.COMPLETE: "COMPLETED",
    TaskAction.VERIFY: "VERIFIED",
    TaskAction.REJECT: "VERIFICATION_REJECTED",
    TaskAction.REASSIGN: "REASSIGNED",
}


@dataclass(frozen=True)
class WorkflowResult:
    task: dict[str, Any]
    replayed: bool = False


@dataclass
class _Context:
    session: Session
    task: Task
    actor: CanonicalEmployee
    directory: OrgDirectory
    now: datetime
    request: dict[str, Any]
    fingerprint: str
    idempotency_key: str | None
    version_after: int
    extra: dict[str, Any] = field(default_factory=dict)

    def event(
        self,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        actor_id: str | None = None,
        **details: Any,
    ) -> None:
        add_task_event(
            self.session,
            self.task,
            event_type,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id or self.actor.employee_id,
            occurred_at=self.now,
            details={
                **details,
                "request": self.request,
                "request_fingerprint": self.fingerprint,
                "version_after": self.version_after,
            },
            idempotency_key=self.idempotency_key,
        )


Handler = Callable[[_Context], None]


class TaskWorkflowService:
    def __init__(self, session_factory: sessionmaker[Session], tenant_id: str, clock: Clock, availability_max_age):
        self._sessions = session_factory
        self._tenant = tenant_id
        self._clock = clock
        self._availability_max_age = availability_max_age

    # ------------------------------------------------------------------ actions

    def acknowledge(self, task_id: uuid.UUID, actor_id: str, **opts: Any) -> WorkflowResult:
        return self._perform(
            TaskAction.ACKNOWLEDGE, task_id, actor_id, {}, self._simple(TaskAction.ACKNOWLEDGE), **opts
        )

    def start(self, task_id: uuid.UUID, actor_id: str, **opts: Any) -> WorkflowResult:
        return self._perform(TaskAction.START, task_id, actor_id, {}, self._simple(TaskAction.START), **opts)

    def block(self, task_id: uuid.UUID, actor_id: str, reason: str, **opts: Any) -> WorkflowResult:
        return self._perform(
            TaskAction.BLOCK,
            task_id,
            actor_id,
            {"reason": reason},
            self._simple(TaskAction.BLOCK, reason=reason),
            **opts,
        )

    def unblock(self, task_id: uuid.UUID, actor_id: str, note: str | None = None, **opts: Any) -> WorkflowResult:
        return self._perform(
            TaskAction.UNBLOCK, task_id, actor_id, {"note": note}, self._simple(TaskAction.UNBLOCK, note=note), **opts
        )

    def complete(
        self,
        task_id: uuid.UUID,
        actor_id: str,
        resolution_code: ResolutionCode,
        evidence_ref: str,
        note: str | None = None,
        **opts: Any,
    ) -> WorkflowResult:
        if resolution_code == ResolutionCode.OTHER and not (note and note.strip()):
            raise InvalidRequest("resolution_code OTHER needs a note explaining the outcome", code="NOTE_REQUIRED")
        request = {"resolution_code": resolution_code.value, "evidence_ref": evidence_ref, "note": note}

        def handler(ctx: _Context) -> None:
            self._require_assignee(ctx)
            task = ctx.task
            previous = task.status
            completed = next_status(previous, TaskAction.COMPLETE)  # refuses an invalid transition first
            rejected = self._last_rejected_submission(ctx)
            if rejected == {"resolution_code": resolution_code.value, "evidence_ref": evidence_ref}:
                raise StateConflict(
                    "this resolution and evidence were rejected at verification; submit new evidence",
                    code="RESUBMISSION_UNCHANGED",
                    details={"rejected_submission": rejected},
                )
            task.status = completed
            task.resolution_code = resolution_code.value
            task.evidence_ref = evidence_ref
            task.completion_note = note
            task.completed_by = ctx.actor.employee_id
            task.completed_at = ctx.now
            ctx.event(
                "COMPLETED", previous, task.status, resolution_code=resolution_code.value, evidence_ref=evidence_ref
            )
            # Every resolution needs a second person's verification, so the server
            # submits it immediately, in the same transaction.
            completed = task.status
            task.status = next_status(completed, TaskAction.SUBMIT_FOR_VERIFICATION)
            ctx.event("SUBMITTED_FOR_VERIFICATION", completed, task.status, actor_id=SYSTEM_WORKFLOW)

        return self._perform(TaskAction.COMPLETE, task_id, actor_id, request, handler, **opts)

    def verify(self, task_id: uuid.UUID, actor_id: str, note: str | None = None, **opts: Any) -> WorkflowResult:
        def handler(ctx: _Context) -> None:
            self._require_verifier(ctx)
            task = ctx.task
            previous = task.status
            task.status = next_status(previous, TaskAction.VERIFY)
            task.verified_by = ctx.actor.employee_id
            task.verified_at = ctx.now
            ctx.event("VERIFIED", previous, task.status, note=note)
            self._resolve_exception_if_done(ctx)

        return self._perform(TaskAction.VERIFY, task_id, actor_id, {"note": note}, handler, **opts)

    def reject(self, task_id: uuid.UUID, actor_id: str, reason: str, **opts: Any) -> WorkflowResult:
        def handler(ctx: _Context) -> None:
            self._require_verifier(ctx)
            task = ctx.task
            previous = task.status
            rejected = {
                "resolution_code": task.resolution_code,
                "evidence_ref": task.evidence_ref,
                "completed_by": task.completed_by,
            }
            task.status = next_status(previous, TaskAction.REJECT)
            task.resolution_code = None
            task.evidence_ref = None
            task.completion_note = None
            task.completed_by = None
            task.completed_at = None
            ctx.event("VERIFICATION_REJECTED", previous, task.status, reason=reason, rejected_submission=rejected)

        return self._perform(TaskAction.REJECT, task_id, actor_id, {"reason": reason}, handler, **opts)

    def reassign(self, task_id: uuid.UUID, actor_id: str, assignee_id: str, reason: str, **opts: Any) -> WorkflowResult:
        def handler(ctx: _Context) -> None:
            task = ctx.task
            if not supervises(
                ctx.actor,
                location_id=task.location_id,
                region_id=task.region_id,
                assignee_id=task.assignee_employee_id,
                employees=ctx.directory.employees,
            ):
                raise PermissionDenied(
                    "only an active store manager for this location, a regional operations lead for its region, "
                    "or the assignee's manager can reassign this task"
                )
            target = ctx.directory.employees.get(assignee_id)
            if target is None:
                raise InvalidRequest("assignee is not in the HRMS roster", code="UNKNOWN_ASSIGNEE")
            if assignee_id == task.assignee_employee_id:
                raise InvalidRequest("task is already assigned to this employee", code="ALREADY_ASSIGNED")
            blockers = manual_assignment_blockers(target, location_id=task.location_id, region_id=task.region_id)
            if blockers:
                raise InvalidRequest(
                    "employee cannot be assigned this task",
                    code="ASSIGNEE_NOT_ELIGIBLE",
                    details={"employee_id": assignee_id, "reasons": blockers},
                )
            # The same eligibility as automatic routing: active, in scope and on shift now.
            sample = latest_sample(ctx.directory.availability.get(assignee_id, []), ctx.now)
            shift_blockers = availability_blockers(sample, ctx.now, self._availability_max_age)
            if shift_blockers:
                raise InvalidRequest(
                    "employee is not available to take this task now",
                    code="ASSIGNEE_NOT_ELIGIBLE",
                    details={"employee_id": assignee_id, "reasons": shift_blockers},
                )
            previous_status, previous_assignee = task.status, task.assignee_employee_id
            task.status = next_status(previous_status, TaskAction.REASSIGN)
            task.assignee_employee_id = assignee_id
            task.queue = None
            record_assignment(
                ctx.session,
                task,
                assignee_id=assignee_id,
                queue=None,
                reason_code="MANUAL_REASSIGNMENT",
                actor_id=ctx.actor.employee_id,
                occurred_at=ctx.now,
                note=reason,
                decision={
                    "previous_assignee": previous_assignee,
                    "previous_status": previous_status,
                    "assignee_checks": {
                        "employment_and_scope": "eligible",
                        "availability": sample.ref.source_record_key if sample else None,
                    },
                },
            )
            ctx.event(
                "REASSIGNED",
                previous_status,
                task.status,
                from_assignee=previous_assignee,
                to_assignee=assignee_id,
                reason=reason,
            )

        return self._perform(
            TaskAction.REASSIGN,
            task_id,
            actor_id,
            {"assignee_employee_id": assignee_id, "reason": reason},
            handler,
            **opts,
        )

    # ------------------------------------------------------------------ machinery

    def _perform(
        self,
        action: TaskAction,
        task_id: uuid.UUID,
        actor_id: str,
        request: dict[str, Any],
        handler: Handler,
        *,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkflowResult:
        fingerprint = content_hash(
            {"action": action.value, "task_id": str(task_id), "actor": actor_id, "request": request}
        )
        with self._sessions() as session, session.begin():
            if idempotency_key is not None:
                stored = self._claim_key(session, idempotency_key, fingerprint)
                if stored is not None:
                    return WorkflowResult(stored, replayed=True)

            exception_id = session.scalar(
                select(Task.exception_id).where(Task.id == task_id, Task.tenant_id == self._tenant)
            )
            if exception_id is None:
                raise NotFound("task not found", details={"task_id": str(task_id)})
            # Lock order everywhere is exception first, then its tasks (the detector
            # locks exceptions and then cancels their tasks). Taking them the other
            # way round here could deadlock against a concurrent reconciliation run.
            session.execute(select(ExceptionCase.id).where(ExceptionCase.id == exception_id).with_for_update())
            task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            assert task is not None

            if self._is_natural_replay(session, task, action, actor_id, fingerprint):
                view = task_view(task)
                self._store_response(session, idempotency_key, view)
                return WorkflowResult(view, replayed=True)

            if expected_version is not None and expected_version != task.version:
                raise StateConflict(
                    "task changed since you last read it",
                    code="VERSION_CONFLICT",
                    details={"expected_version": expected_version, "current_version": task.version},
                )

            directory = load_org_directory(session, self._tenant)
            actor = directory.employees.get(actor_id)
            if actor is None:
                raise PermissionDenied("actor is not in the HRMS roster", code="UNKNOWN_ACTOR")
            if employment_blockers(actor):
                raise PermissionDenied(
                    "actor is not an active employee",
                    code="ACTOR_NOT_ACTIVE",
                    details={"reasons": employment_blockers(actor)},
                )

            ctx = _Context(
                session=session,
                task=task,
                actor=actor,
                directory=directory,
                now=self._clock.now(),
                request=request,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                version_after=task.version + 1,
            )
            handler(ctx)
            task.version = ctx.version_after
            session.flush()
            view = task_view(task)
            self._store_response(session, idempotency_key, view)
            return WorkflowResult(view)

    def _claim_key(self, session: Session, key: str, fingerprint: str) -> dict[str, Any] | None:
        claimed = session.scalar(
            pg_insert(IdempotencyRecord)
            .values(tenant_id=self._tenant, key=key, request_fingerprint=fingerprint)
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
            .returning(IdempotencyRecord.id)
        )
        if claimed is not None:
            return None
        record = session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.tenant_id == self._tenant, IdempotencyRecord.key == key)
        )
        assert record is not None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused("this Idempotency-Key was already used for a different request")
        if record.response_body is None:
            raise StateConflict("the original request with this key did not finish", code="IDEMPOTENCY_IN_FLIGHT")
        return record.response_body

    def _store_response(self, session: Session, key: str | None, view: dict[str, Any]) -> None:
        if key is None:
            return
        record = session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.tenant_id == self._tenant, IdempotencyRecord.key == key)
        )
        assert record is not None
        record.response_status = 200
        record.response_body = view

    def _is_natural_replay(
        self, session: Session, task: Task, action: TaskAction, actor_id: str, fingerprint: str
    ) -> bool:
        last = session.scalar(
            select(TaskEvent)
            .where(
                TaskEvent.task_id == task.id, TaskEvent.actor_id == actor_id, TaskEvent.event_type == EVENT_FOR[action]
            )
            .order_by(TaskEvent.id.desc())
            .limit(1)
        )
        return (
            last is not None
            and last.details.get("request_fingerprint") == fingerprint
            and last.details.get("version_after") == task.version
        )

    def _simple(self, action: TaskAction, **details: Any) -> Handler:
        def handler(ctx: _Context) -> None:
            self._require_assignee(ctx)
            previous = ctx.task.status
            ctx.task.status = next_status(previous, action)
            ctx.event(EVENT_FOR[action], previous, ctx.task.status, **details)

        return handler

    @staticmethod
    def _require_assignee(ctx: _Context) -> None:
        task = ctx.task
        if task.assignee_employee_id != ctx.actor.employee_id:
            raise PermissionDenied(
                "only the current assignee can do this", details={"assignee_employee_id": task.assignee_employee_id}
            )
        # Being the assignee is not enough if HRMS has since moved them out of scope.
        blockers = manual_assignment_blockers(ctx.actor, location_id=task.location_id, region_id=task.region_id)
        if blockers:
            raise PermissionDenied(
                "you are no longer eligible for work at this location; the task will be re-routed",
                code="ASSIGNEE_NO_LONGER_ELIGIBLE",
                details={"reasons": blockers},
            )

    @staticmethod
    def _last_rejected_submission(ctx: _Context) -> dict[str, Any] | None:
        event = ctx.session.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == ctx.task.id, TaskEvent.event_type == "VERIFICATION_REJECTED")
            .order_by(TaskEvent.id.desc())
            .limit(1)
        )
        if event is None:
            return None
        submission = event.details.get("rejected_submission") or {}
        return {"resolution_code": submission.get("resolution_code"), "evidence_ref": submission.get("evidence_ref")}

    @staticmethod
    def _require_verifier(ctx: _Context) -> None:
        task = ctx.task
        if task.completed_by is not None and task.completed_by == ctx.actor.employee_id:
            raise PermissionDenied(
                "the person who completed the task cannot verify it", code="SELF_VERIFICATION_NOT_ALLOWED"
            )
        if not supervises(
            ctx.actor,
            location_id=task.location_id,
            region_id=task.region_id,
            assignee_id=task.assignee_employee_id,
            employees=ctx.directory.employees,
        ):
            raise PermissionDenied(
                "only an active store manager for this location, a regional operations lead for its region, "
                "or the assignee's manager can verify this task"
            )

    def _resolve_exception_if_done(self, ctx: _Context) -> None:
        """An exception is RESOLVED only when a second person has verified a task that
        carries a resolution code and an evidence reference, and no other task for
        the exception is still active."""
        session, task = ctx.session, ctx.task
        exception = session.scalar(select(ExceptionCase).where(ExceptionCase.id == task.exception_id).with_for_update())
        if exception is None or exception.status != ExceptionStatus.OPEN:
            return
        session.flush()
        others_active = session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.exception_id == exception.id,
                Task.id != task.id,
                Task.status.in_([s.value for s in ACTIVE_TASK_STATUSES]),
            )
        )
        if others_active:
            return
        if task.status != TaskStatus.VERIFIED or not task.resolution_code or not task.evidence_ref:
            return
        exception.status = ExceptionStatus.RESOLVED
        exception.resolution_code = task.resolution_code
        exception.resolution_evidence_ref = task.evidence_ref
        exception.resolved_by = ctx.actor.employee_id
        exception.resolved_at = ctx.now
        exception.version += 1
        add_exception_event(
            session,
            exception,
            "RESOLVED",
            actor_id=ctx.actor.employee_id,
            occurred_at=ctx.now,
            from_status=ExceptionStatus.OPEN,
            to_status=ExceptionStatus.RESOLVED,
            details={
                "task_id": task.id,
                "resolution_code": task.resolution_code,
                "evidence_ref": task.evidence_ref,
                "completed_by": task.completed_by,
                "verified_by": task.verified_by,
            },
        )
