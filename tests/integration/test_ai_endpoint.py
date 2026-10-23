"""The advisory summary endpoint degrades safely and never changes the case."""

import json

from tests.helpers import exceptions_by_po, load_fixtures, reconcile


class FakeProvider:
    name = "fake"

    def __init__(self, payload: dict):
        self.payload = payload

    def summarize(self, package, timeout_s):  # noqa: ANN001, ANN201
        return json.dumps(self.payload)


def test_disabled_provider_returns_unavailable_and_the_case_is_untouched(client):
    load_fixtures(client)
    reconcile(client)
    exception = exceptions_by_po(client)["PO-501"]
    before = client.get(f"/exceptions/{exception['id']}").json()
    response = client.post(f"/exceptions/{exception['id']}/investigation-summary")
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["provider"]) == ("UNAVAILABLE", "disabled")
    assert client.get(f"/exceptions/{exception['id']}").json() == before


def test_valid_summary_is_returned_and_invalid_one_is_rejected(make_services, make_client):
    good = {
        "summary": "PO-501 line 1: 92 of 100 EA received and posted; 8 EA not receipted.",
        "missing_context": ["Supplier dispatch quantity"],
        "suggested_steps": ["Request the ASN for PO-501"],
        "cited_record_ids": ["ERP-PO501-L1", "WMS-GRN501-A-L1", "WMS-GRN501-B-L1"],
    }
    client = make_client(make_services(ai_provider=FakeProvider(good)))
    load_fixtures(client)
    reconcile(client)
    exception_id = exceptions_by_po(client)["PO-501"]["id"]
    result = client.post(f"/exceptions/{exception_id}/investigation-summary").json()
    assert result["status"] == "OK"
    assert result["summary"]["cited_record_ids"] == good["cited_record_ids"]
    assert "advisory" in result["advisory_notice"].lower()

    invented = {**good, "cited_record_ids": ["ERP-PO501-L1", "INV-0001"]}
    client.app.state.services.ai_provider = FakeProvider(invented)
    rejected = client.post(f"/exceptions/{exception_id}/investigation-summary").json()
    assert rejected["status"] == "REJECTED"
    assert "summary" not in rejected


def test_unknown_exception_is_404(client):
    response = client.post("/exceptions/00000000-0000-0000-0000-000000000000/investigation-summary")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")
