"""Datasets, their typed payloads, and the source profiles that feed them.

A *dataset* is a business concept (purchase-order lines, receipt lines, ...)
with one typed payload model whose field names are the columns of its fact
table. A *profile* is one source's export format for that dataset: which
system produced it and how its column names map onto the payload. Adding
another retailer's export means registering a profile; the payload, the facts
and the detector stay as they are.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC

from pydantic import BaseModel, ConfigDict, model_validator

from app.db.models import (
    AvailabilityFact,
    EmployeeFact,
    LocationAliasFact,
    PoLineFact,
    ProductAliasFact,
    ReceiptLineFact,
    SourceFactMixin,
)
from app.errors import InvalidRequest, NotFound
from app.ingestion.fields import (
    AwareDatetime,
    Code,
    ConversionFactor,
    DisplayName,
    Identifier,
    LineNumber,
    NonNegativeQuantity,
    PositiveQuantity,
    RoleCode,
)


class FactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PoLinePayload(FactPayload):
    source_record_key: Identifier
    po_number: Identifier
    line_number: LineNumber
    supplier_code: Identifier
    location_ref: Identifier
    item_ref: Identifier
    ordered_qty: PositiveQuantity
    ordered_uom: Code
    status_code: Code
    expected_receipt_by: AwareDatetime


class ReceiptLinePayload(FactPayload):
    source_record_key: Identifier
    receipt_number: Identifier
    receipt_line: LineNumber
    po_number: Identifier
    po_line_number: LineNumber
    location_ref: Identifier
    product_ref: Identifier
    accepted_qty: NonNegativeQuantity
    uom: Code
    received_at: AwareDatetime
    posting_status: Code


class EmployeePayload(FactPayload):
    source_record_key: Identifier
    display_name: DisplayName | None = None
    employment_status: Code
    role: RoleCode
    # Optional on purpose: regional roles are scoped to a region, not a store.
    location_ref: Identifier | None = None
    manager_ref: Identifier | None = None
    region_id: Identifier | None = None


class AvailabilityPayload(FactPayload):
    employee_ref: Identifier
    availability_status: Code
    shift_start: AwareDatetime
    shift_end: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _shift_window_ordered(self) -> "AvailabilityPayload":
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self


class ProductAliasPayload(FactPayload):
    alias_system: Code
    external_item_id: Identifier
    canonical_product_id: Identifier
    source_uom: Code
    base_uom: Code
    to_base_factor: ConversionFactor

    @model_validator(mode="after")
    def _identity_conversion_is_one(self) -> "ProductAliasPayload":
        if self.source_uom == self.base_uom and self.to_base_factor != 1:
            raise ValueError("source_uom equals base_uom, so to_base_factor must be 1")
        return self


class LocationAliasPayload(FactPayload):
    alias_system: Code
    external_location_id: Identifier
    canonical_location_id: Identifier
    region_id: Identifier


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    fact_model: type[SourceFactMixin]
    payload_model: type[FactPayload]
    record_key: Callable[[FactPayload], str]
    description: str

    def fact_columns(self, payload: FactPayload) -> dict[str, object]:
        """Payload fields as fact-table column values (source_record_key is set separately)."""
        values = payload.model_dump()
        values.pop("source_record_key", None)
        return values

    @property
    def payload_fields(self) -> tuple[str, ...]:
        return tuple(self.payload_model.model_fields)


def _own_key(payload: FactPayload) -> str:
    return payload.source_record_key  # type: ignore[attr-defined]


def _availability_key(payload: FactPayload) -> str:
    assert isinstance(payload, AvailabilityPayload)
    return f"{payload.employee_ref}@{payload.observed_at.astimezone(UTC).isoformat()}"


def _product_alias_key(payload: FactPayload) -> str:
    assert isinstance(payload, ProductAliasPayload)
    return f"{payload.alias_system}:{payload.external_item_id}"


def _location_alias_key(payload: FactPayload) -> str:
    assert isinstance(payload, LocationAliasPayload)
    return f"{payload.alias_system}:{payload.external_location_id}"


DATASETS: dict[str, DatasetSpec] = {
    spec.name: spec
    for spec in (
        DatasetSpec("purchase_order_lines", PoLineFact, PoLinePayload, _own_key, "ERP purchase-order lines"),
        DatasetSpec("goods_receipt_lines", ReceiptLineFact, ReceiptLinePayload, _own_key, "WMS goods-receipt lines"),
        DatasetSpec("employees", EmployeeFact, EmployeePayload, _own_key, "HRMS employee roster"),
        DatasetSpec(
            "workforce_status",
            AvailabilityFact,
            AvailabilityPayload,
            _availability_key,
            "Workforce shift/availability samples; one record per employee per observation",
        ),
        DatasetSpec(
            "product_aliases",
            ProductAliasFact,
            ProductAliasPayload,
            _product_alias_key,
            "Product crosswalk and unit conversion; one record per (system, item ID)",
        ),
        DatasetSpec(
            "location_aliases",
            LocationAliasFact,
            LocationAliasPayload,
            _location_alias_key,
            "Location crosswalk; one record per (system, location code)",
        ),
    )
}


@dataclass(frozen=True)
class SourceProfile:
    dataset: str
    name: str
    source_system: str  # the system that produced the file
    column_map: Mapping[str, str]  # file column -> payload field
    description: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def required_columns(self) -> frozenset[str]:
        return frozenset(self.column_map)

    def field_to_column(self, payload_field: str) -> str:
        for column, mapped in self.column_map.items():
            if mapped == payload_field:
                return column
        return payload_field

    def to_payload_input(self, row: Mapping[str, str]) -> dict[str, str]:
        """Blank cells are left out, so a blank required field fails as 'missing'
        and a blank optional field becomes None."""
        data: dict[str, str] = {}
        for column, payload_field in self.column_map.items():
            cell = (row.get(column) or "").strip()
            if cell:
                data[payload_field] = cell
        return data


def _identity(*columns: str, renames: Mapping[str, str] | None = None) -> dict[str, str]:
    renames = renames or {}
    return {column: renames.get(column, column) for column in columns}


DEFAULT_PROFILE = "opsib_v1"

OPSIB_V1_PROFILES: tuple[SourceProfile, ...] = (
    SourceProfile(
        "purchase_order_lines",
        DEFAULT_PROFILE,
        "ERP",
        _identity(
            "source_record_id",
            "po_number",
            "line_number",
            "supplier_code",
            "location_code",
            "item_code",
            "ordered_qty",
            "ordered_uom",
            "status",
            "expected_receipt_by",
            renames={
                "source_record_id": "source_record_key",
                "location_code": "location_ref",
                "item_code": "item_ref",
                "status": "status_code",
            },
        ),
    ),
    SourceProfile(
        "goods_receipt_lines",
        DEFAULT_PROFILE,
        "WMS",
        _identity(
            "source_record_id",
            "receipt_number",
            "receipt_line",
            "po_number",
            "po_line_number",
            "location_code",
            "product_ref",
            "accepted_qty",
            "uom",
            "received_at",
            "posting_status",
            renames={"source_record_id": "source_record_key", "location_code": "location_ref"},
        ),
    ),
    SourceProfile(
        "employees",
        DEFAULT_PROFILE,
        "HRMS",
        _identity(
            "source_employee_id",
            "display_name",
            "employment_status",
            "role",
            "location_code",
            "manager_employee_id",
            "region_id",
            renames={
                "source_employee_id": "source_record_key",
                "location_code": "location_ref",
                "manager_employee_id": "manager_ref",
            },
        ),
    ),
    SourceProfile(
        "workforce_status",
        DEFAULT_PROFILE,
        "WFM",
        _identity(
            "source_employee_id",
            "availability_status",
            "shift_start",
            "shift_end",
            "observed_at",
            renames={"source_employee_id": "employee_ref"},
        ),
    ),
    SourceProfile(
        "product_aliases",
        DEFAULT_PROFILE,
        "MDM",
        _identity(
            "source_system",
            "external_item_id",
            "canonical_product_id",
            "source_uom",
            "base_uom",
            "to_base_factor",
            renames={"source_system": "alias_system"},
        ),
    ),
    SourceProfile(
        "location_aliases",
        DEFAULT_PROFILE,
        "MDM",
        _identity(
            "source_system",
            "external_location_id",
            "canonical_location_id",
            "region_id",
            renames={"source_system": "alias_system"},
        ),
    ),
)


class DatasetRegistry:
    def __init__(self, profiles: Iterable[SourceProfile] = OPSIB_V1_PROFILES):
        self._profiles: dict[tuple[str, str], SourceProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: SourceProfile) -> None:
        spec = self.dataset(profile.dataset)
        unknown = set(profile.column_map.values()) - set(spec.payload_fields)
        if unknown:
            raise ValueError(f"profile {profile.name} maps to unknown fields {sorted(unknown)}")
        self._profiles[(profile.dataset, profile.name)] = profile

    def dataset(self, name: str) -> DatasetSpec:
        try:
            return DATASETS[name]
        except KeyError:
            raise NotFound(
                f"unknown dataset '{name}'", code="UNKNOWN_DATASET", details={"known": sorted(DATASETS)}
            ) from None

    def profile(self, dataset: str, name: str) -> SourceProfile:
        self.dataset(dataset)
        try:
            return self._profiles[(dataset, name)]
        except KeyError:
            known = sorted(p for d, p in self._profiles if d == dataset)
            raise InvalidRequest(
                f"unknown profile '{name}' for dataset '{dataset}'", code="UNKNOWN_PROFILE", details={"known": known}
            ) from None


# Fixture import order: reference data first is convenient but not required,
# because mapping happens at reconciliation time, not at import time.
FIXTURE_LOAD_ORDER = (
    "product_aliases",
    "location_aliases",
    "employees",
    "workforce_status",
    "purchase_order_lines",
    "goods_receipt_lines",
)
