"""File-level CSV parsing. Row-level validation happens in the import service."""

import csv
import io
from dataclasses import dataclass, field


class FileRejected(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_error(self) -> dict:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


@dataclass(frozen=True)
class CsvRow:
    row_number: int  # physical line number; the header is line 1
    cells: dict[str, str] | None  # None when the column count does not match the header
    raw_cells: list[str]


@dataclass
class ParsedCsv:
    header: list[str]
    rows: list[CsvRow]
    warnings: list[dict] = field(default_factory=list)


def parse_csv(content: bytes, required_columns: frozenset[str]) -> ParsedCsv:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRejected("NOT_UTF8", f"file is not valid UTF-8 (byte offset {exc.start})") from None

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header_cells = next(reader)
    except StopIteration:
        raise FileRejected("EMPTY_FILE", "file has no header row") from None
    except csv.Error as exc:
        raise FileRejected("MALFORMED_CSV", f"header could not be parsed: {exc}") from None

    header = [cell.strip() for cell in header_cells]
    duplicated = sorted({c for c in header if header.count(c) > 1})
    if duplicated:
        raise FileRejected("DUPLICATE_COLUMNS", "header repeats column names", {"columns": duplicated})
    missing = sorted(required_columns - set(header))
    if missing:
        raise FileRejected(
            "MISSING_COLUMNS",
            "header is missing required columns",
            {"missing": missing, "found": header},
        )

    warnings: list[dict] = []
    ignored = [c for c in header if c not in required_columns]
    if ignored:
        warnings.append({"code": "IGNORED_COLUMNS", "message": "columns not used by this profile", "columns": ignored})

    rows: list[CsvRow] = []
    blank_lines: list[int] = []
    try:
        for cells in reader:
            line = reader.line_num
            if not cells or all(not c.strip() for c in cells):
                blank_lines.append(line)
                continue
            if len(cells) != len(header):
                rows.append(CsvRow(line, None, cells))
            else:
                rows.append(CsvRow(line, dict(zip(header, cells, strict=True)), cells))
    except csv.Error as exc:
        raise FileRejected("MALFORMED_CSV", f"line {reader.line_num}: {exc}") from None

    if blank_lines:
        warnings.append({"code": "BLANK_LINES_SKIPPED", "message": "blank lines are not records", "lines": blank_lines})
    return ParsedCsv(header=header, rows=rows, warnings=warnings)
