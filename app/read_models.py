"""Read-side views used by the API. Plain queries; no state changes."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DataQualityIssue,
    ExceptionCase,
    ExceptionEvent,
    ImportBatch,
    ImportRow,
    LocationAliasFact,
    ProductAliasFact,
    ReconciliationRun,
    RuleEvaluation,
    SourceConflict,
    Task,
    TaskAssignment,
    TaskEvent,
)
from app.detection.service import evidence_record_keys
from app.enums import ConflictStatus, ImportStatus, IssueStatus, RowOutcome
from app.errors import NotFound
from app.hashing import to_jsonable
from app.ingestion.datasets import DATASETS, DatasetRegistry
from app.ingestion.rejections import outstanding_rejections
from app.ingestion.service import fact_payload_json
from app.workflows.views import assignment_view, task_event_view, task_view

# ---------------------------------------------------------------- imports


def import_view(batch: ImportBatch, rows: list[ImportRow] | None = None) -> dict[str, Any]:
    view = to_jsonable(
        {
            "import_id": batch.id,
            "dataset": batch.dataset,
            "profile": batch.profile,
            "source_system": batch.source_system,
            "status": batch.status,
            "file_name": batch.file_name,
            "file_sha256": batch.file_sha256,
            "file_size_bytes": batch.file_size_bytes,
            "source_extracted_at": batch.source_extracted_at,
            "counts": {
                "total_rows": batch.total_rows,
                "accepted": batch.inserted_rows + batch.unchanged_rows,
                "inserted": batch.inserted_rows,
                "unchanged": batch.unchanged_rows,
                "duplicates": batch.duplicate_rows,
                "rejected": batch.rejected_rows,
                "conflicts": batch.conflict_rows,
            },
            "file_errors": batch.file_errors,
            "warnings": batch.warnings,
            "failure_reason": batch.failure_reason,
            "created_at": batch.created_at,
            "completed_at": batch.completed_at,
        }
    )
    if rows is not None:
        view["rows"] = [row_view(r) for r in rows]
    return view


def row_view(row: ImportRow) -> dict[str, Any]:
    return to_jsonable(
        {
            "row_id": row.id,
            "row_number": row.row_number,
            "outcome": row.outcome,
            "source_record_key": row.source_record_key,
            "errors": row.errors,
            "detail": row.detail,
            "conflict_id": row.conflict_id,
            "raw": row.raw,
        }
    )


def _get_batch(session: Session, tenant_id: str, import_id: uuid.UUID) -> ImportBatch:
    batch = session.scalar(select(ImportBatch).where(ImportBatch.id == import_id, ImportBatch.tenant_id == tenant_id))
    if batch is None:
        raise NotFound("import not found", details={"import_id": str(import_id)})
    return batch


def get_import(session: Session, tenant_id: str, import_id: uuid.UUID, all_rows: bool = False) -> dict[str, Any]:
    batch = _get_batch(session, tenant_id, import_id)
    stmt = select(ImportRow).where(ImportRow.import_id == batch.id).order_by(ImportRow.row_number)
    if not all_rows:
        # By default only the rows that need a look: rejected, conflicting, duplicated.
        stmt = stmt.where(ImportRow.outcome.not_in([RowOutcome.INSERTED, RowOutcome.UNCHANGED]))
    return import_view(batch, list(session.scalars(stmt)))


def list_imports(session: Session, tenant_id: str, dataset: str | None, limit: int) -> list[dict[str, Any]]:
    stmt = select(ImportBatch).where(ImportBatch.tenant_id == tenant_id)
    if dataset:
        stmt = stmt.where(ImportBatch.dataset == dataset)
    stmt = stmt.order_by(ImportBatch.created_at.desc()).limit(limit)
    return [import_view(b) for b in session.scalars(stmt)]


# ---------------------------------------------------------------- data quality


def issue_view(issue: DataQualityIssue) -> dict[str, Any]:
    return to_jsonable(
        {
            "id": issue.id,
            "code": issue.code,
            "status": issue.status,
            "producer": issue.producer,
            "subject": {"dataset": issue.subject_dataset, "record_key": issue.subject_record_key},
            "message": issue.message,
            "details": issue.details,
            "first_seen_run_id": issue.first_seen_run_id,
            "last_seen_run_id": issue.last_seen_run_id,
            "resolved_run_id": issue.resolved_run_id,
        }
    )


def conflict_view(conflict: SourceConflict) -> dict[str, Any]:
    return to_jsonable(
        {
            "id": conflict.id,
            "dataset": conflict.dataset,
            "source_system": conflict.source_system,
            "source_record_key": conflict.source_record_key,
            "status": conflict.status,
            "existing": {"content_hash": conflict.existing_content_hash, "payload": conflict.existing_payload},
            "incoming": {
                "content_hash": conflict.incoming_content_hash,
                "payload": conflict.incoming_payload,
                "import_id": conflict.import_id,
                "row_number": conflict.row_number,
            },
            "changed_fields": sorted(
                k
                for k in set(conflict.existing_payload) | set(conflict.incoming_payload)
                if conflict.existing_payload.get(k) != conflict.incoming_payload.get(k)
            ),
            "resolution": conflict.resolution,
            "resolved_by": conflict.resolved_by,
            "resolution_reason": conflict.resolution_reason,
            "resolved_at": conflict.resolved_at,
        }
    )


def data_quality_report(
    session: Session, tenant_id: str, registry: DatasetRegistry, include_resolved: bool = False
) -> dict[str, Any]:
    issue_stmt = select(DataQualityIssue).where(DataQualityIssue.tenant_id == tenant_id)
    if not include_resolved:
        issue_stmt = issue_stmt.where(DataQualityIssue.status == IssueStatus.OPEN)
    issues = list(session.scalars(issue_stmt.order_by(DataQualityIssue.code, DataQualityIssue.subject_record_key)))

    conflict_stmt = select(SourceConflict).where(SourceConflict.tenant_id == tenant_id)
    if not include_resolved:
        conflict_stmt = conflict_stmt.where(SourceConflict.status == ConflictStatus.OPEN)
    conflicts = list(session.scalars(conflict_stmt.order_by(SourceConflict.created_at)))

    # Every rejected row not yet superseded, from any import (app/ingestion/rejections.py).
    rejected_rows = [
        to_jsonable(
            {
                "row_id": r.row_id,
                "dataset": r.dataset,
                "import_id": r.import_id,
                "row_number": r.row_number,
                "profile": r.profile,
                "source_record_key": r.source_record_key,
                "errors": r.errors,
                "raw": r.raw,
            }
        )
        for r in outstanding_rejections(session, tenant_id, registry)
    ]

    # Files that could not be loaded at all, until a later import of the dataset succeeds.
    batches = list(
        session.scalars(select(ImportBatch).where(ImportBatch.tenant_id == tenant_id).order_by(ImportBatch.created_at))
    )
    last_success: dict[str, Any] = {}
    for batch in batches:
        if batch.status in (ImportStatus.COMPLETED, ImportStatus.COMPLETED_WITH_ERRORS):
            last_success[batch.dataset] = batch.created_at
    file_rejections = [
        to_jsonable(
            {
                "import_id": batch.id,
                "dataset": batch.dataset,
                "status": batch.status,
                "errors": batch.file_errors,
                "failure_reason": batch.failure_reason,
                "created_at": batch.created_at,
            }
        )
        for batch in batches
        if batch.status in (ImportStatus.REJECTED, ImportStatus.FAILED)
        and (batch.dataset not in last_success or last_success[batch.dataset] < batch.created_at)
    ]

    return {
        "summary": {
            "open_issues": sum(1 for i in issues if i.status == IssueStatus.OPEN),
            "open_conflicts": sum(1 for c in conflicts if c.status == ConflictStatus.OPEN),
            "outstanding_rejected_rows": len(rejected_rows),
            "outstanding_rejected_files": len(file_rejections),
        },
        "issues": [issue_view(i) for i in issues],
        "conflicts": [conflict_view(c) for c in conflicts],
        "rejected_rows": rejected_rows,
        "rejected_files": file_rejections,
    }


# ---------------------------------------------------------------- runs


def run_view(run: ReconciliationRun, evaluations: list[RuleEvaluation] | None = None) -> dict[str, Any]:
    view = to_jsonable(
        {
            "run_id": run.id,
            "status": run.status,
            "evaluated_as_of": run.evaluated_as_of,
            "rule_version": run.rule_version,
            "routing_policy_version": run.routing_policy_version,
            "mapping_version": run.mapping_version,
            "params": run.params,
            "summary": run.summary,
            "failure_reason": run.failure_reason,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
        }
    )
    if evaluations is not None:
        view["evaluations"] = [
            {"case_key": e.case_key, "outcome": e.outcome, "reason_codes": e.reason_codes, "details": e.details}
            for e in evaluations
        ]
    return view


def get_run(session: Session, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    run = session.scalar(
        select(ReconciliationRun).where(ReconciliationRun.id == run_id, ReconciliationRun.tenant_id == tenant_id)
    )
    if run is None:
        raise NotFound("reconciliation run not found", details={"run_id": str(run_id)})
    evaluations = list(
        session.scalars(select(RuleEvaluation).where(RuleEvaluation.run_id == run.id).order_by(RuleEvaluation.case_key))
    )
    return run_view(run, evaluations)


# ---------------------------------------------------------------- exceptions


def exception_view(exception: ExceptionCase) -> dict[str, Any]:
    return to_jsonable(
        {
            "id": exception.id,
            "case_key": exception.case_key,
            "exception_type": exception.exception_type,
            "status": exception.status,
            "last_evaluation_outcome": exception.last_outcome,
            "detected_at": exception.detected_at,
            "rule_version": exception.rule_version,
            "mapping_version": exception.mapping_version,
            "rule_params": exception.rule_params,
            "po": {
                "source_system": exception.po_source_system,
                "po_number": exception.po_number,
                "line_number": exception.po_line_number,
                "source_record_key": exception.po_source_record_key,
                "supplier_code": exception.supplier_code,
                "expected_receipt_by": exception.expected_receipt_by,
            },
            "canonical_product_id": exception.canonical_product_id,
            "canonical_location_id": exception.canonical_location_id,
            "region_id": exception.region_id,
            "expected_qty": exception.expected_qty,
            "received_qty": exception.received_qty,
            "variance_qty": exception.variance_qty,
            "unit": exception.unit,
            "explanation": exception.explanation,
            "resolution": {
                "code": exception.resolution_code,
                "evidence_ref": exception.resolution_evidence_ref,
                "resolved_by": exception.resolved_by,
                "resolved_at": exception.resolved_at,
            },
            "first_run_id": exception.first_run_id,
            "last_run_id": exception.last_run_id,
            "version": exception.version,
        }
    )


def get_exception_model(session: Session, tenant_id: str, exception_id: uuid.UUID) -> ExceptionCase:
    exception = session.scalar(
        select(ExceptionCase).where(ExceptionCase.id == exception_id, ExceptionCase.tenant_id == tenant_id)
    )
    if exception is None:
        raise NotFound("exception not found", details={"exception_id": str(exception_id)})
    return exception


def list_exceptions(
    session: Session, tenant_id: str, status: str | None, location_id: str | None, exception_type: str | None
) -> list[dict[str, Any]]:
    stmt = select(ExceptionCase).where(ExceptionCase.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(ExceptionCase.status == status)
    if location_id:
        stmt = stmt.where(ExceptionCase.canonical_location_id == location_id)
    if exception_type:
        stmt = stmt.where(ExceptionCase.exception_type == exception_type)
    return [exception_view(e) for e in session.scalars(stmt.order_by(ExceptionCase.case_key))]


def get_exception_detail(session: Session, tenant_id: str, exception_id: uuid.UUID) -> dict[str, Any]:
    exception = get_exception_model(session, tenant_id, exception_id)
    view = exception_view(exception)
    view["evidence"] = exception.evidence
    view["supporting_records"] = _supporting_records(session, tenant_id, exception)
    view["tasks"] = [
        task_view(t)
        for t in session.scalars(
            select(Task).where(Task.exception_id == exception.id).order_by(Task.opened_at, Task.id)
        )
    ]
    view["events"] = [
        to_jsonable(
            {
                "id": e.id,
                "event_type": e.event_type,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "actor_id": e.actor_id,
                "run_id": e.run_id,
                "occurred_at": e.occurred_at,
                "recorded_at": e.recorded_at,
                "details": e.details,
            }
        )
        for e in session.scalars(
            select(ExceptionEvent).where(ExceptionEvent.exception_id == exception.id).order_by(ExceptionEvent.id)
        )
    ]
    return view


def _supporting_records(session: Session, tenant_id: str, exception: ExceptionCase) -> list[dict[str, Any]]:
    """Current state of every source record the finding cites, the import rows that
    carried it, and whether it changed since the finding was computed."""
    evidence = exception.evidence or {}
    hashes = {evidence.get("po_line", {}).get("source_record_key"): evidence.get("po_line", {}).get("content_hash")}
    for r in evidence.get("receipts_counted", []) + evidence.get("receipts_excluded", []):
        hashes[r["source_record_key"]] = r.get("content_hash")

    out: list[dict[str, Any]] = []
    for dataset, keys in evidence_record_keys(exception).items():
        spec = DATASETS[dataset]
        model = spec.fact_model
        facts = session.scalars(
            select(model).where(model.tenant_id == tenant_id, model.source_record_key.in_([k for k in keys if k]))
        )
        for fact in sorted(facts, key=lambda f: f.source_record_key):
            rows = session.execute(
                select(ImportRow, ImportBatch.file_name)
                .join(ImportBatch, ImportBatch.id == ImportRow.import_id)
                .where(
                    ImportBatch.tenant_id == tenant_id,
                    ImportBatch.dataset == dataset,
                    ImportRow.source_record_key == fact.source_record_key,
                )
                .order_by(ImportBatch.created_at, ImportRow.row_number)
            ).all()
            out.append(
                to_jsonable(
                    {
                        "dataset": dataset,
                        "source_system": fact.source_system,
                        "source_record_key": fact.source_record_key,
                        "version": fact.version,
                        "review_status": fact.review_status,
                        "content_hash": fact.content_hash,
                        "changed_since_detection": fact.content_hash != hashes.get(fact.source_record_key),
                        "current": fact_payload_json(spec, fact),
                        "import_rows": [
                            {
                                "import_id": row.import_id,
                                "file_name": file_name,
                                "row_number": row.row_number,
                                "outcome": row.outcome,
                                "raw": row.raw,
                            }
                            for row, file_name in rows
                        ],
                    }
                )
            )
    alias_keys = [m["source_record_key"] for m in evidence.get("mappings_used", [])]
    for model, dataset in ((ProductAliasFact, "product_aliases"), (LocationAliasFact, "location_aliases")):
        for fact in session.scalars(
            select(model).where(model.tenant_id == tenant_id, model.source_record_key.in_(alias_keys))
        ):
            out.append(
                to_jsonable(
                    {
                        "dataset": dataset,
                        "source_system": fact.source_system,
                        "source_record_key": fact.source_record_key,
                        "version": fact.version,
                        "review_status": fact.review_status,
                        "content_hash": fact.content_hash,
                        "current": fact_payload_json(DATASETS[dataset], fact),
                    }
                )
            )
    return out


# ---------------------------------------------------------------- tasks


def list_tasks(
    session: Session,
    tenant_id: str,
    assignee_id: str | None,
    location_id: str | None,
    status: str | None,
    queue: str | None,
) -> list[dict[str, Any]]:
    stmt = select(Task).where(Task.tenant_id == tenant_id)
    if assignee_id:
        stmt = stmt.where(Task.assignee_employee_id == assignee_id)
    if location_id:
        stmt = stmt.where(Task.location_id == location_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if queue:
        stmt = stmt.where(Task.queue == queue)
    return [task_view(t) for t in session.scalars(stmt.order_by(Task.due_at, Task.id))]


def get_task_detail(session: Session, tenant_id: str, task_id: uuid.UUID) -> dict[str, Any]:
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None:
        raise NotFound("task not found", details={"task_id": str(task_id)})
    view = task_view(task)
    view["assignments"] = [
        assignment_view(a)
        for a in session.scalars(
            select(TaskAssignment).where(TaskAssignment.task_id == task.id).order_by(TaskAssignment.sequence)
        )
    ]
    view["events"] = [
        task_event_view(e)
        for e in session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id))
    ]
    return view
