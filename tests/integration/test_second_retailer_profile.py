"""Adding another retailer's export format is a profile registration. The fact
tables, the mapping and the detector are unchanged."""

from app.ingestion.datasets import DatasetRegistry, SourceProfile
from tests.helpers import derived, exceptions_by_po, fixture_rows, load_fixtures, reconcile, to_csv, upload

RETAILER_B_RECEIPTS = SourceProfile(
    dataset="goods_receipt_lines",
    name="retailer_b_wms_v2",
    source_system="WMS",
    column_map={
        "GRN_LINE_ID": "source_record_key",
        "GRN_NO": "receipt_number",
        "GRN_LINE": "receipt_line",
        "PO_REF": "po_number",
        "PO_LINE_REF": "po_line_number",
        "SITE": "location_ref",
        "ITEM": "product_ref",
        "QTY_OK": "accepted_qty",
        "UNIT": "uom",
        "RECEIVED_TS": "received_at",
        "STATE": "posting_status",
    },
    description="A second retailer's WMS export with its own column names",
)


def as_retailer_b(content_rows: list[dict[str, str]]) -> bytes:
    rename = {
        "source_record_id": "GRN_LINE_ID",
        "receipt_number": "GRN_NO",
        "receipt_line": "GRN_LINE",
        "po_number": "PO_REF",
        "po_line_number": "PO_LINE_REF",
        "location_code": "SITE",
        "product_ref": "ITEM",
        "accepted_qty": "QTY_OK",
        "uom": "UNIT",
        "received_at": "RECEIVED_TS",
        "posting_status": "STATE",
    }
    rows = [{rename[k]: v.lower() if k == "posting_status" else v for k, v in r.items()} for r in content_rows]
    return to_csv(list(rename.values()), rows)


def test_alternative_export_format_gives_the_same_findings(make_services, make_client):
    registry = DatasetRegistry()
    registry.register(RETAILER_B_RECEIPTS)
    client = make_client(make_services(registry=registry))

    load_fixtures(client, skip=("goods_receipt_lines",))
    _, rows = fixture_rows("goods_receipt_lines")
    response = upload(client, "goods_receipt_lines", as_retailer_b(rows), profile="retailer_b_wms_v2")
    assert response.status_code == 201, response.text
    assert (response.json()["profile"], response.json()["counts"]["duplicates"]) == ("retailer_b_wms_v2", 1)
    reconcile(client)
    assert {po: e["variance_qty"] for po, e in exceptions_by_po(client).items()} == {
        "PO-501": "8",
        "PO-505": "24",
        "PO-506": "10",
    }

    # Identical content from either format is the same fact: the original export is UNCHANGED.
    original = upload(client, "goods_receipt_lines", derived("goods_receipt_lines", lambda r: r)).json()
    assert (original["counts"]["inserted"], original["counts"]["unchanged"]) == (0, 6)


def test_rejected_row_in_another_format_still_blocks_its_line(make_services, make_client):
    registry = DatasetRegistry()
    registry.register(RETAILER_B_RECEIPTS)
    client = make_client(make_services(registry=registry))
    load_fixtures(client, skip=("goods_receipt_lines",))
    _, rows = fixture_rows("goods_receipt_lines")
    rows = [dict(r, accepted_qty="forty") if r["source_record_id"] == "WMS-GRN502-A-L1" else r for r in rows]
    result = upload(client, "goods_receipt_lines", as_retailer_b(rows), profile="retailer_b_wms_v2").json()
    assert result["counts"]["rejected"] == 1
    reconcile(client)
    assert "PO-502" not in exceptions_by_po(client)  # never reported 40 EA short
    [rejected] = client.get("/data-quality").json()["rejected_rows"]
    assert (rejected["source_record_key"], rejected["profile"]) == ("WMS-GRN502-A-L1", "retailer_b_wms_v2")
