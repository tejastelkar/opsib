"""Runtime configuration.

Every setting can be overridden with an environment variable prefixed ``OPSIB_``
(for example ``OPSIB_DATABASE_URL``) or a ``.env`` file in the working directory.
"""

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The fixture README's frozen evaluation time. Scenario logic (deadlines, shift
# windows, availability freshness, due times) reads the clock, never datetime.now().
FIXTURE_EVALUATION_TIME = datetime.fromisoformat("2026-10-16T15:00:00+05:30")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSIB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://opsib@127.0.0.1:55433/opsib"

    # Single-tenant pilot: every row carries tenant_id, but the tenant comes from
    # configuration, not from an authenticated principal (see README, production gaps).
    tenant_id: str = Field(default="tenant-demo", min_length=1, max_length=64)

    # "frozen" pins the business clock to frozen_time; "system" uses the wall clock.
    clock_mode: Literal["frozen", "system"] = "frozen"
    frozen_time: datetime = FIXTURE_EVALUATION_TIME

    # Used only to render human-readable explanation text; storage is UTC.
    business_timezone: str = "Asia/Kolkata"

    # Receipt-shortfall rule: a line is a shortfall when variance > tolerance (base units).
    shortfall_tolerance_base_qty: Decimal = Field(default=Decimal("0"), ge=0)

    # Routing: a workforce sample older than this is not trusted for "on shift".
    availability_max_age_minutes: int = Field(default=60, gt=0)
    task_due_hours: int = Field(default=24, gt=0)

    max_upload_bytes: int = Field(default=5_000_000, gt=0)

    # Optional AI investigation summaries. "disabled" needs no key and no network.
    ai_provider: Literal["disabled", "claude"] = "disabled"
    ai_timeout_seconds: float = Field(default=15.0, gt=0)
    anthropic_model: str = "claude-opus-5-5"

    log_level: str = "INFO"

    @field_validator("frozen_time")
    @classmethod
    def _frozen_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("frozen_time must include a UTC offset, e.g. 2026-10-16T15:00:00+05:30")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
