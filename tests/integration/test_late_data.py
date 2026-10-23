"""Late and partial data after a finding exists: the case is updated, cleared or
reopened through recorded events, never silently rewritten."""

from tests.helpers import act, active_task, derived, exceptions_by_po, load_fixtures, reconcile, upload


def late_receipt(qty: str, received_at: str = "2026-10-16T12:00:00+05:30") -> bytes:
    def add(rows):
        base = next(r for r in rows if r["source_record_id"] == "WMS-GRN501-B-L1")
        extra = dict(
            base,
            source_record_id="WMS-GRN501-C-L1",
            receipt_number="GRN-501-C",
            accepted_qty=qty,
            received_at=received_at,
        )
        return rows + [extra]

    return derived("goods_receipt_lines", add)


def events(client, exception_id):
    return [e["event_type"] for e in client.get(f"/exceptions/{exception_id}").json()["events"]]


def test_late_receipt_that_closes_the_gap_clears_the_case_and_cancels_work(client):
    load_fixtures(client)
    reconcile(client)
    exception = exceptions_by_po(client)["PO-501"]
    task_id = active_task(client, exception["id"])["id"]

    upload(client, "goods_receipt_lines", late_receipt("8"))
    reconcile(client)
    cleared = client.get(f"/exceptions/{exception['id']}").json()
    assert (cleared["status"], cleared["received_qty"], cleared["variance_qty"]) == ("CLEARED", "100", "0")
    assert events(client, exception["id"]) == ["DETECTED", "CLEARED"]
    # The evidence now shows what closed the gap, not the old shortfall.
    counted = {r["source_record_key"] for r in cleared["evidence"]["receipts_counted"]}
    assert counted == {"WMS-GRN501-A-L1", "WMS-GRN501-B-L1", "WMS-GRN501-C-L1"}
    assert cleared["evidence"]["computation"]["variance_qty"] == "0"
    assert "Cleared on re-evaluation" in cleared["explanation"]["status_note"]
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "CANCELLED"
    assert task["events"][-1]["details"]["reason"] == "exception cleared: NO_SHORTFALL"


def test_late_partial_receipt_updates_the_same_case_and_keeps_its_task(client):
    load_fixtures(client)
    reconcile(client)
    exception = exceptions_by_po(client)["PO-501"]
    task_id = active_task(client, exception["id"])["id"]

    upload(client, "goods_receipt_lines", late_receipt("5"))
    reconcile(client)
    updated = client.get(f"/exceptions/{exception['id']}").json()
    assert (updated["id"], updated["status"], updated["variance_qty"]) == (exception["id"], "OPEN", "3")
    evidence_event = updated["events"][-1]
    assert evidence_event["event_type"] == "EVIDENCE_UPDATED"
    assert (
        evidence_event["details"]["before"]["variance_qty"],
        evidence_event["details"]["after"]["variance_qty"],
    ) == ("8", "3")
    late = next(r for r in updated["evidence"]["receipts_counted"] if r["source_record_key"] == "WMS-GRN501-C-L1")
    assert late["on_time"] is False
    assert active_task(client, exception["id"])["id"] == task_id


def test_receipt_dated_after_the_evaluation_time_is_not_counted_yet(client):
    load_fixtures(client)
    reconcile(client)
    upload(client, "goods_receipt_lines", late_receipt("8", received_at="2026-10-16T16:00:00+05:30"))
    reconcile(client)
    exception = exceptions_by_po(client)["PO-501"]
    assert (exception["status"], exception["variance_qty"]) == ("OPEN", "8")


def test_new_evidence_after_resolution_reopens_with_a_new_task(client):
    load_fixtures(client)
    reconcile(client)
    exception = exceptions_by_po(client)["PO-501"]
    first_task = active_task(client, exception["id"])["id"]
    for action, actor, body in (
        ("acknowledge", "HR-102", None),
        ("start", "HR-102", None),
        ("complete", "HR-102", {"resolution_code": "BALANCE_TO_FOLLOW", "evidence_ref": "SUP07-MAIL-77"}),
        ("verify", "HR-201", None),
    ):
        assert act(client, first_task, action, actor, body).status_code == 200
    assert client.get(f"/exceptions/{exception['id']}").json()["status"] == "RESOLVED"

    upload(client, "goods_receipt_lines", late_receipt("5"))  # part of the balance arrived
    reconcile(client)
    reopened = client.get(f"/exceptions/{exception['id']}").json()
    assert (reopened["status"], reopened["variance_qty"], reopened["resolution"]["code"]) == ("OPEN", "3", None)
    assert events(client, exception["id"]) == ["DETECTED", "RESOLVED", "REOPENED"]
    second_task = active_task(client, exception["id"])
    assert second_task["id"] != first_task and second_task["assignee_employee_id"] == "HR-102"
    assert len(client.get("/exceptions").json()) == 3
