"""DiffReport data model — JSON schema v1, frozen.

Additive changes only within v1: never rename, remove, or retype anything in
report/json_schema.py's REPORT_V1. This is the shared contract for CLI JSON,
MCP structuredContent, and the GitHub Action artifact.
"""

from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1"


class InvariantError(Exception):
    """A report violated the frozen row-arithmetic invariants."""


@dataclass
class RowCounts:
    a: int
    b: int
    matched: int
    added: int
    removed: int
    modified: int
    unchanged: int

    def validate(self) -> None:
        # Frozen invariants: a = matched+removed, b = matched+added, matched = modified+unchanged
        if self.a != self.matched + self.removed:
            raise InvariantError(
                f"rows.a != matched+removed ({self.a} vs {self.matched + self.removed})"
            )
        if self.b != self.matched + self.added:
            raise InvariantError(
                f"rows.b != matched+added ({self.b} vs {self.matched + self.added})"
            )
        if self.matched != self.modified + self.unchanged:
            raise InvariantError(
                f"rows.matched != modified+unchanged "
                f"({self.matched} vs {self.modified + self.unchanged})"
            )


@dataclass
class ColumnChange:
    name: str
    changed_rows: int
    change_rate: float | None  # changed_rows / rows.matched; None when matched == 0
    compared_as: str | None = None  # "json_text" | None
    stats: dict[str, int] = field(default_factory=dict)  # nulls_a, nulls_b (presence-gated)


@dataclass
class SchemaDiff:
    columns_added: list[dict[str, str]] = field(default_factory=list)
    columns_removed: list[dict[str, str]] = field(default_factory=list)
    columns_type_changed: list[dict[str, Any]] = field(default_factory=list)
    columns_skipped: list[dict[str, str]] = field(default_factory=list)
    identical: bool = True


@dataclass
class PrimaryKey:
    columns: list[str]
    inferred: bool
    rule: str | None = None  # which inference rule matched


@dataclass
class ExecutionInfo:
    elapsed_seconds: float
    statements: int
    snapshot: str | None = None
    warnings: list[str] = field(default_factory=list)
    capped: bool = False


@dataclass
class DiffReport:
    status: str  # ok | error
    strategy: str  # localdiff | joindiff
    tables: dict[str, str]
    primary_key: PrimaryKey
    rows: RowCounts
    schema_diff: SchemaDiff
    columns: list[ColumnChange]
    samples: dict[str, list[Any]]
    execution: ExecutionInfo
    attribution: Any | None = None  # core.attribution.Attribution when computed
    report_schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.rows.validate()

    @property
    def has_diff(self) -> bool:
        return (self.rows.added + self.rows.removed + self.rows.modified) > 0

    def column(self, name: str) -> ColumnChange:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))

    def to_json(self, indent: int | None = 2) -> str:
        # allow_nan=False: a non-finite float leaking past _plain must fail
        # loudly, never emit invalid JSON (this is the MCP/Action contract).
        return json.dumps(self.to_dict(), indent=indent, allow_nan=False)


def _plain(value: Any) -> Any:
    """Make values JSON-serializable without losing information silently."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        if value != value:
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value
