"""Build transient (unsaved) fact objects from the fixture CSVs using the production
parsing code, so unit tests exercise the real data without a database."""

from datetime import datetime
from typing import Any

from app.enums import ReviewStatus
from app.hashing import content_hash
from app.ingestion.csv_reader import parse_csv
from app.ingestion.datasets import DEFAULT_PROFILE, DatasetRegistry
from app.mapping.canonicalize import (
    canonical_availability,
    canonical_employees,
    canonical_po_line,
    canonical_receipt_line,
)
from app.mapping.mapping_set import MappingSet
from app.mapping.model import CanonicalAvailability, CanonicalEmployee, CanonicalPoLine, CanonicalReceiptLine
from tests.helpers import fixture_bytes

REGISTRY = DatasetRegistry()
FROZEN_AS_OF = datetime.fromisoformat("2026-10-16T15:00:00+05:30")


def facts(dataset: str, content: bytes | None = None) -> list[Any]:
    spec = REGISTRY.dataset(dataset)
    profile = REGISTRY.profile(dataset, DEFAULT_PROFILE)
    parsed = parse_csv(content if content is not None else fixture_bytes(dataset), profile.required_columns)
    out: dict[str, Any] = {}
    for row in parsed.rows:
        assert row.cells is not None
        payload = spec.payload_model.model_validate(profile.to_payload_input(row.cells))
        key = spec.record_key(payload)
        if key in out:
            continue  # identical repeat, as ingestion would collapse it
        out[key] = spec.fact_model(
            tenant_id="t",
            source_system=profile.source_system,
            source_record_key=key,
            content_hash=content_hash(payload.model_dump()),
            version=1,
            review_status=ReviewStatus.OK.value,
            **spec.fact_columns(payload),
        )
    return list(out.values())


def by_key(dataset: str) -> dict[str, Any]:
    return {f.source_record_key: f for f in facts(dataset)}


def clone(fact: Any, **changes: Any) -> Any:
    columns = {
        c.key: getattr(fact, c.key) for c in fact.__table__.columns if c.key not in ("id", "created_at", "updated_at")
    }
    columns.update(changes)
    return type(fact)(**columns)


def mapping(product_facts: list[Any] | None = None, location_facts: list[Any] | None = None) -> MappingSet:
    return MappingSet.from_facts(
        product_facts if product_facts is not None else facts("product_aliases"),
        location_facts if location_facts is not None else facts("location_aliases"),
    )


def po_line(key: str, mapping_set: MappingSet | None = None, **changes: Any) -> CanonicalPoLine:
    fact = clone(by_key("purchase_order_lines")[key], **changes)
    return canonical_po_line(fact, mapping_set or mapping())


def receipt(key: str, mapping_set: MappingSet | None = None, **changes: Any) -> CanonicalReceiptLine:
    fact = clone(by_key("goods_receipt_lines")[key], **changes)
    return canonical_receipt_line(fact, mapping_set or mapping())


def receipts_for(po_number: str, mapping_set: MappingSet | None = None) -> list[CanonicalReceiptLine]:
    m = mapping_set or mapping()
    return [canonical_receipt_line(f, m) for f in facts("goods_receipt_lines") if f.po_number == po_number]


def employees(
    mapping_set: MappingSet | None = None, employee_facts: list[Any] | None = None
) -> dict[str, CanonicalEmployee]:
    return canonical_employees(
        employee_facts if employee_facts is not None else facts("employees"), mapping_set or mapping()
    )


def availability(availability_facts: list[Any] | None = None) -> dict[str, list[CanonicalAvailability]]:
    out: dict[str, list[CanonicalAvailability]] = {}
    for fact in availability_facts if availability_facts is not None else facts("workforce_status"):
        sample = canonical_availability(fact)
        out.setdefault(sample.employee_id, []).append(sample)
    return out
