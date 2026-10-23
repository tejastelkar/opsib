"""Run the shortfall rule over the canonical model and persist what it found.

Everything here runs inside the caller's transaction, which holds the
per-tenant reconciliation lock. Exceptions are keyed by UNIQUE(tenant_id,
case_key) and inserted with ON CONFLICT DO NOTHING, so even without the lock a
second worker could not create a second exception for the same PO line.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataquality import IssueDraft, upsert_issues
from app.db.models import (
    ExceptionCase,
    ImportBatch,
    ImportRow,
    PoLineFact,
    ReceiptLineFact,
    ReconciliationRun,
    RuleEvaluation,
)
from app.detection.explanations import explain_shortfall
from app.detection.rules import (
    EXCEPTION_TYPE,
    RULE_VERSION,
    Evaluation,
    ReceiptFeed,
    RejectedReceiptRow,
    RuleParams,
    evaluate_po_line,
)
from app.enums import EvaluationOutcome, ExceptionStatus, ImportStatus, RowOutcome
from app.hashing import content_hash, normalize_decimal, to_jsonable
from app.ingestion.datasets import DatasetRegistry
from app.ingestion.rejections import outstanding_rejections
from app.mapping.canonicalize import canonical_po_line, canonical_receipt_line
from app.mapping.mapping_set import MappingSet
from app.mapping.model import CanonicalReceiptLine
from app.mapping.repository import load_mapping_set
from app.workflows.records import SYSTEM_DETECTOR, add_exception_event, cancel_active_tasks

EXCEPTION_ID_NAMESPACE = uuid.UUID("6f1c3c2e-4f7a-5b8e-9d21-0a7e5c1b9f30")
LINEAGE_DATASETS = ("purchase_order_lines", "goods_receipt_lines")


def exception_id_for(tenant_id: str, case_key: str) -> uuid.UUID:
    """Deterministic: the same business case gets the same ID in every environment."""
    return uuid.uuid5(EXCEPTION_ID_NAMESPACE, f"{tenant_id}|{case_key}")


@dataclass(frozen=True)
class RowRef:
    import_id: str
    row_number: int
    outcome: str
    content_hash: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"import_id": self.import_id, "row_number": self.row_number, "outcome": self.outcome}


class ShortfallDetector:
    def __init__(self, tenant_id: str, settings: Settings, registry: DatasetRegistry):
        self._tenant = tenant_id
        self._settings = settings
        self._registry = registry
        self._params = RuleParams(settings.shortfall_tolerance_base_qty)

    @property
    def params(self) -> RuleParams:
        return self._params

    def detect(self, session: Session, run: ReconciliationRun) -> dict[str, Any]:
        as_of = run.evaluated_as_of
        mapping = load_mapping_set(session, self._tenant)
        run.mapping_version = mapping.version

        po_lines = [
            canonical_po_line(f, mapping)
            for f in session.scalars(select(PoLineFact).where(PoLineFact.tenant_id == self._tenant))
        ]
        receipts = [
            canonical_receipt_line(f, mapping)
            for f in session.scalars(select(ReceiptLineFact).where(ReceiptLineFact.tenant_id == self._tenant))
        ]
        rejected, unreadable = self._receipt_rejections(session)
        feed = replace(self._receipt_feed(session), unreadable_rows=unreadable)
        lineage = self._lineage(session)

        groups: dict[tuple[str, str, int], list] = defaultdict(list)
        for line in po_lines:
            groups[line.business_key].append(line)
        linked: dict[tuple[str, int], list[CanonicalReceiptLine]] = defaultdict(list)
        claimants: dict[tuple[str, str, int], list[str]] = defaultdict(list)
        for receipt in receipts:
            linked[(receipt.po_number, receipt.po_line_number)].append(receipt)
            claimants[receipt.business_key].append(receipt.ref.source_record_key)
        # A GRN line claimed by two source records is ambiguous wherever either points.
        collisions = {k: v for k, v in claimants.items() if len(v) > 1}

        evaluations: list[Evaluation] = []
        for business_key, lines in sorted(groups.items()):
            link = (business_key[1], business_key[2])
            evaluations.append(
                evaluate_po_line(
                    lines, linked.get(link, []), feed, as_of, self._params, rejected.get(link, ()), collisions
                )
            )

        for evaluation in evaluations:
            session.add(
                RuleEvaluation(
                    run_id=run.id,
                    case_key=evaluation.case_key,
                    outcome=evaluation.outcome,
                    reason_codes=[r.code for r in evaluation.reasons],
                    details=_evaluation_details(evaluation),
                )
            )

        known_po_lines = {(k[1], k[2]) for k in groups}
        issues = upsert_issues(
            session,
            self._tenant,
            "detection",
            self._issue_drafts(evaluations, receipts, known_po_lines, mapping),
            run,
        )
        exceptions = self._apply_exceptions(session, run, mapping, evaluations, lineage, feed)
        outcomes = defaultdict(int)
        for evaluation in evaluations:
            outcomes[evaluation.outcome.value] += 1
        return {
            "mapping_version": mapping.version,
            "po_lines_evaluated": len(evaluations),
            "outcomes": dict(sorted(outcomes.items())),
            "exceptions": exceptions,
            "data_quality_issues": issues,
            "receipt_feed_extracted_through": to_jsonable(feed.extracted_through),
        }

    # ------------------------------------------------------------------ inputs

    def _receipt_feed(self, session: Session) -> ReceiptFeed:
        rows = session.execute(
            select(ImportBatch.id, ImportBatch.source_extracted_at).where(
                ImportBatch.tenant_id == self._tenant,
                ImportBatch.dataset == "goods_receipt_lines",
                ImportBatch.status.in_([ImportStatus.COMPLETED, ImportStatus.COMPLETED_WITH_ERRORS]),
            )
        ).all()
        if not rows:
            return ReceiptFeed(None)
        return ReceiptFeed(max(r.source_extracted_at for r in rows), tuple(sorted(str(r.id) for r in rows)))

    def _lineage(self, session: Session) -> dict[tuple[str, str], list[RowRef]]:
        """Every import row that carried each PO / receipt source record."""
        rows = session.execute(
            select(
                ImportBatch.dataset,
                ImportRow.source_record_key,
                ImportRow.import_id,
                ImportRow.row_number,
                ImportRow.outcome,
                ImportRow.content_hash,
            )
            .join(ImportBatch, ImportBatch.id == ImportRow.import_id)
            .where(
                ImportBatch.tenant_id == self._tenant,
                ImportBatch.dataset.in_(LINEAGE_DATASETS),
                ImportRow.source_record_key.is_not(None),
                ImportRow.outcome != RowOutcome.REJECTED,
            )
            .order_by(ImportBatch.created_at, ImportRow.row_number)
        ).all()
        out: dict[tuple[str, str], list[RowRef]] = defaultdict(list)
        for r in rows:
            out[(r.dataset, r.source_record_key)].append(
                RowRef(str(r.import_id), r.row_number, r.outcome, r.content_hash)
            )
        return out

    def _receipt_rejections(
        self, session: Session
    ) -> tuple[dict[tuple[str, int], tuple[RejectedReceiptRow, ...]], tuple[RejectedReceiptRow, ...]]:
        """Outstanding rejected goods-receipt rows: those that name a PO line (read
        through the rejecting import's profile) block that line; those that name no
        readable line block every line."""
        by_line: dict[tuple[str, int], list[RejectedReceiptRow]] = defaultdict(list)
        unreadable: list[RejectedReceiptRow] = []
        for r in outstanding_rejections(session, self._tenant, self._registry, "goods_receipt_lines"):
            row = RejectedReceiptRow(r.import_id, r.row_number, r.source_record_key)
            po_number = r.values.get("po_number")
            try:
                line = int(r.values.get("po_line_number", ""))
            except ValueError:
                line = None
            if po_number and line is not None:
                by_line[(po_number, line)].append(row)
            else:
                unreadable.append(row)
        return {k: tuple(v) for k, v in by_line.items()}, tuple(unreadable)

    # ------------------------------------------------------------------ outputs

    def _issue_drafts(
        self,
        evaluations: list[Evaluation],
        receipts: list[CanonicalReceiptLine],
        known_po_lines: set[tuple[str, int]],
        mapping: MappingSet,
    ) -> list[IssueDraft]:
        drafts: list[IssueDraft] = []
        for evaluation in evaluations:
            if evaluation.outcome != EvaluationOutcome.NOT_EVALUABLE:
                continue
            for reason in evaluation.reasons:
                subject = reason.subject
                dataset = subject.dataset if subject else "purchase_order_lines"
                key = subject.record_key if subject else evaluation.po_line.ref.source_record_key
                drafts.append(
                    IssueDraft(
                        reason.code,
                        dataset,
                        key,
                        reason.message,
                        to_jsonable(dict(reason.details)),
                        {evaluation.case_key},
                    )
                )
        for receipt in receipts:
            if (receipt.po_number, receipt.po_line_number) not in known_po_lines:
                drafts.append(
                    IssueDraft(
                        "ORPHAN_RECEIPT",
                        "goods_receipt_lines",
                        receipt.ref.source_record_key,
                        f"receipt references PO {receipt.po_number} line {receipt.po_line_number}, "
                        "which is not in the ERP data",
                        {"problems": [p.as_dict() for p in receipt.problems]},
                    )
                )
        for canonical_id, problem in mapping.crosswalk_problems():
            dataset = "product_aliases" if problem.code.startswith("PRODUCT") else "location_aliases"
            drafts.append(
                IssueDraft(problem.code, dataset, canonical_id, problem.message, to_jsonable(dict(problem.details)))
            )
        return drafts

    def _apply_exceptions(
        self,
        session: Session,
        run: ReconciliationRun,
        mapping: MappingSet,
        evaluations: list[Evaluation],
        lineage: dict[tuple[str, str], list[RowRef]],
        feed: ReceiptFeed,
    ) -> dict[str, int]:
        as_of = run.evaluated_as_of
        existing = {
            e.case_key: e
            for e in session.scalars(
                select(ExceptionCase).where(ExceptionCase.tenant_id == self._tenant).with_for_update()
            )
        }
        counts = defaultdict(int)
        for evaluation in evaluations:
            exception = existing.get(evaluation.case_key)
            if evaluation.is_shortfall:
                fields = self._exception_fields(evaluation, run, mapping, lineage, feed)
                if exception is None:
                    exception = self._insert_exception(session, evaluation, fields, as_of)
                    if exception is not None:
                        add_exception_event(
                            session,
                            exception,
                            "DETECTED",
                            actor_id=SYSTEM_DETECTOR,
                            occurred_at=as_of,
                            to_status=ExceptionStatus.OPEN,
                            run_id=run.id,
                            details={"variance_qty": fields["variance_qty"], "unit": fields["unit"]},
                        )
                        counts["created"] += 1
                        continue
                    exception = session.scalar(
                        select(ExceptionCase)
                        .where(ExceptionCase.tenant_id == self._tenant, ExceptionCase.case_key == evaluation.case_key)
                        .with_for_update()
                    )
                    assert exception is not None
                counts[self._update_for_shortfall(session, exception, fields, run)] += 1
            elif exception is not None:
                counts[self._update_for_non_shortfall(session, exception, evaluation, run, lineage, feed)] += 1
        return {k: counts[k] for k in ("created", "updated", "reopened", "cleared", "blocked", "unchanged")}

    def _insert_exception(
        self, session: Session, evaluation: Evaluation, fields: dict[str, Any], as_of: Any
    ) -> ExceptionCase | None:
        exception_id = exception_id_for(self._tenant, evaluation.case_key)
        stmt = (
            pg_insert(ExceptionCase)
            .values(
                id=exception_id,
                tenant_id=self._tenant,
                case_key=evaluation.case_key,
                exception_type=EXCEPTION_TYPE,
                status=ExceptionStatus.OPEN,
                detected_at=as_of,
                first_run_id=fields["last_run_id"],
                version=1,
                **fields,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "case_key"])
            .returning(ExceptionCase.id)
        )
        inserted = session.scalar(stmt)
        if inserted is None:
            return None
        return session.get_one(ExceptionCase, inserted)

    def _update_for_shortfall(
        self, session: Session, exception: ExceptionCase, fields: dict[str, Any], run: ReconciliationRun
    ) -> str:
        as_of = run.evaluated_as_of
        previous_outcome = exception.last_outcome
        exception.last_run_id = run.id
        exception.last_outcome = EvaluationOutcome.SHORTFALL
        changed = exception.evidence_hash != fields["evidence_hash"]
        before = _quantities(exception)

        if exception.status == ExceptionStatus.OPEN:
            if previous_outcome != EvaluationOutcome.SHORTFALL:
                add_exception_event(
                    session,
                    exception,
                    "REEVALUATION_UNBLOCKED",
                    actor_id=SYSTEM_DETECTOR,
                    occurred_at=as_of,
                    run_id=run.id,
                    details={"previous_outcome": previous_outcome},
                )
            if not changed:
                return "unchanged"
            _apply(exception, fields)
            add_exception_event(
                session,
                exception,
                "EVIDENCE_UPDATED",
                actor_id=SYSTEM_DETECTOR,
                occurred_at=as_of,
                run_id=run.id,
                details={"before": before, "after": _quantities(exception)},
            )
            return "updated"

        if exception.status == ExceptionStatus.RESOLVED and not changed:
            return "unchanged"  # the verified resolution still stands for the same evidence

        previous_status = exception.status
        _apply(exception, fields)
        exception.status = ExceptionStatus.OPEN
        exception.resolution_code = None
        exception.resolution_evidence_ref = None
        exception.resolved_by = None
        exception.resolved_at = None
        add_exception_event(
            session,
            exception,
            "REOPENED",
            actor_id=SYSTEM_DETECTOR,
            occurred_at=as_of,
            run_id=run.id,
            from_status=previous_status,
            to_status=ExceptionStatus.OPEN,
            details={"before": before, "after": _quantities(exception), "evidence_changed": changed},
        )
        return "reopened"

    def _update_for_non_shortfall(
        self,
        session: Session,
        exception: ExceptionCase,
        evaluation: Evaluation,
        run: ReconciliationRun,
        lineage: dict[tuple[str, str], list[RowRef]],
        feed: ReceiptFeed,
    ) -> str:
        as_of = run.evaluated_as_of
        previous_outcome = exception.last_outcome
        exception.last_run_id = run.id
        exception.last_outcome = evaluation.outcome
        reasons = [{"code": r.code, "message": r.message} for r in evaluation.reasons]

        if evaluation.outcome == EvaluationOutcome.NOT_EVALUABLE:
            # Keep the case open and visible; we cannot confirm or clear it.
            if exception.status == ExceptionStatus.OPEN and previous_outcome != EvaluationOutcome.NOT_EVALUABLE:
                add_exception_event(
                    session,
                    exception,
                    "REEVALUATION_BLOCKED",
                    actor_id=SYSTEM_DETECTOR,
                    occurred_at=as_of,
                    run_id=run.id,
                    details={"reasons": reasons},
                )
                return "blocked"
            return "unchanged"

        if exception.status != ExceptionStatus.OPEN:
            return "unchanged"
        before = _quantities(exception)
        if evaluation.expected_qty is not None and evaluation.received_qty is not None:
            # Show what cleared it: the evidence now holds the receipts that close the gap.
            exception.expected_qty = evaluation.expected_qty
            exception.received_qty = evaluation.received_qty
            exception.variance_qty = evaluation.expected_qty - evaluation.received_qty
            exception.evidence, exception.evidence_hash = build_evidence(evaluation, lineage, feed, self._params)
        exception.explanation = {
            **exception.explanation,
            "status_note": (
                f"Cleared on re-evaluation at {as_of.isoformat()}: {evaluation.outcome.value} "
                f"({'; '.join(r['message'] for r in reasons)}). The text above describes the shortfall as first found."
            ),
        }
        exception.version += 1
        exception.status = ExceptionStatus.CLEARED
        add_exception_event(
            session,
            exception,
            "CLEARED",
            actor_id=SYSTEM_DETECTOR,
            occurred_at=as_of,
            run_id=run.id,
            from_status=ExceptionStatus.OPEN,
            to_status=ExceptionStatus.CLEARED,
            details={
                "outcome": evaluation.outcome,
                "reasons": reasons,
                "before": before,
                "after": _quantities(exception),
            },
        )
        cancel_active_tasks(
            session,
            exception,
            reason=f"exception cleared: {evaluation.outcome.value}",
            occurred_at=as_of,
            run_id=run.id,
        )
        return "cleared"

    def _exception_fields(
        self,
        evaluation: Evaluation,
        run: ReconciliationRun,
        mapping: MappingSet,
        lineage: dict[tuple[str, str], list[RowRef]],
        feed: ReceiptFeed,
    ) -> dict[str, Any]:
        po = evaluation.po_line
        assert po.quantity is not None and po.location is not None
        assert evaluation.expected_qty is not None and evaluation.received_qty is not None
        assert evaluation.variance_qty is not None and evaluation.unit is not None
        evidence, evidence_hash = build_evidence(evaluation, lineage, feed, self._params)
        explanation = explain_shortfall(
            evaluation, self._settings.business_timezone, _identical_repeats(evaluation, lineage)
        )
        return {
            "rule_version": RULE_VERSION,
            "mapping_version": mapping.version,
            "rule_params": self._params.as_dict(),
            "last_run_id": run.id,
            "last_outcome": EvaluationOutcome.SHORTFALL,
            "po_source_system": po.ref.source_system,
            "po_number": po.po_number,
            "po_line_number": po.line_number,
            "po_source_record_key": po.ref.source_record_key,
            "supplier_code": po.supplier_code,
            "canonical_product_id": po.quantity.canonical_product_id,
            "canonical_location_id": po.location.canonical_location_id,
            "region_id": po.location.region_id,
            "expected_receipt_by": po.deadline,
            "expected_qty": evaluation.expected_qty,
            "received_qty": evaluation.received_qty,
            "variance_qty": evaluation.variance_qty,
            "unit": evaluation.unit,
            "evidence": evidence,
            "evidence_hash": evidence_hash,
            "explanation": explanation,
        }


def build_evidence(
    evaluation: Evaluation,
    lineage: dict[tuple[str, str], list[RowRef]],
    feed: ReceiptFeed,
    params: RuleParams,
) -> tuple[dict[str, Any], str]:
    """The finding's evidence snapshot, and a hash over the parts that define the
    business result. Import-row lineage is included for tracing but not hashed,
    so re-importing the same files does not look like new evidence."""
    po = evaluation.po_line
    assert po.quantity is not None and po.location is not None

    def rows(dataset: str, key: str) -> list[dict[str, Any]]:
        return [r.as_dict() for r in lineage.get((dataset, key), [])]

    mappings = {m.source_record_key: m for m in po.mappings_used}
    for counted in evaluation.counted:
        mappings.update({m.source_record_key: m for m in counted.receipt.mappings_used})

    evidence = to_jsonable(
        {
            "rule": {"version": RULE_VERSION, "params": params.as_dict()},
            "evaluated_as_of": evaluation.as_of,
            "po_line": {
                **po.ref.as_dict(),
                "po_number": po.po_number,
                "line_number": po.line_number,
                "supplier_code": po.supplier_code,
                "item_ref": po.item_ref,
                "location_ref": po.location_ref,
                "ordered_qty": po.ordered_qty,
                "ordered_uom": po.ordered_uom,
                "to_base_factor": po.quantity.factor,
                "expected_qty_base": po.quantity.base_qty,
                "status_code": po.status_code,
                "expected_receipt_by": po.deadline,
                "import_rows": rows("purchase_order_lines", po.ref.source_record_key),
            },
            "receipts_counted": [
                {
                    **c.receipt.ref.as_dict(),
                    "receipt_number": c.receipt.receipt_number,
                    "receipt_line": c.receipt.receipt_line,
                    "product_ref": c.receipt.product_ref,
                    "location_ref": c.receipt.location_ref,
                    "accepted_qty": c.receipt.accepted_qty,
                    "uom": c.receipt.uom,
                    "to_base_factor": c.receipt.quantity.factor if c.receipt.quantity else None,
                    "accepted_qty_base": c.base_qty,
                    "received_at": c.receipt.received_at,
                    "on_time": c.on_time,
                    "posting_status": c.receipt.posting_status_code,
                    "import_rows": rows("goods_receipt_lines", c.receipt.ref.source_record_key),
                }
                for c in evaluation.counted
            ],
            "receipts_excluded": [
                {**x.receipt.ref.as_dict(), "reason_code": x.reason_code, "received_at": x.receipt.received_at}
                for x in evaluation.excluded
            ],
            "mappings_used": [m.as_dict() for _, m in sorted(mappings.items())],
            "canonical": {
                "product_id": po.quantity.canonical_product_id,
                "location_id": po.location.canonical_location_id,
                "region_id": po.location.region_id,
                "base_uom": po.quantity.base_uom,
            },
            "computation": {
                "expected_qty": evaluation.expected_qty,
                "received_qty": evaluation.received_qty,
                "variance_qty": evaluation.variance_qty,
                "unit": evaluation.unit,
                "formula": "variance = ordered_qty x to_base_factor - sum(accepted_qty_base of counted receipts)",
            },
            "receipt_feed": {"extracted_through": feed.extracted_through, "import_ids": list(feed.import_ids)},
        }
    )
    core = {
        "po": [po.ref.source_record_key, po.ref.content_hash],
        "counted": sorted([c.receipt.ref.source_record_key, c.receipt.ref.content_hash] for c in evaluation.counted),
        "excluded": sorted([x.receipt.ref.source_record_key, x.reason_code] for x in evaluation.excluded),
        "mappings": sorted([m.source_record_key, m.content_hash] for m in mappings.values()),
        "computation": evidence["computation"],
        "canonical": evidence["canonical"],
    }
    return evidence, content_hash(core)


def _identical_repeats(evaluation: Evaluation, lineage: dict[tuple[str, str], list[RowRef]]) -> dict[str, int]:
    """For each counted receipt: the most times it appeared, identically, in one file."""
    out = {}
    for counted in evaluation.counted:
        per_import: dict[str, int] = defaultdict(int)
        for row in lineage.get(("goods_receipt_lines", counted.receipt.ref.source_record_key), []):
            if row.content_hash == counted.receipt.ref.content_hash:
                per_import[row.import_id] += 1
        out[counted.receipt.ref.source_record_key] = max(per_import.values(), default=1)
    return out


def _apply(exception: ExceptionCase, fields: dict[str, Any]) -> None:
    for name, value in fields.items():
        setattr(exception, name, value)
    exception.version += 1


def _quantities(exception: ExceptionCase) -> dict[str, str]:
    return {
        "expected_qty": normalize_decimal(exception.expected_qty),
        "received_qty": normalize_decimal(exception.received_qty),
        "variance_qty": normalize_decimal(exception.variance_qty),
    }


def _evaluation_details(evaluation: Evaluation) -> dict[str, Any]:
    return to_jsonable(
        {
            "po_source_record_keys": [line.ref.source_record_key for line in evaluation.po_lines],
            "reasons": [
                {
                    "code": r.code,
                    "message": r.message,
                    "subject": {"dataset": r.subject.dataset, "record_key": r.subject.record_key}
                    if r.subject
                    else None,
                    "details": dict(r.details),
                }
                for r in evaluation.reasons
            ],
            "expected_qty": evaluation.expected_qty,
            "received_qty": evaluation.received_qty,
            "variance_qty": evaluation.variance_qty,
            "unit": evaluation.unit,
            "counted_receipts": [c.receipt.ref.source_record_key for c in evaluation.counted],
            "excluded_receipts": [
                {"source_record_key": x.receipt.ref.source_record_key, "reason_code": x.reason_code}
                for x in evaluation.excluded
            ],
            "blocked_receipts": [r.ref.source_record_key for r in evaluation.blocked],
        }
    )


def evidence_record_keys(exception: ExceptionCase) -> dict[str, list[str]]:
    evidence = exception.evidence or {}
    return {
        "purchase_order_lines": [evidence.get("po_line", {}).get("source_record_key")],
        "goods_receipt_lines": [r["source_record_key"] for r in evidence.get("receipts_counted", [])]
        + [r["source_record_key"] for r in evidence.get("receipts_excluded", [])],
    }
