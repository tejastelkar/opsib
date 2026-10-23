"""Database schema.

Three layers, kept apart on purpose:

1. Raw input      import_batches, import_rows: every row of every file exactly as
                  received, with its outcome and errors. Nothing is dropped.
2. Source facts   *_facts tables: validated, typed records in the source's own
                  vocabulary (source IDs, source units), one row per source
                  identity. UNIQUE(tenant_id, source_system, source_record_key)
                  is what makes re-imports idempotent. Conflicting re-sends go to
                  source_conflicts instead of overwriting.
3. Business state reconciliation_runs, rule_evaluations, exceptions, tasks and
                  their append-only event tables, plus data_quality_issues.

The canonical model (products, locations, PO lines and receipts in base units,
employees with canonical scope) is a deterministic projection of layer 2
through the alias facts. It is rebuilt on each run and its outputs are stored
with the mapping version that produced them (see app/mapping).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.enums import (
    ConflictResolution,
    ConflictStatus,
    EvaluationOutcome,
    ExceptionStatus,
    ImportStatus,
    IssueStatus,
    ResolutionCode,
    ReviewStatus,
    RowOutcome,
    RunStatus,
    TaskStatus,
    sql_in,
)

QTY = Numeric(18, 4)
TS = DateTime(timezone=True)
JSON_LIST = text("'[]'::jsonb")
JSON_OBJECT = text("'{}'::jsonb")


def _now_column() -> Mapped[datetime]:
    return mapped_column(TS, server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- raw input


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    # Business time the snapshot covers: the source's extract time if supplied,
    # otherwise the business clock when the file was received.
    source_extracted_at: Mapped[datetime] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(32))
    total_rows: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    inserted_rows: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    unchanged_rows: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    conflict_rows: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    file_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=JSON_LIST)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=JSON_LIST)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()
    completed_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(ImportStatus)})", name="status"),
        CheckConstraint(
            "total_rows = inserted_rows + unchanged_rows + duplicate_rows + rejected_rows + conflict_rows",
            name="row_counts_add_up",
        ),
        Index("ix_import_batches_tenant_dataset_created", "tenant_id", "dataset", "created_at"),
    )


class ImportRow(Base):
    __tablename__ = "import_rows"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    # Physical line in the file: the header is line 1, the first data row line 2.
    row_number: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_record_key: Mapped[str | None] = mapped_column(String(256))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=JSON_LIST)
    detail: Mapped[str | None] = mapped_column(Text)
    conflict_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source_conflicts.id"))

    __table_args__ = (
        UniqueConstraint("import_id", "row_number"),
        CheckConstraint(f"outcome IN ({sql_in(RowOutcome)})", name="outcome"),
        CheckConstraint("outcome <> 'REJECTED' OR jsonb_array_length(errors) > 0", name="rejected_rows_have_reasons"),
        Index("ix_import_rows_source_record_key", "source_record_key"),
    )


class RejectedRowDismissal(Base):
    """A data steward's decision that a rejected row needs no correction (for example
    a garbage line). Until a row is dismissed or validly re-sent it stays outstanding
    and keeps blocking the decisions it could affect. Kept apart from import_rows so
    the raw layer stays exactly as received."""

    __tablename__ = "rejected_row_dismissals"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    import_row_id: Mapped[int] = mapped_column(ForeignKey("import_rows.id"), unique=True)
    dismissed_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    dismissed_at: Mapped[datetime] = mapped_column(TS)  # business clock
    created_at: Mapped[datetime] = _now_column()


# --------------------------------------------------------------------------- source facts


class SourceFactMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    source_record_key: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    review_status: Mapped[str] = mapped_column(
        String(16), default=ReviewStatus.OK.value, server_default=text(f"'{ReviewStatus.OK.value}'")
    )
    first_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"))
    last_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())


def _fact_table_args(*extra: Any) -> tuple[Any, ...]:
    return (
        UniqueConstraint("tenant_id", "source_system", "source_record_key"),
        CheckConstraint(f"review_status IN ({sql_in(ReviewStatus)})", name="review_status"),
        CheckConstraint("version >= 1", name="version_positive"),
        *extra,
    )


class PoLineFact(SourceFactMixin, Base):
    """ERP purchase-order line. Authoritative for what was ordered, when, from whom."""

    __tablename__ = "po_line_facts"

    po_number: Mapped[str] = mapped_column(String(64))
    line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str] = mapped_column(String(64))
    location_ref: Mapped[str] = mapped_column(String(64))
    item_ref: Mapped[str] = mapped_column(String(64))
    ordered_qty: Mapped[Decimal] = mapped_column(QTY)
    ordered_uom: Mapped[str] = mapped_column(String(16))
    status_code: Mapped[str] = mapped_column(String(32))
    expected_receipt_by: Mapped[datetime] = mapped_column(TS)

    __table_args__ = _fact_table_args(
        CheckConstraint("ordered_qty > 0", name="ordered_qty_positive"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        # Not unique: two source records claiming one PO line is a data-quality
        # finding the detector reports, not an insert error.
        Index("ix_po_line_facts_business_key", "tenant_id", "source_system", "po_number", "line_number"),
    )


class ReceiptLineFact(SourceFactMixin, Base):
    """WMS goods-receipt line. Authoritative for what was physically accepted and posted."""

    __tablename__ = "receipt_line_facts"

    receipt_number: Mapped[str] = mapped_column(String(64))
    receipt_line: Mapped[int] = mapped_column(Integer)
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    location_ref: Mapped[str] = mapped_column(String(64))
    product_ref: Mapped[str] = mapped_column(String(64))
    accepted_qty: Mapped[Decimal] = mapped_column(QTY)
    uom: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(TS)
    posting_status: Mapped[str] = mapped_column(String(32))

    __table_args__ = _fact_table_args(
        CheckConstraint("accepted_qty >= 0", name="accepted_qty_not_negative"),
        CheckConstraint("receipt_line > 0 AND po_line_number > 0", name="line_numbers_positive"),
        Index("ix_receipt_line_facts_po_line", "tenant_id", "po_number", "po_line_number"),
    )


class EmployeeFact(SourceFactMixin, Base):
    """HRMS roster. Authoritative for employment status, role, home location and manager."""

    __tablename__ = "employee_facts"

    display_name: Mapped[str | None] = mapped_column(String(128))
    employment_status: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(64))
    location_ref: Mapped[str | None] = mapped_column(String(64))
    manager_ref: Mapped[str | None] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = _fact_table_args()

    @property
    def employee_id(self) -> str:
        return self.source_record_key


class AvailabilityFact(SourceFactMixin, Base):
    """Workforce-management shift sample. Authoritative for availability only: it can
    never make an employee eligible whom HRMS says is not ACTIVE."""

    __tablename__ = "availability_facts"

    employee_ref: Mapped[str] = mapped_column(String(64))
    availability_status: Mapped[str] = mapped_column(String(32))
    shift_start: Mapped[datetime] = mapped_column(TS)
    shift_end: Mapped[datetime] = mapped_column(TS)
    observed_at: Mapped[datetime] = mapped_column(TS)

    __table_args__ = _fact_table_args(
        CheckConstraint("shift_end > shift_start", name="shift_window_ordered"),
        Index("ix_availability_facts_employee_observed", "tenant_id", "employee_ref", "observed_at"),
    )


class ProductAliasFact(SourceFactMixin, Base):
    """Crosswalk from a system's item ID to the canonical product and its base unit."""

    __tablename__ = "product_alias_facts"

    alias_system: Mapped[str] = mapped_column(String(32))
    external_item_id: Mapped[str] = mapped_column(String(64))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    source_uom: Mapped[str] = mapped_column(String(16))
    base_uom: Mapped[str] = mapped_column(String(16))
    to_base_factor: Mapped[Decimal] = mapped_column(Numeric(18, 6))

    __table_args__ = _fact_table_args(
        CheckConstraint("to_base_factor > 0", name="factor_positive"),
        CheckConstraint("source_uom <> base_uom OR to_base_factor = 1", name="identity_factor_is_one"),
    )


class LocationAliasFact(SourceFactMixin, Base):
    """Crosswalk from a system's location code to the canonical OPSiB location."""

    __tablename__ = "location_alias_facts"

    alias_system: Mapped[str] = mapped_column(String(32))
    external_location_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str] = mapped_column(String(64))

    __table_args__ = _fact_table_args()


class SourceConflict(Base):
    """A source key re-sent with different business content. The stored fact is kept
    unchanged and marked CONFLICT until a person decides."""

    __tablename__ = "source_conflicts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    source_record_key: Mapped[str] = mapped_column(String(256))
    existing_content_hash: Mapped[str] = mapped_column(String(64))
    existing_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    incoming_content_hash: Mapped[str] = mapped_column(String(64))
    incoming_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"))
    row_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    resolution: Mapped[str | None] = mapped_column(String(32))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    # The fact version a KEEP_EXISTING decision was made against. The same re-send
    # is only treated as already reviewed while the fact is still at that version.
    decided_fact_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now_column()

    __table_args__ = (
        # One open conflict per (record, stored version, re-sent version). Resolved
        # ones are history: the same change arriving again later can open a new one.
        Index(
            "uq_source_conflicts_open_change",
            "tenant_id",
            "dataset",
            "source_record_key",
            "existing_content_hash",
            "incoming_content_hash",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("ix_source_conflicts_record", "tenant_id", "dataset", "source_record_key", "status"),
        CheckConstraint(f"status IN ({sql_in(ConflictStatus)})", name="status"),
        CheckConstraint(f"resolution IS NULL OR resolution IN ({sql_in(ConflictResolution)})", name="resolution"),
        CheckConstraint(
            "(status = 'OPEN' AND resolution IS NULL) OR "
            "(status = 'RESOLVED' AND resolution IS NOT NULL AND resolved_by IS NOT NULL)",
            name="resolution_complete",
        ),
    )


# --------------------------------------------------------------------------- business state


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    evaluated_as_of: Mapped[datetime] = mapped_column(TS)
    rule_version: Mapped[str] = mapped_column(String(64))
    routing_policy_version: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()
    completed_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (CheckConstraint(f"status IN ({sql_in(RunStatus)})", name="status"),)


class RuleEvaluation(Base):
    """Why each PO line did or did not become an exception in a given run."""

    __tablename__ = "rule_evaluations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id", ondelete="CASCADE"))
    case_key: Mapped[str] = mapped_column(String(256))
    outcome: Mapped[str] = mapped_column(String(32))
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=JSON_LIST)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)

    __table_args__ = (
        UniqueConstraint("run_id", "case_key"),
        CheckConstraint(f"outcome IN ({sql_in(EvaluationOutcome)})", name="outcome"),
    )


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    issue_key: Mapped[str] = mapped_column(String(400))
    producer: Mapped[str] = mapped_column(String(32))  # "detection" or "routing"
    code: Mapped[str] = mapped_column(String(64))
    subject_dataset: Mapped[str] = mapped_column(String(64))
    subject_record_key: Mapped[str] = mapped_column(String(256))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)
    status: Mapped[str] = mapped_column(String(16))
    first_seen_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id"))
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id"))
    resolved_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reconciliation_runs.id"))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "issue_key"),
        CheckConstraint(f"status IN ({sql_in(IssueStatus)})", name="status"),
    )


class ExceptionCase(Base):
    """A business case: one per (tenant, case_key), however many runs see it."""

    __tablename__ = "exceptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # uuid5 of tenant + case_key
    tenant_id: Mapped[str] = mapped_column(String(64))
    case_key: Mapped[str] = mapped_column(String(256))
    exception_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    rule_version: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[str] = mapped_column(String(64))
    rule_params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    detected_at: Mapped[datetime] = mapped_column(TS)  # business clock, first detection
    first_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id"))
    last_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id"))
    last_outcome: Mapped[str] = mapped_column(String(32))
    po_source_system: Mapped[str] = mapped_column(String(32))
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    po_source_record_key: Mapped[str] = mapped_column(String(256))
    supplier_code: Mapped[str] = mapped_column(String(64))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    expected_receipt_by: Mapped[datetime] = mapped_column(TS)
    expected_qty: Mapped[Decimal] = mapped_column(QTY)
    received_qty: Mapped[Decimal] = mapped_column(QTY)
    variance_qty: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    resolution_evidence_ref: Mapped[str | None] = mapped_column(String(512))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "case_key"),
        CheckConstraint(f"status IN ({sql_in(ExceptionStatus)})", name="status"),
        CheckConstraint(f"last_outcome IN ({sql_in(EvaluationOutcome)})", name="last_outcome"),
        CheckConstraint("variance_qty = expected_qty - received_qty", name="variance_is_expected_minus_received"),
        CheckConstraint(
            "status <> 'RESOLVED' OR (resolution_code IS NOT NULL AND resolution_evidence_ref IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolved_has_resolution",
        ),
        Index("ix_exceptions_tenant_status", "tenant_id", "status"),
    )


class ExceptionEvent(Base):
    __tablename__ = "exception_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    exception_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exceptions.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reconciliation_runs.id"))
    occurred_at: Mapped[datetime] = mapped_column(TS)  # business clock
    recorded_at: Mapped[datetime] = _now_column()  # wall clock
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)

    __table_args__ = (Index("ix_exception_events_exception", "exception_id", "id"),)


ACTIVE_TASK_PREDICATE = "status NOT IN ('VERIFIED', 'CANCELLED')"


class Task(Base):
    """Work a person performs for an exception. Reassignment changes the assignee,
    never the exception."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    exception_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exceptions.id"))
    task_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    assignee_employee_id: Mapped[str | None] = mapped_column(String(64))
    queue: Mapped[str | None] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(TS)  # business clock
    due_at: Mapped[datetime] = mapped_column(TS)
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    evidence_ref: Mapped[str | None] = mapped_column(String(512))
    completion_note: Mapped[str | None] = mapped_column(Text)
    completed_by: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(TaskStatus)})", name="status"),
        CheckConstraint(
            f"resolution_code IS NULL OR resolution_code IN ({sql_in(ResolutionCode)})", name="resolution_code"
        ),
        # Anyone past CREATED has a named person on it; nobody is assigned "by accident".
        CheckConstraint(
            "status IN ('CREATED', 'CANCELLED') OR assignee_employee_id IS NOT NULL", name="assigned_has_assignee"
        ),
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'PENDING_VERIFICATION', 'VERIFIED') "
            "OR (resolution_code IS NOT NULL AND evidence_ref IS NOT NULL AND completed_by IS NOT NULL)",
            name="completed_has_resolution",
        ),
        CheckConstraint(
            "status <> 'VERIFIED' OR (verified_by IS NOT NULL AND verified_by <> completed_by)",
            name="verified_by_second_person",
        ),
        # At most one active task of a type per exception: two workers (or two runs)
        # cannot both open work for the same business issue.
        Index(
            "uq_tasks_one_active_per_exception_type",
            "exception_id",
            "task_type",
            unique=True,
            postgresql_where=text(ACTIVE_TASK_PREDICATE),
        ),
        Index("ix_tasks_tenant_assignee_status", "tenant_id", "assignee_employee_id", "status"),
        Index("ix_tasks_tenant_location_status", "tenant_id", "location_id", "status"),
    )


class TaskAssignment(Base):
    """Assignment history: who/which queue, why, under which policy, decided by whom."""

    __tablename__ = "task_assignments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    assignee_employee_id: Mapped[str | None] = mapped_column(String(64))
    queue: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)
    note: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(TS)
    recorded_at: Mapped[datetime] = _now_column()

    __table_args__ = (
        UniqueConstraint("task_id", "sequence"),
        CheckConstraint("(assignee_employee_id IS NULL) <> (queue IS NULL)", name="person_xor_queue"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(TS)
    recorded_at: Mapped[datetime] = _now_column()
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=JSON_OBJECT)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (Index("ix_task_events_task", "task_id", "id"),)


class IdempotencyRecord(Base):
    """Stored outcome of a mutating request, keyed by the client's Idempotency-Key.
    Written in the same transaction as the state change it describes."""

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now_column()

    __table_args__ = (UniqueConstraint("tenant_id", "key"),)


FACT_MODELS = (
    PoLineFact,
    ReceiptLineFact,
    EmployeeFact,
    AvailabilityFact,
    ProductAliasFact,
    LocationAliasFact,
)

APPEND_ONLY_TABLES = ("exception_events", "task_events", "task_assignments", "rejected_row_dismissals")
