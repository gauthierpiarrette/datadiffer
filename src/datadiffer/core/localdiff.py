"""Local diff engine: both sides in one DuckDB connection.

Explicit ON join with per-side presence markers (never USING). The CTEs project
ONLY key + compared columns under internal ``_dd_*`` aliases — user column names
never appear in the join output, so no name (``_p``, ``_added``, ...) can shadow
an internal marker. The join result is materialized to a temp table in OUR
scratch DuckDB (the no-materialization rule applies to user warehouses, not
local scratch), so counts and samples read one consistent snapshot.
"""

from __future__ import annotations

import time

import duckdb

from datadiffer.errors import DatadifferError
from datadiffer.report.model import (
    ColumnChange,
    DiffReport,
    ExecutionInfo,
    PrimaryKey,
    RowCounts,
    SchemaDiff,
)

# DuckDB type prefixes we compare directly (same engine on both sides).
_DIRECT = ("BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
           "USMALLINT", "UINTEGER", "UBIGINT", "DECIMAL", "FLOAT", "DOUBLE", "REAL",
           "VARCHAR", "DATE", "TIME", "TIMESTAMP", "BLOB", "UUID", "INTERVAL", "BIT",
           "ENUM")
# Nested/JSON-ish types are compared as engine-normalized JSON text.
_JSON_COERCE = ("STRUCT", "MAP", "UNION", "JSON")

_J = "__dd_j"  # temp join table (our scratch connection; not user namespace)


def read_schema(con: duckdb.DuckDBPyConnection, rel: str, label: str) -> dict[str, str]:
    try:
        rows = con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
    except duckdb.Error as e:
        raise DatadifferError(f"Cannot read {label}: {e}") from e
    return {r[0]: r[1] for r in rows}


def run_localdiff(
    con: duckdb.DuckDBPyConnection,
    rel_a: str,
    rel_b: str,
    displays: tuple[str, str],
    schema_a: dict[str, str],
    schema_b: dict[str, str],
    keys: list[str],
    key_meta: tuple[bool, str | None],
    where_sql: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    sample_limit: int = 10,
    base_statements: int = 0,
    extra_warnings: list[str] | None = None,
    attribution: bool = True,
    snapshot: str = "single_connection",
    source_schemas: tuple[dict, dict] | None = None,
) -> DiffReport:
    t0 = time.monotonic()
    count = {"n": base_statements}

    def ex(sql: str):
        count["n"] += 1
        return con.execute(sql)

    sd, compare, warnings = _align(schema_a, schema_b, keys, include, exclude,
                                   source_schemas=source_schemas)
    warnings = (extra_warnings or []) + warnings

    where = f" WHERE {where_sql}" if where_sql else ""
    kalias = [f"_dd_k_{i}" for i in range(len(keys))]

    def cte(rel: str) -> str:
        proj = [f"{_q(k)} AS {kalias[i]}" for i, k in enumerate(keys)]
        proj += [
            f"{_val_expr(col, mode)} AS _dd_c_{i}" for i, (col, mode) in enumerate(compare)
        ]
        proj.append("TRUE AS _dd_p")
        return f"SELECT {', '.join(proj)} FROM {rel}{where}"

    on = " AND ".join(f"a.{k} = b.{k}" for k in kalias)
    sel = [f"COALESCE(a.{k}, b.{k}) AS {k}" for k in kalias]
    sel += [
        "(a._dd_p IS NULL) AS _dd_added",
        "(b._dd_p IS NULL) AS _dd_removed",
        "(a._dd_p IS NOT NULL AND b._dd_p IS NOT NULL) AS _dd_matched",
    ]
    for i in range(len(compare)):
        sel += [
            f"a._dd_c_{i} AS _dd_a_{i}",
            f"b._dd_c_{i} AS _dd_b_{i}",
            f"CASE WHEN a._dd_p IS NOT NULL AND b._dd_p IS NOT NULL"
            f" AND a._dd_c_{i} IS DISTINCT FROM b._dd_c_{i} THEN 1 ELSE 0 END AS _dd_d_{i}",
        ]
    dirty = " + ".join(f"_dd_d_{i}" for i in range(len(compare))) or "0"
    # DuckDB lateral alias reference: _dd_dirty can read the _dd_d_* aliases
    # defined in the same SELECT list.
    sel.append(f"(({dirty}) > 0) AS _dd_dirty")

    ex(f"""
        CREATE OR REPLACE TEMP TABLE {_J} AS
        WITH a AS ({cte(rel_a)}), b AS ({cte(rel_b)})
        SELECT {', '.join(sel)}
        FROM a FULL OUTER JOIN b ON {on}
    """)

    aggs = [
        "SUM(CASE WHEN _dd_added THEN 1 ELSE 0 END)",
        "SUM(CASE WHEN _dd_removed THEN 1 ELSE 0 END)",
        "SUM(CASE WHEN _dd_matched THEN 1 ELSE 0 END)",
        "SUM(CASE WHEN _dd_matched AND _dd_dirty THEN 1 ELSE 0 END)",
    ]
    for i in range(len(compare)):
        aggs += [
            f"SUM(_dd_d_{i})",
            f"SUM(CASE WHEN NOT _dd_added AND _dd_a_{i} IS NULL THEN 1 ELSE 0 END)",
            f"SUM(CASE WHEN NOT _dd_removed AND _dd_b_{i} IS NULL THEN 1 ELSE 0 END)",
        ]
    row = ex(f"SELECT {', '.join(aggs)} FROM {_J}").fetchone()

    added, removed, matched, modified = (int(row[i] or 0) for i in range(4))
    rows = RowCounts(
        a=matched + removed, b=matched + added, matched=matched,
        added=added, removed=removed, modified=modified, unchanged=matched - modified,
    )

    columns: list[ColumnChange] = []
    for i, (col, mode) in enumerate(compare):
        changed = int(row[4 + 3 * i] or 0)
        columns.append(ColumnChange(
            name=col,
            changed_rows=changed,
            change_rate=(changed / matched) if matched else None,
            compared_as="json_text" if mode == "json" else None,
            stats={"nulls_a": int(row[5 + 3 * i] or 0), "nulls_b": int(row[6 + 3 * i] or 0)},
        ))

    samples = _samples(ex, keys, kalias, compare, sample_limit)

    attribution_obj = None
    if attribution and (added + removed + modified) > 0:
        from datadiffer.core.attribution import compute_attribution

        attribution_obj = compute_attribution(
            ex, _J, compare, schema_a, schema_b, keys,
            {"added": added, "removed": removed, "modified": modified},
        )
    ex(f"DROP TABLE IF EXISTS {_J}")

    return DiffReport(
        attribution=attribution_obj,
        status="ok",
        strategy="localdiff",
        tables={"a": displays[0], "b": displays[1]},
        primary_key=PrimaryKey(columns=keys, inferred=key_meta[0], rule=key_meta[1]),
        rows=rows,
        schema_diff=sd,
        columns=columns,
        samples=samples,
        execution=ExecutionInfo(
            elapsed_seconds=round(time.monotonic() - t0, 3),
            statements=count["n"],
            snapshot=snapshot,
            warnings=warnings,
        ),
    )


def _align(
    schema_a: dict[str, str],
    schema_b: dict[str, str],
    keys: list[str],
    include: list[str] | None,
    exclude: list[str] | None,
    source_schemas: tuple[dict, dict] | None = None,
) -> tuple[SchemaDiff, list[tuple[str, str]], list[str]]:
    """Fail-closed: only columns with a known comparison mode get a predicate.

    ``include`` (--columns) is applied first; ``exclude`` then subtracts.
    Unknown names in either are an error — a typo must never silently pass CI.
    """
    common = [c for c in schema_a if c in schema_b]
    for k in keys:
        if k not in common:
            raise DatadifferError(f"Key column {k!r} must exist on both sides")
        if not _comparable(schema_a[k], schema_b[k]):
            raise DatadifferError(
                f"Key column {k!r} has incomparable types across sides: "
                f"{schema_a[k]} vs {schema_b[k]}"
            )
    for flag, names in (("--columns", include), ("--exclude-columns", exclude)):
        unknown = [n for n in names or [] if n not in common]
        if unknown:
            raise DatadifferError(
                f"{flag} names not present on both sides: {', '.join(unknown)}. "
                f"Common columns: {', '.join(common)}"
            )

    # Added/removed come from the SOURCE schemas: a projected pull must never
    # make a dropped column invisible or an excluded one look "added".
    # type_changed stays engine-level (cross-source native names never match).
    src_a, src_b = source_schemas or (schema_a, schema_b)
    sd = SchemaDiff(
        columns_added=[{"name": c, "type": t} for c, t in src_b.items() if c not in src_a],
        columns_removed=[{"name": c, "type": t} for c, t in src_a.items() if c not in src_b],
    )
    compare: list[tuple[str, str]] = []  # (column, mode: direct|json)
    candidates = 0
    for col in common:
        type_a, type_b = schema_a[col], schema_b[col]
        if col in keys:
            continue
        candidates += 1
        if include and col not in include:
            continue
        if exclude and col in exclude:
            continue
        mode = _mode(type_a, type_b)
        if mode == "skip":
            sd.columns_skipped.append(
                {"column": col, "reason": f"type_mismatch:{type_a}!={type_b}"}
            )
        elif mode == "unsupported":
            sd.columns_skipped.append({"column": col, "reason": f"unsupported_type:{type_a}"})
        else:
            compare.append((col, mode))
        if type_a != type_b:
            sd.columns_type_changed.append(
                {"name": col, "type_a": type_a, "type_b": type_b, "compared": mode != "skip"}
            )
    sd.identical = not (
        sd.columns_added or sd.columns_removed or sd.columns_type_changed or sd.columns_skipped
    )

    warnings: list[str] = []
    if not compare and candidates:
        if include or exclude:
            raise DatadifferError(
                "--columns/--exclude-columns left no columns to compare"
            )
        warnings.append("no_comparable_columns")
    return sd, compare, warnings


def _mode(type_a: str, type_b: str) -> str:
    ca, cb = _category(type_a), _category(type_b)
    if ca == "unsupported" or cb == "unsupported":
        return "unsupported"
    if ca == cb == "direct":
        return "direct" if _comparable(type_a, type_b) else "skip"
    if "json" in (ca, cb):
        return "json"
    return "skip"


def _category(t: str) -> str:
    up = t.upper()
    if up.startswith(_JSON_COERCE) or "[]" in up or up.startswith("LIST"):
        return "json"
    if up.startswith(_DIRECT):
        return "direct"
    return "unsupported"


_NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
            "UINTEGER", "UBIGINT", "DECIMAL", "FLOAT", "DOUBLE", "REAL")


def _comparable(type_a: str, type_b: str) -> bool:
    a, b = type_a.upper(), type_b.upper()
    if a == b:
        return True
    return a.startswith(_NUMERIC) and b.startswith(_NUMERIC)


def _val_expr(col: str, mode: str) -> str:
    return f"to_json({_q(col)})" if mode == "json" else _q(col)


def _samples(ex, keys, kalias, compare, limit) -> dict[str, list]:
    key_cols = ", ".join(kalias)
    out: dict[str, list] = {"added": [], "removed": [], "modified": []}

    for status, flag, side in (("added", "_dd_added", "b"), ("removed", "_dd_removed", "a")):
        vals = ", ".join(f"_dd_{side}_{i}" for i in range(len(compare)))
        sel = f"{key_cols}{', ' + vals if vals else ''}"
        for r in ex(
            f"SELECT {sel} FROM {_J} WHERE {flag} ORDER BY {key_cols} LIMIT {limit}"
        ).fetchall():
            rec = dict(zip(keys, r[: len(keys)], strict=True))
            rec.update({c: v for (c, _), v in zip(compare, r[len(keys):], strict=True)})
            out[status].append(rec)

    if compare:
        dcols = ", ".join(f"_dd_d_{i}, _dd_a_{i}, _dd_b_{i}" for i in range(len(compare)))
        for r in ex(
            f"SELECT {key_cols}, {dcols} FROM {_J} "
            f"WHERE _dd_matched AND _dd_dirty ORDER BY {key_cols} LIMIT {limit}"
        ).fetchall():
            key = dict(zip(keys, r[: len(keys)], strict=True))
            changes = {}
            for i, (c, _) in enumerate(compare):
                d, va, vb = r[len(keys) + 3 * i: len(keys) + 3 * i + 3]
                if d:
                    changes[c] = {"a": va, "b": vb}
            out["modified"].append({"key": key, "changes": changes})
    return out


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
