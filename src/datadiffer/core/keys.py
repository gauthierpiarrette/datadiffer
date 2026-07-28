"""Primary-key validation and conservative inference.

Precedence: declared PK/UNIQUE constraints (the only source of composite
inference) -> user --key -> conventional-name candidates. Every key is checked
non-null AND unique on both FILTERED sides; unique alone is not identifying, and a
key only needs to hold within the --where slice being diffed.
"""

from __future__ import annotations

import duckdb

from datadiffer.errors import DatadifferError

_ELIGIBLE_PREFIXES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
                      "USMALLINT", "UINTEGER", "UBIGINT", "VARCHAR", "UUID")


def resolve_keys(
    con: duckdb.DuckDBPyConnection,
    rel_a: str,
    rel_b: str,
    schema: dict[str, str],
    user_keys: list[str] | None,
    stems: tuple[str, str],
    where_sql: str | None = None,
    declared: list[list[str]] | None = None,
) -> tuple[list[str], bool, str | None, int]:
    """Return (key_columns, inferred, rule, probe_statements)."""
    probes = {"n": 0}

    if user_keys:
        for k in user_keys:
            if k not in schema:
                raise DatadifferError(f"--key column {k!r} not found in both tables")
        _check_key(con, rel_a, rel_b, user_keys, where_sql, probes, label="--key")
        return user_keys, False, None, probes["n"]

    for cols in declared or []:
        if all(c in schema for c in cols):
            try:
                _check_key(con, rel_a, rel_b, cols, where_sql, probes)
            except DatadifferError:
                continue
            return cols, True, "declared", probes["n"]

    for rule, candidate in _candidates(schema, stems):
        try:
            _check_key(con, rel_a, rel_b, [candidate], where_sql, probes)
        except DatadifferError:
            continue
        return [candidate], True, rule, probes["n"]

    raise DatadifferError(
        "Could not infer a primary key (no declared constraint or conventional-name "
        "column is non-null and unique on both sides). "
        "Pass one explicitly: --key <column>[,<column>]"
    )


def _candidates(schema: dict[str, str], stems: tuple[str, str]):
    """Yield (rule, column) in deterministic precedence order, max 5 candidates."""
    eligible = [c for c, t in schema.items() if t.upper().startswith(_ELIGIBLE_PREFIXES)]
    seen: list[str] = []

    def take(rule: str, cols: list[str]):
        for c in cols:
            if c in eligible and c not in seen:
                seen.append(c)
                yield rule, c

    yield from take("exact_id", [c for c in eligible if c.lower() == "id"])
    stem_ids = [f"{s.lower()}_id" for s in stems]
    yield from take("table_id", [c for c in eligible if c.lower() in stem_ids])
    suffixed = [c for c in eligible if c.lower().endswith(("_id", "_key"))]
    for rule, c in take("suffix", suffixed):
        if len(seen) > 5:
            return
        yield rule, c


def _check_key(
    con: duckdb.DuckDBPyConnection,
    rel_a: str,
    rel_b: str,
    keys: list[str],
    where_sql: str | None,
    probes: dict[str, int],
    label: str | None = None,
) -> None:
    """Non-null and unique on both filtered sides. The GROUP BY/HAVING form
    also yields the duplicate sample shown in the error."""
    klist = ", ".join(_q(k) for k in keys)
    for side, rel in (("a", rel_a), ("b", rel_b)):
        src = f"(SELECT * FROM {rel} WHERE {where_sql})" if where_sql else rel
        null_pred = " OR ".join(f"{_q(k)} IS NULL" for k in keys)
        probes["n"] += 1
        (nulls,) = con.execute(
            f"SELECT count(*) FROM (SELECT 1 FROM {src} AS t WHERE {null_pred} LIMIT 1)"
        ).fetchone()
        if nulls:
            raise DatadifferError(
                f"Key ({', '.join(keys)}) has NULLs on side {side}"
                + (f" ({label} is not usable as a key)" if label else "")
            )
        probes["n"] += 1
        dupes = con.execute(
            f"SELECT {klist}, count(*) AS n FROM {src} AS t "
            f"GROUP BY {klist} HAVING count(*) > 1 LIMIT 5"
        ).fetchall()
        if dupes:
            sample = ", ".join(str(tuple(d[:-1])) for d in dupes[:5])
            raise DatadifferError(
                f"Key ({', '.join(keys)}) is not unique on side {side}. "
                f"Duplicate keys (first 5): {sample}"
            )


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
