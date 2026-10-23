"""Neutral, deterministic explanation of a shortfall finding.

The data supports one claim: fewer units were received and posted against the
PO line than it ordered, after the receiving window closed. It does not say
why. There is no supplier dispatch / ASN data, so the text never attributes
the gap to the supplier, a carrier, the store or a loss.
"""

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from app.detection.rules import Evaluation
from app.hashing import normalize_decimal

NOT_ESTABLISHED = (
    "This finding is a receipt shortfall against the purchase order: fewer units were received and posted "
    "than ordered once the expected-receipt window had closed. It does not establish supplier fault, carrier "
    "loss, store error or a financial loss."
)

UNKNOWN = (
    "Whether the supplier dispatched the full ordered quantity (no supplier dispatch / ASN data was supplied).",
    "Whether units were lost, damaged or mis-delivered in transit, and where any discrepancy occurred.",
    "Whether units arrived but are not yet receipted or posted, or were receipted against a different PO line.",
)

EVIDENCE_NEEDED = (
    "Supplier ASN or dispatch note for this PO line, with dispatched quantity.",
    "Carrier proof of delivery with the delivered quantity and any exceptions noted on delivery.",
    "Store receiving paperwork or a physical count for the delivery.",
    "WMS check for unposted, reversed or mis-linked receipts of this product at this location.",
)


def _local(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).isoformat(timespec="minutes")


def explain_shortfall(
    evaluation: Evaluation, business_tz: str, record_occurrences: Mapping[str, int] | None = None
) -> dict:
    tz = ZoneInfo(business_tz)
    po = evaluation.po_line
    assert po.quantity is not None and po.location is not None
    assert evaluation.expected_qty is not None and evaluation.received_qty is not None
    assert evaluation.variance_qty is not None
    unit = evaluation.unit
    expected = normalize_decimal(evaluation.expected_qty)
    received = normalize_decimal(evaluation.received_qty)
    variance = normalize_decimal(evaluation.variance_qty)
    occurrences = record_occurrences or {}

    conversion = (
        f" ({normalize_decimal(po.ordered_qty)} {po.ordered_uom} x {normalize_decimal(po.quantity.factor)} "
        f"{unit}/{po.ordered_uom})"
        if po.ordered_uom != unit
        else ""
    )
    known = [
        f"ERP PO {po.po_number} line {po.line_number} (record {po.ref.source_record_key}, supplier "
        f"{po.supplier_code}) ordered {expected} {unit} of {po.quantity.canonical_product_id}{conversion} for "
        f"{po.location.canonical_location_id}.",
        f"The expected-receipt deadline {_local(po.deadline, tz)} had passed at the evaluation time "
        f"{_local(evaluation.as_of, tz)}.",
    ]
    if evaluation.counted:
        parts = ", ".join(
            f"{c.receipt.receipt_number} line {c.receipt.receipt_line}: {normalize_decimal(c.base_qty)} {unit}"
            + ("" if c.on_time else " (received after the deadline)")
            for c in evaluation.counted
        )
        known.append(
            f"{len(evaluation.counted)} posted WMS receipt line(s) linked to this PO line total {received} {unit} "
            f"({parts})."
        )
    else:
        known.append("No posted WMS receipt line is linked to this PO line.")
    for c in evaluation.counted:
        seen = occurrences.get(c.receipt.ref.source_record_key, 1)
        if seen > 1:
            known.append(
                f"Receipt record {c.receipt.ref.source_record_key} appeared {seen} times in the imported files; "
                "it is one receipt and is counted once."
            )
    for x in evaluation.excluded:
        known.append(f"Receipt record {x.receipt.ref.source_record_key} is not counted ({x.reason_code}).")
    known.append(f"{variance} {unit} of the ordered quantity has not been received and posted.")

    return {
        "summary": (
            f"Receipt shortfall of {variance} {unit} on PO {po.po_number} line {po.line_number} at "
            f"{po.location.canonical_location_id}: {received} of {expected} {unit} received and posted."
        ),
        "known": known,
        "unknown": list(UNKNOWN),
        "evidence_needed": list(EVIDENCE_NEEDED),
        "not_established": NOT_ESTABLISHED,
    }
