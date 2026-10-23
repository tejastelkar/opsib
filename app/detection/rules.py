"""Receipt-shortfall rule. Pure and deterministic: canonical records in, an
Evaluation out. No database, no clock, no I/O.

Grain
* PO line: one per (ERP system, po_number, line_number). If more than one
  source record claims the same business key, the line cannot be evaluated.
* Receipt line: one per WMS source record. Identical re-sends were already
  collapsed to one fact at ingestion, so summing distinct facts cannot
  double-count. Two different source records claiming the same
  (receipt_number, receipt_line) anywhere in the tenant block every PO line
  either of them links to.
* Receipts link to a PO line by the PO number and line number they carry, and
  must agree with the PO line on canonical product and canonical location.

Order of checks (the first that applies decides the outcome)
  1. key collision / record in conflict / unknown status   -> NOT_EVALUABLE
  2. PO line cancelled                                     -> NOT_ELIGIBLE
  3. PO product, unit or location cannot be mapped         -> NOT_EVALUABLE
  4. deadline not passed (as_of <= deadline)               -> NOT_DUE
  5. no receipt feed, or feed extracted before deadline,
     or an outstanding rejected receipt row names this line
     or names no readable line at all                      -> NOT_EVALUABLE
  6. a linked receipt is unmapped, mismatched, in conflict,
     or not finally posted                                 -> NOT_EVALUABLE
  7. expected - received > tolerance                       -> SHORTFALL
     otherwise                                             -> NO_SHORTFALL
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.enums import EvaluationOutcome
from app.hashing import normalize_decimal
from app.mapping.model import CanonicalPoLine, CanonicalReceiptLine, SourceRef
from app.mapping.vocabularies import PoLineStatus, PostingClass

RULE_ID = "RECEIPT_SHORTFALL"
RULE_VERSION = "receipt_shortfall/1.0.0"
EXCEPTION_TYPE = "RECEIPT_SHORTFALL"


def case_key_for(source_system: str, po_number: str, line_number: int) -> str:
    return f"{RULE_ID}|{source_system}|{po_number}|{line_number}"


@dataclass(frozen=True)
class RuleParams:
    tolerance_base_qty: Decimal = Decimal(0)

    def as_dict(self) -> dict[str, str]:
        return {"tolerance_base_qty": normalize_decimal(self.tolerance_base_qty)}


@dataclass(frozen=True)
class ReceiptFeed:
    """Coverage of the WMS receipt feed: the latest extract time among completed
    goods-receipt imports. Receipts posted after that are unknown to us."""

    extracted_through: datetime | None
    import_ids: tuple[str, ...] = ()
    # Outstanding rejected receipt rows that name no readable PO line: any line
    # could be affected, so no line can be judged until they are dealt with.
    unreadable_rows: tuple["RejectedReceiptRow", ...] = ()


@dataclass(frozen=True)
class RejectedReceiptRow:
    """A goods-receipt row that failed validation, was never accepted later, and
    names this PO line. Its quantity is unknown, so the line cannot be judged."""

    import_id: str
    row_number: int
    source_record_id: str | None


@dataclass(frozen=True)
class Subject:
    dataset: str
    record_key: str

    @classmethod
    def of(cls, ref: SourceRef) -> "Subject":
        return cls(ref.dataset, ref.source_record_key)


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    subject: Subject | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CountedReceipt:
    receipt: CanonicalReceiptLine
    base_qty: Decimal
    on_time: bool  # received at or before the expected-receipt deadline


@dataclass(frozen=True)
class ExcludedReceipt:
    receipt: CanonicalReceiptLine
    reason_code: str


@dataclass(frozen=True)
class Evaluation:
    case_key: str
    outcome: EvaluationOutcome
    reasons: tuple[Reason, ...]
    as_of: datetime
    params: RuleParams
    po_lines: tuple[CanonicalPoLine, ...]
    counted: tuple[CountedReceipt, ...] = ()
    excluded: tuple[ExcludedReceipt, ...] = ()
    blocked: tuple[CanonicalReceiptLine, ...] = ()
    expected_qty: Decimal | None = None
    received_qty: Decimal | None = None
    variance_qty: Decimal | None = None
    unit: str | None = None

    @property
    def po_line(self) -> CanonicalPoLine:
        return self.po_lines[0]

    @property
    def is_shortfall(self) -> bool:
        return self.outcome == EvaluationOutcome.SHORTFALL


def evaluate_po_line(
    po_lines: Sequence[CanonicalPoLine],
    receipts: Sequence[CanonicalReceiptLine],
    feed: ReceiptFeed,
    as_of: datetime,
    params: RuleParams = RuleParams(),
    rejected_receipts: Sequence[RejectedReceiptRow] = (),
    receipt_key_collisions: Mapping[tuple[str, str, int], Sequence[str]] | None = None,
) -> Evaluation:
    """`receipt_key_collisions` maps a receipt business key to the source records
    claiming it across the whole tenant (the caller sees all receipts; this
    function only sees the ones linked to one PO line)."""
    if not po_lines:
        raise ValueError("evaluate_po_line needs at least one PO line")
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    keys = {line.business_key for line in po_lines}
    if len(keys) != 1:
        raise ValueError(f"PO lines span several business keys: {sorted(keys)}")

    ordered = tuple(sorted(po_lines, key=lambda line: line.ref.source_record_key))
    po = ordered[0]
    case_key = case_key_for(*po.business_key)

    def result(outcome: EvaluationOutcome, *reasons: Reason, **extra: Any) -> Evaluation:
        return Evaluation(case_key, outcome, tuple(reasons), as_of, params, ordered, **extra)

    # 1. identity and interpretability of the PO line itself
    if len(ordered) > 1:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            Reason(
                "PO_LINE_KEY_COLLISION",
                f"{len(ordered)} source records claim PO {po.po_number} line {po.line_number}",
                Subject("purchase_order_lines", f"{po.po_number}#{po.line_number}"),
                {"source_record_keys": [line.ref.source_record_key for line in ordered]},
            ),
        )
    if po.in_conflict:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            Reason(
                "SOURCE_RECORD_IN_CONFLICT",
                f"PO line record {po.ref.source_record_key} has an unresolved conflicting re-send",
                Subject.of(po.ref),
            ),
        )
    if po.status is None:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            *[_reason_from_problem(p, po.ref) for p in po.problems if p.code == "UNRECOGNIZED_PO_STATUS"],
        )

    # 2. eligibility
    if po.status == PoLineStatus.CANCELLED:
        return result(
            EvaluationOutcome.NOT_ELIGIBLE,
            Reason("PO_LINE_CANCELLED", f"PO {po.po_number} line {po.line_number} is CANCELLED in the ERP"),
        )

    # 3. mapping of the PO line
    if po.problems or po.quantity is None or po.location is None:
        return result(EvaluationOutcome.NOT_EVALUABLE, *[_reason_from_problem(p, po.ref) for p in po.problems])

    expected = po.quantity.base_qty
    unit = po.quantity.base_uom

    # Receipts are classified before the deadline check so that a NOT_DUE
    # evaluation can still record the position so far.
    counted, excluded, blocked, receipt_reasons = _classify_receipts(po, receipts, as_of, receipt_key_collisions or {})
    received = sum((c.base_qty for c in counted), Decimal(0))
    quantities: dict[str, Any] = {"expected_qty": expected, "received_qty": received, "unit": unit}

    # 4. timing
    if as_of <= po.deadline:
        return result(
            EvaluationOutcome.NOT_DUE,
            Reason(
                "DEADLINE_NOT_PASSED",
                "the expected-receipt window has not closed at the evaluation time",
                details={"deadline": po.deadline, "evaluated_as_of": as_of},
            ),
            counted=counted,
            excluded=excluded,
            blocked=blocked,
            variance_qty=expected - received,
            **quantities,
        )

    # 5. is the receipt evidence complete enough to judge?
    if feed.extracted_through is None:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            Reason(
                "RECEIPT_FEED_MISSING",
                "no completed goods-receipt import exists, so absent receipts cannot be told from missing data",
                Subject("goods_receipt_lines", "(feed)"),
            ),
        )
    if feed.extracted_through < po.deadline:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            Reason(
                "RECEIPT_FEED_STALE",
                "the latest goods-receipt extract predates the expected-receipt deadline",
                Subject("goods_receipt_lines", "(feed)"),
                {"extracted_through": feed.extracted_through, "deadline": po.deadline},
            ),
        )

    if feed.unreadable_rows:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            Reason(
                "UNREADABLE_RECEIPT_ROW_IN_FEED",
                f"{len(feed.unreadable_rows)} rejected goods-receipt row(s) name no readable PO line; "
                "any line could be affected",
                Subject("goods_receipt_lines", "(feed)"),
                {"rows": [{"import_id": r.import_id, "row_number": r.row_number} for r in feed.unreadable_rows]},
            ),
        )

    if rejected_receipts:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            *[
                Reason(
                    "REJECTED_RECEIPT_ROW_FOR_LINE",
                    f"a goods-receipt row naming this PO line failed validation (import {r.import_id}, "
                    f"row {r.row_number}) and has not been accepted since",
                    Subject("goods_receipt_lines", r.source_record_id or f"import:{r.import_id}:row:{r.row_number}"),
                    {"import_id": r.import_id, "row_number": r.row_number},
                )
                for r in rejected_receipts
            ],
        )

    # 6. every linked receipt must be interpretable
    if receipt_reasons:
        return result(
            EvaluationOutcome.NOT_EVALUABLE,
            *receipt_reasons,
            counted=counted,
            excluded=excluded,
            blocked=blocked,
        )

    # 7. compare in base units
    variance = expected - received
    reasons: list[Reason] = []
    if variance > params.tolerance_base_qty:
        outcome = EvaluationOutcome.SHORTFALL
        reasons.append(
            Reason(
                "RECEIVED_LESS_THAN_ORDERED",
                f"{normalize_decimal(received)} of {normalize_decimal(expected)} {unit} received and posted",
            )
        )
    else:
        outcome = EvaluationOutcome.NO_SHORTFALL
        if variance < 0:
            reasons.append(Reason("OVER_RECEIPT", "more was received than ordered", details={"over_qty": -variance}))
        elif variance > 0:
            reasons.append(
                Reason(
                    "WITHIN_TOLERANCE", "variance is within the configured tolerance", details={"variance": variance}
                )
            )
        else:
            reasons.append(Reason("RECEIVED_IN_FULL", "received quantity equals the ordered quantity"))
    return result(
        outcome,
        *reasons,
        counted=counted,
        excluded=excluded,
        blocked=blocked,
        variance_qty=variance,
        **quantities,
    )


def _classify_receipts(
    po: CanonicalPoLine,
    receipts: Sequence[CanonicalReceiptLine],
    as_of: datetime,
    global_collisions: Mapping[tuple[str, str, int], Sequence[str]],
) -> tuple[tuple[CountedReceipt, ...], tuple[ExcludedReceipt, ...], tuple[CanonicalReceiptLine, ...], list[Reason]]:
    assert po.quantity is not None and po.location is not None
    counted: list[CountedReceipt] = []
    excluded: list[ExcludedReceipt] = []
    blocked: list[CanonicalReceiptLine] = []
    reasons: list[Reason] = []

    in_scope = []
    for receipt in sorted(receipts, key=lambda r: r.ref.source_record_key):
        if receipt.received_at > as_of:
            # Not knowable at the evaluation time; it will count on a later run.
            excluded.append(ExcludedReceipt(receipt, "RECEIVED_AFTER_EVALUATION_TIME"))
            continue
        in_scope.append(receipt)

    claimants: dict[tuple, set[str]] = defaultdict(set)
    for receipt in in_scope:
        claimants[receipt.business_key].add(receipt.ref.source_record_key)
    for key, records in global_collisions.items():
        claimants[key].update(records)
    colliding = {k for k, records in claimants.items() if len(records) > 1}

    for receipt in in_scope:
        subject = Subject.of(receipt.ref)
        problem: Reason | None = None
        if receipt.business_key in colliding:
            problem = Reason(
                "RECEIPT_LINE_KEY_COLLISION",
                f"several source records claim receipt {receipt.receipt_number} line {receipt.receipt_line}",
                subject,
                {"source_record_keys": sorted(claimants[receipt.business_key])},
            )
        elif receipt.in_conflict:
            problem = Reason(
                "SOURCE_RECORD_IN_CONFLICT",
                f"receipt record {receipt.ref.source_record_key} has an unresolved conflicting re-send",
                subject,
            )
        elif receipt.posting is None:
            problem = Reason(
                "UNRECOGNIZED_POSTING_STATUS",
                f"posting status '{receipt.posting_status_code}' is not in the declared vocabulary",
                subject,
            )
        elif receipt.posting == PostingClass.NOT_FINAL:
            problem = Reason(
                "RECEIPT_POSTING_NOT_FINAL",
                f"receipt {receipt.receipt_number} is still {receipt.posting_status_code}",
                subject,
            )
        elif receipt.posting == PostingClass.EXCLUDED:
            excluded.append(ExcludedReceipt(receipt, f"POSTING_STATUS_{receipt.posting_status_code}"))
            continue
        elif receipt.problems or receipt.quantity is None or receipt.location is None:
            reasons.extend(_reason_from_problem(p, receipt.ref) for p in receipt.problems)
            blocked.append(receipt)
            continue
        elif receipt.quantity.canonical_product_id != po.quantity.canonical_product_id:
            problem = Reason(
                "RECEIPT_PRODUCT_MISMATCH",
                f"receipt maps to {receipt.quantity.canonical_product_id}, the PO line to "
                f"{po.quantity.canonical_product_id}",
                subject,
            )
        elif receipt.location.canonical_location_id != po.location.canonical_location_id:
            problem = Reason(
                "RECEIPT_LOCATION_MISMATCH",
                f"receipt maps to {receipt.location.canonical_location_id}, the PO line to "
                f"{po.location.canonical_location_id}",
                subject,
            )
        elif receipt.quantity.base_uom != po.quantity.base_uom:
            problem = Reason(
                "BASE_UOM_MISMATCH",
                f"receipt base unit {receipt.quantity.base_uom} differs from {po.quantity.base_uom}",
                subject,
            )

        if problem is not None:
            reasons.append(problem)
            blocked.append(receipt)
        else:
            counted.append(CountedReceipt(receipt, receipt.quantity.base_qty, receipt.received_at <= po.deadline))
    return tuple(counted), tuple(excluded), tuple(blocked), reasons


def _reason_from_problem(problem: Any, ref: SourceRef) -> Reason:
    return Reason(problem.code, problem.message, Subject.of(ref), dict(problem.details))
