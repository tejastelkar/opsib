"""The canonical model: source facts expressed in OPSiB identities and base units,
each carrying the lineage needed to explain any decision made from it."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.hashing import to_jsonable
from app.mapping.vocabularies import PoLineStatus, PostingClass


@dataclass(frozen=True)
class SourceRef:
    """Points at one source fact (and through it, at every import row that carried it)."""

    dataset: str
    source_system: str
    source_record_key: str
    content_hash: str
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source_system": self.source_system,
            "source_record_key": self.source_record_key,
            "content_hash": self.content_hash,
            "version": self.version,
        }


@dataclass(frozen=True)
class MappingRef:
    """The alias record a mapping decision used."""

    dataset: str
    source_record_key: str
    content_hash: str

    def as_dict(self) -> dict[str, str]:
        return {"dataset": self.dataset, "source_record_key": self.source_record_key, "content_hash": self.content_hash}


@dataclass(frozen=True)
class MappingProblem:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": to_jsonable(dict(self.details))}


@dataclass(frozen=True)
class QuantityInBase:
    canonical_product_id: str
    base_uom: str
    source_qty: Decimal
    source_uom: str
    factor: Decimal
    base_qty: Decimal
    alias: MappingRef


@dataclass(frozen=True)
class ResolvedLocation:
    canonical_location_id: str
    region_id: str
    alias: MappingRef


@dataclass(frozen=True)
class CanonicalPoLine:
    ref: SourceRef
    po_number: str
    line_number: int
    supplier_code: str
    status_code: str
    status: PoLineStatus | None  # None: code not in the source's vocabulary
    deadline: datetime
    item_ref: str
    location_ref: str
    ordered_qty: Decimal
    ordered_uom: str
    quantity: QuantityInBase | None
    location: ResolvedLocation | None
    in_conflict: bool
    problems: tuple[MappingProblem, ...] = ()

    @property
    def business_key(self) -> tuple[str, str, int]:
        return (self.ref.source_system, self.po_number, self.line_number)

    @property
    def mappings_used(self) -> tuple[MappingRef, ...]:
        refs = []
        if self.quantity:
            refs.append(self.quantity.alias)
        if self.location:
            refs.append(self.location.alias)
        return tuple(refs)


@dataclass(frozen=True)
class CanonicalReceiptLine:
    ref: SourceRef
    receipt_number: str
    receipt_line: int
    po_number: str
    po_line_number: int
    posting_status_code: str
    posting: PostingClass | None
    received_at: datetime
    product_ref: str
    location_ref: str
    accepted_qty: Decimal
    uom: str
    quantity: QuantityInBase | None
    location: ResolvedLocation | None
    in_conflict: bool
    problems: tuple[MappingProblem, ...] = ()

    @property
    def business_key(self) -> tuple[str, str, int]:
        return (self.ref.source_system, self.receipt_number, self.receipt_line)

    @property
    def mappings_used(self) -> tuple[MappingRef, ...]:
        refs = []
        if self.quantity:
            refs.append(self.quantity.alias)
        if self.location:
            refs.append(self.location.alias)
        return tuple(refs)


class ScopeKind(StrEnum):
    STORE = "STORE"
    REGION = "REGION"


@dataclass(frozen=True)
class OrgScope:
    kind: ScopeKind
    region_id: str
    location_id: str | None = None  # set for STORE scope only

    def covers_location(self, location_id: str, region_id: str | None) -> bool:
        if self.kind == ScopeKind.STORE:
            return self.location_id == location_id
        return region_id is not None and self.region_id == region_id

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "location_id": self.location_id, "region_id": self.region_id}


@dataclass(frozen=True)
class CanonicalEmployee:
    employee_id: str
    ref: SourceRef
    role: str
    employment_status_code: str
    is_active: bool | None  # None: status code not in the HRMS vocabulary
    scope: OrgScope | None  # None: location could not be mapped, or no scope at all
    manager_id: str | None
    in_conflict: bool
    problems: tuple[MappingProblem, ...] = ()


@dataclass(frozen=True)
class CanonicalAvailability:
    employee_id: str
    ref: SourceRef
    status_code: str
    on_shift: bool | None  # None: status code not in the WFM vocabulary
    shift_start: datetime
    shift_end: datetime
    observed_at: datetime
    in_conflict: bool
