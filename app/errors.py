"""Domain errors. The API turns each into a JSON error envelope with a stable code."""

from typing import Any


class DomainError(Exception):
    http_status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFound(DomainError):
    http_status = 404
    code = "NOT_FOUND"


class InvalidRequest(DomainError):
    http_status = 422
    code = "INVALID_REQUEST"


class PayloadTooLarge(DomainError):
    http_status = 413
    code = "PAYLOAD_TOO_LARGE"


class InvalidTransition(DomainError):
    http_status = 409
    code = "INVALID_TRANSITION"


class StateConflict(DomainError):
    """The resource changed underneath the caller (version mismatch, already resolved)."""

    http_status = 409
    code = "STATE_CONFLICT"


class PermissionDenied(DomainError):
    http_status = 403
    code = "ACTOR_NOT_PERMITTED"


class IdempotencyKeyReused(DomainError):
    http_status = 422
    code = "IDEMPOTENCY_KEY_REUSED"


class ImportFailed(DomainError):
    http_status = 500
    code = "IMPORT_FAILED"


class RunFailed(DomainError):
    http_status = 500
    code = "RUN_FAILED"
