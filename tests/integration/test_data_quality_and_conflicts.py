"""Failure paths: unmapped records, conflicting re-sends, rejected rows and files,
incomplete receipt evidence and an import that crashes half way."""

import pytest
from sqlalchemy import func, select

from app.db.models import ImportBatch, PoLineFact, ReceiptLineFact
from app.ingestion.service import ImportService
from tests.helpers import (
    active_task,
    derived,
    evaluations_by_po,
    exceptions_by_po,
    fixture_bytes,
    fixture_rows,
    load_fixtures,
    reconcile,
    to_csv,
    upload,
)


def issues(client) -> dict[tuple[str, str], dict]:
    return {(i["code"], i["subject"]["record_key"]): i for i in client.get("/data-quality").json()["issues"]}


def with_extra_po_line(item_code: str) -> bytes:
    def transform(rows):
        extra = dict(rows[0], source_record_id="ERP-PO507-L1", po_number="PO-507", item_code=item_code)
        return rows + [extra]

    return derived("purchase_order_lines", transform)


# ---------------------------------------------------------------- unmapped identity


def test_unknown_product_is_a_data_quality_issue_not_an_exception(client):
    load_fixtures(client, replace={"purchase_order_lines": with_extra_po_line("ERP-SKU-999")})
    run = reconcile(client)
    assert evaluations_by_po(run)["PO-507"]["reason_codes"] == ["PRODUCT_UNMAPPED"]
    assert "PO-507" not in exceptions_by_po(client)
    issue = issues(client)[("PRODUCT_UNMAPPED", "ERP-PO507-L1")]
    assert issue["details"]["affected_cases"] == ["RECEIPT_SHORTFALL|ERP|PO-507|1"]

    # Once the crosswalk knows the item, the next run evaluates it and the issue resolves itself.
    header, rows = fixture_rows("product_aliases")
    rows.append(
        {
            "source_system": "ERP",
            "external_item_id": "ERP-SKU-999",
            "canonical_product_id": "SKU-999",
            "source_uom": "EA",
            "base_uom": "EA",
            "to_base_factor": "1",
        }
    )
    upload(client, "product_aliases", to_csv(header, rows))
    reconcile(client)
    assert exceptions_by_po(client)["PO-507"]["variance_qty"] == "100"  # no receipts at all for PO-507
    assert ("PRODUCT_UNMAPPED", "ERP-PO507-L1") not in issues(client)
    resolved = client.get("/data-quality", params={"include_resolved": True}).json()["issues"]
    assert any(i["code"] == "PRODUCT_UNMAPPED" and i["status"] == "RESOLVED" for i in resolved)


def test_orphan_receipt_is_reported(client):
    def add_orphan(rows):
        return rows + [
            dict(rows[0], source_record_id="WMS-GRN599-A-L1", receipt_number="GRN-599-A", po_number="PO-599")
        ]

    load_fixtures(client, replace={"goods_receipt_lines": derived("goods_receipt_lines", add_orphan)})
    reconcile(client)
    assert ("ORPHAN_RECEIPT", "WMS-GRN599-A-L1") in issues(client)


# ---------------------------------------------------------------- conflicting re-sends


def resend_with_changed_quantity(qty: str) -> bytes:
    def transform(rows):
        return [dict(r, accepted_qty=qty) if r["source_record_id"] == "WMS-GRN501-B-L1" else r for r in rows]

    return derived("goods_receipt_lines", transform)


def test_conflicting_resend_is_flagged_and_does_not_overwrite(client, services):
    load_fixtures(client)
    reconcile(client)
    result = upload(client, "goods_receipt_lines", resend_with_changed_quantity("40")).json()
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    assert result["counts"]["conflicts"] == 1
    conflict_row = next(r for r in result["rows"] if r["outcome"] == "CONFLICT")
    assert conflict_row["source_record_key"] == "WMS-GRN501-B-L1"

    with services.sessions() as session, session.begin():
        fact = session.scalar(select(ReceiptLineFact).where(ReceiptLineFact.source_record_key == "WMS-GRN501-B-L1"))
        assert (fact.accepted_qty, fact.version, fact.review_status) == (32, 1, "CONFLICT")

    report = client.get("/data-quality").json()
    [conflict] = report["conflicts"]
    assert conflict["changed_fields"] == ["accepted_qty"]
    assert (conflict["existing"]["payload"]["accepted_qty"], conflict["incoming"]["payload"]["accepted_qty"]) == (
        "32",
        "40",
    )

    # The existing case is kept open but cannot be re-evaluated until someone decides.
    run = reconcile(client)
    assert evaluations_by_po(run)["PO-501"]["reason_codes"] == ["SOURCE_RECORD_IN_CONFLICT"]
    exception = exceptions_by_po(client)["PO-501"]
    assert (exception["status"], exception["last_evaluation_outcome"]) == ("OPEN", "NOT_EVALUABLE")
    events = [e["event_type"] for e in client.get(f"/exceptions/{exception['id']}").json()["events"]]
    assert events == ["DETECTED", "REEVALUATION_BLOCKED"]
    assert ("SOURCE_RECORD_IN_CONFLICT", "WMS-GRN501-B-L1") in issues(client)

    # Re-sending the same conflicting file does not open a second conflict.
    again = upload(client, "goods_receipt_lines", resend_with_changed_quantity("40")).json()
    assert next(r for r in again["rows"] if r["outcome"] == "CONFLICT")["conflict_id"] == conflict["id"]
    assert len(client.get("/data-quality").json()["conflicts"]) == 1


def test_accepting_the_incoming_version_reevaluates_with_it(client, services):
    load_fixtures(client)
    reconcile(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("40"))
    [conflict] = client.get("/data-quality").json()["conflicts"]
    task_id = active_task(client, exceptions_by_po(client)["PO-501"]["id"])["id"]

    response = client.post(
        f"/data-quality/conflicts/{conflict['id']}/resolve",
        json={"resolution": "ACCEPT_INCOMING", "reason": "WMS confirmed the corrected count"},
        headers={"X-Actor-Id": "HR-201"},
    )
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["resolved_by"]) == ("RESOLVED", "HR-201")
    with services.sessions() as session, session.begin():
        fact = session.scalar(select(ReceiptLineFact).where(ReceiptLineFact.source_record_key == "WMS-GRN501-B-L1"))
        assert (fact.accepted_qty, fact.version, fact.review_status) == (40, 2, "OK")

    reconcile(client)  # 60 + 40 = 100: no longer a shortfall
    exception = exceptions_by_po(client)["PO-501"]
    assert (exception["status"], exception["variance_qty"]) == ("CLEARED", "0")
    assert client.get(f"/tasks/{task_id}").json()["status"] == "CANCELLED"

    second = client.post(
        f"/data-quality/conflicts/{conflict['id']}/resolve",
        json={"resolution": "KEEP_EXISTING", "reason": "again"},
        headers={"X-Actor-Id": "HR-201"},
    )
    assert (second.status_code, second.json()["error"]["code"]) == (409, "CONFLICT_ALREADY_RESOLVED")


def test_keeping_the_existing_version_unblocks_evaluation(client):
    load_fixtures(client)
    reconcile(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("40"))
    reconcile(client)
    [conflict] = client.get("/data-quality").json()["conflicts"]
    client.post(
        f"/data-quality/conflicts/{conflict['id']}/resolve",
        json={"resolution": "KEEP_EXISTING", "reason": "re-send was a WMS replay error"},
        headers={"X-Actor-Id": "HR-201"},
    )
    reconcile(client)
    exception = exceptions_by_po(client)["PO-501"]
    assert (exception["status"], exception["last_evaluation_outcome"], exception["variance_qty"]) == (
        "OPEN",
        "SHORTFALL",
        "8",
    )
    events = [e["event_type"] for e in client.get(f"/exceptions/{exception['id']}").json()["events"]]
    assert events == ["DETECTED", "REEVALUATION_BLOCKED", "REEVALUATION_UNBLOCKED"]


# ---------------------------------------------------------------- rejected rows and files


def test_invalid_rows_are_rejected_with_reasons_and_valid_rows_still_load(client):
    def break_rows(rows):
        rows[0]["expected_receipt_by"] = "2026-10-15 12:00"  # no offset
        rows[1]["ordered_qty"] = "-40"
        rows[2]["location_code"] = ""
        return rows

    response = upload(client, "purchase_order_lines", derived("purchase_order_lines", break_rows))
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    assert (result["counts"]["accepted"], result["counts"]["rejected"], result["counts"]["duplicates"]) == (3, 3, 1)
    rejected = {r["row_number"]: r["errors"] for r in result["rows"] if r["outcome"] == "REJECTED"}
    assert rejected[2] == [
        {
            "field": "expected_receipt_by",
            "code": "value_error",
            "message": "timestamp has no UTC offset; refusing to guess a timezone",
        }
    ]
    assert rejected[3][0]["field"] == "ordered_qty"
    assert rejected[4][0] == {
        "field": "location_code",
        "code": "missing",
        "message": "required value is blank or missing",
    }

    report = client.get("/data-quality").json()
    assert report["summary"]["outstanding_rejected_rows"] == 3
    assert {r["raw"]["source_record_id"] for r in report["rejected_rows"]} == {
        "ERP-PO501-L1",
        "ERP-PO502-L1",
        "ERP-PO503-L1",
    }


def test_rejected_receipt_row_blocks_the_line_it_names(client):
    def break_receipt(rows):
        return [dict(r, accepted_qty="lots") if r["source_record_id"] == "WMS-GRN501-B-L1" else r for r in rows]

    load_fixtures(client, replace={"goods_receipt_lines": derived("goods_receipt_lines", break_receipt)})
    run = reconcile(client)
    # Without the rejected row PO-501 would look 40 EA short; we refuse to say so.
    assert evaluations_by_po(run)["PO-501"]["reason_codes"] == ["REJECTED_RECEIPT_ROW_FOR_LINE"]
    assert "PO-501" not in exceptions_by_po(client)

    upload(client, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))  # corrected re-send
    reconcile(client)
    assert exceptions_by_po(client)["PO-501"]["variance_qty"] == "8"


def test_file_missing_columns_is_rejected_whole(client, services):
    header, rows = fixture_rows("goods_receipt_lines")
    header.remove("posting_status")
    response = upload(client, "goods_receipt_lines", to_csv(header, [{k: r[k] for k in header} for r in rows]))
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["file_errors"][0]["code"] == "MISSING_COLUMNS"
    assert body["file_errors"][0]["details"]["missing"] == ["posting_status"]
    with services.sessions() as session, session.begin():
        assert session.scalar(select(func.count()).select_from(ReceiptLineFact)) == 0
    assert client.get("/data-quality").json()["summary"]["outstanding_rejected_files"] == 1


def test_unknown_dataset_and_profile(client):
    unknown = upload(client, "invoices", b"a\n1\n")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "UNKNOWN_DATASET")
    bad_profile = upload(client, "employees", fixture_bytes("employees"), profile="sap_v9")
    assert (bad_profile.status_code, bad_profile.json()["error"]["code"]) == (422, "UNKNOWN_PROFILE")


# ---------------------------------------------------------------- receipt evidence completeness


def test_no_receipt_feed_means_no_shortfall_claims(client):
    load_fixtures(client, skip=("goods_receipt_lines",))
    run = reconcile(client)
    outcomes = {po: e["reason_codes"] for po, e in evaluations_by_po(run).items()}
    assert outcomes["PO-501"] == ["RECEIPT_FEED_MISSING"]
    assert client.get("/exceptions").json() == []
    assert ("RECEIPT_FEED_MISSING", "(feed)") in issues(client)


def test_receipt_feed_extracted_before_the_deadline_is_stale(client):
    load_fixtures(client, skip=("goods_receipt_lines",))
    upload(
        client,
        "goods_receipt_lines",
        fixture_bytes("goods_receipt_lines"),
        source_extracted_at="2026-10-15T11:59:00+05:30",
    )
    run = reconcile(client)
    reasons = {po: e["reason_codes"] for po, e in evaluations_by_po(run).items()}
    assert reasons["PO-501"] == ["RECEIPT_FEED_STALE"]  # deadline 12:00
    assert reasons["PO-505"] == ["RECEIPT_FEED_STALE"]
    assert client.get("/exceptions").json() == []


def test_extract_time_in_the_future_or_naive_is_refused(client):
    future = upload(
        client,
        "goods_receipt_lines",
        fixture_bytes("goods_receipt_lines"),
        source_extracted_at="2026-10-17T00:00:00+05:30",
    )
    assert (future.status_code, future.json()["error"]["code"]) == (422, "EXTRACT_TIME_IN_FUTURE")
    naive = upload(
        client, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"), source_extracted_at="2026-10-16T10:00:00"
    )
    assert (naive.status_code, naive.json()["error"]["code"]) == (422, "NAIVE_TIMESTAMP")


# ---------------------------------------------------------------- crash half way


def test_import_that_fails_midway_rolls_back_and_can_be_retried(client, services, monkeypatch):
    original = ImportService._load_rows

    def explode(self, session, batch, spec, profile, parsed):  # noqa: ANN001, ANN202
        original(self, session, batch, spec, profile, parsed)
        session.flush()  # rows are written inside the transaction...
        raise RuntimeError("worker lost its connection")  # ...then it dies before commit

    monkeypatch.setattr(ImportService, "_load_rows", explode)
    response = upload(client, "purchase_order_lines", fixture_bytes("purchase_order_lines"))
    assert response.status_code == 500
    error = response.json()["error"]
    assert (error["code"], error["details"]["reason"]) == ("IMPORT_FAILED", "RuntimeError")

    import_id = error["details"]["import_id"]
    failed = client.get(f"/imports/{import_id}").json()
    assert (failed["status"], failed["counts"]["total_rows"], failed["rows"]) == ("FAILED", 0, [])
    with services.sessions() as session, session.begin():
        assert session.scalar(select(func.count()).select_from(PoLineFact)) == 0

    monkeypatch.setattr(ImportService, "_load_rows", original)
    retry = upload(client, "purchase_order_lines", fixture_bytes("purchase_order_lines")).json()
    assert (retry["status"], retry["counts"]["inserted"]) == ("COMPLETED", 6)
    with services.sessions() as session, session.begin():
        statuses = sorted(session.scalars(select(ImportBatch.status)))
    assert statuses == ["COMPLETED", "FAILED"]


@pytest.mark.parametrize("size_limit", [100])
def test_oversized_upload_is_refused(make_services, make_client, size_limit):
    client = make_client(make_services(settings={"max_upload_bytes": size_limit}))
    response = upload(client, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
    assert (response.status_code, response.json()["error"]["code"]) == (413, "PAYLOAD_TOO_LARGE")


# ---------------------------------------------------------------- review regressions


def grn_text_with(line_for_502: str) -> bytes:
    lines = fixture_bytes("goods_receipt_lines").decode().splitlines()
    return ("\n".join(line_for_502 if line.startswith("WMS-GRN502-A-L1,") else line for line in lines) + "\n").encode()


def test_row_with_a_stray_delimiter_still_blocks_the_line_it_names(client):
    # "40" became "4,0": one cell too many, so the row is rejected, but its PO columns
    # (before the stray comma) are still readable and PO-502 must not look 40 EA short.
    broken = grn_text_with(
        "WMS-GRN502-A-L1,GRN-502-A,1,PO-502,1,WH-MH-014,WMS-BRC-001,4,0,EA,2026-10-15T11:00:00+05:30,POSTED"
    )
    load_fixtures(client, replace={"goods_receipt_lines": broken})
    run = reconcile(client)
    assert evaluations_by_po(run)["PO-502"]["reason_codes"] == ["REJECTED_RECEIPT_ROW_FOR_LINE"]
    assert "PO-502" not in exceptions_by_po(client)


def test_unreadable_receipt_row_blocks_every_line_until_dismissed(client):
    load_fixtures(client, replace={"goods_receipt_lines": fixture_bytes("goods_receipt_lines") + b"%%%garbage%%%\n"})
    run = reconcile(client)
    reasons = {po: e["reason_codes"] for po, e in evaluations_by_po(run).items()}
    assert reasons["PO-501"] == reasons["PO-505"] == ["UNREADABLE_RECEIPT_ROW_IN_FEED"]
    assert client.get("/exceptions").json() == []

    [row] = client.get("/data-quality").json()["rejected_rows"]
    not_a_steward = client.post(
        f"/data-quality/rejected-rows/{row['row_id']}/dismiss",
        json={"reason": "noise"},
        headers={"X-Actor-Id": "HR-102"},
    )
    assert not_a_steward.status_code == 403
    dismissed = client.post(
        f"/data-quality/rejected-rows/{row['row_id']}/dismiss",
        json={"reason": "trailing garbage line in the WMS export"},
        headers={"X-Actor-Id": "HR-201"},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert client.get("/data-quality").json()["rejected_rows"] == []
    reconcile(client)
    assert set(exceptions_by_po(client)) == {"PO-501", "PO-505", "PO-506"}


def test_rejected_rows_stay_visible_until_the_record_is_validly_resent(client):
    def break_receipt(rows):
        return [dict(r, accepted_qty="lots") if r["source_record_id"] == "WMS-GRN501-B-L1" else r for r in rows]

    load_fixtures(client, replace={"goods_receipt_lines": derived("goods_receipt_lines", break_receipt)})
    # A later, clean, partial file says nothing about the row that failed.
    partial = derived("goods_receipt_lines", lambda rows: [r for r in rows if r["po_number"] == "PO-505"])
    assert upload(client, "goods_receipt_lines", partial).json()["status"] == "COMPLETED"
    report = client.get("/data-quality").json()
    assert [r["source_record_key"] for r in report["rejected_rows"]] == ["WMS-GRN501-B-L1"]

    upload(client, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))  # corrected re-send
    assert client.get("/data-quality").json()["rejected_rows"] == []


def test_one_grn_line_cannot_count_against_two_po_lines(client):
    def rekey(rows):
        twin = dict(next(r for r in rows if r["source_record_id"] == "WMS-GRN501-A-L1"))
        twin.update(source_record_id="WMS-GRN501-A-L1-R", po_number="PO-506", product_ref="WMS-BRC-003")
        return rows + [twin]

    load_fixtures(client, replace={"goods_receipt_lines": derived("goods_receipt_lines", rekey)})
    run = reconcile(client)
    reasons = {po: e["reason_codes"] for po, e in evaluations_by_po(run).items()}
    assert reasons["PO-501"] == ["RECEIPT_LINE_KEY_COLLISION"]
    assert reasons["PO-506"] == ["RECEIPT_LINE_KEY_COLLISION"]
    assert set(exceptions_by_po(client)) == {"PO-505"}


def test_only_an_active_data_steward_can_decide_a_conflict(client):
    load_fixtures(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("40"))
    [conflict] = client.get("/data-quality").json()["conflicts"]
    body = {"resolution": "ACCEPT_INCOMING", "reason": "looks right"}
    for actor, code in (
        ("nobody-at-all", "UNKNOWN_ACTOR"),
        ("HR-103", "ACTOR_NOT_ACTIVE"),
        ("HR-102", "ACTOR_NOT_PERMITTED"),
    ):
        response = client.post(
            f"/data-quality/conflicts/{conflict['id']}/resolve", json=body, headers={"X-Actor-Id": actor}
        )
        assert (response.status_code, response.json()["error"]["code"]) == (403, code)
    superseded = client.post(
        f"/data-quality/conflicts/{conflict['id']}/resolve",
        json={"resolution": "SUPERSEDED", "reason": "x"},
        headers={"X-Actor-Id": "HR-201"},
    )
    assert superseded.status_code == 422
    assert client.get("/data-quality").json()["summary"]["open_conflicts"] == 1


def resolve(client, conflict_id, resolution):
    response = client.post(
        f"/data-quality/conflicts/{conflict_id}/resolve",
        json={"resolution": resolution, "reason": "checked with WMS"},
        headers={"X-Actor-Id": "HR-201"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def open_conflicts(client):
    return {c["incoming"]["payload"]["accepted_qty"]: c for c in client.get("/data-quality").json()["conflicts"]}


def test_deciding_one_conflict_never_closes_another_unreviewed_one(client):
    load_fixtures(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("50"))
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("60"))
    conflicts = open_conflicts(client)
    assert set(conflicts) == {"50", "60"}

    resolve(client, conflicts["50"]["id"], "KEEP_EXISTING")
    assert set(open_conflicts(client)) == {"60"}  # still waiting for a person
    # Re-sending 60 is the same open question, not "already reviewed".
    again = upload(client, "goods_receipt_lines", resend_with_changed_quantity("60")).json()
    assert "flagged for review" in next(r for r in again["rows"] if r["outcome"] == "CONFLICT")["detail"]

    # Accepting 60 makes it current; a rejected-50 re-send is then a new question again.
    resolve(client, open_conflicts(client)["60"]["id"], "ACCEPT_INCOMING")
    assert open_conflicts(client) == {}
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("50"))
    reopened = open_conflicts(client)["50"]
    assert reopened["existing"]["payload"]["accepted_qty"] == "60"


def test_accepting_one_version_rebases_other_open_conflicts(client):
    load_fixtures(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("50"))
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("60"))
    resolve(client, open_conflicts(client)["50"]["id"], "ACCEPT_INCOMING")
    [still_open] = open_conflicts(client).values()
    assert (still_open["existing"]["payload"]["accepted_qty"], still_open["incoming"]["payload"]["accepted_qty"]) == (
        "50",
        "60",
    )


def test_a_rejected_change_resent_for_the_same_version_is_not_reraised(client):
    load_fixtures(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("50"))
    resolve(client, open_conflicts(client)["50"]["id"], "KEEP_EXISTING")
    again = upload(client, "goods_receipt_lines", resend_with_changed_quantity("50")).json()
    row = next(r for r in again["rows"] if r["outcome"] == "CONFLICT")
    assert "already rejected this exact change" in row["detail"]
    assert open_conflicts(client) == {}


def test_the_same_change_after_the_record_moved_on_is_a_new_question(client):
    # accept 50, accept 32 back, then 50 again: the old acceptance must not hide it.
    load_fixtures(client)
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("50"))
    resolve(client, open_conflicts(client)["50"]["id"], "ACCEPT_INCOMING")
    upload(client, "goods_receipt_lines", fixture_bytes("goods_receipt_lines"))  # 32 again
    resolve(client, open_conflicts(client)["32"]["id"], "ACCEPT_INCOMING")
    upload(client, "goods_receipt_lines", resend_with_changed_quantity("50"))
    assert set(open_conflicts(client)) == {"50"}
