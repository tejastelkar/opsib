"""The receipt-shortfall rule, in isolation: canonical records in, Evaluation out."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.detection.explanations import explain_shortfall
from app.detection.rules import ReceiptFeed, RejectedReceiptRow, RuleParams, evaluate_po_line
from app.enums import EvaluationOutcome as O
from tests import factories as f

AS_OF = f.FROZEN_AS_OF
FEED = ReceiptFeed(AS_OF, ("import-1",))


def evaluate(po, receipts, *, as_of=AS_OF, feed=FEED, params=RuleParams(), rejected=()):
    return evaluate_po_line([po], receipts, feed, as_of, params, rejected)


def codes(evaluation):
    return [r.code for r in evaluation.reasons]


# ---------------------------------------------------------------- partial receipts and grain


def test_split_receipts_are_summed_per_po_line():
    ev = evaluate(f.po_line("ERP-PO501-L1"), f.receipts_for("PO-501"))
    assert ev.outcome == O.SHORTFALL
    assert (ev.expected_qty, ev.received_qty, ev.variance_qty, ev.unit) == (100, 92, 8, "EA")
    assert sorted(c.receipt.receipt_number for c in ev.counted) == ["GRN-501-A", "GRN-501-B"]


def test_full_receipt_is_not_a_shortfall():
    ev = evaluate(f.po_line("ERP-PO502-L1"), f.receipts_for("PO-502"))
    assert ev.outcome == O.NO_SHORTFALL
    assert codes(ev) == ["RECEIVED_IN_FULL"]
    assert ev.variance_qty == 0


def test_identical_receipt_repeats_count_once():
    # The fixture repeats WMS-GRN506-A-L1; the factory collapses it to one fact, as ingestion does.
    receipts = f.receipts_for("PO-506")
    assert len(receipts) == 1
    ev = evaluate(f.po_line("ERP-PO506-L1"), receipts)
    assert (ev.outcome, ev.received_qty, ev.variance_qty) == (O.SHORTFALL, 10, 10)


def test_two_source_records_for_one_po_line_cannot_be_evaluated():
    first = f.po_line("ERP-PO501-L1")
    second = f.po_line("ERP-PO501-L1", source_record_key="ERP-PO501-L1-COPY")
    ev = evaluate_po_line([first, second], f.receipts_for("PO-501"), FEED, AS_OF)
    assert ev.outcome == O.NOT_EVALUABLE
    assert codes(ev) == ["PO_LINE_KEY_COLLISION"]


def test_two_source_records_for_one_receipt_line_cannot_be_evaluated():
    receipts = f.receipts_for("PO-501")
    duplicate = f.receipt("WMS-GRN501-A-L1", source_record_key="WMS-GRN501-A-L1-RESENT")
    ev = evaluate(f.po_line("ERP-PO501-L1"), [*receipts, duplicate])
    assert ev.outcome == O.NOT_EVALUABLE
    assert set(codes(ev)) == {"RECEIPT_LINE_KEY_COLLISION"}


# ---------------------------------------------------------------- units


def test_cartons_are_converted_to_base_units_before_comparison():
    ev = evaluate(f.po_line("ERP-PO505-L1"), f.receipts_for("PO-505"))
    po = ev.po_line
    assert (po.ordered_qty, po.ordered_uom, po.quantity.factor) == (10, "BOX", 12)
    assert (ev.expected_qty, ev.received_qty, ev.variance_qty, ev.unit) == (120, 96, 24, "EA")


def test_unconvertible_unit_is_not_evaluable():
    ev = evaluate(f.po_line("ERP-PO505-L1", ordered_uom="PALLET"), f.receipts_for("PO-505"))
    assert ev.outcome == O.NOT_EVALUABLE
    assert codes(ev) == ["UOM_NOT_CONVERTIBLE"]


# ---------------------------------------------------------------- deadline boundaries


@pytest.mark.parametrize(
    "offset, outcome",
    [
        (timedelta(hours=-1), O.NOT_DUE),
        (timedelta(0), O.NOT_DUE),  # at the deadline the window has not yet closed
        (timedelta(microseconds=1), O.SHORTFALL),
        (timedelta(days=1), O.SHORTFALL),
    ],
)
def test_deadline_must_have_passed(offset, outcome):
    po = f.po_line("ERP-PO501-L1")
    ev = evaluate(po, f.receipts_for("PO-501"), as_of=po.deadline + offset, feed=ReceiptFeed(po.deadline + offset))
    assert ev.outcome == outcome


def test_future_deadline_reports_position_so_far_without_raising():
    ev = evaluate(f.po_line("ERP-PO503-L1"), f.receipts_for("PO-503"))
    assert ev.outcome == O.NOT_DUE
    assert codes(ev) == ["DEADLINE_NOT_PASSED"]
    assert (ev.expected_qty, ev.received_qty) == (50, 20)


def test_receipt_after_evaluation_time_is_not_counted():
    late = f.receipt("WMS-GRN501-B-L1", received_at=AS_OF + timedelta(minutes=1))
    ev = evaluate(f.po_line("ERP-PO501-L1"), [f.receipt("WMS-GRN501-A-L1"), late])
    assert ev.received_qty == 60
    assert [(x.receipt.ref.source_record_key, x.reason_code) for x in ev.excluded] == [
        ("WMS-GRN501-B-L1", "RECEIVED_AFTER_EVALUATION_TIME")
    ]


def test_receipt_after_deadline_but_before_evaluation_counts_and_is_marked_late():
    po = f.po_line("ERP-PO501-L1")
    late = f.receipt("WMS-GRN501-B-L1", received_at=po.deadline + timedelta(hours=2))
    ev = evaluate(po, [f.receipt("WMS-GRN501-A-L1"), late])
    assert ev.received_qty == 92
    assert {c.receipt.ref.source_record_key: c.on_time for c in ev.counted} == {
        "WMS-GRN501-A-L1": True,
        "WMS-GRN501-B-L1": False,
    }


# ---------------------------------------------------------------- eligibility and status


def test_cancelled_line_is_not_eligible_even_with_no_receipts():
    ev = evaluate(f.po_line("ERP-PO504-L1"), [])
    assert ev.outcome == O.NOT_ELIGIBLE
    assert codes(ev) == ["PO_LINE_CANCELLED"]


def test_cancelled_line_is_not_eligible_even_when_unmapped():
    ev = evaluate(f.po_line("ERP-PO504-L1", item_ref="ERP-UNKNOWN"), [])
    assert ev.outcome == O.NOT_ELIGIBLE


def test_unknown_po_status_is_not_guessed():
    ev = evaluate(f.po_line("ERP-PO501-L1", status_code="CLOSED_SHORT"), f.receipts_for("PO-501"))
    assert ev.outcome == O.NOT_EVALUABLE
    assert codes(ev) == ["UNRECOGNIZED_PO_STATUS"]


@pytest.mark.parametrize("status, expected_received", [("REVERSED", 60), ("CANCELLED", 60)])
def test_reversed_or_cancelled_receipts_contribute_nothing(status, expected_received):
    ev = evaluate(
        f.po_line("ERP-PO501-L1"),
        [f.receipt("WMS-GRN501-A-L1"), f.receipt("WMS-GRN501-B-L1", posting_status=status)],
    )
    assert ev.outcome == O.SHORTFALL
    assert ev.received_qty == expected_received
    assert ev.excluded[0].reason_code == f"POSTING_STATUS_{status}"


@pytest.mark.parametrize(
    "status, code", [("PENDING", "RECEIPT_POSTING_NOT_FINAL"), ("ON_HOLD", "UNRECOGNIZED_POSTING_STATUS")]
)
def test_receipts_not_finally_posted_block_evaluation(status, code):
    ev = evaluate(
        f.po_line("ERP-PO501-L1"),
        [f.receipt("WMS-GRN501-A-L1"), f.receipt("WMS-GRN501-B-L1", posting_status=status)],
    )
    assert ev.outcome == O.NOT_EVALUABLE
    assert codes(ev) == [code]


# ---------------------------------------------------------------- identity


def test_po_line_with_unmapped_product_is_a_data_quality_case():
    ev = evaluate(f.po_line("ERP-PO501-L1", item_ref="ERP-SKU-999"), f.receipts_for("PO-501"))
    assert ev.outcome == O.NOT_EVALUABLE
    assert codes(ev) == ["PRODUCT_UNMAPPED"]


def test_po_line_with_unmapped_location_is_a_data_quality_case():
    ev = evaluate(f.po_line("ERP-PO501-L1", location_ref="MUM-099"), f.receipts_for("PO-501"))
    assert codes(ev) == ["LOCATION_UNMAPPED"]


def test_receipt_for_a_different_product_is_not_counted_silently():
    wrong = f.receipt("WMS-GRN501-B-L1", product_ref="WMS-BRC-003")
    ev = evaluate(f.po_line("ERP-PO501-L1"), [f.receipt("WMS-GRN501-A-L1"), wrong])
    assert ev.outcome == O.NOT_EVALUABLE
    assert codes(ev) == ["RECEIPT_PRODUCT_MISMATCH"]


def test_receipt_at_a_different_location_is_not_counted_silently():
    wrong = f.receipt("WMS-GRN501-B-L1", location_ref="WH-MH-015")
    ev = evaluate(f.po_line("ERP-PO501-L1"), [f.receipt("WMS-GRN501-A-L1"), wrong])
    assert codes(ev) == ["RECEIPT_LOCATION_MISMATCH"]


def test_unmapped_receipt_blocks_evaluation():
    unmapped = f.receipt("WMS-GRN501-B-L1", product_ref="WMS-NEW-777")
    ev = evaluate(f.po_line("ERP-PO501-L1"), [f.receipt("WMS-GRN501-A-L1"), unmapped])
    assert codes(ev) == ["PRODUCT_UNMAPPED"]
    assert [r.ref.source_record_key for r in ev.blocked] == ["WMS-GRN501-B-L1"]


def test_records_in_conflict_are_not_trusted():
    po_conflict = f.po_line("ERP-PO501-L1", review_status="CONFLICT")
    assert codes(evaluate(po_conflict, f.receipts_for("PO-501"))) == ["SOURCE_RECORD_IN_CONFLICT"]
    receipt_conflict = f.receipt("WMS-GRN501-B-L1", review_status="CONFLICT")
    ev = evaluate(f.po_line("ERP-PO501-L1"), [f.receipt("WMS-GRN501-A-L1"), receipt_conflict])
    assert codes(ev) == ["SOURCE_RECORD_IN_CONFLICT"]


# ---------------------------------------------------------------- evidence completeness


def test_no_receipt_feed_means_absent_receipts_cannot_be_judged():
    ev = evaluate(f.po_line("ERP-PO501-L1"), [], feed=ReceiptFeed(None))
    assert codes(ev) == ["RECEIPT_FEED_MISSING"]


def test_receipt_feed_extracted_before_deadline_is_stale():
    po = f.po_line("ERP-PO501-L1")
    ev = evaluate(po, f.receipts_for("PO-501"), feed=ReceiptFeed(po.deadline - timedelta(hours=1)))
    assert codes(ev) == ["RECEIPT_FEED_STALE"]


def test_rejected_receipt_row_naming_the_line_blocks_evaluation():
    ev = evaluate(
        f.po_line("ERP-PO501-L1"),
        f.receipts_for("PO-501"),
        rejected=[RejectedReceiptRow("import-2", 5, "WMS-GRN501-C-L1")],
    )
    assert codes(ev) == ["REJECTED_RECEIPT_ROW_FOR_LINE"]


# ---------------------------------------------------------------- tolerance and over-receipt


def test_tolerance_is_configurable_and_zero_by_default():
    po, receipts = f.po_line("ERP-PO501-L1"), f.receipts_for("PO-501")
    assert evaluate(po, receipts).outcome == O.SHORTFALL
    within = evaluate(po, receipts, params=RuleParams(Decimal(8)))
    assert (within.outcome, codes(within)) == (O.NO_SHORTFALL, ["WITHIN_TOLERANCE"])
    assert evaluate(po, receipts, params=RuleParams(Decimal(7))).outcome == O.SHORTFALL


def test_over_receipt_is_not_a_shortfall():
    ev = evaluate(f.po_line("ERP-PO502-L1"), [f.receipt("WMS-GRN502-A-L1", accepted_qty=Decimal(45))])
    assert (ev.outcome, codes(ev), ev.variance_qty) == (O.NO_SHORTFALL, ["OVER_RECEIPT"], -5)


def test_naive_evaluation_time_is_refused():
    with pytest.raises(ValueError):
        evaluate(f.po_line("ERP-PO501-L1"), [], as_of=AS_OF.replace(tzinfo=None))


# ---------------------------------------------------------------- explanation


def test_explanation_states_the_shortfall_and_does_not_assert_a_cause():
    ev = evaluate(f.po_line("ERP-PO505-L1"), f.receipts_for("PO-505"))
    explanation = explain_shortfall(ev, "Asia/Kolkata")
    assert explanation["summary"].startswith("Receipt shortfall of 24 EA on PO PO-505 line 1 at STORE-015")
    known = " ".join(explanation["known"]).lower()
    assert "10 box x 12 ea/box" in known
    for claim in ("fault", "short-shipped", "lost", "stole", "supplier failed", "carrier lost"):
        assert claim not in known
    assert "does not establish supplier fault" in explanation["not_established"]
    assert any("ASN" in u for u in explanation["unknown"])
    assert any("ASN" in e for e in explanation["evidence_needed"])


def test_explanation_mentions_identical_repeats_counted_once():
    ev = evaluate(f.po_line("ERP-PO506-L1"), f.receipts_for("PO-506"))
    explanation = explain_shortfall(ev, "Asia/Kolkata", {"WMS-GRN506-A-L1": 2})
    assert any("appeared 2 times" in line and "counted once" in line for line in explanation["known"])
