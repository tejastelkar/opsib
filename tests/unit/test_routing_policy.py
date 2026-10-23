"""Employee eligibility and selection, using the fixture roster and shift snapshot."""

from datetime import timedelta

from app.routing.policy import (
    MANUAL_QUEUE,
    manual_assignment_blockers,
    select_assignee,
    shortfall_policy,
    supervises,
)
from tests import factories as f

AS_OF = f.FROZEN_AS_OF
POLICY = shortfall_policy(timedelta(minutes=60))


def decide(location_id, *, employees=None, availability=None, counts=None, as_of=AS_OF):
    return select_assignee(
        location_id=location_id,
        employees=employees or f.employees(),
        availability=availability if availability is not None else f.availability(),
        open_task_counts=counts or {},
        as_of=as_of,
        policy=POLICY,
    )


def availability_with(**changes_by_employee):
    samples = []
    for fact in f.facts("workforce_status"):
        changes = changes_by_employee.get(fact.employee_ref)
        samples.append(f.clone(fact, **changes) if changes else fact)
    return f.availability(samples)


def checks(decision):
    return {c.employee_id: c.reasons for c in decision.candidates}


def test_store_014_goes_to_the_on_shift_active_receiving_associate():
    decision = decide("STORE-014")
    assert (decision.assignee_id, decision.reason_code) == ("HR-102", "PRIMARY_ROLE_AT_LOCATION")
    assert checks(decision) == {"HR-101": ("OFF_SHIFT",), "HR-102": (), "HR-103": ("EMPLOYMENT_NOT_ACTIVE",)}


def test_inactive_employee_stays_ineligible_although_the_shift_export_says_on_shift():
    hr103 = next(c for c in decide("STORE-014").candidates if c.employee_id == "HR-103")
    assert hr103.availability["status"] == "ON_SHIFT"
    assert hr103.eligible is False
    assert hr103.reasons == ("EMPLOYMENT_NOT_ACTIVE",)


def test_store_015_falls_back_to_the_on_shift_store_manager():
    decision = decide("STORE-015")
    assert (decision.assignee_id, decision.reason_code) == ("HR-401", "FALLBACK_STORE_MANAGER")


def test_no_eligible_person_means_the_manual_queue_not_a_best_guess():
    decision = decide("STORE-015", availability=availability_with(**{"HR-401": {"availability_status": "OFF_SHIFT"}}))
    assert (decision.assignee_id, decision.queue, decision.reason_code) == (None, MANUAL_QUEUE, "NO_ELIGIBLE_ASSIGNEE")
    assert checks(decision) == {"HR-401": ("OFF_SHIFT",)}


def test_stale_availability_sample_is_not_trusted():
    stale = AS_OF - timedelta(hours=3)
    decision = decide("STORE-014", availability=availability_with(**{"HR-102": {"observed_at": stale}}))
    assert "AVAILABILITY_STALE" in checks(decision)["HR-102"]
    assert decision.assignee_id == "HR-201"  # store manager fallback


def test_sample_from_after_the_evaluation_time_is_ignored():
    decision = decide("STORE-014", as_of=AS_OF - timedelta(minutes=20))  # samples were observed at 14:50
    assert checks(decision)["HR-102"] == ("NO_AVAILABILITY_SAMPLE",)


def test_on_shift_outside_the_sampled_shift_window_is_not_on_shift():
    early_end = AS_OF - timedelta(minutes=5)
    decision = decide("STORE-014", availability=availability_with(**{"HR-102": {"shift_end": early_end}}))
    assert checks(decision)["HR-102"] == ("OUTSIDE_SHIFT_WINDOW",)


def test_employee_whose_location_cannot_be_mapped_is_never_routed():
    roster = [f.clone(e, location_ref="ST-099") if e.source_record_key == "HR-102" else e for e in f.facts("employees")]
    decision = decide("STORE-014", employees=f.employees(employee_facts=roster))
    assert decision.assignee_id == "HR-201"
    assert "HR-102" not in checks(decision)
    assert decision.unresolved[0]["employee_id"] == "HR-102"


def test_regional_role_is_not_auto_assigned_store_work():
    roster = [e for e in f.facts("employees") if e.source_record_key in ("HR-501",)]
    decision = decide("STORE-014", employees=f.employees(employee_facts=roster))
    assert decision.queue == MANUAL_QUEUE


def test_ties_go_to_the_least_loaded_then_lowest_id():
    availability = availability_with(**{"HR-101": {"availability_status": "ON_SHIFT"}})
    assert decide("STORE-014", availability=availability).assignee_id == "HR-101"
    assert decide("STORE-014", availability=availability, counts={"HR-101": 2, "HR-102": 1}).assignee_id == "HR-102"


def test_supervision_scope():
    employees = f.employees()

    def can(actor, location):
        region = "REGION-MH"
        return supervises(
            employees[actor], location_id=location, region_id=region, assignee_id=None, employees=employees
        )

    assert can("HR-201", "STORE-014") and not can("HR-201", "STORE-015")
    assert can("HR-401", "STORE-015") and not can("HR-401", "STORE-014")
    assert can("HR-501", "STORE-014") and can("HR-501", "STORE-015")  # region-scoped
    assert not can("HR-102", "STORE-014")


def test_manual_assignment_never_to_inactive_or_out_of_scope_people():
    employees = f.employees()
    assert manual_assignment_blockers(employees["HR-103"], location_id="STORE-014", region_id="REGION-MH") == [
        "EMPLOYMENT_NOT_ACTIVE"
    ]
    assert manual_assignment_blockers(employees["HR-401"], location_id="STORE-014", region_id="REGION-MH") == [
        "OUT_OF_SCOPE_FOR_LOCATION"
    ]
    assert manual_assignment_blockers(employees["HR-101"], location_id="STORE-014", region_id="REGION-MH") == []
