"""Load the supplied fixture pack through the API and verify the six required
scenario outcomes, the routing outcomes and the traceability of every finding."""

from tests.helpers import evaluations_by_po, exceptions_by_po, load_fixtures, reconcile


def test_imports_report_duplicates_and_accept_everything_else(client):
    results = load_fixtures(client)
    for dataset in ("purchase_order_lines", "goods_receipt_lines"):
        result = results[dataset]
        assert result["status"] == "COMPLETED"
        assert result["counts"] == {
            "total_rows": 7,
            "accepted": 6,
            "inserted": 6,
            "unchanged": 0,
            "duplicates": 1,
            "rejected": 0,
            "conflicts": 0,
        }
        [duplicate] = result["rows"]
        assert (duplicate["row_number"], duplicate["outcome"]) == (8, "DUPLICATE_IN_FILE")
        assert "identical to row 7" in duplicate["detail"]
    for dataset in ("product_aliases", "location_aliases", "employees", "workforce_status"):
        assert results[dataset]["counts"]["accepted"] == 6


def test_six_required_scenarios(client):
    load_fixtures(client)
    run = reconcile(client)
    assert run["status"] == "COMPLETED"
    assert run["evaluated_as_of"] == "2026-10-16T09:30:00+00:00"  # 15:00 Asia/Kolkata, the frozen time

    outcomes = {po: e["outcome"] for po, e in evaluations_by_po(run).items()}
    assert outcomes == {
        "PO-501": "SHORTFALL",
        "PO-502": "NO_SHORTFALL",
        "PO-503": "NOT_DUE",
        "PO-504": "NOT_ELIGIBLE",
        "PO-505": "SHORTFALL",
        "PO-506": "SHORTFALL",
    }

    exceptions = exceptions_by_po(client)
    assert set(exceptions) == {"PO-501", "PO-505", "PO-506"}
    quantities = {
        po: (e["expected_qty"], e["received_qty"], e["variance_qty"], e["unit"]) for po, e in exceptions.items()
    }
    assert quantities == {
        "PO-501": ("100", "92", "8", "EA"),  # split receipts 60 + 32
        "PO-505": ("120", "96", "24", "EA"),  # 10 BOX x 12 vs 96 EA
        "PO-506": ("20", "10", "10", "EA"),  # repeated receipt counted once
    }
    assert {po: e["canonical_location_id"] for po, e in exceptions.items()} == {
        "PO-501": "STORE-014",
        "PO-505": "STORE-015",
        "PO-506": "STORE-014",
    }

    evaluations = evaluations_by_po(run)
    assert evaluations["PO-503"]["reason_codes"] == ["DEADLINE_NOT_PASSED"]
    assert (evaluations["PO-503"]["details"]["received_qty"], evaluations["PO-503"]["details"]["expected_qty"]) == (
        "20",
        "50",
    )
    assert evaluations["PO-504"]["reason_codes"] == ["PO_LINE_CANCELLED"]
    assert evaluations["PO-502"]["reason_codes"] == ["RECEIVED_IN_FULL"]


def test_tasks_are_routed_to_eligible_people(client):
    load_fixtures(client)
    reconcile(client)
    exceptions = exceptions_by_po(client)
    tasks = {t["exception_id"]: t for t in client.get("/tasks").json()}
    assert {po: (tasks[e["id"]]["status"], tasks[e["id"]]["assignee_employee_id"]) for po, e in exceptions.items()} == {
        "PO-501": ("ASSIGNED", "HR-102"),
        "PO-505": ("ASSIGNED", "HR-401"),
        "PO-506": ("ASSIGNED", "HR-102"),
    }
    task = tasks[exceptions["PO-505"]["id"]]
    assert task["due_at"] == "2026-10-17T09:30:00+00:00"  # frozen time + 24h
    [assignment] = client.get(f"/tasks/{task['id']}").json()["assignments"]
    assert assignment["reason_code"] == "FALLBACK_STORE_MANAGER"
    assert assignment["decision"]["policy_version"] == "shortfall_routing/1.0.0"

    by_assignee = client.get("/tasks", params={"assignee_id": "HR-102"}).json()
    assert len(by_assignee) == 2
    assert len(client.get("/tasks", params={"location_id": "STORE-015"}).json()) == 1


def test_every_finding_traces_to_source_rows_and_versions(client):
    imports = load_fixtures(client)
    run = reconcile(client)
    exception = exceptions_by_po(client)["PO-505"]
    detail = client.get(f"/exceptions/{exception['id']}").json()

    assert detail["rule_version"] == "receipt_shortfall/1.0.0" == run["rule_version"]
    assert detail["mapping_version"] == run["mapping_version"]
    assert detail["detected_at"] == run["evaluated_as_of"]
    assert detail["case_key"] == "RECEIPT_SHORTFALL|ERP|PO-505|1"

    evidence = detail["evidence"]
    po = evidence["po_line"]
    assert (po["source_record_key"], po["ordered_qty"], po["ordered_uom"], po["to_base_factor"]) == (
        "ERP-PO505-L1",
        "10",
        "BOX",
        "12",
    )
    assert po["import_rows"] == [
        {"import_id": imports["purchase_order_lines"]["import_id"], "row_number": 6, "outcome": "INSERTED"}
    ]
    [receipt] = evidence["receipts_counted"]
    assert (receipt["source_record_key"], receipt["accepted_qty_base"], receipt["on_time"]) == (
        "WMS-GRN505-A-L1",
        "96",
        True,
    )
    assert {m["source_record_key"] for m in evidence["mappings_used"]} == {
        "ERP:ERP-BOX-12",
        "ERP:MUM-015",
        "WMS:WMS-MAT-2002",
        "WMS:WH-MH-015",
    }

    supporting = {(r["dataset"], r["source_record_key"]): r for r in detail["supporting_records"]}
    po_record = supporting[("purchase_order_lines", "ERP-PO505-L1")]
    assert po_record["changed_since_detection"] is False
    assert po_record["import_rows"][0]["raw"]["item_code"] == "ERP-BOX-12"
    assert ("product_aliases", "ERP:ERP-BOX-12") in supporting

    import_detail = client.get(f"/imports/{po['import_rows'][0]['import_id']}", params={"all_rows": True}).json()
    assert any(r["source_record_key"] == "ERP-PO505-L1" for r in import_detail["rows"])

    assert [e["event_type"] for e in detail["events"]] == ["DETECTED"]
    assert detail["events"][0]["run_id"] == run["run_id"]


def test_repeated_receipt_is_visible_in_lineage_and_explanation(client):
    imports = load_fixtures(client)
    reconcile(client)
    exception = exceptions_by_po(client)["PO-506"]
    detail = client.get(f"/exceptions/{exception['id']}").json()
    [receipt] = detail["evidence"]["receipts_counted"]
    grn_import = imports["goods_receipt_lines"]["import_id"]
    assert receipt["import_rows"] == [
        {"import_id": grn_import, "row_number": 7, "outcome": "INSERTED"},
        {"import_id": grn_import, "row_number": 8, "outcome": "DUPLICATE_IN_FILE"},
    ]
    assert any("appeared 2 times" in k and "counted once" in k for k in detail["explanation"]["known"])


def test_finding_is_neutral_about_cause(client):
    load_fixtures(client)
    reconcile(client)
    for exception in client.get("/exceptions").json():
        explanation = exception["explanation"]
        assert "does not establish supplier fault" in explanation["not_established"]
        assert any("ASN" in line for line in explanation["unknown"])
        text = " ".join([explanation["summary"], *explanation["known"]]).lower()
        assert "fault" not in text and "lost" not in text


def test_data_quality_is_clean_for_the_fixture_pack(client):
    load_fixtures(client)
    reconcile(client)
    report = client.get("/data-quality").json()
    assert report["summary"] == {
        "open_issues": 0,
        "open_conflicts": 0,
        "outstanding_rejected_rows": 0,
        "outstanding_rejected_files": 0,
    }
