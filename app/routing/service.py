"""Make sure every open exception has exactly one active investigation task, keep
existing tasks in step with their exception and the roster, and route
unassigned tasks.

Runs in the caller's transaction (holding the per-tenant reconciliation lock).
Open exceptions are locked FOR UPDATE first (the same exception-then-task order
the workflow uses), so a verification committing concurrently either finishes
first, and the exception is no longer OPEN when re-checked, or waits. Task
creation uses INSERT ... ON CONFLICT DO NOTHING against the partial unique
index uq_tasks_one_active_per_exception_type as the backstop.

A task whose assignee is no longer eligible (left, moved store, dropped from the
roster) is released back to CREATED and re-routed; being off shift is not a
reason to take work away.
"""

import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataquality import IssueDraft, upsert_issues
from app.db.models import ACTIVE_TASK_PREDICATE, ExceptionCase, ReconciliationRun, Task
from app.enums import ACTIVE_TASK_STATUSES, ExceptionStatus, TaskStatus
from app.mapping.repository import OrgDirectory, load_org_directory
from app.routing.policy import (
    MANUAL_QUEUE,
    TASK_TYPE,
    RoutingDecision,
    manual_assignment_blockers,
    select_assignee,
    shortfall_policy,
)
from app.workflows.records import SYSTEM_ROUTER, add_task_event, record_assignment
from app.workflows.state_machine import TaskAction, next_status

# Work in hand that can be taken back from someone who is no longer eligible.
RELEASABLE = frozenset({TaskStatus.ASSIGNED, TaskStatus.ACKNOWLEDGED, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED})


class TaskRouter:
    def __init__(self, tenant_id: str, settings: Settings):
        self._tenant = tenant_id
        self._settings = settings
        self.policy = shortfall_policy(timedelta(minutes=settings.availability_max_age_minutes))

    def ensure_tasks(self, session: Session, run: ReconciliationRun) -> dict[str, Any]:
        as_of = run.evaluated_as_of
        directory = load_org_directory(session, self._tenant)
        open_counts: dict[str, int] = defaultdict(int)
        for assignee, count in session.execute(
            select(Task.assignee_employee_id, func.count())
            .where(
                Task.tenant_id == self._tenant,
                Task.assignee_employee_id.is_not(None),
                Task.status.in_([s.value for s in ACTIVE_TASK_STATUSES]),
            )
            .group_by(Task.assignee_employee_id)
        ):
            open_counts[assignee] = count

        summary = defaultdict(int)
        exceptions = session.scalars(
            select(ExceptionCase)
            .where(ExceptionCase.tenant_id == self._tenant, ExceptionCase.status == ExceptionStatus.OPEN)
            .order_by(ExceptionCase.case_key)
            .with_for_update()
        ).all()
        for exception in exceptions:
            task = session.scalar(
                select(Task)
                .where(
                    Task.exception_id == exception.id,
                    Task.task_type == TASK_TYPE,
                    Task.status.in_([s.value for s in ACTIVE_TASK_STATUSES]),
                )
                .with_for_update()
            )
            if task is None:
                task = self._create_task(session, exception, run)
                if task is None:
                    summary["created_elsewhere"] += 1
                    continue
                summary["created"] += 1
            else:
                for outcome in self._keep_in_step(session, task, exception, directory, open_counts, run):
                    summary[outcome] += 1
            if task.status != TaskStatus.CREATED:
                continue
            decision = select_assignee(
                location_id=task.location_id,
                employees=directory.employees,
                availability=directory.availability,
                open_task_counts=open_counts,
                as_of=as_of,
                policy=self.policy,
            )
            summary[self._apply_decision(session, task, decision, run)] += 1
            if decision.assignee_id:
                open_counts[decision.assignee_id] += 1

        summary["data_quality_issues"] = upsert_issues(  # type: ignore[assignment]
            session, self._tenant, "routing", self._issue_drafts(directory), run
        )
        return dict(summary)

    def _keep_in_step(
        self,
        session: Session,
        task: Task,
        exception: ExceptionCase,
        directory: OrgDirectory,
        open_counts: dict[str, int],
        run: ReconciliationRun,
    ) -> list[str]:
        as_of = run.evaluated_as_of
        outcomes = []
        target = (exception.canonical_location_id, exception.region_id)
        if (task.location_id, task.region_id) != target:
            before = {"location_id": task.location_id, "region_id": task.region_id}
            task.location_id, task.region_id = target
            task.version += 1
            add_task_event(
                session,
                task,
                "LOCATION_CHANGED",
                from_status=task.status,
                to_status=task.status,
                actor_id=SYSTEM_ROUTER,
                occurred_at=as_of,
                details={
                    "before": before,
                    "after": {"location_id": target[0], "region_id": target[1]},
                    "run_id": run.id,
                    "version_after": task.version,
                },
            )
            outcomes.append("location_changed")

        if task.assignee_employee_id is None or task.status not in RELEASABLE:
            return outcomes
        employee = directory.employees.get(task.assignee_employee_id)
        blockers = (
            ["NOT_IN_ROSTER"]
            if employee is None
            else manual_assignment_blockers(employee, location_id=task.location_id, region_id=task.region_id)
        )
        if not blockers:
            return outcomes
        previous_status, previous_assignee = task.status, task.assignee_employee_id
        task.status = next_status(previous_status, TaskAction.RELEASE)
        task.assignee_employee_id = None
        task.queue = MANUAL_QUEUE
        task.version += 1
        open_counts[previous_assignee] = max(0, open_counts.get(previous_assignee, 0) - 1)
        record_assignment(
            session,
            task,
            assignee_id=None,
            queue=MANUAL_QUEUE,
            reason_code="ASSIGNEE_NO_LONGER_ELIGIBLE",
            actor_id=SYSTEM_ROUTER,
            occurred_at=as_of,
            policy_version=self.policy.version,
            decision={"previous_assignee": previous_assignee, "previous_status": previous_status, "reasons": blockers},
        )
        add_task_event(
            session,
            task,
            "RELEASED",
            from_status=previous_status,
            to_status=task.status,
            actor_id=SYSTEM_ROUTER,
            occurred_at=as_of,
            details={
                "previous_assignee": previous_assignee,
                "reasons": blockers,
                "run_id": run.id,
                "version_after": task.version,
            },
        )
        outcomes.append("released")
        return outcomes

    def _create_task(self, session: Session, exception: ExceptionCase, run: ReconciliationRun) -> Task | None:
        as_of = run.evaluated_as_of
        due_at = as_of + timedelta(hours=self._settings.task_due_hours)
        stmt = (
            pg_insert(Task)
            .values(
                id=uuid.uuid4(),
                tenant_id=self._tenant,
                exception_id=exception.id,
                task_type=TASK_TYPE,
                status=TaskStatus.CREATED,
                location_id=exception.canonical_location_id,
                region_id=exception.region_id,
                opened_at=as_of,
                due_at=due_at,
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=["exception_id", "task_type"], index_where=text(ACTIVE_TASK_PREDICATE)
            )
            .returning(Task.id)
        )
        task_id = session.scalar(stmt)
        if task_id is None:
            return None
        task = session.get_one(Task, task_id)
        add_task_event(
            session,
            task,
            "CREATED",
            from_status=None,
            to_status=TaskStatus.CREATED,
            actor_id=SYSTEM_ROUTER,
            occurred_at=as_of,
            details={
                "exception_id": exception.id,
                "case_key": exception.case_key,
                "due_at": due_at,
                "run_id": run.id,
                "version_after": task.version,
            },
        )
        return task

    def _apply_decision(self, session: Session, task: Task, decision: RoutingDecision, run: ReconciliationRun) -> str:
        as_of = run.evaluated_as_of
        if decision.assignee_id is not None:
            previous = task.status
            task.status = next_status(previous, TaskAction.ASSIGN)
            task.assignee_employee_id = decision.assignee_id
            task.queue = None
            task.version += 1
            record_assignment(
                session,
                task,
                assignee_id=decision.assignee_id,
                queue=None,
                reason_code=decision.reason_code,
                actor_id=SYSTEM_ROUTER,
                occurred_at=as_of,
                policy_version=decision.policy_version,
                decision=decision.as_dict(),
            )
            add_task_event(
                session,
                task,
                "ASSIGNED",
                from_status=previous,
                to_status=task.status,
                actor_id=SYSTEM_ROUTER,
                occurred_at=as_of,
                details={
                    "assignee_employee_id": decision.assignee_id,
                    "reason_code": decision.reason_code,
                    "run_id": run.id,
                    "version_after": task.version,
                },
            )
            return "assigned"
        if task.queue == MANUAL_QUEUE:
            return "still_unassigned"  # already queued; do not repeat the same history entry every run
        task.queue = MANUAL_QUEUE
        task.version += 1
        record_assignment(
            session,
            task,
            assignee_id=None,
            queue=MANUAL_QUEUE,
            reason_code=decision.reason_code,
            actor_id=SYSTEM_ROUTER,
            occurred_at=as_of,
            policy_version=decision.policy_version,
            decision=decision.as_dict(),
        )
        add_task_event(
            session,
            task,
            "QUEUED_FOR_MANUAL_ASSIGNMENT",
            from_status=task.status,
            to_status=task.status,
            actor_id=SYSTEM_ROUTER,
            occurred_at=as_of,
            details={
                "queue": MANUAL_QUEUE,
                "reason_code": decision.reason_code,
                "run_id": run.id,
                "version_after": task.version,
            },
        )
        return "queued_for_manual_assignment"

    def _issue_drafts(self, directory: OrgDirectory) -> list[IssueDraft]:
        drafts: list[IssueDraft] = []
        for employee in sorted(directory.employees.values(), key=lambda e: e.employee_id):
            if employee.in_conflict:
                drafts.append(
                    IssueDraft(
                        "SOURCE_RECORD_IN_CONFLICT",
                        "employees",
                        employee.employee_id,
                        "employee record has an unresolved conflicting re-send; not routable",
                    )
                )
            for problem in employee.problems:
                drafts.append(
                    IssueDraft(problem.code, "employees", employee.employee_id, problem.message, dict(problem.details))
                )
        for samples in directory.availability.values():
            for sample in samples:
                if sample.on_shift is None:
                    drafts.append(
                        IssueDraft(
                            "UNRECOGNIZED_AVAILABILITY_STATUS",
                            "workforce_status",
                            sample.ref.source_record_key,
                            f"availability status '{sample.status_code}' is not in the declared vocabulary",
                            {"status": sample.status_code},
                        )
                    )
        for sample in directory.unknown_employee_samples:
            drafts.append(
                IssueDraft(
                    "AVAILABILITY_FOR_UNKNOWN_EMPLOYEE",
                    "workforce_status",
                    sample.ref.source_record_key,
                    f"availability sample for {sample.employee_id}, who is not in the HRMS roster",
                    {"employee_id": sample.employee_id},
                )
            )
        return drafts
