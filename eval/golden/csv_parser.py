"""CSV ingestion with typed coercion and a bounded error report."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_ERRORS_REPORTED = 50

COERCERS: dict[str, Callable[[str], Any]] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": lambda v: v.strip().lower() in {"1", "true", "yes", "y"},
}


class CsvError(Exception):
    """Raised when the file cannot be parsed at all."""


@dataclass
class ParseReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def coerce_row(row: dict[str, str], schema: dict[str, str]) -> dict[str, Any]:
    """Apply the schema to one row. Unknown columns are passed through as text."""
    out: dict[str, Any] = {}
    for key, raw in row.items():
        coercer = COERCERS.get(schema.get(key, "str"), str)
        out[key] = coercer(raw) if raw != "" else None
    return out


def parse_text(text: str, schema: dict[str, str] | None = None) -> ParseReport:
    """Parse CSV text. A bad row is recorded and skipped, never fatal."""
    schema = schema or {}
    report = ParseReport()
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvError("file has no header row")

    for line_no, row in enumerate(reader, start=2):
        try:
            report.rows.append(coerce_row(row, schema))
        except (ValueError, TypeError) as exc:
            report.skipped += 1
            if len(report.errors) < MAX_ERRORS_REPORTED:
                report.errors.append(f"line {line_no}: {exc}")
    return report


def parse_file(path: Path, schema: dict[str, str] | None = None) -> ParseReport:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return parse_text(fh.read(), schema)


def load_cached_rows(path: Path) -> list[dict[str, Any]]:
    """Read a previously written cache. JSON only -- never an executable format."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def write_cached_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")
