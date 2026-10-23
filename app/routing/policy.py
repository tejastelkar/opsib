"""Who should investigate a shortfall. Pure functions over the canonical org model.

Safety invariants
* HRMS employment status is checked first and nothing overrides it: a
  workforce export saying ON_SHIFT never makes an INACTIVE employee eligible.
* Locations are compared as canonical IDs, after mapping the HRMS location
  code through the crosswalk. An employee whose location cannot be mapped is
  never routed to a store.
* On-shift means: the latest sample at or before the evaluation time says
  ON_SHIFT, it is no older than the freshness limit, and the evaluation time
  falls inside the sampled shift window.
* If no tier yields an eligible person, the task goes to the manual-assignment
  queue. Nobody is assigned "as a best effort".
* Every candidate considered is recorded with the reasons they were or were
  not eligible, so a reviewer can see why a person or queue was chosen.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.mapping.model import CanonicalAvailability, CanonicalEmployee, ScopeKind

TASK_TYPE = "INVESTIGATE_RECEIPT_SHORTFALL"
MANUAL_QUEUE = "MANUAL_ASSIGNMENT"
NO_ELIGIBLE_ASSIGNEE = "NO_ELIGIBLE_ASSIGNEE"

# Roles a supervisor may hand this task to manually, and the roles that supervise it.
ASSIGNABLE_ROLES = frozenset({"receiving_associate", "store_manager", "regional_operations"})
STORE_SUPERVISOR_ROLE = "store_manager"
REGIONAL_SUPERVISOR_ROLE = "regional_operations"


@dataclass(frozen=True)
class Tier:
    role: str
    reason_code: str


@dataclass(frozen=True)
class RoutingPolicy:
    version: str
    tiers: tuple[Tier, ...]
    availability_max_age: timedelta
    require_on_shift: bool = True


def shortfall_policy(availability_max_age: timedelta) -> RoutingPolicy:
    return RoutingPolicy(
        version="shortfall_routing/1.0.0",
        tiers=(
            Tier("receiving_associate", "PRIMARY_ROLE_AT_LOCATION"),
            Tier("store_manager", "FALLBACK_STORE_MANAGER"),
        ),
        availability_max_age=availability_max_age,
    )


@dataclass(frozen=True)
class CandidateCheck:
    employee_id: str
    role: str
    tier: str
    eligible: bool
    reasons: tuple[str, ...]
    availability: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "role": self.role,
            "tier": self.tier,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "availability": dict(self.availability) if self.availability else None,
        }


@dataclass(frozen=True)
class RoutingDecision:
    location_id: str
    evaluated_as_of: datetime
    policy_version: str
    reason_code: str
    assignee_id: str | None = None
    queue: str | None = None
    candidates: tuple[CandidateCheck, ...] = ()
    unresolved: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "evaluated_as_of": self.evaluated_as_of.isoformat(),
            "policy_version": self.policy_version,
            "reason_code": self.reason_code,
            "assignee_id": self.assignee_id,
            "queue": self.queue,
            "candidates": [c.as_dict() for c in self.candidates],
            "employees_with_unmapped_location": [dict(u) for u in self.unresolved],
        }


def latest_sample(samples: Iterable[CanonicalAvailability], as_of: datetime) -> CanonicalAvailability | None:
    known = [s for s in samples if s.observed_at <= as_of]
    return max(known, key=lambda s: (s.observed_at, s.ref.source_record_key), default=None)


def employment_blockers(employee: CanonicalEmployee) -> list[str]:
    reasons = []
    if employee.in_conflict:
        reasons.append("EMPLOYEE_RECORD_IN_CONFLICT")
    if employee.is_active is None:
        reasons.append("EMPLOYMENT_STATUS_UNRECOGNIZED")
    elif not employee.is_active:
        reasons.append("EMPLOYMENT_NOT_ACTIVE")
    return reasons


def availability_blockers(sample: CanonicalAvailability | None, as_of: datetime, max_age: timedelta) -> list[str]:
    if sample is None:
        return ["NO_AVAILABILITY_SAMPLE"]
    reasons = []
    if sample.in_conflict:
        reasons.append("AVAILABILITY_RECORD_IN_CONFLICT")
    if as_of - sample.observed_at > max_age:
        reasons.append("AVAILABILITY_STALE")
    if sample.on_shift is None:
        reasons.append("AVAILABILITY_STATUS_UNRECOGNIZED")
    elif not sample.on_shift:
        reasons.append("OFF_SHIFT")
    elif not (sample.shift_start <= as_of < sample.shift_end):
        reasons.append("OUTSIDE_SHIFT_WINDOW")
    return reasons


def _sample_summary(sample: CanonicalAvailability | None, as_of: datetime) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "source_record_key": sample.ref.source_record_key,
        "status": sample.status_code,
        "observed_at": sample.observed_at.isoformat(),
        "age_minutes": round((as_of - sample.observed_at).total_seconds() / 60, 1),
        "shift_start": sample.shift_start.isoformat(),
        "shift_end": sample.shift_end.isoformat(),
    }


def select_assignee(
    *,
    location_id: str,
    employees: Mapping[str, CanonicalEmployee],
    availability: Mapping[str, list[CanonicalAvailability]],
    open_task_counts: Mapping[str, int],
    as_of: datetime,
    policy: RoutingPolicy,
) -> RoutingDecision:
    tier_roles = {tier.role for tier in policy.tiers}
    unresolved = tuple(
        {"employee_id": e.employee_id, "role": e.role, "reason": "LOCATION_UNMAPPED_OR_UNSCOPED"}
        for e in sorted(employees.values(), key=lambda e: e.employee_id)
        if e.role in tier_roles and e.scope is None
    )
    checks: list[CandidateCheck] = []
    for tier in policy.tiers:
        pool = sorted(
            (
                e
                for e in employees.values()
                if e.role == tier.role
                and e.scope is not None
                and e.scope.kind == ScopeKind.STORE
                and e.scope.location_id == location_id
            ),
            key=lambda e: e.employee_id,
        )
        eligible: list[CanonicalEmployee] = []
        for employee in pool:
            sample = latest_sample(availability.get(employee.employee_id, []), as_of)
            reasons = employment_blockers(employee)
            if policy.require_on_shift:
                reasons += availability_blockers(sample, as_of, policy.availability_max_age)
            checks.append(
                CandidateCheck(
                    employee.employee_id,
                    employee.role,
                    tier.reason_code,
                    not reasons,
                    tuple(reasons),
                    _sample_summary(sample, as_of),
                )
            )
            if not reasons:
                eligible.append(employee)
        if eligible:
            # Deterministic: least loaded first, then employee ID.
            chosen = min(eligible, key=lambda e: (open_task_counts.get(e.employee_id, 0), e.employee_id))
            return RoutingDecision(
                location_id,
                as_of,
                policy.version,
                tier.reason_code,
                chosen.employee_id,
                None,
                tuple(checks),
                unresolved,
            )
    return RoutingDecision(
        location_id, as_of, policy.version, NO_ELIGIBLE_ASSIGNEE, None, MANUAL_QUEUE, tuple(checks), unresolved
    )


def supervises(
    actor: CanonicalEmployee,
    *,
    location_id: str,
    region_id: str | None,
    assignee_id: str | None,
    employees: Mapping[str, CanonicalEmployee],
) -> bool:
    """May this actor reassign or verify work at this location?"""
    if employment_blockers(actor) or actor.scope is None:
        return False
    if (
        actor.role == STORE_SUPERVISOR_ROLE
        and actor.scope.kind == ScopeKind.STORE
        and actor.scope.location_id == location_id
    ):
        return True
    if actor.role == REGIONAL_SUPERVISOR_ROLE and actor.scope.covers_location(location_id, region_id):
        return True
    assignee = employees.get(assignee_id) if assignee_id else None
    return assignee is not None and assignee.manager_id == actor.employee_id


def manual_assignment_blockers(employee: CanonicalEmployee, *, location_id: str, region_id: str | None) -> list[str]:
    """A supervisor may hand the task to someone currently off shift (they may be
    picking it up next shift), but never to someone inactive or out of scope."""
    reasons = employment_blockers(employee)
    if employee.role not in ASSIGNABLE_ROLES:
        reasons.append("ROLE_NOT_ASSIGNABLE")
    if employee.scope is None:
        reasons.append("LOCATION_UNMAPPED_OR_UNSCOPED")
    elif not employee.scope.covers_location(location_id, region_id):
        reasons.append("OUT_OF_SCOPE_FOR_LOCATION")
    return reasons
