"""What each source system's status codes mean.

Interpretation is declared per source system. A code that is not declared is
not guessed: the record becomes a data-quality issue and is excluded from
business decisions until someone extends the vocabulary or fixes the source.
"""

from enum import StrEnum


class PoLineStatus(StrEnum):
    OPEN = "OPEN"
    CANCELLED = "CANCELLED"


class PostingClass(StrEnum):
    COUNTS = "COUNTS"  # posted: the accepted quantity is booked
    EXCLUDED = "EXCLUDED"  # final but contributes nothing (reversed / cancelled)
    NOT_FINAL = "NOT_FINAL"  # still being posted; the line cannot be judged yet


PO_LINE_STATUS: dict[str, dict[str, PoLineStatus]] = {
    "ERP": {"OPEN": PoLineStatus.OPEN, "CANCELLED": PoLineStatus.CANCELLED},
}

RECEIPT_POSTING_STATUS: dict[str, dict[str, PostingClass]] = {
    "WMS": {
        "POSTED": PostingClass.COUNTS,
        "REVERSED": PostingClass.EXCLUDED,
        "CANCELLED": PostingClass.EXCLUDED,
        "PENDING": PostingClass.NOT_FINAL,
        "DRAFT": PostingClass.NOT_FINAL,
    },
}

# True = employed and eligible for work; False = not.
EMPLOYMENT_STATUS: dict[str, dict[str, bool]] = {
    "HRMS": {"ACTIVE": True, "INACTIVE": False},
}

# True = on shift, False = not on shift.
AVAILABILITY_STATUS: dict[str, dict[str, bool]] = {
    "WFM": {"ON_SHIFT": True, "OFF_SHIFT": False},
}


def interpret[T](vocabulary: dict[str, dict[str, T]], source_system: str, code: str) -> T | None:
    return vocabulary.get(source_system, {}).get(code)
