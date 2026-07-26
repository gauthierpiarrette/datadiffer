"""Report JSON schema v1 — FROZEN.

This is the stable contract shared by CLI ``--format json``, MCP
``structuredContent``, and the GitHub Action artifact. Within v1, changes are
ADDITIVE ONLY: new optional fields may appear; nothing listed here may be
renamed, removed, retyped, or have its semantics changed. Consumers must
tolerate unknown fields.

Frozen invariants: rows.a == rows.matched + rows.removed;
rows.b == rows.matched + rows.added; rows.matched == rows.modified +
rows.unchanged. change_rate == changed_rows / rows.matched (null when
matched == 0). attribution is null when disabled, absent never.
"""

from __future__ import annotations

# {field: type-or-nested-spec}; every listed field is REQUIRED and frozen.
REPORT_V1: dict = {
    "report_schema_version": str,
    "status": str,          # ok | refused_cap | refused_no_key | timeout | error
    "strategy": str,        # localdiff | joindiff (push-down, v0.2)
    "tables": {"a": str, "b": str},
    "primary_key": {"columns": list, "inferred": bool},
    "rows": {
        "a": int, "b": int, "matched": int,
        "added": int, "removed": int, "modified": int, "unchanged": int,
    },
    "schema_diff": {
        "columns_added": list, "columns_removed": list,
        "columns_type_changed": list, "columns_skipped": list, "identical": bool,
    },
    "columns": list,        # [{name, changed_rows, change_rate, compared_as, stats}]
    "samples": {"added": list, "removed": list, "modified": list},
    "execution": {
        "elapsed_seconds": (int, float), "statements": int, "warnings": list,
    },
}

COLUMN_V1 = {"name": str, "changed_rows": int, "stats": dict}
SEGMENT_V1 = {
    "column": str, "rows": int, "support": (int, float),
    "baseline_share": (int, float), "lift": (int, float), "score": (int, float),
}


def validate_report(d: dict) -> list[str]:
    """Return a list of contract violations (empty == conforming)."""
    problems: list[str] = []
    _check(d, REPORT_V1, "", problems)
    for col in d.get("columns", []):
        _check(col, COLUMN_V1, f"columns[{col.get('name', '?')}].", problems)
    attr = d.get("attribution")
    if attr is not None:
        for status, sa in attr.get("by_status", {}).items():
            for seg in sa.get("segments", []):
                _check(seg, SEGMENT_V1, f"attribution.{status}.", problems)
    return problems


def _check(d, spec, prefix, problems) -> None:
    if not isinstance(d, dict):
        problems.append(f"{prefix or 'report'}: expected object")
        return
    for field, expected in spec.items():
        if field not in d:
            problems.append(f"{prefix}{field}: missing")
        elif isinstance(expected, dict):
            _check(d[field], expected, f"{prefix}{field}.", problems)
        elif d[field] is not None and not isinstance(d[field], expected):
            problems.append(
                f"{prefix}{field}: expected {expected}, got {type(d[field]).__name__}"
            )
