"""Project source facts into canonical records. Pure functions: no database access,
so the same inputs always produce the same canonical model."""

from collections.abc import Iterable
from decimal import Decimal

from app.db.models import AvailabilityFact, EmployeeFact, PoLineFact, ReceiptLineFact, SourceFactMixin
from app.enums import ReviewStatus
from app.mapping.mapping_set import MappingSet
from app.mapping.model import (
    CanonicalAvailability,
    CanonicalEmployee,
    CanonicalPoLine,
    CanonicalReceiptLine,
    MappingProblem,
    OrgScope,
    QuantityInBase,
    ResolvedLocation,
    ScopeKind,
    SourceRef,
)
from app.mapping.vocabularies import (
    AVAILABILITY_STATUS,
    EMPLOYMENT_STATUS,
    PO_LINE_STATUS,
    RECEIPT_POSTING_STATUS,
    interpret,
)


def source_ref(dataset: str, fact: SourceFactMixin) -> SourceRef:
    return SourceRef(dataset, fact.source_system, fact.source_record_key, fact.content_hash, fact.version or 1)


def _in_conflict(fact: SourceFactMixin) -> bool:
    return fact.review_status == ReviewStatus.CONFLICT


def canonical_po_line(fact: PoLineFact, mapping: MappingSet) -> CanonicalPoLine:
    problems: list[MappingProblem] = []
    status = interpret(PO_LINE_STATUS, fact.source_system, fact.status_code)
    if status is None:
        problems.append(
            MappingProblem(
                "UNRECOGNIZED_PO_STATUS",
                f"{fact.source_system} PO status '{fact.status_code}' is not in the declared vocabulary",
                {"status_code": fact.status_code},
            )
        )
    quantity = mapping.resolve_quantity(fact.source_system, fact.item_ref, Decimal(fact.ordered_qty), fact.ordered_uom)
    location = mapping.resolve_location(fact.source_system, fact.location_ref)
    if isinstance(quantity, MappingProblem):
        problems.append(quantity)
    if isinstance(location, MappingProblem):
        problems.append(location)
    return CanonicalPoLine(
        ref=source_ref("purchase_order_lines", fact),
        po_number=fact.po_number,
        line_number=fact.line_number,
        supplier_code=fact.supplier_code,
        status_code=fact.status_code,
        status=status,
        deadline=fact.expected_receipt_by,
        item_ref=fact.item_ref,
        location_ref=fact.location_ref,
        ordered_qty=Decimal(fact.ordered_qty),
        ordered_uom=fact.ordered_uom,
        quantity=quantity if isinstance(quantity, QuantityInBase) else None,
        location=location if isinstance(location, ResolvedLocation) else None,
        in_conflict=_in_conflict(fact),
        problems=tuple(problems),
    )


def canonical_receipt_line(fact: ReceiptLineFact, mapping: MappingSet) -> CanonicalReceiptLine:
    problems: list[MappingProblem] = []
    posting = interpret(RECEIPT_POSTING_STATUS, fact.source_system, fact.posting_status)
    if posting is None:
        problems.append(
            MappingProblem(
                "UNRECOGNIZED_POSTING_STATUS",
                f"{fact.source_system} posting status '{fact.posting_status}' is not in the declared vocabulary",
                {"posting_status": fact.posting_status},
            )
        )
    quantity = mapping.resolve_quantity(fact.source_system, fact.product_ref, Decimal(fact.accepted_qty), fact.uom)
    location = mapping.resolve_location(fact.source_system, fact.location_ref)
    if isinstance(quantity, MappingProblem):
        problems.append(quantity)
    if isinstance(location, MappingProblem):
        problems.append(location)
    return CanonicalReceiptLine(
        ref=source_ref("goods_receipt_lines", fact),
        receipt_number=fact.receipt_number,
        receipt_line=fact.receipt_line,
        po_number=fact.po_number,
        po_line_number=fact.po_line_number,
        posting_status_code=fact.posting_status,
        posting=posting,
        received_at=fact.received_at,
        product_ref=fact.product_ref,
        location_ref=fact.location_ref,
        accepted_qty=Decimal(fact.accepted_qty),
        uom=fact.uom,
        quantity=quantity if isinstance(quantity, QuantityInBase) else None,
        location=location if isinstance(location, ResolvedLocation) else None,
        in_conflict=_in_conflict(fact),
        problems=tuple(problems),
    )


def canonical_employee(fact: EmployeeFact, mapping: MappingSet) -> CanonicalEmployee:
    problems: list[MappingProblem] = []
    is_active = interpret(EMPLOYMENT_STATUS, fact.source_system, fact.employment_status)
    if is_active is None:
        problems.append(
            MappingProblem(
                "UNRECOGNIZED_EMPLOYMENT_STATUS",
                f"employment status '{fact.employment_status}' is not in the declared vocabulary; "
                "treated as not eligible",
                {"employment_status": fact.employment_status},
            )
        )

    scope: OrgScope | None = None
    if fact.location_ref:
        # Employee locations are mapped through the same crosswalk as everything
        # else before they can be compared with a task's canonical location.
        resolved = mapping.resolve_location(fact.source_system, fact.location_ref)
        if isinstance(resolved, MappingProblem):
            problems.append(
                MappingProblem(
                    "EMPLOYEE_LOCATION_UNMAPPED",
                    f"employee location {fact.location_ref} could not be mapped ({resolved.code}); "
                    "the employee is not routable to any store",
                    {"location_ref": fact.location_ref, "cause": resolved.code},
                )
            )
        else:
            scope = OrgScope(ScopeKind.STORE, resolved.region_id, resolved.canonical_location_id)
            if fact.region_id and fact.region_id != resolved.region_id:
                problems.append(
                    MappingProblem(
                        "EMPLOYEE_REGION_MISMATCH",
                        f"HRMS region {fact.region_id} differs from the location crosswalk's region "
                        f"{resolved.region_id}; the crosswalk is used",
                        {"hrms_region_id": fact.region_id, "crosswalk_region_id": resolved.region_id},
                    )
                )
    elif fact.region_id:
        scope = OrgScope(ScopeKind.REGION, fact.region_id)
    else:
        problems.append(MappingProblem("EMPLOYEE_UNSCOPED", "employee has neither a location nor a region", {}))

    return CanonicalEmployee(
        employee_id=fact.source_record_key,
        ref=source_ref("employees", fact),
        role=fact.role,
        employment_status_code=fact.employment_status,
        is_active=is_active,
        scope=scope,
        manager_id=fact.manager_ref,
        in_conflict=_in_conflict(fact),
        problems=tuple(problems),
    )


def canonical_availability(fact: AvailabilityFact) -> CanonicalAvailability:
    return CanonicalAvailability(
        employee_id=fact.employee_ref,
        ref=source_ref("workforce_status", fact),
        status_code=fact.availability_status,
        on_shift=interpret(AVAILABILITY_STATUS, fact.source_system, fact.availability_status),
        shift_start=fact.shift_start,
        shift_end=fact.shift_end,
        observed_at=fact.observed_at,
        in_conflict=_in_conflict(fact),
    )


def canonical_employees(facts: Iterable[EmployeeFact], mapping: MappingSet) -> dict[str, CanonicalEmployee]:
    return {e.employee_id: e for e in (canonical_employee(f, mapping) for f in facts)}
