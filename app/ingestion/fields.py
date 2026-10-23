"""Typed field definitions shared by the dataset payload models."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, StringConstraints


def parse_aware_datetime(value: Any) -> Any:
    """ISO-8601 with an explicit offset, nothing else. A naive timestamp, a bare date or
    an epoch number would force us to guess a timezone, so they are rejected."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 timestamp with a UTC offset") from exc
    else:
        raise ValueError("must be an ISO-8601 timestamp with a UTC offset")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no UTC offset; refusing to guess a timezone")
    return parsed


def _normalise_case(upper: bool) -> Callable[[Any], Any]:
    def normalise(value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value.upper() if upper else value.lower()
        return value

    return normalise


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
# Status and unit codes: case-insensitive by convention, normalised (before the
# pattern check) to upper case. Role codes are normalised to lower case.
Code = Annotated[
    str,
    BeforeValidator(_normalise_case(upper=True)),
    StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Z0-9_-]+$"),
]
RoleCode = Annotated[
    str,
    BeforeValidator(_normalise_case(upper=False)),
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
]
LineNumber = Annotated[int, Field(gt=0, lt=1_000_000)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4, allow_inf_nan=False)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4, allow_inf_nan=False)]
ConversionFactor = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6, allow_inf_nan=False)]
AwareDatetime = Annotated[datetime, BeforeValidator(parse_aware_datetime)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
