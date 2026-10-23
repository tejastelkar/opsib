"""Request bodies. Responses are the read-model views (see app/read_models.py and
app/workflows/views.py); their shapes are documented in docs/API.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.enums import ConflictResolution, ResolutionCode


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionedBody(_Body):
    expected_version: int | None = Field(
        default=None, ge=1, description="Optional optimistic-concurrency check against the task's current version."
    )


class BlockBody(VersionedBody):
    reason: str = Field(min_length=3, max_length=1000)


class UnblockBody(VersionedBody):
    note: str | None = Field(default=None, max_length=1000)


class CompleteBody(VersionedBody):
    resolution_code: ResolutionCode
    evidence_ref: str = Field(
        min_length=3, max_length=512, description="Pointer to the evidence collected, e.g. a document or ticket ID."
    )
    note: str | None = Field(default=None, max_length=2000)


class VerifyBody(VersionedBody):
    note: str | None = Field(default=None, max_length=2000)


class RejectBody(VersionedBody):
    reason: str = Field(min_length=3, max_length=1000)


class ReassignBody(VersionedBody):
    assignee_employee_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=1000)


class ResolveConflictBody(_Body):
    resolution: Literal[ConflictResolution.KEEP_EXISTING, ConflictResolution.ACCEPT_INCOMING]
    reason: str = Field(min_length=3, max_length=1000)


class DismissRowBody(_Body):
    reason: str = Field(min_length=3, max_length=1000)
