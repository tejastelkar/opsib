# ruff: noqa: E501  (generated DDL: long CHECK expressions are kept on one line)
"""Initial schema: raw imports, source facts, conflicts, runs, exceptions, tasks, audit.

Generated from app/db/models.py with `alembic revision --autogenerate`, then reviewed;
the append-only triggers at the end are hand-written.

Revision ID: 0001
Revises:
Create Date: 2026-10-23 19:29:49.655871
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("exception_events", "task_events", "task_assignments", "rejected_row_dismissals")


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint("tenant_id", "key", name=op.f("uq_idempotency_records_tenant_id_key")),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("inserted_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("unchanged_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("conflict_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "file_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "warnings", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PROCESSING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'REJECTED', 'FAILED')",
            name=op.f("ck_import_batches_status"),
        ),
        sa.CheckConstraint(
            "total_rows = inserted_rows + unchanged_rows + duplicate_rows + rejected_rows + conflict_rows",
            name=op.f("ck_import_batches_row_counts_add_up"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
    )
    op.create_index(
        "ix_import_batches_tenant_dataset_created",
        "import_batches",
        ["tenant_id", "dataset", "created_at"],
        unique=False,
    )
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("evaluated_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("routing_policy_version", sa.String(length=64), nullable=False),
        sa.Column("mapping_version", sa.String(length=64), nullable=True),
        sa.Column(
            "params", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "summary", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING', 'COMPLETED', 'FAILED')", name=op.f("ck_reconciliation_runs_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_runs")),
    )
    op.create_table(
        "availability_facts",
        sa.Column("employee_ref", sa.String(length=64), nullable=False),
        sa.Column("availability_status", sa.String(length=32), nullable=False),
        sa.Column("shift_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default=sa.text("'OK'"), nullable=False),
        sa.Column("first_import_id", sa.Uuid(), nullable=False),
        sa.Column("last_import_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("review_status IN ('OK', 'CONFLICT')", name=op.f("ck_availability_facts_review_status")),
        sa.CheckConstraint("shift_end > shift_start", name=op.f("ck_availability_facts_shift_window_ordered")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_availability_facts_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_import_id"],
            ["import_batches.id"],
            name=op.f("fk_availability_facts_first_import_id_import_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"], ["import_batches.id"], name=op.f("fk_availability_facts_last_import_id_import_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_availability_facts")),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_key",
            name=op.f("uq_availability_facts_tenant_id_source_system_source_record_key"),
        ),
    )
    op.create_index(
        "ix_availability_facts_employee_observed",
        "availability_facts",
        ["tenant_id", "employee_ref", "observed_at"],
        unique=False,
    )
    op.create_table(
        "data_quality_issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("issue_key", sa.String(length=400), nullable=False),
        sa.Column("producer", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("subject_dataset", sa.String(length=64), nullable=False),
        sa.Column("subject_record_key", sa.String(length=256), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("last_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name=op.f("ck_data_quality_issues_status")),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id"],
            ["reconciliation_runs.id"],
            name=op.f("fk_data_quality_issues_first_seen_run_id_reconciliation_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["last_seen_run_id"],
            ["reconciliation_runs.id"],
            name=op.f("fk_data_quality_issues_last_seen_run_id_reconciliation_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["resolved_run_id"],
            ["reconciliation_runs.id"],
            name=op.f("fk_data_quality_issues_resolved_run_id_reconciliation_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_quality_issues")),
        sa.UniqueConstraint("tenant_id", "issue_key", name=op.f("uq_data_quality_issues_tenant_id_issue_key")),
    )
    op.create_table(
        "employee_facts",
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("employment_status", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("location_ref", sa.String(length=64), nullable=True),
        sa.Column("manager_ref", sa.String(length=64), nullable=True),
        sa.Column("region_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default=sa.text("'OK'"), nullable=False),
        sa.Column("first_import_id", sa.Uuid(), nullable=False),
        sa.Column("last_import_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("review_status IN ('OK', 'CONFLICT')", name=op.f("ck_employee_facts_review_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_employee_facts_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_import_id"], ["import_batches.id"], name=op.f("fk_employee_facts_first_import_id_import_batches")
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"], ["import_batches.id"], name=op.f("fk_employee_facts_last_import_id_import_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_facts")),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_key",
            name=op.f("uq_employee_facts_tenant_id_source_system_source_record_key"),
        ),
    )
    op.create_table(
        "exceptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("case_key", sa.String(length=256), nullable=False),
        sa.Column("exception_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("mapping_version", sa.String(length=64), nullable=False),
        sa.Column("rule_params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_run_id", sa.Uuid(), nullable=False),
        sa.Column("last_run_id", sa.Uuid(), nullable=False),
        sa.Column("last_outcome", sa.String(length=32), nullable=False),
        sa.Column("po_source_system", sa.String(length=32), nullable=False),
        sa.Column("po_number", sa.String(length=64), nullable=False),
        sa.Column("po_line_number", sa.Integer(), nullable=False),
        sa.Column("po_source_record_key", sa.String(length=256), nullable=False),
        sa.Column("supplier_code", sa.String(length=64), nullable=False),
        sa.Column("canonical_product_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_location_id", sa.String(length=64), nullable=False),
        sa.Column("region_id", sa.String(length=64), nullable=True),
        sa.Column("expected_receipt_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("received_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("variance_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolution_code", sa.String(length=64), nullable=True),
        sa.Column("resolution_evidence_ref", sa.String(length=512), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "last_outcome IN ('SHORTFALL', 'NO_SHORTFALL', 'NOT_DUE', 'NOT_ELIGIBLE', 'NOT_EVALUABLE')",
            name=op.f("ck_exceptions_last_outcome"),
        ),
        sa.CheckConstraint(
            "status <> 'RESOLVED' OR (resolution_code IS NOT NULL AND resolution_evidence_ref IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name=op.f("ck_exceptions_resolved_has_resolution"),
        ),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED', 'CLEARED')", name=op.f("ck_exceptions_status")),
        sa.CheckConstraint(
            "variance_qty = expected_qty - received_qty", name=op.f("ck_exceptions_variance_is_expected_minus_received")
        ),
        sa.ForeignKeyConstraint(
            ["first_run_id"], ["reconciliation_runs.id"], name=op.f("fk_exceptions_first_run_id_reconciliation_runs")
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"], ["reconciliation_runs.id"], name=op.f("fk_exceptions_last_run_id_reconciliation_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exceptions")),
        sa.UniqueConstraint("tenant_id", "case_key", name=op.f("uq_exceptions_tenant_id_case_key")),
    )
    op.create_index("ix_exceptions_tenant_status", "exceptions", ["tenant_id", "status"], unique=False)
    op.create_table(
        "location_alias_facts",
        sa.Column("alias_system", sa.String(length=32), nullable=False),
        sa.Column("external_location_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_location_id", sa.String(length=64), nullable=False),
        sa.Column("region_id", sa.String(length=64), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default=sa.text("'OK'"), nullable=False),
        sa.Column("first_import_id", sa.Uuid(), nullable=False),
        sa.Column("last_import_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("review_status IN ('OK', 'CONFLICT')", name=op.f("ck_location_alias_facts_review_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_location_alias_facts_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_import_id"],
            ["import_batches.id"],
            name=op.f("fk_location_alias_facts_first_import_id_import_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"],
            ["import_batches.id"],
            name=op.f("fk_location_alias_facts_last_import_id_import_batches"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_location_alias_facts")),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_key",
            name=op.f("uq_location_alias_facts_tenant_id_source_system_source_record_key"),
        ),
    )
    op.create_table(
        "po_line_facts",
        sa.Column("po_number", sa.String(length=64), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("supplier_code", sa.String(length=64), nullable=False),
        sa.Column("location_ref", sa.String(length=64), nullable=False),
        sa.Column("item_ref", sa.String(length=64), nullable=False),
        sa.Column("ordered_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("ordered_uom", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.String(length=32), nullable=False),
        sa.Column("expected_receipt_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default=sa.text("'OK'"), nullable=False),
        sa.Column("first_import_id", sa.Uuid(), nullable=False),
        sa.Column("last_import_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("review_status IN ('OK', 'CONFLICT')", name=op.f("ck_po_line_facts_review_status")),
        sa.CheckConstraint("line_number > 0", name=op.f("ck_po_line_facts_line_number_positive")),
        sa.CheckConstraint("ordered_qty > 0", name=op.f("ck_po_line_facts_ordered_qty_positive")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_po_line_facts_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_import_id"], ["import_batches.id"], name=op.f("fk_po_line_facts_first_import_id_import_batches")
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"], ["import_batches.id"], name=op.f("fk_po_line_facts_last_import_id_import_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_po_line_facts")),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_key",
            name=op.f("uq_po_line_facts_tenant_id_source_system_source_record_key"),
        ),
    )
    op.create_index(
        "ix_po_line_facts_business_key",
        "po_line_facts",
        ["tenant_id", "source_system", "po_number", "line_number"],
        unique=False,
    )
    op.create_table(
        "product_alias_facts",
        sa.Column("alias_system", sa.String(length=32), nullable=False),
        sa.Column("external_item_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_product_id", sa.String(length=64), nullable=False),
        sa.Column("source_uom", sa.String(length=16), nullable=False),
        sa.Column("base_uom", sa.String(length=16), nullable=False),
        sa.Column("to_base_factor", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default=sa.text("'OK'"), nullable=False),
        sa.Column("first_import_id", sa.Uuid(), nullable=False),
        sa.Column("last_import_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("review_status IN ('OK', 'CONFLICT')", name=op.f("ck_product_alias_facts_review_status")),
        sa.CheckConstraint(
            "source_uom <> base_uom OR to_base_factor = 1", name=op.f("ck_product_alias_facts_identity_factor_is_one")
        ),
        sa.CheckConstraint("to_base_factor > 0", name=op.f("ck_product_alias_facts_factor_positive")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_product_alias_facts_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_import_id"],
            ["import_batches.id"],
            name=op.f("fk_product_alias_facts_first_import_id_import_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"], ["import_batches.id"], name=op.f("fk_product_alias_facts_last_import_id_import_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_alias_facts")),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_key",
            name=op.f("uq_product_alias_facts_tenant_id_source_system_source_record_key"),
        ),
    )
    op.create_table(
        "receipt_line_facts",
        sa.Column("receipt_number", sa.String(length=64), nullable=False),
        sa.Column("receipt_line", sa.Integer(), nullable=False),
        sa.Column("po_number", sa.String(length=64), nullable=False),
        sa.Column("po_line_number", sa.Integer(), nullable=False),
        sa.Column("location_ref", sa.String(length=64), nullable=False),
        sa.Column("product_ref", sa.String(length=64), nullable=False),
        sa.Column("accepted_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom", sa.String(length=16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("posting_status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("review_status", sa.String(length=16), server_default=sa.text("'OK'"), nullable=False),
        sa.Column("first_import_id", sa.Uuid(), nullable=False),
        sa.Column("last_import_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("review_status IN ('OK', 'CONFLICT')", name=op.f("ck_receipt_line_facts_review_status")),
        sa.CheckConstraint("accepted_qty >= 0", name=op.f("ck_receipt_line_facts_accepted_qty_not_negative")),
        sa.CheckConstraint(
            "receipt_line > 0 AND po_line_number > 0", name=op.f("ck_receipt_line_facts_line_numbers_positive")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_receipt_line_facts_version_positive")),
        sa.ForeignKeyConstraint(
            ["first_import_id"],
            ["import_batches.id"],
            name=op.f("fk_receipt_line_facts_first_import_id_import_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"], ["import_batches.id"], name=op.f("fk_receipt_line_facts_last_import_id_import_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt_line_facts")),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_key",
            name=op.f("uq_receipt_line_facts_tenant_id_source_system_source_record_key"),
        ),
    )
    op.create_index(
        "ix_receipt_line_facts_po_line",
        "receipt_line_facts",
        ["tenant_id", "po_number", "po_line_number"],
        unique=False,
    )
    op.create_table(
        "rule_evaluations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("case_key", sa.String(length=256), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('SHORTFALL', 'NO_SHORTFALL', 'NOT_DUE', 'NOT_ELIGIBLE', 'NOT_EVALUABLE')",
            name=op.f("ck_rule_evaluations_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reconciliation_runs.id"],
            name=op.f("fk_rule_evaluations_run_id_reconciliation_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_evaluations")),
        sa.UniqueConstraint("run_id", "case_key", name=op.f("uq_rule_evaluations_run_id_case_key")),
    )
    op.create_table(
        "source_conflicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=False),
        sa.Column("existing_content_hash", sa.String(length=64), nullable=False),
        sa.Column("existing_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("incoming_content_hash", sa.String(length=64), nullable=False),
        sa.Column("incoming_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_fact_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolution IS NULL) OR (status = 'RESOLVED' AND resolution IS NOT NULL AND resolved_by IS NOT NULL)",
            name=op.f("ck_source_conflicts_resolution_complete"),
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('KEEP_EXISTING', 'ACCEPT_INCOMING', 'SUPERSEDED')",
            name=op.f("ck_source_conflicts_resolution"),
        ),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name=op.f("ck_source_conflicts_status")),
        sa.ForeignKeyConstraint(
            ["import_id"], ["import_batches.id"], name=op.f("fk_source_conflicts_import_id_import_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_conflicts")),
    )
    op.create_index(
        "ix_source_conflicts_record",
        "source_conflicts",
        ["tenant_id", "dataset", "source_record_key", "status"],
        unique=False,
    )
    op.create_index(
        "uq_source_conflicts_open_change",
        "source_conflicts",
        ["tenant_id", "dataset", "source_record_key", "existing_content_hash", "incoming_content_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_table(
        "exception_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("exception_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["exception_id"], ["exceptions.id"], name=op.f("fk_exception_events_exception_id_exceptions")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["reconciliation_runs.id"], name=op.f("fk_exception_events_run_id_reconciliation_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exception_events")),
    )
    op.create_index("ix_exception_events_exception", "exception_events", ["exception_id", "id"], unique=False)
    op.create_table(
        "import_rows",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_record_key", sa.String(length=256), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "errors", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("conflict_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "outcome <> 'REJECTED' OR jsonb_array_length(errors) > 0",
            name=op.f("ck_import_rows_rejected_rows_have_reasons"),
        ),
        sa.CheckConstraint(
            "outcome IN ('INSERTED', 'UNCHANGED', 'DUPLICATE_IN_FILE', 'REJECTED', 'CONFLICT')",
            name=op.f("ck_import_rows_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["conflict_id"], ["source_conflicts.id"], name=op.f("fk_import_rows_conflict_id_source_conflicts")
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["import_batches.id"],
            name=op.f("fk_import_rows_import_id_import_batches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
        sa.UniqueConstraint("import_id", "row_number", name=op.f("uq_import_rows_import_id_row_number")),
    )
    op.create_index("ix_import_rows_source_record_key", "import_rows", ["source_record_key"], unique=False)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("exception_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("location_id", sa.String(length=64), nullable=False),
        sa.Column("region_id", sa.String(length=64), nullable=True),
        sa.Column("assignee_employee_id", sa.String(length=64), nullable=True),
        sa.Column("queue", sa.String(length=64), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_code", sa.String(length=64), nullable=True),
        sa.Column("evidence_ref", sa.String(length=512), nullable=True),
        sa.Column("completion_note", sa.Text(), nullable=True),
        sa.Column("completed_by", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(length=64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "resolution_code IS NULL OR resolution_code IN ('RECEIPT_MISSED_NOW_POSTED', 'SUPPLIER_CONFIRMED_SHORT_DISPATCH', 'CARRIER_LOSS_CLAIM_RAISED', 'BALANCE_TO_FOLLOW', 'PO_QUANTITY_AMENDED', 'STORE_COUNT_CONFIRMED_SHORT', 'OTHER')",
            name=op.f("ck_tasks_resolution_code"),
        ),
        sa.CheckConstraint(
            "status <> 'VERIFIED' OR (verified_by IS NOT NULL AND verified_by <> completed_by)",
            name=op.f("ck_tasks_verified_by_second_person"),
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'ASSIGNED', 'ACKNOWLEDGED', 'IN_PROGRESS', 'BLOCKED', 'COMPLETED', 'PENDING_VERIFICATION', 'VERIFIED', 'CANCELLED')",
            name=op.f("ck_tasks_status"),
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'CANCELLED') OR assignee_employee_id IS NOT NULL",
            name=op.f("ck_tasks_assigned_has_assignee"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('COMPLETED', 'PENDING_VERIFICATION', 'VERIFIED') OR (resolution_code IS NOT NULL AND evidence_ref IS NOT NULL AND completed_by IS NOT NULL)",
            name=op.f("ck_tasks_completed_has_resolution"),
        ),
        sa.ForeignKeyConstraint(["exception_id"], ["exceptions.id"], name=op.f("fk_tasks_exception_id_exceptions")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    op.create_index(
        "ix_tasks_tenant_assignee_status", "tasks", ["tenant_id", "assignee_employee_id", "status"], unique=False
    )
    op.create_index("ix_tasks_tenant_location_status", "tasks", ["tenant_id", "location_id", "status"], unique=False)
    op.create_index(
        "uq_tasks_one_active_per_exception_type",
        "tasks",
        ["exception_id", "task_type"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('VERIFIED', 'CANCELLED')"),
    )
    op.create_table(
        "rejected_row_dismissals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("import_row_id", sa.BigInteger(), nullable=False),
        sa.Column("dismissed_by", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_row_id"], ["import_rows.id"], name=op.f("fk_rejected_row_dismissals_import_row_id_import_rows")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rejected_row_dismissals")),
        sa.UniqueConstraint("import_row_id", name=op.f("uq_rejected_row_dismissals_import_row_id")),
    )
    op.create_table(
        "task_assignments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("assignee_employee_id", sa.String(length=64), nullable=True),
        sa.Column("queue", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column(
            "decision", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(assignee_employee_id IS NULL) <> (queue IS NULL)", name=op.f("ck_task_assignments_person_xor_queue")
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_task_assignments_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_assignments")),
        sa.UniqueConstraint("task_id", "sequence", name=op.f("uq_task_assignments_task_id_sequence")),
    )
    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_task_events_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_events")),
    )
    op.create_index("ix_task_events_task", "task_events", ["task_id", "id"], unique=False)

    # Audit history is append-only: the database refuses UPDATE and DELETE on it.
    op.execute(
        """
        CREATE FUNCTION opsib_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
        END
        $$;
        """
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION opsib_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS opsib_reject_mutation()")
    op.drop_index("ix_task_events_task", table_name="task_events")
    op.drop_table("task_events")
    op.drop_table("task_assignments")
    op.drop_table("rejected_row_dismissals")
    op.drop_index(
        "uq_tasks_one_active_per_exception_type",
        table_name="tasks",
        postgresql_where=sa.text("status NOT IN ('VERIFIED', 'CANCELLED')"),
    )
    op.drop_index("ix_tasks_tenant_location_status", table_name="tasks")
    op.drop_index("ix_tasks_tenant_assignee_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_import_rows_source_record_key", table_name="import_rows")
    op.drop_table("import_rows")
    op.drop_index("ix_exception_events_exception", table_name="exception_events")
    op.drop_table("exception_events")
    op.drop_index(
        "uq_source_conflicts_open_change", table_name="source_conflicts", postgresql_where=sa.text("status = 'OPEN'")
    )
    op.drop_index("ix_source_conflicts_record", table_name="source_conflicts")
    op.drop_table("source_conflicts")
    op.drop_table("rule_evaluations")
    op.drop_index("ix_receipt_line_facts_po_line", table_name="receipt_line_facts")
    op.drop_table("receipt_line_facts")
    op.drop_table("product_alias_facts")
    op.drop_index("ix_po_line_facts_business_key", table_name="po_line_facts")
    op.drop_table("po_line_facts")
    op.drop_table("location_alias_facts")
    op.drop_index("ix_exceptions_tenant_status", table_name="exceptions")
    op.drop_table("exceptions")
    op.drop_table("employee_facts")
    op.drop_table("data_quality_issues")
    op.drop_index("ix_availability_facts_employee_observed", table_name="availability_facts")
    op.drop_table("availability_facts")
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_import_batches_tenant_dataset_created", table_name="import_batches")
    op.drop_table("import_batches")
    op.drop_table("idempotency_records")
