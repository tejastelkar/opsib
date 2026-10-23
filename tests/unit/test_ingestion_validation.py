"""Row-level typed validation, file-level CSV checks and content hashing."""

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.errors import InvalidRequest, NotFound
from app.hashing import content_hash
from app.ingestion.csv_reader import FileRejected, parse_csv
from app.ingestion.datasets import (
    AvailabilityPayload,
    DatasetRegistry,
    PoLinePayload,
    ProductAliasPayload,
    SourceProfile,
)

GOOD_PO = {
    "source_record_key": "ERP-PO501-L1",
    "po_number": "PO-501",
    "line_number": "1",
    "supplier_code": "SUP-07",
    "location_ref": "MUM-014",
    "item_ref": "ERP-SKU-001",
    "ordered_qty": "100",
    "ordered_uom": "EA",
    "status_code": "OPEN",
    "expected_receipt_by": "2026-10-15T12:00:00+05:30",
}


def error_types(exc: ValidationError) -> dict[str, str]:
    return {str(e["loc"][0]) if e["loc"] else "": e["type"] for e in exc.errors()}


def test_valid_po_line_parses_with_typed_values():
    payload = PoLinePayload.model_validate(GOOD_PO)
    assert payload.ordered_qty == Decimal(100)
    assert payload.expected_receipt_by.utcoffset().total_seconds() == 5.5 * 3600


@pytest.mark.parametrize("value", ["2026-10-15T12:00:00", "2026-10-15", "1760510400", "15/10/2026 12:00", "not a date"])
def test_timestamps_without_explicit_offset_are_rejected(value):
    with pytest.raises(ValidationError) as info:
        PoLinePayload.model_validate({**GOOD_PO, "expected_receipt_by": value})
    assert "expected_receipt_by" in error_types(info.value)


@pytest.mark.parametrize(
    "qty, error",
    [
        ("0", "greater_than"),
        ("-5", "greater_than"),
        ("abc", "decimal_parsing"),
        ("1.23456", "decimal_max_places"),
        ("NaN", "finite_number"),
    ],
)
def test_bad_quantities_are_rejected(qty, error):
    with pytest.raises(ValidationError) as info:
        PoLinePayload.model_validate({**GOOD_PO, "ordered_qty": qty})
    assert error_types(info.value)["ordered_qty"] == error


def test_status_and_unit_codes_are_case_normalised():
    payload = PoLinePayload.model_validate({**GOOD_PO, "status_code": " open ", "ordered_uom": "ea"})
    assert (payload.status_code, payload.ordered_uom) == ("OPEN", "EA")


def test_missing_required_value_is_reported():
    data = {k: v for k, v in GOOD_PO.items() if k != "location_ref"}
    with pytest.raises(ValidationError) as info:
        PoLinePayload.model_validate(data)
    assert error_types(info.value) == {"location_ref": "missing"}


def test_identity_conversion_factor_must_be_one():
    with pytest.raises(ValidationError):
        ProductAliasPayload.model_validate(
            {
                "alias_system": "ERP",
                "external_item_id": "X",
                "canonical_product_id": "SKU-9",
                "source_uom": "EA",
                "base_uom": "EA",
                "to_base_factor": "12",
            }
        )


def test_shift_window_must_be_ordered():
    with pytest.raises(ValidationError):
        AvailabilityPayload.model_validate(
            {
                "employee_ref": "HR-1",
                "availability_status": "ON_SHIFT",
                "shift_start": "2026-10-16T18:00:00+05:30",
                "shift_end": "2026-10-16T09:00:00+05:30",
                "observed_at": "2026-10-16T14:50:00+05:30",
            }
        )


def test_content_hash_compares_business_content_not_text():
    a = PoLinePayload.model_validate(GOOD_PO).model_dump()
    b = PoLinePayload.model_validate(
        {**GOOD_PO, "ordered_qty": "100.0", "expected_receipt_by": "2026-10-15T06:30:00Z", "status_code": "open"}
    ).model_dump()
    c = PoLinePayload.model_validate({**GOOD_PO, "ordered_qty": "101"}).model_dump()
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(c)


def test_content_hash_refuses_naive_datetimes():
    with pytest.raises(ValueError):
        content_hash({"at": datetime(2026, 10, 16, 15, 0)})


# ---------------------------------------------------------------- file level

REQUIRED = frozenset({"a", "b"})


def test_missing_columns_reject_the_file():
    with pytest.raises(FileRejected) as info:
        parse_csv(b"a,c\n1,2\n", REQUIRED)
    assert info.value.code == "MISSING_COLUMNS"
    assert info.value.details["missing"] == ["b"]


@pytest.mark.parametrize(
    "content, code",
    [(b"", "EMPTY_FILE"), (b"a,a,b\n1,2,3\n", "DUPLICATE_COLUMNS"), (b"a,b\n\xff\xfe,1\n", "NOT_UTF8")],
)
def test_unusable_files_are_rejected(content, code):
    with pytest.raises(FileRejected) as info:
        parse_csv(content, REQUIRED)
    assert info.value.code == code


def test_bom_extra_columns_blank_lines_and_ragged_rows():
    parsed = parse_csv("﻿a,b,extra\n1,2,x\n\n3,4\n".encode(), REQUIRED)
    assert parsed.header == ["a", "b", "extra"]
    assert [r.row_number for r in parsed.rows] == [2, 4]
    assert parsed.rows[0].cells == {"a": "1", "b": "2", "extra": "x"}
    assert parsed.rows[1].cells is None  # wrong column count -> rejected later with a reason
    assert {w["code"] for w in parsed.warnings} == {"IGNORED_COLUMNS", "BLANK_LINES_SKIPPED"}


# ---------------------------------------------------------------- registry


def test_unknown_dataset_and_profile_are_explicit_errors():
    registry = DatasetRegistry()
    with pytest.raises(NotFound):
        registry.dataset("invoices")
    with pytest.raises(InvalidRequest):
        registry.profile("purchase_order_lines", "retailer_x")


def test_profile_cannot_map_to_unknown_fields():
    with pytest.raises(ValueError):
        DatasetRegistry().register(SourceProfile("purchase_order_lines", "bad", "ERP", {"PO": "purchase_order"}))
