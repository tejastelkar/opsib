"""The business clock.

Scenario logic asks a Clock for "now" so the frozen evaluation time can be
injected. Wall-clock time is still used for operational metadata (row
created_at / recorded_at columns set by the database), which is never an input
to a business decision.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.config import Settings


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FrozenClock:
    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")

    def now(self) -> datetime:
        return self.at


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def clock_from_settings(settings: Settings) -> Clock:
    if settings.clock_mode == "frozen":
        return FrozenClock(settings.frozen_time)
    return SystemClock()
