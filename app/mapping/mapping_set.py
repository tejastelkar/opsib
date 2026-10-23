"""Identity crosswalks and unit conversion.

Identity rules
* A source ID maps to a canonical ID only through an alias record for that exact
  (source system, source ID). Nothing is joined on names, descriptions or
  similar-looking codes.
* An alias record under an open conflict is ambiguous and is not used.
* If two aliases for one canonical product disagree on its base unit (or two
  aliases for one location disagree on its region), every mapping to it is
  refused: the crosswalk itself is inconsistent.

Unit rule
* A quantity in the alias's declared source UOM is multiplied by its factor.
* A quantity already in the product's base UOM converts with factor 1.
* Any other unit is not convertible. There is no implicit conversion.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.db.models import LocationAliasFact, ProductAliasFact
from app.enums import ReviewStatus
from app.hashing import content_hash
from app.mapping.model import MappingProblem, MappingRef, QuantityInBase, ResolvedLocation

PRODUCT_ALIAS_DATASET = "product_aliases"
LOCATION_ALIAS_DATASET = "location_aliases"


@dataclass(frozen=True)
class ProductAlias:
    alias_system: str
    external_item_id: str
    canonical_product_id: str
    source_uom: str
    base_uom: str
    factor: Decimal
    ref: MappingRef
    in_conflict: bool


@dataclass(frozen=True)
class LocationAlias:
    alias_system: str
    external_location_id: str
    canonical_location_id: str
    region_id: str
    ref: MappingRef
    in_conflict: bool


class MappingSet:
    def __init__(self, products: Iterable[ProductAlias], locations: Iterable[LocationAlias]):
        self._products = {(a.alias_system, a.external_item_id): a for a in products}
        self._locations = {(a.alias_system, a.external_location_id): a for a in locations}

        base_uoms: dict[str, set[str]] = defaultdict(set)
        for alias in self._products.values():
            if not alias.in_conflict:
                base_uoms[alias.canonical_product_id].add(alias.base_uom)
        self._inconsistent_products = {pid: sorted(u) for pid, u in base_uoms.items() if len(u) > 1}

        regions: dict[str, set[str]] = defaultdict(set)
        for alias in self._locations.values():
            if not alias.in_conflict:
                regions[alias.canonical_location_id].add(alias.region_id)
        self._inconsistent_locations = {lid: sorted(r) for lid, r in regions.items() if len(r) > 1}

        fingerprint = sorted(
            [
                (a.ref.dataset, a.ref.source_record_key, a.ref.content_hash, a.in_conflict)
                for a in self._products.values()
            ]
            + [
                (a.ref.dataset, a.ref.source_record_key, a.ref.content_hash, a.in_conflict)
                for a in self._locations.values()
            ]
        )
        self.version = "map-" + content_hash(fingerprint)[:12]

    @classmethod
    def from_facts(
        cls, product_facts: Iterable[ProductAliasFact], location_facts: Iterable[LocationAliasFact]
    ) -> "MappingSet":
        return cls(
            products=[
                ProductAlias(
                    alias_system=f.alias_system,
                    external_item_id=f.external_item_id,
                    canonical_product_id=f.canonical_product_id,
                    source_uom=f.source_uom,
                    base_uom=f.base_uom,
                    factor=Decimal(f.to_base_factor),
                    ref=MappingRef(PRODUCT_ALIAS_DATASET, f.source_record_key, f.content_hash),
                    in_conflict=f.review_status == ReviewStatus.CONFLICT,
                )
                for f in product_facts
            ],
            locations=[
                LocationAlias(
                    alias_system=f.alias_system,
                    external_location_id=f.external_location_id,
                    canonical_location_id=f.canonical_location_id,
                    region_id=f.region_id,
                    ref=MappingRef(LOCATION_ALIAS_DATASET, f.source_record_key, f.content_hash),
                    in_conflict=f.review_status == ReviewStatus.CONFLICT,
                )
                for f in location_facts
            ],
        )

    @property
    def is_empty(self) -> bool:
        return not self._products and not self._locations

    def canonical_products(self) -> dict[str, str]:
        """canonical_product_id -> base_uom, for consistent products only."""
        out: dict[str, str] = {}
        for alias in self._products.values():
            if not alias.in_conflict and alias.canonical_product_id not in self._inconsistent_products:
                out[alias.canonical_product_id] = alias.base_uom
        return out

    def canonical_locations(self) -> dict[str, str]:
        """canonical_location_id -> region_id, for consistent locations only."""
        out: dict[str, str] = {}
        for alias in self._locations.values():
            if not alias.in_conflict and alias.canonical_location_id not in self._inconsistent_locations:
                out[alias.canonical_location_id] = alias.region_id
        return out

    def crosswalk_problems(self) -> list[tuple[str, MappingProblem]]:
        """Problems in the crosswalk itself, as (canonical id, problem)."""
        problems = [
            (
                pid,
                MappingProblem(
                    "PRODUCT_BASE_UOM_INCONSISTENT",
                    f"aliases for {pid} declare different base units",
                    {"canonical_product_id": pid, "base_uoms": uoms},
                ),
            )
            for pid, uoms in sorted(self._inconsistent_products.items())
        ]
        problems += [
            (
                lid,
                MappingProblem(
                    "LOCATION_REGION_INCONSISTENT",
                    f"aliases for {lid} declare different regions",
                    {"canonical_location_id": lid, "region_ids": regions},
                ),
            )
            for lid, regions in sorted(self._inconsistent_locations.items())
        ]
        return problems

    def resolve_quantity(
        self, source_system: str, item_ref: str, qty: Decimal, uom: str
    ) -> QuantityInBase | MappingProblem:
        alias = self._products.get((source_system, item_ref))
        where = {"source_system": source_system, "item_ref": item_ref}
        if alias is None:
            return MappingProblem("PRODUCT_UNMAPPED", f"no product alias for {source_system} item {item_ref}", where)
        if alias.in_conflict:
            return MappingProblem(
                "PRODUCT_MAPPING_IN_CONFLICT",
                f"the product alias for {source_system} item {item_ref} has an unresolved conflict",
                {**where, "alias": alias.ref.source_record_key},
            )
        if alias.canonical_product_id in self._inconsistent_products:
            return MappingProblem(
                "PRODUCT_BASE_UOM_INCONSISTENT",
                f"aliases for {alias.canonical_product_id} declare different base units",
                {**where, "base_uoms": self._inconsistent_products[alias.canonical_product_id]},
            )
        if uom == alias.source_uom:
            factor = alias.factor
        elif uom == alias.base_uom:
            factor = Decimal(1)
        else:
            return MappingProblem(
                "UOM_NOT_CONVERTIBLE",
                f"{uom} is neither the alias unit {alias.source_uom} nor the base unit {alias.base_uom}",
                {**where, "uom": uom, "alias_source_uom": alias.source_uom, "base_uom": alias.base_uom},
            )
        return QuantityInBase(
            canonical_product_id=alias.canonical_product_id,
            base_uom=alias.base_uom,
            source_qty=qty,
            source_uom=uom,
            factor=factor,
            base_qty=qty * factor,
            alias=alias.ref,
        )

    def resolve_location(self, source_system: str, location_ref: str) -> ResolvedLocation | MappingProblem:
        alias = self._locations.get((source_system, location_ref))
        where = {"source_system": source_system, "location_ref": location_ref}
        if alias is None:
            return MappingProblem(
                "LOCATION_UNMAPPED", f"no location alias for {source_system} location {location_ref}", where
            )
        if alias.in_conflict:
            return MappingProblem(
                "LOCATION_MAPPING_IN_CONFLICT",
                f"the location alias for {source_system} location {location_ref} has an unresolved conflict",
                {**where, "alias": alias.ref.source_record_key},
            )
        if alias.canonical_location_id in self._inconsistent_locations:
            return MappingProblem(
                "LOCATION_REGION_INCONSISTENT",
                f"aliases for {alias.canonical_location_id} declare different regions",
                {**where, "region_ids": self._inconsistent_locations[alias.canonical_location_id]},
            )
        return ResolvedLocation(alias.canonical_location_id, alias.region_id, alias.ref)
