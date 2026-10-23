"""Rejected rows that are still outstanding.

A rejected row is data we failed to read, not data that does not exist. It stays
outstanding, visible in /data-quality and blocking any decision it could affect,
until one of two things happens:

* its source record key can be read and a later import carried that record
  validly (the fact's last import is newer than the rejecting import), or
* a data steward dismisses it with a reason (for example a garbage line).

A later file that happens to be clean is not enough: exports may be deltas, and
a clean delta says nothing about the row that failed.

Rows are read through the profile of the import that rejected them, so this
works for any export format. A row with the wrong number of cells is read
positionally against the file's header, as far as its cells go.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ImportBatch, ImportRow, RejectedRowDismissal
from app.enums import RowOutcome
from app.errors import DomainError
from app.ingestion.datasets import DatasetRegistry, SourceProfile

# Datasets whose records carry their own source ID (others have derived keys).
OWN_KEY_DATASETS = frozenset({"purchase_order_lines", "goods_receipt_lines", "employees"})


@dataclass(frozen=True)
class OutstandingRejection:
    row_id: int
    dataset: str
    import_id: str
    row_number: int
    imported_at: datetime
    profile: str
    source_record_key: str | None
    values: dict[str, str]  # payload field -> cell text, as far as the row could be read
    raw: dict[str, Any]
    errors: list[dict[str, Any]]


def readable_values(raw: dict[str, Any], profile: SourceProfile | None) -> dict[str, str]:
    cells = dict(zip(raw.get("_header") or [], raw["_cells"], strict=False)) if "_cells" in raw else raw
    if profile is None:
        return {k: str(v).strip() for k, v in cells.items() if str(v or "").strip()}
    out = {}
    for column, field in profile.column_map.items():
        text = str(cells.get(column) or "").strip()
        if text:
            out[field] = text
    return out


def outstanding_rejections(
    session: Session, tenant_id: str, registry: DatasetRegistry, dataset: str | None = None
) -> list[OutstandingRejection]:
    stmt = (
        select(ImportRow, ImportBatch)
        .join(ImportBatch, ImportBatch.id == ImportRow.import_id)
        .outerjoin(RejectedRowDismissal, RejectedRowDismissal.import_row_id == ImportRow.id)
        .where(
            ImportBatch.tenant_id == tenant_id,
            ImportRow.outcome == RowOutcome.REJECTED,
            RejectedRowDismissal.id.is_(None),
        )
        .order_by(ImportBatch.created_at, ImportRow.row_number)
    )
    if dataset:
        stmt = stmt.where(ImportBatch.dataset == dataset)
    candidates = []
    for row, batch in session.execute(stmt):
        try:
            profile: SourceProfile | None = registry.profile(batch.dataset, batch.profile)
        except DomainError:
            profile = None  # profile no longer registered: fall back to the raw column names
        values = readable_values(row.raw or {}, profile)
        key = values.get("source_record_key") if batch.dataset in OWN_KEY_DATASETS else None
        candidates.append((row, batch, values, key))
    if not candidates:
        return []

    # When each readable key was last carried validly, per (dataset, source system).
    wanted: dict[tuple[str, str], set[str]] = defaultdict(set)
    for _, batch, _, key in candidates:
        if key:
            wanted[(batch.dataset, batch.source_system)].add(key)
    last_valid: dict[tuple[str, str, str], datetime] = {}
    for (ds, system), keys in wanted.items():
        model = registry.dataset(ds).fact_model
        for record_key, created_at in session.execute(
            select(model.source_record_key, ImportBatch.created_at)
            .join(ImportBatch, ImportBatch.id == model.last_import_id)
            .where(model.tenant_id == tenant_id, model.source_system == system, model.source_record_key.in_(keys))
        ):
            last_valid[(ds, system, record_key)] = created_at

    out = []
    for row, batch, values, key in candidates:
        seen = last_valid.get((batch.dataset, batch.source_system, key)) if key else None
        if seen is not None and seen > batch.created_at:
            continue
        out.append(
            OutstandingRejection(
                row_id=row.id,
                dataset=batch.dataset,
                import_id=str(batch.id),
                row_number=row.row_number,
                imported_at=batch.created_at,
                profile=batch.profile,
                source_record_key=key,
                values=values,
                raw=row.raw or {},
                errors=row.errors or [],
            )
        )
    return out
