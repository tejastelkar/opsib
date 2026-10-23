"""The optional AI summary: schema, citation and number checks, timeouts, and the
fact that a broken provider never breaks the case."""

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from app.ai.investigation import (
    ClaudeProvider,
    DisabledProvider,
    SummaryRejected,
    build_evidence_package,
    summarize_exception,
    validate_summary,
)
from app.db.models import ExceptionCase
from app.detection.explanations import explain_shortfall
from app.detection.rules import ReceiptFeed, RuleParams, evaluate_po_line
from app.detection.service import build_evidence
from tests import factories as f


@pytest.fixture
def exception() -> ExceptionCase:
    ev = evaluate_po_line(
        [f.po_line("ERP-PO505-L1")], f.receipts_for("PO-505"), ReceiptFeed(f.FROZEN_AS_OF), f.FROZEN_AS_OF
    )
    evidence, evidence_hash = build_evidence(ev, {}, ReceiptFeed(f.FROZEN_AS_OF), RuleParams())
    return ExceptionCase(
        id=uuid.uuid4(),
        exception_type="RECEIPT_SHORTFALL",
        po_number="PO-505",
        po_line_number=1,
        po_source_record_key="ERP-PO505-L1",
        canonical_product_id="SKU-002",
        canonical_location_id="STORE-015",
        unit="EA",
        evidence=evidence,
        evidence_hash=evidence_hash,
        explanation=explain_shortfall(ev, "Asia/Kolkata"),
    )


def good_output(**overrides) -> str:
    body = {
        "summary": "PO-505 line 1 expected 120 EA; posted receipts total 96 EA, leaving 24 EA unreceipted.",
        "missing_context": ["No supplier dispatch note is available."],
        "suggested_steps": ["Ask the supplier for the ASN for PO-505."],
        "cited_record_ids": ["ERP-PO505-L1", "WMS-GRN505-A-L1"],
    }
    body.update(overrides)
    return json.dumps(body)


class StaticProvider:
    name = "static"

    def __init__(self, text: str = "", delay: float = 0.0, error: Exception | None = None):
        self.text, self.delay, self.error = text, delay, error

    def summarize(self, package, timeout_s):  # noqa: ANN001, ANN201
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.text


def test_package_is_minimal_and_has_no_employee_data(exception):
    package = build_evidence_package(exception)
    assert package.record_ids() == {"ERP-PO505-L1", "WMS-GRN505-A-L1"}
    serialized = package.model_dump_json()
    for forbidden in ("HR-", "display_name", "Rohan", "import_rows"):
        assert forbidden not in serialized


def test_valid_output_is_accepted(exception):
    summary = validate_summary(good_output(), build_evidence_package(exception))
    assert summary.cited_record_ids == ["ERP-PO505-L1", "WMS-GRN505-A-L1"]


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("not json", "not valid JSON"),
        (good_output(verified=True), "schema"),
        (good_output(cited_record_ids=[]), "schema"),
        (good_output(cited_record_ids=["ERP-PO505-L1", "WMS-GRN999-Z-L9"]), "not in the evidence package"),
        (good_output(summary="The supplier shipped 18 units fewer than invoiced."), "numbers not present"),
    ],
)
def test_invalid_output_is_rejected(exception, raw, reason):
    with pytest.raises(SummaryRejected) as info:
        validate_summary(raw, build_evidence_package(exception))
    assert any(reason in r for r in info.value.reasons)


def test_disabled_provider_degrades_to_unavailable(exception):
    result = summarize_exception(exception, DisabledProvider(), timeout_s=1)
    assert result["status"] == "UNAVAILABLE"


def test_slow_provider_times_out_without_blocking_the_case(exception):
    started = time.perf_counter()
    result = summarize_exception(exception, StaticProvider(good_output(), delay=1.0), timeout_s=0.1)
    assert result["status"] == "UNAVAILABLE"
    assert "within" in result["reasons"][0]
    assert time.perf_counter() - started < 0.9


def test_crashing_provider_degrades_to_unavailable(exception):
    result = summarize_exception(exception, StaticProvider(error=RuntimeError("boom")), timeout_s=1)
    assert (result["status"], result["reasons"]) == ("UNAVAILABLE", ["provider error: RuntimeError"])


def test_rejected_output_is_not_shown(exception):
    result = summarize_exception(exception, StaticProvider(good_output(cited_record_ids=["MADE-UP"])), timeout_s=1)
    assert result["status"] == "REJECTED"
    assert "summary" not in result


def test_claude_provider_parses_the_text_block_from_a_stub_client(exception):
    calls = {}

    class Messages:
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            calls.update(kwargs)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=good_output())])

    client = SimpleNamespace(with_options=lambda **_: SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    result = summarize_exception(exception, ClaudeProvider("claude-opus-5-5", client=client), timeout_s=2)
    assert result["status"] == "OK"
    assert calls["model"] == "claude-opus-5-5"
    assert calls["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in calls


def test_claude_refusal_is_unavailable(exception):
    class Messages:
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            return SimpleNamespace(stop_reason="refusal", content=[])

    client = SimpleNamespace(with_options=lambda **_: SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    result = summarize_exception(exception, ClaudeProvider("claude-opus-5-5", client=client), timeout_s=2)
    assert result["status"] == "UNAVAILABLE"
