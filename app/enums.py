"""Status vocabularies shared by the database schema and the services.

The schema turns each of these into a CHECK constraint, so a value the code
does not know about can never be written.
"""

from enum import StrEnum


class ImportStatus(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"  # every row accepted (identical repeats are fine)
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"  # some rows rejected or in conflict
    REJECTED = "REJECTED"  # file-level problem (bad header, encoding); nothing loaded
    FAILED = "FAILED"  # unexpected error; the transaction rolled back, nothing loaded


class RowOutcome(StrEnum):
    INSERTED = "INSERTED"  # new source fact
    UNCHANGED = "UNCHANGED"  # same source key and same content as a stored fact
    DUPLICATE_IN_FILE = "DUPLICATE_IN_FILE"  # identical to an earlier row of the same file
    REJECTED = "REJECTED"  # failed validation; reasons in errors
    CONFLICT = "CONFLICT"  # same source key, different content; flagged for review


class ConflictStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ConflictResolution(StrEnum):
    KEEP_EXISTING = "KEEP_EXISTING"  # a reviewer kept the stored version
    ACCEPT_INCOMING = "ACCEPT_INCOMING"  # a reviewer accepted the re-sent version
    SUPERSEDED = "SUPERSEDED"  # system: another decision already made this re-send the current version


class ReviewStatus(StrEnum):
    OK = "OK"
    CONFLICT = "CONFLICT"  # an open conflict exists; the fact is not trusted for detection


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EvaluationOutcome(StrEnum):
    SHORTFALL = "SHORTFALL"
    NO_SHORTFALL = "NO_SHORTFALL"
    NOT_DUE = "NOT_DUE"  # expected-receipt deadline has not passed
    NOT_ELIGIBLE = "NOT_ELIGIBLE"  # e.g. PO line cancelled
    NOT_EVALUABLE = "NOT_EVALUABLE"  # evidence missing, stale, conflicting or unmapped


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"  # a verified task closed it
    CLEARED = "CLEARED"  # re-evaluation no longer shows a shortfall (e.g. a late receipt)


class IssueStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class TaskStatus(StrEnum):
    CREATED = "CREATED"  # no eligible assignee yet: sits in the manual-assignment queue
    ASSIGNED = "ASSIGNED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    CANCELLED = "CANCELLED"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.VERIFIED, TaskStatus.CANCELLED})
ACTIVE_TASK_STATUSES = frozenset(set(TaskStatus) - TERMINAL_TASK_STATUSES)


class ResolutionCode(StrEnum):
    """What the investigation established. Cause is recorded here, from evidence the
    investigator collected, never by the detector."""

    RECEIPT_MISSED_NOW_POSTED = "RECEIPT_MISSED_NOW_POSTED"
    SUPPLIER_CONFIRMED_SHORT_DISPATCH = "SUPPLIER_CONFIRMED_SHORT_DISPATCH"
    CARRIER_LOSS_CLAIM_RAISED = "CARRIER_LOSS_CLAIM_RAISED"
    BALANCE_TO_FOLLOW = "BALANCE_TO_FOLLOW"
    PO_QUANTITY_AMENDED = "PO_QUANTITY_AMENDED"
    STORE_COUNT_CONFIRMED_SHORT = "STORE_COUNT_CONFIRMED_SHORT"
    OTHER = "OTHER"


def sql_in(enum_cls: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_cls)
