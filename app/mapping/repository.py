"""Load source facts and build the canonical projections used by detection, routing
and the workflow's permission checks."""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AvailabilityFact, EmployeeFact, LocationAliasFact, ProductAliasFact
from app.mapping.canonicalize import canonical_availability, canonical_employees
from app.mapping.mapping_set import MappingSet
from app.mapping.model import CanonicalAvailability, CanonicalEmployee


def load_mapping_set(session: Session, tenant_id: str) -> MappingSet:
    products = session.scalars(select(ProductAliasFact).where(ProductAliasFact.tenant_id == tenant_id))
    locations = session.scalars(select(LocationAliasFact).where(LocationAliasFact.tenant_id == tenant_id))
    return MappingSet.from_facts(list(products), list(locations))


@dataclass(frozen=True)
class OrgDirectory:
    employees: dict[str, CanonicalEmployee]
    availability: dict[str, list[CanonicalAvailability]]
    unknown_employee_samples: list[CanonicalAvailability]
    mapping_version: str


def load_org_directory(session: Session, tenant_id: str, mapping: MappingSet | None = None) -> OrgDirectory:
    mapping = mapping or load_mapping_set(session, tenant_id)
    employees = canonical_employees(
        session.scalars(select(EmployeeFact).where(EmployeeFact.tenant_id == tenant_id)), mapping
    )
    availability: dict[str, list[CanonicalAvailability]] = defaultdict(list)
    unknown: list[CanonicalAvailability] = []
    for fact in session.scalars(select(AvailabilityFact).where(AvailabilityFact.tenant_id == tenant_id)):
        sample = canonical_availability(fact)
        if sample.employee_id in employees:
            availability[sample.employee_id].append(sample)
        else:
            unknown.append(sample)
    return OrgDirectory(employees, dict(availability), unknown, mapping.version)
