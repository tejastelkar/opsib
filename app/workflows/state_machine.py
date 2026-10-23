"""Task lifecycle. The server owns every transition; clients ask for an action,
never for a status.

    CREATED ──assign/reassign──> ASSIGNED ──acknowledge──> ACKNOWLEDGED ──start──> IN_PROGRESS
                                    ^                          │                     │  ^
                                    │                        block                 block│ unblock
                                    │                          v                     v  │
                 reassign (from any active state           BLOCKED ────unblock────> (IN_PROGRESS)
                 before completion; must re-acknowledge)
    IN_PROGRESS ──complete──> COMPLETED ──(server, same request)──> PENDING_VERIFICATION
    PENDING_VERIFICATION ──verify──> VERIFIED                      (second person; terminal)
    PENDING_VERIFICATION ──reject──> IN_PROGRESS                   (rework, reason required)
    any active state ──cancel (system only)──> CANCELLED           (e.g. exception cleared; terminal)
    assigned..blocked ──release (system only)──> CREATED            (assignee no longer eligible: re-routed)
"""

from enum import StrEnum

from app.enums import TERMINAL_TASK_STATUSES, TaskStatus
from app.errors import InvalidTransition


class TaskAction(StrEnum):
    ASSIGN = "ASSIGN"
    REASSIGN = "REASSIGN"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    START = "START"
    BLOCK = "BLOCK"
    UNBLOCK = "UNBLOCK"
    COMPLETE = "COMPLETE"
    SUBMIT_FOR_VERIFICATION = "SUBMIT_FOR_VERIFICATION"
    VERIFY = "VERIFY"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    RELEASE = "RELEASE"


S = TaskStatus
A = TaskAction

TRANSITIONS: dict[tuple[TaskStatus, TaskAction], TaskStatus] = {
    (S.CREATED, A.ASSIGN): S.ASSIGNED,
    (S.CREATED, A.REASSIGN): S.ASSIGNED,
    (S.ASSIGNED, A.REASSIGN): S.ASSIGNED,
    (S.ACKNOWLEDGED, A.REASSIGN): S.ASSIGNED,
    (S.IN_PROGRESS, A.REASSIGN): S.ASSIGNED,
    (S.BLOCKED, A.REASSIGN): S.ASSIGNED,
    (S.ASSIGNED, A.ACKNOWLEDGE): S.ACKNOWLEDGED,
    (S.ACKNOWLEDGED, A.START): S.IN_PROGRESS,
    (S.ACKNOWLEDGED, A.BLOCK): S.BLOCKED,
    (S.IN_PROGRESS, A.BLOCK): S.BLOCKED,
    (S.BLOCKED, A.UNBLOCK): S.IN_PROGRESS,
    (S.IN_PROGRESS, A.COMPLETE): S.COMPLETED,
    (S.COMPLETED, A.SUBMIT_FOR_VERIFICATION): S.PENDING_VERIFICATION,
    (S.PENDING_VERIFICATION, A.VERIFY): S.VERIFIED,
    (S.PENDING_VERIFICATION, A.REJECT): S.IN_PROGRESS,
}
for _status in TaskStatus:
    if _status not in TERMINAL_TASK_STATUSES:
        TRANSITIONS[(_status, A.CANCEL)] = S.CANCELLED
for _status in (S.ASSIGNED, S.ACKNOWLEDGED, S.IN_PROGRESS, S.BLOCKED):
    TRANSITIONS[(_status, A.RELEASE)] = S.CREATED

# Actions only the server performs; the API never exposes them.
SYSTEM_ACTIONS = frozenset({A.ASSIGN, A.SUBMIT_FOR_VERIFICATION, A.CANCEL, A.RELEASE})


def next_status(current: TaskStatus | str, action: TaskAction) -> TaskStatus:
    current = TaskStatus(current)
    try:
        return TRANSITIONS[(current, action)]
    except KeyError:
        raise InvalidTransition(
            f"cannot {action.value.lower()} a task that is {current.value}",
            details={"status": current.value, "action": action.value, "allowed_actions": allowed_actions(current)},
        ) from None


def allowed_actions(current: TaskStatus | str) -> list[str]:
    current = TaskStatus(current)
    return sorted(a.value for (s, a) in TRANSITIONS if s == current and a not in SYSTEM_ACTIONS)
