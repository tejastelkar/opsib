import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import read_models
from app.ai.investigation import summarize_exception
from app.api.deps import ActorDep, IdempotencyKeyDep, ServicesDep
from app.api.schemas import (
    BlockBody,
    CompleteBody,
    DismissRowBody,
    ReassignBody,
    RejectBody,
    ResolveConflictBody,
    UnblockBody,
    VerifyBody,
    VersionedBody,
)
from app.enums import ExceptionStatus, ImportStatus, TaskStatus
from app.errors import PayloadTooLarge
from app.hashing import to_jsonable
from app.ingestion.datasets import DATASETS, DEFAULT_PROFILE
from app.ingestion.service import ImportRequest
from app.workflows.service import WorkflowResult

router = APIRouter()


def _read(services: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    with services.sessions() as session, session.begin():
        return fn(session, services.tenant_id, *args, **kwargs)


# ---------------------------------------------------------------- health


@router.get("/health", tags=["system"])
def health(services: ServicesDep) -> dict[str, Any]:
    with services.sessions() as session, session.begin():
        session.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "tenant_id": services.tenant_id,
        "clock_mode": services.settings.clock_mode,
        "business_now": services.clock.now().isoformat(),
        "ai_provider": services.ai_provider.name,
    }


# ---------------------------------------------------------------- imports


@router.post("/imports/{dataset}", status_code=201, tags=["imports"])
def create_import(
    dataset: str,
    services: ServicesDep,
    file: Annotated[UploadFile, File(description="CSV export for the dataset")],
    profile: Annotated[str, Query(description="Source profile (export format)")] = DEFAULT_PROFILE,
    source_extracted_at: Annotated[
        datetime | None,
        Query(
            description="When the source system extracted the file (ISO-8601 with offset). "
            "Defaults to the business clock."
        ),
    ] = None,
) -> JSONResponse:
    """Validate and load one dataset file. 201 with row-level outcomes (the status says
    COMPLETED or COMPLETED_WITH_ERRORS); 422 if the file itself is unusable."""
    services.registry.dataset(dataset)
    limit = services.settings.max_upload_bytes
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise PayloadTooLarge(f"file is larger than {limit} bytes", details={"limit_bytes": limit})
    batch = services.imports.import_file(
        ImportRequest(
            dataset, content, file_name=file.filename, profile=profile, source_extracted_at=source_extracted_at
        )
    )
    view = _read(services, read_models.get_import, batch.id)
    return JSONResponse(view, status_code=422 if batch.status == ImportStatus.REJECTED else 201)


@router.get("/imports", tags=["imports"])
def list_imports(
    services: ServicesDep,
    dataset: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return _read(services, read_models.list_imports, dataset, limit)


@router.get("/imports/{import_id}", tags=["imports"])
def get_import(
    import_id: uuid.UUID,
    services: ServicesDep,
    all_rows: Annotated[bool, Query(description="Also include rows inserted or unchanged without issue")] = False,
) -> dict[str, Any]:
    return _read(services, read_models.get_import, import_id, all_rows)


@router.get("/datasets", tags=["imports"])
def list_datasets(services: ServicesDep) -> list[dict[str, Any]]:
    return [{"dataset": name, "description": spec.description} for name, spec in sorted(DATASETS.items())]


# ---------------------------------------------------------------- data quality


@router.get("/data-quality", tags=["data quality"])
def data_quality(services: ServicesDep, include_resolved: Annotated[bool, Query()] = False) -> dict[str, Any]:
    """Unresolved mappings, records that cannot be evaluated, open source conflicts,
    and every rejected row or file that has not been superseded by a later import."""
    return _read(services, read_models.data_quality_report, services.registry, include_resolved)


@router.post("/data-quality/rejected-rows/{row_id}/dismiss", tags=["data quality"])
def dismiss_rejected_row(row_id: int, body: DismissRowBody, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """A data steward records that a rejected row needs no correction (e.g. a garbage
    line), so it stops blocking evaluation. Correcting the source is the usual path."""
    dismissal = services.conflicts.dismiss_rejected_row(row_id, actor, body.reason)
    return to_jsonable(
        {
            "row_id": dismissal.import_row_id,
            "dismissed_by": dismissal.dismissed_by,
            "reason": dismissal.reason,
            "dismissed_at": dismissal.dismissed_at,
        }
    )


@router.post("/data-quality/conflicts/{conflict_id}/resolve", tags=["data quality"])
def resolve_conflict(
    conflict_id: uuid.UUID, body: ResolveConflictBody, services: ServicesDep, actor: ActorDep
) -> dict[str, Any]:
    conflict = services.conflicts.resolve(conflict_id, body.resolution, actor, body.reason)
    return read_models.conflict_view(conflict)


# ---------------------------------------------------------------- reconciliation


@router.post("/reconciliation-runs", status_code=201, tags=["reconciliation"])
def create_run(services: ServicesDep) -> dict[str, Any]:
    """Detect receipt shortfalls at the business clock's time, then route tasks.
    Safe to repeat: an unchanged input produces no new exceptions, tasks or events."""
    run = services.reconciliation.run()
    return _read(services, read_models.get_run, run.id)


@router.get("/reconciliation-runs/{run_id}", tags=["reconciliation"])
def get_run(run_id: uuid.UUID, services: ServicesDep) -> dict[str, Any]:
    return _read(services, read_models.get_run, run_id)


# ---------------------------------------------------------------- exceptions


@router.get("/exceptions", tags=["exceptions"])
def list_exceptions(
    services: ServicesDep,
    status: Annotated[ExceptionStatus | None, Query()] = None,
    location_id: Annotated[str | None, Query()] = None,
    exception_type: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    return _read(services, read_models.list_exceptions, status, location_id, exception_type)


@router.get("/exceptions/{exception_id}", tags=["exceptions"])
def get_exception(exception_id: uuid.UUID, services: ServicesDep) -> dict[str, Any]:
    """The finding with its evidence snapshot, the current source records it cites
    (with the import rows that carried them), its tasks and its event history."""
    return _read(services, read_models.get_exception_detail, exception_id)


@router.post("/exceptions/{exception_id}/investigation-summary", tags=["exceptions"])
def investigation_summary(exception_id: uuid.UUID, services: ServicesDep) -> dict[str, Any]:
    """Optional, advisory AI summary of the evidence. Never changes the case."""
    exception = _read(services, read_models.get_exception_model, exception_id)
    return summarize_exception(exception, services.ai_provider, services.settings.ai_timeout_seconds)


# ---------------------------------------------------------------- tasks


@router.get("/tasks", tags=["tasks"])
def list_tasks(
    services: ServicesDep,
    assignee_id: Annotated[str | None, Query()] = None,
    location_id: Annotated[str | None, Query()] = None,
    status: Annotated[TaskStatus | None, Query()] = None,
    queue: Annotated[str | None, Query(description="e.g. MANUAL_ASSIGNMENT")] = None,
) -> list[dict[str, Any]]:
    return _read(services, read_models.list_tasks, assignee_id, location_id, status, queue)


@router.get("/tasks/{task_id}", tags=["tasks"])
def get_task(task_id: uuid.UUID, services: ServicesDep) -> dict[str, Any]:
    return _read(services, read_models.get_task_detail, task_id)


def _workflow_response(result: WorkflowResult) -> JSONResponse:
    headers = {"Idempotent-Replay": "true"} if result.replayed else {}
    return JSONResponse(result.task, status_code=200, headers=headers)


def _version(body: VersionedBody | None) -> int | None:
    return body.expected_version if body else None


@router.post("/tasks/{task_id}/acknowledge", tags=["tasks"])
def acknowledge(
    task_id: uuid.UUID,
    services: ServicesDep,
    actor: ActorDep,
    key: IdempotencyKeyDep,
    body: VersionedBody | None = None,
) -> JSONResponse:
    return _workflow_response(
        services.workflow.acknowledge(task_id, actor, idempotency_key=key, expected_version=_version(body))
    )


@router.post("/tasks/{task_id}/start", tags=["tasks"])
def start(
    task_id: uuid.UUID,
    services: ServicesDep,
    actor: ActorDep,
    key: IdempotencyKeyDep,
    body: VersionedBody | None = None,
) -> JSONResponse:
    return _workflow_response(
        services.workflow.start(task_id, actor, idempotency_key=key, expected_version=_version(body))
    )


@router.post("/tasks/{task_id}/block", tags=["tasks"])
def block(
    task_id: uuid.UUID, body: BlockBody, services: ServicesDep, actor: ActorDep, key: IdempotencyKeyDep
) -> JSONResponse:
    return _workflow_response(
        services.workflow.block(
            task_id, actor, body.reason, idempotency_key=key, expected_version=body.expected_version
        )
    )


@router.post("/tasks/{task_id}/unblock", tags=["tasks"])
def unblock(
    task_id: uuid.UUID,
    services: ServicesDep,
    actor: ActorDep,
    key: IdempotencyKeyDep,
    body: UnblockBody | None = None,
) -> JSONResponse:
    return _workflow_response(
        services.workflow.unblock(
            task_id, actor, body.note if body else None, idempotency_key=key, expected_version=_version(body)
        )
    )


@router.post("/tasks/{task_id}/complete", tags=["tasks"])
def complete(
    task_id: uuid.UUID, body: CompleteBody, services: ServicesDep, actor: ActorDep, key: IdempotencyKeyDep
) -> JSONResponse:
    """Record the resolution code and evidence reference. The server moves the task
    straight to PENDING_VERIFICATION; a second person must verify it."""
    return _workflow_response(
        services.workflow.complete(
            task_id,
            actor,
            body.resolution_code,
            body.evidence_ref,
            body.note,
            idempotency_key=key,
            expected_version=body.expected_version,
        )
    )


@router.post("/tasks/{task_id}/verify", tags=["tasks"])
def verify(
    task_id: uuid.UUID,
    services: ServicesDep,
    actor: ActorDep,
    key: IdempotencyKeyDep,
    body: VerifyBody | None = None,
) -> JSONResponse:
    return _workflow_response(
        services.workflow.verify(
            task_id, actor, body.note if body else None, idempotency_key=key, expected_version=_version(body)
        )
    )


@router.post("/tasks/{task_id}/reject", tags=["tasks"])
def reject(
    task_id: uuid.UUID, body: RejectBody, services: ServicesDep, actor: ActorDep, key: IdempotencyKeyDep
) -> JSONResponse:
    """Send a submitted resolution back for rework (PENDING_VERIFICATION -> IN_PROGRESS)."""
    return _workflow_response(
        services.workflow.reject(
            task_id, actor, body.reason, idempotency_key=key, expected_version=body.expected_version
        )
    )


@router.post("/tasks/{task_id}/reassign", tags=["tasks"])
def reassign(
    task_id: uuid.UUID, body: ReassignBody, services: ServicesDep, actor: ActorDep, key: IdempotencyKeyDep
) -> JSONResponse:
    return _workflow_response(
        services.workflow.reassign(
            task_id,
            actor,
            body.assignee_employee_id,
            body.reason,
            idempotency_key=key,
            expected_version=body.expected_version,
        )
    )
