"""JSON-ready representations. The workflow stores these verbatim as idempotent
replay responses, so they must be deterministic for a given row state."""

from typing import Any

from app.db.models import Task, TaskAssignment, TaskEvent
from app.hashing import to_jsonable
from app.workflows.state_machine import allowed_actions


def task_view(task: Task) -> dict[str, Any]:
    return to_jsonable(
        {
            "id": task.id,
            "exception_id": task.exception_id,
            "task_type": task.task_type,
            "status": task.status,
            "allowed_actions": allowed_actions(task.status),
            "location_id": task.location_id,
            "region_id": task.region_id,
            "assignee_employee_id": task.assignee_employee_id,
            "queue": task.queue,
            "opened_at": task.opened_at,
            "due_at": task.due_at,
            "resolution_code": task.resolution_code,
            "evidence_ref": task.evidence_ref,
            "completion_note": task.completion_note,
            "completed_by": task.completed_by,
            "completed_at": task.completed_at,
            "verified_by": task.verified_by,
            "verified_at": task.verified_at,
            "version": task.version,
        }
    )


def assignment_view(assignment: TaskAssignment) -> dict[str, Any]:
    return to_jsonable(
        {
            "sequence": assignment.sequence,
            "assignee_employee_id": assignment.assignee_employee_id,
            "queue": assignment.queue,
            "reason_code": assignment.reason_code,
            "policy_version": assignment.policy_version,
            "decision": assignment.decision,
            "note": assignment.note,
            "actor_id": assignment.actor_id,
            "occurred_at": assignment.occurred_at,
        }
    )


def task_event_view(event: TaskEvent) -> dict[str, Any]:
    return to_jsonable(
        {
            "id": event.id,
            "event_type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "actor_id": event.actor_id,
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "details": event.details,
            "idempotency_key": event.idempotency_key,
        }
    )
