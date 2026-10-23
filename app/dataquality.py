"""Data-quality issues: problems that stop a record from being used for a business
decision. They are recomputed on every run by the step that found them (the
"producer") and resolve themselves once a later run no longer sees them."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DataQualityIssue, ReconciliationRun
from app.enums import IssueStatus
from app.hashing import to_jsonable


@dataclass
class IssueDraft:
    code: str
    subject_dataset: str
    subject_record_key: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    affected_cases: set[str] = field(default_factory=set)

    @property
    def issue_key(self) -> str:
        return f"{self.code}|{self.subject_dataset}|{self.subject_record_key}"


def merge_drafts(drafts: Iterable[IssueDraft]) -> dict[str, IssueDraft]:
    merged: dict[str, IssueDraft] = {}
    for draft in drafts:
        current = merged.get(draft.issue_key)
        if current is None:
            merged[draft.issue_key] = draft
        else:
            current.affected_cases |= draft.affected_cases
    return merged


def upsert_issues(
    session: Session, tenant_id: str, producer: str, drafts: Iterable[IssueDraft], run: ReconciliationRun
) -> dict[str, int]:
    merged = merge_drafts(drafts)
    existing = {
        issue.issue_key: issue
        for issue in session.scalars(
            select(DataQualityIssue)
            .where(DataQualityIssue.tenant_id == tenant_id, DataQualityIssue.producer == producer)
            .with_for_update()
        )
    }
    opened = reopened = resolved = 0
    for key, draft in merged.items():
        details = to_jsonable({**draft.details, "affected_cases": sorted(draft.affected_cases)})
        issue = existing.get(key)
        if issue is None:
            session.add(
                DataQualityIssue(
                    tenant_id=tenant_id,
                    issue_key=key,
                    producer=producer,
                    code=draft.code,
                    subject_dataset=draft.subject_dataset,
                    subject_record_key=draft.subject_record_key,
                    message=draft.message,
                    details=details,
                    status=IssueStatus.OPEN,
                    first_seen_run_id=run.id,
                    last_seen_run_id=run.id,
                )
            )
            opened += 1
            continue
        if issue.status == IssueStatus.RESOLVED:
            issue.status = IssueStatus.OPEN
            issue.resolved_run_id = None
            reopened += 1
        issue.last_seen_run_id = run.id
        if issue.message != draft.message:
            issue.message = draft.message
        if issue.details != details:
            issue.details = details
    for key, issue in existing.items():
        if key not in merged and issue.status == IssueStatus.OPEN:
            issue.status = IssueStatus.RESOLVED
            issue.resolved_run_id = run.id
            resolved += 1
    return {
        "open": len(merged),
        "opened": opened,
        "reopened": reopened,
        "resolved": resolved,
    }
