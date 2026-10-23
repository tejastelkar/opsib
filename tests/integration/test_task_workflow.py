"""Task lifecycle through the API: valid and invalid transitions, who may act,
retried requests, reassignment, verification and the audit trail."""

import threading
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import TaskEvent
from app.enums import ResolutionCode
from tests.helpers import act, active_task, exceptions_by_po, load_fixtures, reconcile

COMPLETE = {"resolution_code": "SUPPLIER_CONFIRMED_SHORT_DISPATCH", "evidence_ref": "ASN-SUP07-PO501"}


@pytest.fixture
def setup(client):
    load_fixtures(client)
    reconcile(client)
    exceptions = exceptions_by_po(client)
    return {po: (e["id"], active_task(client, e["id"])["id"]) for po, e in exceptions.items()}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def error_code(response):
    return response.status_code, response.json()["error"]["code"]


def task_events(client, task_id):
    return [e["event_type"] for e in client.get(f"/tasks/{task_id}").json()["events"]]


def test_full_lifecycle_resolves_the_exception_only_after_verification(client, setup):
    exception_id, task_id = setup["PO-501"]
    assert ok(act(client, task_id, "acknowledge", "HR-102"))["status"] == "ACKNOWLEDGED"
    assert ok(act(client, task_id, "start", "HR-102"))["status"] == "IN_PROGRESS"
    completed = ok(act(client, task_id, "complete", "HR-102", COMPLETE))
    assert (completed["status"], completed["completed_by"], completed["allowed_actions"]) == (
        "PENDING_VERIFICATION",
        "HR-102",
        ["REJECT", "VERIFY"],
    )
    assert client.get(f"/exceptions/{exception_id}").json()["status"] == "OPEN"  # not yet verified

    verified = ok(act(client, task_id, "verify", "HR-201", {"note": "ASN checked"}))
    assert (verified["status"], verified["verified_by"]) == ("VERIFIED", "HR-201")
    exception = client.get(f"/exceptions/{exception_id}").json()
    assert exception["status"] == "RESOLVED"
    assert exception["resolution"] == {
        "code": "SUPPLIER_CONFIRMED_SHORT_DISPATCH",
        "evidence_ref": "ASN-SUP07-PO501",
        "resolved_by": "HR-201",
        "resolved_at": "2026-10-16T09:30:00+00:00",
    }
    assert [e["event_type"] for e in exception["events"]] == ["DETECTED", "RESOLVED"]
    assert task_events(client, task_id) == [
        "CREATED",
        "ASSIGNED",
        "ACKNOWLEDGED",
        "STARTED",
        "COMPLETED",
        "SUBMITTED_FOR_VERIFICATION",
        "VERIFIED",
    ]
    events = client.get(f"/tasks/{task_id}").json()["events"]
    assert [e["actor_id"] for e in events] == [
        "system:router",
        "system:router",
        "HR-102",
        "HR-102",
        "HR-102",
        "system:workflow",
        "HR-201",
    ]
    assert all(e["occurred_at"] and e["recorded_at"] for e in events)

    reconcile(client)  # the same evidence does not reopen a verified resolution
    assert client.get(f"/exceptions/{exception_id}").json()["status"] == "RESOLVED"


def test_invalid_transitions_are_refused_with_the_allowed_actions(client, setup):
    _, task_id = setup["PO-501"]
    response = act(client, task_id, "complete", "HR-102", COMPLETE)
    assert error_code(response) == (409, "INVALID_TRANSITION")
    assert response.json()["error"]["details"]["allowed_actions"] == ["ACKNOWLEDGE", "REASSIGN"]
    assert error_code(act(client, task_id, "start", "HR-102")) == (409, "INVALID_TRANSITION")
    assert error_code(act(client, task_id, "verify", "HR-201")) == (409, "INVALID_TRANSITION")
    # Cancelling is a system action (exception cleared); there is no client route for it.
    assert client.post(f"/tasks/{task_id}/cancel", headers={"X-Actor-Id": "HR-201"}).status_code == 404
    assert task_events(client, task_id) == ["CREATED", "ASSIGNED"]


def test_only_the_assignee_works_the_task_and_actors_must_be_real(client, setup):
    _, task_id = setup["PO-501"]
    assert error_code(act(client, task_id, "acknowledge", "HR-101")) == (403, "ACTOR_NOT_PERMITTED")
    assert error_code(act(client, task_id, "acknowledge", "HR-999")) == (403, "UNKNOWN_ACTOR")
    assert error_code(act(client, task_id, "acknowledge", "HR-103")) == (403, "ACTOR_NOT_ACTIVE")
    assert error_code(act(client, task_id, "acknowledge", "system:router")) == (403, "SYSTEM_ACTOR_FORBIDDEN")
    assert error_code(client.post(f"/tasks/{task_id}/acknowledge")) == (422, "ACTOR_REQUIRED")
    assert error_code(act(client, task_id, "complete", "HR-102", {"evidence_ref": "x"})) == (422, "VALIDATION_ERROR")


def test_retried_completion_with_the_same_key_replays_the_stored_response(client, setup, services):
    _, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    ok(act(client, task_id, "start", "HR-102"))
    first = act(client, task_id, "complete", "HR-102", COMPLETE, key="retry-key-1")
    assert first.status_code == 200 and "Idempotent-Replay" not in first.headers
    # The client timed out and retries the identical request.
    second = act(client, task_id, "complete", "HR-102", COMPLETE, key="retry-key-1")
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json() == first.json()
    assert task_events(client, task_id).count("COMPLETED") == 1

    # Same key, different request: refused rather than guessed.
    other = act(client, task_id, "complete", "HR-102", {**COMPLETE, "evidence_ref": "ASN-OTHER"}, key="retry-key-1")
    assert error_code(other) == (422, "IDEMPOTENCY_KEY_REUSED")


def test_retried_completion_without_a_key_is_recognised_or_refused(client, setup):
    _, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    ok(act(client, task_id, "start", "HR-102"))
    first = ok(act(client, task_id, "complete", "HR-102", COMPLETE))
    retry = act(client, task_id, "complete", "HR-102", COMPLETE)
    assert (retry.status_code, retry.headers.get("Idempotent-Replay")) == (200, "true")
    assert retry.json() == first
    different = act(client, task_id, "complete", "HR-102", {**COMPLETE, "evidence_ref": "ASN-OTHER"})
    assert error_code(different) == (409, "INVALID_TRANSITION")
    assert task_events(client, task_id).count("COMPLETED") == 1


def test_concurrent_duplicate_completions_apply_once(client, setup, services):
    _, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    ok(act(client, task_id, "start", "HR-102"))
    results = []

    def worker():
        results.append(
            services.workflow.complete(
                uuid.UUID(task_id),
                "HR-102",
                ResolutionCode(COMPLETE["resolution_code"]),
                COMPLETE["evidence_ref"],
                idempotency_key="concurrent-1",
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4
    assert sum(not r.replayed for r in results) == 1
    assert len({r.task["version"] for r in results}) == 1
    with services.sessions() as session, session.begin():
        assert (
            session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.event_type == "COMPLETED")) == 1
        )


def test_stale_expected_version_is_a_conflict(client, setup):
    _, task_id = setup["PO-501"]
    version = client.get(f"/tasks/{task_id}").json()["version"]
    ok(act(client, task_id, "acknowledge", "HR-102", {"expected_version": version}))
    stale = act(client, task_id, "start", "HR-102", {"expected_version": version})
    assert error_code(stale) == (409, "VERSION_CONFLICT")
    assert stale.json()["error"]["details"] == {"expected_version": version, "current_version": version + 1}


def test_reassignment_is_recorded_and_does_not_create_a_second_exception(client, setup):
    exception_id, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    # HR-101 is active but OFF_SHIFT: the same eligibility as routing applies, so no.
    off_shift = act(client, task_id, "reassign", "HR-201", {"assignee_employee_id": "HR-101", "reason": "cover"})
    assert error_code(off_shift) == (422, "ASSIGNEE_NOT_ELIGIBLE")
    assert off_shift.json()["error"]["details"]["reasons"] == ["OFF_SHIFT"]

    reassigned = ok(
        act(
            client,
            task_id,
            "reassign",
            "HR-501",
            {"assignee_employee_id": "HR-201", "reason": "HR-102 is covering the dock"},
        )
    )
    assert (reassigned["status"], reassigned["assignee_employee_id"]) == ("ASSIGNED", "HR-201")
    detail = client.get(f"/tasks/{task_id}").json()
    assert [(a["assignee_employee_id"], a["reason_code"], a["actor_id"]) for a in detail["assignments"]] == [
        ("HR-102", "PRIMARY_ROLE_AT_LOCATION", "system:router"),
        ("HR-201", "MANUAL_REASSIGNMENT", "HR-501"),
    ]
    assert detail["assignments"][1]["note"] == "HR-102 is covering the dock"
    reassign_event = detail["events"][-1]
    assert (reassign_event["event_type"], reassign_event["details"]["from_assignee"]) == ("REASSIGNED", "HR-102")
    assert len(client.get("/exceptions").json()) == 3
    assert [t["id"] for t in client.get(f"/exceptions/{exception_id}").json()["tasks"]] == [task_id]
    # The new assignee must acknowledge; the previous one can no longer act.
    assert error_code(act(client, task_id, "acknowledge", "HR-102")) == (403, "ACTOR_NOT_PERMITTED")
    ok(act(client, task_id, "acknowledge", "HR-201"))


def test_reassignment_rules(client, setup):
    _, task_id = setup["PO-501"]
    body = {"assignee_employee_id": "HR-103", "reason": "please take this"}
    inactive = act(client, task_id, "reassign", "HR-201", body)
    assert error_code(inactive) == (422, "ASSIGNEE_NOT_ELIGIBLE")
    assert inactive.json()["error"]["details"]["reasons"] == ["EMPLOYMENT_NOT_ACTIVE"]
    out_of_scope = act(client, task_id, "reassign", "HR-201", {**body, "assignee_employee_id": "HR-401"})
    assert out_of_scope.json()["error"]["details"]["reasons"] == ["OUT_OF_SCOPE_FOR_LOCATION"]
    not_a_supervisor = act(client, task_id, "reassign", "HR-102", {**body, "assignee_employee_id": "HR-101"})
    assert error_code(not_a_supervisor) == (403, "ACTOR_NOT_PERMITTED")
    other_store_manager = act(client, task_id, "reassign", "HR-401", {**body, "assignee_employee_id": "HR-101"})
    assert error_code(other_store_manager) == (403, "ACTOR_NOT_PERMITTED")
    regional = act(client, task_id, "reassign", "HR-501", {**body, "assignee_employee_id": "HR-201"})
    assert ok(regional)["assignee_employee_id"] == "HR-201"


def test_a_person_cannot_verify_their_own_work(client, setup):
    exception_id, task_id = setup["PO-505"]  # assigned to HR-401, the STORE-015 manager (fallback)
    ok(act(client, task_id, "acknowledge", "HR-401"))
    ok(act(client, task_id, "start", "HR-401"))
    ok(
        act(
            client,
            task_id,
            "complete",
            "HR-401",
            {"resolution_code": "BALANCE_TO_FOLLOW", "evidence_ref": "SUP21-EMAIL-1"},
        )
    )
    assert error_code(act(client, task_id, "verify", "HR-401")) == (403, "SELF_VERIFICATION_NOT_ALLOWED")
    assert error_code(act(client, task_id, "verify", "HR-201")) == (403, "ACTOR_NOT_PERMITTED")  # other store
    ok(act(client, task_id, "verify", "HR-501"))  # regional operations, region-scoped
    assert client.get(f"/exceptions/{exception_id}").json()["status"] == "RESOLVED"


def test_rejected_verification_sends_the_task_back_for_rework(client, setup):
    exception_id, task_id = setup["PO-506"]
    for action in ("acknowledge", "start"):
        ok(act(client, task_id, action, "HR-102"))
    ok(
        act(
            client,
            task_id,
            "complete",
            "HR-102",
            {"resolution_code": "OTHER", "evidence_ref": "NOTE-1", "note": "count sheet missing"},
        )
    )
    rejected = ok(act(client, task_id, "reject", "HR-201", {"reason": "attach the count sheet"}))
    assert (rejected["status"], rejected["resolution_code"], rejected["completed_by"]) == ("IN_PROGRESS", None, None)
    event = client.get(f"/tasks/{task_id}").json()["events"][-1]
    assert event["event_type"] == "VERIFICATION_REJECTED"
    assert event["details"]["rejected_submission"]["evidence_ref"] == "NOTE-1"
    assert client.get(f"/exceptions/{exception_id}").json()["status"] == "OPEN"

    ok(
        act(
            client,
            task_id,
            "complete",
            "HR-102",
            {"resolution_code": "STORE_COUNT_CONFIRMED_SHORT", "evidence_ref": "COUNT-SHEET-506"},
        )
    )
    ok(act(client, task_id, "verify", "HR-201"))
    assert client.get(f"/exceptions/{exception_id}").json()["resolution"]["code"] == "STORE_COUNT_CONFIRMED_SHORT"


def test_block_and_unblock(client, setup):
    _, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    assert ok(act(client, task_id, "block", "HR-102", {"reason": "waiting for supplier ASN"}))["status"] == "BLOCKED"
    assert error_code(act(client, task_id, "complete", "HR-102", COMPLETE)) == (409, "INVALID_TRANSITION")
    assert ok(act(client, task_id, "unblock", "HR-102", {"note": "ASN received"}))["status"] == "IN_PROGRESS"


def test_other_resolution_needs_a_note(client, setup):
    _, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    ok(act(client, task_id, "start", "HR-102"))
    response = act(client, task_id, "complete", "HR-102", {"resolution_code": "OTHER", "evidence_ref": "X-1"})
    assert error_code(response) == (422, "NOTE_REQUIRED")


def test_audit_history_is_append_only_in_the_database(client, setup, services):
    _, task_id = setup["PO-501"]
    with pytest.raises(DBAPIError, match="append-only"), services.sessions() as session, session.begin():
        session.execute(text("UPDATE task_events SET actor_id = 'someone-else'"))
    with pytest.raises(DBAPIError, match="append-only"), services.sessions() as session, session.begin():
        session.execute(text("DELETE FROM task_assignments"))
    with pytest.raises(DBAPIError, match="append-only"), services.sessions() as session, session.begin():
        session.execute(text("DELETE FROM exception_events"))
    assert task_events(client, task_id) == ["CREATED", "ASSIGNED"]


def test_a_delayed_retry_cannot_resubmit_a_rejected_completion(client, setup):
    _, task_id = setup["PO-501"]
    ok(act(client, task_id, "acknowledge", "HR-102"))
    ok(act(client, task_id, "start", "HR-102"))
    ok(act(client, task_id, "complete", "HR-102", COMPLETE))
    ok(act(client, task_id, "reject", "HR-201", {"reason": "ASN does not cover line 1"}))
    # The original completion, timed out on the client, is retried without a key.
    stale = act(client, task_id, "complete", "HR-102", COMPLETE)
    assert error_code(stale) == (409, "RESUBMISSION_UNCHANGED")
    assert client.get(f"/tasks/{task_id}").json()["status"] == "IN_PROGRESS"
    fresh = ok(act(client, task_id, "complete", "HR-102", {**COMPLETE, "evidence_ref": "ASN-SUP07-PO501-REV2"}))
    assert fresh["status"] == "PENDING_VERIFICATION"
