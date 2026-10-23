"""Routing through the API: source-location mapping, ineligible people, fallback,
the manual queue and recorded reasons."""

import pytest

from tests.helpers import act, active_task, derived, exceptions_by_po, load_fixtures, reconcile, upload


def assignment_history(client, task_id):
    return client.get(f"/tasks/{task_id}").json()["assignments"]


def test_selection_reasons_are_persisted(client):
    load_fixtures(client)
    reconcile(client)
    task = active_task(client, exceptions_by_po(client)["PO-501"]["id"])
    [assignment] = assignment_history(client, task["id"])
    assert (assignment["assignee_employee_id"], assignment["reason_code"], assignment["actor_id"]) == (
        "HR-102",
        "PRIMARY_ROLE_AT_LOCATION",
        "system:router",
    )
    candidates = {c["employee_id"]: c for c in assignment["decision"]["candidates"]}
    assert candidates["HR-101"]["reasons"] == ["OFF_SHIFT"]
    assert candidates["HR-103"]["reasons"] == ["EMPLOYMENT_NOT_ACTIVE"]
    assert candidates["HR-103"]["availability"]["status"] == "ON_SHIFT"  # the export said on shift; HRMS wins
    assert candidates["HR-102"]["eligible"] is True
    assert assignment["decision"]["location_id"] == "STORE-014"  # HRMS ST-014 mapped through the crosswalk


def test_no_eligible_person_leaves_the_task_visible_in_the_manual_queue(client):
    off = derived(
        "workforce_status",
        lambda rows: [
            dict(r, availability_status="OFF_SHIFT") if r["source_employee_id"] == "HR-401" else r for r in rows
        ],
    )
    load_fixtures(client, replace={"workforce_status": off})
    reconcile(client)
    task = active_task(client, exceptions_by_po(client)["PO-505"]["id"])
    assert (task["status"], task["assignee_employee_id"], task["queue"]) == ("CREATED", None, "MANUAL_ASSIGNMENT")
    assert [t["id"] for t in client.get("/tasks", params={"queue": "MANUAL_ASSIGNMENT"}).json()] == [task["id"]]
    [entry] = assignment_history(client, task["id"])
    assert (entry["queue"], entry["reason_code"]) == ("MANUAL_ASSIGNMENT", "NO_ELIGIBLE_ASSIGNEE")

    reconcile(client)  # still nobody: no duplicate history entries
    assert len(assignment_history(client, task["id"])) == 1

    # A fresher shift sample arrives: the next run routes the waiting task.
    fresh = derived(
        "workforce_status",
        lambda rows: [
            dict(r, observed_at="2026-10-16T14:58:00+05:30") for r in rows if r["source_employee_id"] == "HR-401"
        ],
    )
    assert upload(client, "workforce_status", fresh).json()["counts"]["inserted"] == 1
    reconcile(client)
    routed = client.get(f"/tasks/{task['id']}").json()
    assert (routed["status"], routed["assignee_employee_id"], routed["queue"]) == ("ASSIGNED", "HR-401", None)
    assert [a["reason_code"] for a in routed["assignments"]] == ["NO_ELIGIBLE_ASSIGNEE", "FALLBACK_STORE_MANAGER"]


def test_employee_whose_location_cannot_be_mapped_is_not_routed(client):
    roster = derived(
        "employees",
        lambda rows: [dict(r, location_code="ST-099") if r["source_employee_id"] == "HR-102" else r for r in rows],
    )
    load_fixtures(client, replace={"employees": roster})
    reconcile(client)
    task = active_task(client, exceptions_by_po(client)["PO-501"]["id"])
    [entry] = assignment_history(client, task["id"])
    # HR-101 off shift, HR-103 inactive, HR-102 unroutable -> approved fallback: the store manager.
    assert (task["assignee_employee_id"], entry["reason_code"]) == ("HR-201", "FALLBACK_STORE_MANAGER")
    assert entry["decision"]["employees_with_unmapped_location"][0]["employee_id"] == "HR-102"
    report = client.get("/data-quality").json()
    assert any(
        i["code"] == "EMPLOYEE_LOCATION_UNMAPPED" and i["subject"]["record_key"] == "HR-102" for i in report["issues"]
    )


def accept_roster_change(client, dataset, transform):
    """Changed HRMS/ERP records arrive as conflicts; a steward accepts them."""
    upload(client, dataset, derived(dataset, transform))
    for conflict in client.get("/data-quality").json()["conflicts"]:
        response = client.post(
            f"/data-quality/conflicts/{conflict['id']}/resolve",
            json={"resolution": "ACCEPT_INCOMING", "reason": "HRMS change confirmed"},
            headers={"X-Actor-Id": "HR-501"},
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"employment_status": "INACTIVE"}, "EMPLOYMENT_NOT_ACTIVE"),
        ({"location_code": "ST-015"}, "OUT_OF_SCOPE_FOR_LOCATION"),
    ],
)
def test_task_of_an_assignee_who_is_no_longer_eligible_is_released_and_rerouted(client, change, reason):
    load_fixtures(client)
    reconcile(client)
    task_id = active_task(client, exceptions_by_po(client)["PO-501"]["id"])["id"]
    assert act(client, task_id, "acknowledge", "HR-102").status_code == 200

    accept_roster_change(
        client,
        "employees",
        lambda rows: [dict(r, **change) if r["source_employee_id"] == "HR-102" else r for r in rows],
    )
    # Until the next run re-routes it, HR-102 can no longer work it.
    blocked = act(client, task_id, "start", "HR-102")
    expected = (403, "ACTOR_NOT_ACTIVE") if reason == "EMPLOYMENT_NOT_ACTIVE" else (403, "ASSIGNEE_NO_LONGER_ELIGIBLE")
    assert (blocked.status_code, blocked.json()["error"]["code"]) == expected

    reconcile(client)
    task = client.get(f"/tasks/{task_id}").json()
    # HR-101 off shift, HR-103 inactive: the store manager is the approved fallback.
    assert (task["status"], task["assignee_employee_id"]) == ("ASSIGNED", "HR-201")
    released = next(e for e in task["events"] if e["event_type"] == "RELEASED")
    assert (released["from_status"], released["details"]["reasons"]) == ("ACKNOWLEDGED", [reason])
    assert [a["reason_code"] for a in task["assignments"]] == [
        "PRIMARY_ROLE_AT_LOCATION",
        "ASSIGNEE_NO_LONGER_ELIGIBLE",
        "FALLBACK_STORE_MANAGER",
    ]


def test_task_follows_a_po_line_that_moved_store(client):
    load_fixtures(client)
    reconcile(client)
    exception_id = exceptions_by_po(client)["PO-501"]["id"]
    task_id = active_task(client, exception_id)["id"]
    accept_roster_change(
        client,
        "purchase_order_lines",
        lambda rows: [dict(r, location_code="MUM-015") if r["source_record_id"] == "ERP-PO501-L1" else r for r in rows],
    )
    # The ERP moved the line to STORE-015 and the WMS re-booked its receipts there too:
    # the same case is now evaluated at STORE-015, and its task must follow it.
    upload(
        client,
        "goods_receipt_lines",
        derived(
            "goods_receipt_lines",
            lambda rows: [dict(r, location_code="WH-MH-015") if r["po_number"] == "PO-501" else r for r in rows],
        ),
    )
    for conflict in client.get("/data-quality").json()["conflicts"]:
        client.post(
            f"/data-quality/conflicts/{conflict['id']}/resolve",
            json={"resolution": "ACCEPT_INCOMING", "reason": "receipts re-booked"},
            headers={"X-Actor-Id": "HR-501"},
        )
    reconcile(client)
    exception = client.get(f"/exceptions/{exception_id}").json()
    assert (exception["status"], exception["canonical_location_id"]) == ("OPEN", "STORE-015")
    task = client.get(f"/tasks/{task_id}").json()
    assert (task["location_id"], task["assignee_employee_id"]) == ("STORE-015", "HR-401")
    assert [e["event_type"] for e in task["events"]][-3:] == ["LOCATION_CHANGED", "RELEASED", "ASSIGNED"]
