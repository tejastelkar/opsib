"""Append-only history helpers shared by routing, detection and the task workflow.
The event and assignment tables reject UPDATE and DELETE at the database level."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ExceptionCase, ExceptionEvent, Task, TaskAssignment, TaskEvent
from app.enums import ACTIVE_TASK_STATUSES, TaskStatus
from app.hashing import to_jsonable
from app.workflows.state_machine import TaskAction, next_status

SYSTEM_DETECTOR = "system:detector"
SYSTEM_ROUTER = "system:router"
SYSTEM_WORKFLOW = "system:workflow"


def add_task_event(
    session: Session,
    task: Task,
    event_type: str,
    *,
    from_status: str | None,
    to_status: str | None,
    actor_id: str,
    occurred_at: datetime,
    details: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_id,
        occurred_at=occurred_at,
        details=to_jsonable(details or {}),
        idempotency_key=idempotency_key,
    )
    session.add(event)
    return event


def add_exception_event(
    session: Session,
    exception: ExceptionCase,
    event_type: str,
    *,
    actor_id: str,
    occurred_at: datetime,
    from_status: str | None = None,
    to_status: str | None = None,
    run_id: Any = None,
    details: dict[str, Any] | None = None,
) -> ExceptionEvent:
    event = ExceptionEvent(
        exception_id=exception.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_id,
        run_id=run_id,
        occurred_at=occurred_at,
        details=to_jsonable(details or {}),
    )
    session.add(event)
    return event


def record_assignment(
    session: Session,
    task: Task,
    *,
    assignee_id: str | None,
    queue: str | None,
    reason_code: str,
    actor_id: str,
    occurred_at: datetime,
    policy_version: str | None = None,
    decision: dict[str, Any] | None = None,
    note: str | None = None,
) -> TaskAssignment:
    last = session.scalar(select(func.max(TaskAssignment.sequence)).where(TaskAssignment.task_id == task.id))
    assignment = TaskAssignment(
        task_id=task.id,
        sequence=(last or 0) + 1,
        assignee_employee_id=assignee_id,
        queue=queue,
        reason_code=reason_code,
        policy_version=policy_version,
        decision=to_jsonable(decision or {}),
        note=note,
        actor_id=actor_id,
        occurred_at=occurred_at,
    )
    session.add(assignment)
    return assignment


def cancel_active_tasks(
    session: Session, exception: ExceptionCase, *, reason: str, occurred_at: datetime, run_id: Any = None
) -> list[Task]:
    tasks = list(
        session.scalars(
            select(Task)
            .where(Task.exception_id == exception.id, Task.status.in_([s.value for s in ACTIVE_TASK_STATUSES]))
            .with_for_update()
        )
    )
    for task in tasks:
        previous = task.status
        task.status = next_status(previous, TaskAction.CANCEL)
        task.version += 1
        add_task_event(
            session,
            task,
            "CANCELLED",
            from_status=previous,
            to_status=TaskStatus.CANCELLED,
            actor_id=SYSTEM_DETECTOR,
            occurred_at=occurred_at,
            details={"reason": reason, "run_id": run_id, "version_after": task.version},
        )
    return tasks
