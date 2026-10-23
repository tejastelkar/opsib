import pytest

from app.enums import TERMINAL_TASK_STATUSES, TaskStatus
from app.errors import InvalidTransition
from app.workflows.state_machine import SYSTEM_ACTIONS, TRANSITIONS, TaskAction, allowed_actions, next_status

S, A = TaskStatus, TaskAction


def test_happy_path():
    path = [
        (A.ASSIGN, S.ASSIGNED),
        (A.ACKNOWLEDGE, S.ACKNOWLEDGED),
        (A.START, S.IN_PROGRESS),
        (A.COMPLETE, S.COMPLETED),
        (A.SUBMIT_FOR_VERIFICATION, S.PENDING_VERIFICATION),
        (A.VERIFY, S.VERIFIED),
    ]
    status = S.CREATED
    for action, expected in path:
        status = next_status(status, action)
        assert status == expected


def test_rework_and_blocking_paths():
    assert next_status(S.PENDING_VERIFICATION, A.REJECT) == S.IN_PROGRESS
    assert next_status(S.IN_PROGRESS, A.BLOCK) == S.BLOCKED
    assert next_status(S.BLOCKED, A.UNBLOCK) == S.IN_PROGRESS
    assert next_status(S.IN_PROGRESS, A.REASSIGN) == S.ASSIGNED  # new assignee must acknowledge


@pytest.mark.parametrize(
    "status, action",
    [(s, a) for s in TaskStatus for a in TaskAction if (s, a) not in TRANSITIONS],
)
def test_every_undeclared_transition_is_refused(status, action):
    with pytest.raises(InvalidTransition) as info:
        next_status(status, action)
    assert info.value.details["status"] == status.value


def test_terminal_states_allow_nothing_and_cancel_is_never_client_facing():
    for status in TERMINAL_TASK_STATUSES:
        assert allowed_actions(status) == []
    for status in TaskStatus:
        assert not set(allowed_actions(status)) & {a.value for a in SYSTEM_ACTIONS}


def test_cannot_skip_straight_to_completion_or_verification():
    for status in (S.ASSIGNED, S.ACKNOWLEDGED, S.BLOCKED):
        with pytest.raises(InvalidTransition):
            next_status(status, A.COMPLETE)
    with pytest.raises(InvalidTransition):
        next_status(S.IN_PROGRESS, A.VERIFY)
