"""Import pipeline: register, parse, validate, classify, persist.

Transaction boundaries
  1. A short transaction registers the import (status PROCESSING) so it is
     visible and traceable even if processing later fails.
  2. One transaction loads the whole file while holding an advisory lock on
     (tenant, dataset). Either every accepted row of the file lands, or none.
     Rejected rows are a normal outcome of that transaction, not a failure.
  3. If (2) raises, it rolls back and a third transaction marks the import
     FAILED. Because nothing from (2) was committed, re-running is safe.

Idempotency
  Each fact has a natural identity (tenant, source_system, source_record_key)
  protected by a UNIQUE constraint. A row whose key and content hash match a
  stored fact is UNCHANGED; a row whose key matches but content differs is a
  CONFLICT and the stored fact is not overwritten. Quantities are never
  accumulated at import time, so a repeat cannot double-count anything.
"""

import hashlib
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ImportBatch, ImportRow, SourceConflict, SourceFactMixin
from app.db.session import advisory_xact_lock
from app.enums import ConflictResolution, ConflictStatus, ImportStatus, ReviewStatus, RowOutcome
from app.errors import ImportFailed, InvalidRequest, NotFound
from app.hashing import content_hash, to_jsonable
from app.ingestion.csv_reader import CsvRow, FileRejected, ParsedCsv, parse_csv
from app.ingestion.datasets import (
    DEFAULT_PROFILE,
    FIXTURE_LOAD_ORDER,
    DatasetRegistry,
    DatasetSpec,
    FactPayload,
    SourceProfile,
)
from app.observability import log_event

logger = logging.getLogger("opsib.ingestion")


@dataclass(frozen=True)
class ImportRequest:
    dataset: str
    content: bytes
    file_name: str | None = None
    profile: str = DEFAULT_PROFILE
    source_extracted_at: datetime | None = None


@dataclass
class _ValidatedRow:
    row: CsvRow
    payload: FactPayload | None
    errors: list[dict] = field(default_factory=list)
    key: str | None = None
    hash: str | None = None


def describe_failure(exc: BaseException) -> str:
    """A failure reason safe to store and show: the error class and, for database
    errors, the constraint name. Never the row values."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        diag = getattr(exc.orig, "diag", None)
        constraint = getattr(diag, "constraint_name", None)
        suffix = f" (constraint {constraint})" if constraint else ""
        return f"{type(exc.orig).__name__}{suffix}"
    return type(exc).__name__


def fact_payload_json(spec: DatasetSpec, fact: SourceFactMixin) -> dict:
    return {name: to_jsonable(getattr(fact, name)) for name in spec.payload_fields}


class ImportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        tenant_id: str,
        clock: Clock,
        registry: DatasetRegistry,
    ):
        self._sessions = session_factory
        self._tenant = tenant_id
        self._clock = clock
        self._registry = registry

    # ------------------------------------------------------------------ public

    def import_file(self, request: ImportRequest) -> ImportBatch:
        spec = self._registry.dataset(request.dataset)
        profile = self._registry.profile(request.dataset, request.profile)
        extracted_at = self._extracted_at(request.source_extracted_at)

        with self._sessions() as session, session.begin():
            batch = ImportBatch(
                id=uuid.uuid4(),
                tenant_id=self._tenant,
                dataset=spec.name,
                profile=profile.name,
                source_system=profile.source_system,
                file_name=request.file_name,
                file_sha256=_sha256(request.content),
                file_size_bytes=len(request.content),
                source_extracted_at=extracted_at,
                status=ImportStatus.PROCESSING,
            )
            session.add(batch)
        batch_id = batch.id

        try:
            with self._sessions() as session, session.begin():
                batch = session.get_one(ImportBatch, batch_id, with_for_update=True)
                advisory_xact_lock(session, self._tenant, "import", spec.name)
                try:
                    parsed = parse_csv(request.content, profile.required_columns)
                except FileRejected as rejected:
                    batch.status = ImportStatus.REJECTED
                    batch.file_errors = [rejected.as_error()]
                    batch.completed_at = datetime.now(UTC)
                else:
                    self._load_rows(session, batch, spec, profile, parsed)
        except Exception as exc:
            reason = describe_failure(exc)
            logger.exception("import_failed", extra={"fields": {"import_id": str(batch_id), "reason": reason}})
            with self._sessions() as session, session.begin():
                failed = session.get_one(ImportBatch, batch_id, with_for_update=True)
                failed.status = ImportStatus.FAILED
                failed.failure_reason = reason
                failed.completed_at = datetime.now(UTC)
            raise ImportFailed(
                "import failed and was rolled back; nothing from this file was loaded",
                details={"import_id": str(batch_id), "reason": reason},
            ) from exc

        log_event(
            logger,
            "import_finished",
            import_id=str(batch.id),
            dataset=batch.dataset,
            profile=batch.profile,
            status=batch.status,
            total=batch.total_rows,
            inserted=batch.inserted_rows,
            unchanged=batch.unchanged_rows,
            duplicates=batch.duplicate_rows,
            rejected=batch.rejected_rows,
            conflicts=batch.conflict_rows,
        )
        return batch

    def import_directory(self, directory: Path, profile: str = DEFAULT_PROFILE) -> list[ImportBatch]:
        """Load <dataset>.csv for every dataset found in a directory, in fixture order."""
        batches = []
        for dataset in FIXTURE_LOAD_ORDER:
            path = directory / f"{dataset}.csv"
            if not path.exists():
                continue
            batches.append(
                self.import_file(ImportRequest(dataset, path.read_bytes(), file_name=path.name, profile=profile))
            )
        if not batches:
            raise NotFound(f"no dataset CSVs found in {directory}", code="NO_DATASET_FILES")
        return batches

    # ------------------------------------------------------------------ internals

    def _extracted_at(self, supplied: datetime | None) -> datetime:
        now = self._clock.now()
        if supplied is None:
            return now
        if supplied.tzinfo is None:
            raise InvalidRequest("source_extracted_at needs a UTC offset", code="NAIVE_TIMESTAMP")
        if supplied > now:
            raise InvalidRequest(
                "source_extracted_at is later than the business clock",
                code="EXTRACT_TIME_IN_FUTURE",
                details={"source_extracted_at": supplied.isoformat(), "business_now": now.isoformat()},
            )
        return supplied

    def _validate(self, row: CsvRow, spec: DatasetSpec, profile: SourceProfile) -> _ValidatedRow:
        if row.cells is None:
            return _ValidatedRow(
                row,
                None,
                [
                    {
                        "field": None,
                        "code": "COLUMN_COUNT_MISMATCH",
                        "message": f"expected {len(profile.column_map)} cells, found {len(row.raw_cells)}",
                    }
                ],
            )
        try:
            payload = spec.payload_model.model_validate(profile.to_payload_input(row.cells))
        except ValidationError as exc:
            return _ValidatedRow(row, None, [_error(e, profile) for e in exc.errors()])
        return _ValidatedRow(row, payload, key=spec.record_key(payload), hash=content_hash(payload.model_dump()))

    def _load_rows(
        self,
        session: Session,
        batch: ImportBatch,
        spec: DatasetSpec,
        profile: SourceProfile,
        parsed: ParsedCsv,
    ) -> None:
        validated = [self._validate(row, spec, profile) for row in parsed.rows]
        fact_model = spec.fact_model
        keys = {v.key for v in validated if v.key is not None}
        existing: dict[str, SourceFactMixin] = {}
        if keys:
            stmt = (
                select(fact_model)
                .where(
                    fact_model.tenant_id == self._tenant,
                    fact_model.source_system == profile.source_system,
                    fact_model.source_record_key.in_(keys),
                )
                .with_for_update()
            )
            existing = {fact.source_record_key: fact for fact in session.scalars(stmt)}

        first_seen: dict[str, tuple[str, int]] = {}
        counts: Counter[RowOutcome] = Counter()
        for v in validated:
            import_row = ImportRow(
                import_id=batch.id,
                row_number=v.row.row_number,
                # A row with the wrong number of cells keeps the header next to its
                # cells so it can still be read positionally (app/ingestion/rejections.py).
                raw=v.row.cells if v.row.cells is not None else {"_cells": v.row.raw_cells, "_header": parsed.header},
                source_record_key=v.key,
                content_hash=v.hash,
                errors=v.errors,
            )
            if v.payload is None:
                import_row.outcome = RowOutcome.REJECTED
            else:
                assert v.key is not None and v.hash is not None
                prior = first_seen.get(v.key)
                fact = existing.get(v.key)
                if prior is not None and prior[0] == v.hash:
                    import_row.outcome = RowOutcome.DUPLICATE_IN_FILE
                    import_row.detail = f"identical to row {prior[1]} of this file; counted once"
                elif fact is None:
                    fact = fact_model(
                        tenant_id=self._tenant,
                        source_system=profile.source_system,
                        source_record_key=v.key,
                        content_hash=v.hash,
                        first_import_id=batch.id,
                        last_import_id=batch.id,
                        **spec.fact_columns(v.payload),
                    )
                    session.add(fact)
                    existing[v.key] = fact
                    import_row.outcome = RowOutcome.INSERTED
                elif fact.content_hash == v.hash:
                    fact.last_import_id = batch.id
                    import_row.outcome = RowOutcome.UNCHANGED
                    import_row.detail = "identical to the stored record; nothing changed"
                else:
                    conflict = self._record_conflict(session, spec, profile, fact, v, batch)
                    import_row.outcome = RowOutcome.CONFLICT
                    import_row.conflict_id = conflict.id
                    import_row.detail = (
                        "same source key with different content; stored record kept and flagged for review"
                        if conflict.status == ConflictStatus.OPEN
                        else f"{conflict.resolved_by} already rejected this exact change for the stored version "
                        f"{fact.version}; stored record kept"
                    )
                first_seen.setdefault(v.key, (v.hash, v.row.row_number))
            session.add(import_row)
            counts[RowOutcome(import_row.outcome)] += 1

        batch.total_rows = len(validated)
        batch.inserted_rows = counts[RowOutcome.INSERTED]
        batch.unchanged_rows = counts[RowOutcome.UNCHANGED]
        batch.duplicate_rows = counts[RowOutcome.DUPLICATE_IN_FILE]
        batch.rejected_rows = counts[RowOutcome.REJECTED]
        batch.conflict_rows = counts[RowOutcome.CONFLICT]
        batch.warnings = parsed.warnings
        batch.status = (
            ImportStatus.COMPLETED_WITH_ERRORS if batch.rejected_rows or batch.conflict_rows else ImportStatus.COMPLETED
        )
        batch.completed_at = datetime.now(UTC)

    def _record_conflict(
        self,
        session: Session,
        spec: DatasetSpec,
        profile: SourceProfile,
        fact: SourceFactMixin,
        v: _ValidatedRow,
        batch: ImportBatch,
    ) -> SourceConflict:
        """Same open question again -> the existing open conflict. The same change a
        reviewer rejected for this very version of the record -> that decision stands.
        Anything else -> a new open conflict."""
        assert v.payload is not None and v.key is not None and v.hash is not None
        same_change = (
            SourceConflict.tenant_id == self._tenant,
            SourceConflict.dataset == spec.name,
            SourceConflict.source_record_key == v.key,
            SourceConflict.existing_content_hash == fact.content_hash,
            SourceConflict.incoming_content_hash == v.hash,
        )
        conflict = session.scalar(
            select(SourceConflict).where(*same_change, SourceConflict.status == ConflictStatus.OPEN)
        )
        if conflict is None:
            conflict = session.scalar(
                select(SourceConflict)
                .where(
                    *same_change,
                    SourceConflict.resolution == ConflictResolution.KEEP_EXISTING,
                    SourceConflict.decided_fact_version == fact.version,
                )
                .limit(1)
            )
        if conflict is None:
            conflict = SourceConflict(
                id=uuid.uuid4(),
                tenant_id=self._tenant,
                dataset=spec.name,
                source_system=profile.source_system,
                source_record_key=v.key,
                existing_content_hash=fact.content_hash,
                existing_payload=fact_payload_json(spec, fact),
                incoming_content_hash=v.hash,
                incoming_payload=to_jsonable(v.payload.model_dump()),
                import_id=batch.id,
                row_number=v.row.row_number,
                status=ConflictStatus.OPEN,
            )
            session.add(conflict)
            session.flush()  # import_rows.conflict_id references it
        if conflict.status == ConflictStatus.OPEN:
            fact.review_status = ReviewStatus.CONFLICT
        return conflict


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _error(error: dict, profile: SourceProfile) -> dict:
    loc = error.get("loc") or ()
    payload_field = str(loc[0]) if loc else None
    message = str(error.get("msg", "invalid value"))
    if error.get("type") == "missing":
        message = "required value is blank or missing"
    message = message.removeprefix("Value error, ")
    return {
        "field": profile.field_to_column(payload_field) if payload_field else None,
        "code": str(error.get("type", "invalid")),
        "message": message,
    }
