from typing import Annotated

from fastapi import Depends, Header, Request

from app.container import Services
from app.errors import InvalidRequest, PermissionDenied


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


def get_actor_id(
    x_actor_id: Annotated[
        str | None,
        Header(
            alias="X-Actor-Id",
            description="HRMS employee ID of the person acting. Stand-in for an authenticated principal (see README).",
        ),
    ] = None,
) -> str:
    actor = (x_actor_id or "").strip()
    if not actor:
        raise InvalidRequest("the X-Actor-Id header is required for this action", code="ACTOR_REQUIRED")
    if actor.startswith("system:"):
        raise PermissionDenied("system actors cannot act through the API", code="SYSTEM_ACTOR_FORBIDDEN")
    return actor


ActorDep = Annotated[str, Depends(get_actor_id)]


def get_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Optional client-generated key; a retry with the same key replays the stored response.",
        ),
    ] = None,
) -> str | None:
    if idempotency_key is None:
        return None
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise InvalidRequest("Idempotency-Key must be 1-128 characters", code="BAD_IDEMPOTENCY_KEY")
    return key


IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
