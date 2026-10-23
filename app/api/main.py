"""FastAPI application factory. Run with:

uv run uvicorn --factory app.api.main:create_app
"""

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.container import Services, build_services
from app.errors import DomainError
from app.hashing import to_jsonable
from app.observability import configure_logging, log_event, request_id_var

logger = logging.getLogger("opsib.api")

DESCRIPTION = """
Imports retailer exports (ERP purchase orders, WMS receipts, product/location crosswalks,
HRMS roster, workforce shifts), reconciles them into canonical identities and base units,
detects receipt shortfalls with a deterministic rule, routes investigation tasks to eligible
employees and records an auditable task lifecycle.

Errors use one envelope: `{"error": {"code": "...", "message": "...", "details": {...}}}`.
Mutating task endpoints take `X-Actor-Id` (HRMS employee ID) and an optional `Idempotency-Key`.
"""


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": to_jsonable(details or {})}}
    request_id = request_id_var.get()
    if request_id:
        body["error"]["request_id"] = request_id
    return JSONResponse(body, status_code=status)


def create_app(services: Services | None = None) -> FastAPI:
    settings = services.settings if services else get_settings()
    configure_logging(settings.log_level)
    services = services or build_services(settings)

    app = FastAPI(title="OPSiB receipt-exception service", version="0.1.0", description=DESCRIPTION)
    app.state.services = services

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        log_event(
            logger,
            "http_request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return _error(exc.http_status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"location": [str(p) for p in e.get("loc", ())], "message": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return _error(422, "VALIDATION_ERROR", "request is invalid", {"errors": errors})

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra={"fields": {"exc_type": type(exc).__name__}})
        return _error(500, "INTERNAL_ERROR", "unexpected error; see logs for the request ID")

    app.include_router(router)
    return app
