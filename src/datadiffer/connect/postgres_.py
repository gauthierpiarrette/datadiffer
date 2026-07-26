"""Postgres sources via DuckDB's core postgres extension (read-only ATTACH).

The extension scans Postgres over binary COPY; control-channel lookups
(row/byte estimates, declared keys) go through postgres_query() on the same
attached connection. Reads pull through the attach into the local engine, and that path is
UNSNAPSHOTTED across passes (the caller records the warning).
"""

from __future__ import annotations

import re

import duckdb

from datadiffer.connect.duckdb_ import LocalSource, _qident, _sqlstr
from datadiffer.errors import DatadifferError

ROW_HARD_CAP = 50_000_000
ROW_WARN = 10_000_000
BYTE_HARD_CAP = 10 * 1024**3  # 10 GiB/side — bytes, not rows, are the real limit

_SECRET_PARAMS = re.compile(r"(?i)\b(password|sslpassword|sslkey)=[^&\s]*")
_USERINFO = re.compile(r"(?i)(://)[^/@\s]+@")


def redact(uri: str) -> str:
    """Strip credentials for display — this string lands in reports and logs.
    Covers both userinfo (user:pass@) and libpq query params (?password=...)."""
    scheme, sep, rest = uri.partition("://")
    if sep and "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    rest = _SECRET_PARAMS.sub(lambda m: m.group(0).split("=")[0] + "=***", rest)
    return f"{scheme}://{rest}" if sep else _SECRET_PARAMS.sub(
        lambda m: m.group(0).split("=")[0] + "=***", uri
    )


def redact_text(text: str, uri: str | None = None) -> str:
    """Scrub credentials from arbitrary text (driver error messages embed the
    raw DSN — the failed-connection path is where passwords leak)."""
    if uri:
        text = text.replace(uri, redact(uri))
    text = _USERINFO.sub(r"\1", text)
    return _SECRET_PARAMS.sub(lambda m: m.group(0).split("=")[0] + "=***", text)



def open_postgres(
    con: duckdb.DuckDBPyConnection, uri: str, table_ref: str, alias: str
) -> LocalSource:
    """ATTACH a Postgres database read-only and resolve <schema>.<table>."""
    if "://" in table_ref:
        raise DatadifferError(
            f"{table_ref!r} looks like a connection URI — pass the URI via --source "
            "and a bare [schema.]table name as the locator."
        )
    if "::" in table_ref:
        raise DatadifferError(f"Bad table reference {table_ref!r}.")
    parts = table_ref.split(".")
    if len(parts) > 2 or not all(parts):
        raise DatadifferError(
            f"Bad table reference {table_ref!r} — use <table> or <schema>.<table>"
        )
    schema = parts[0] if len(parts) == 2 else "public"
    table = parts[-1]
    try:
        con.execute(f"ATTACH {_sqlstr(uri)} AS {alias} (TYPE postgres, READ_ONLY)")
    except duckdb.Error as e:
        raise DatadifferError(
            f"Cannot connect to Postgres: {redact_text(str(e), uri)}"
        ) from e
    return LocalSource(
        rel=f"{alias}.{_qident(schema)}.{_qident(table)}",
        display=f"{table_ref} ({redact(uri)})",
        stem=table, kind="postgres", alias=alias, schema=schema, table=table,
    )


def _pg_query(con: duckdb.DuckDBPyConnection, alias: str, pg_sql: str):
    return con.execute(
        f"SELECT * FROM postgres_query({_sqlstr(alias)}, {_sqlstr(pg_sql)})"
    )


def estimated_rows(con: duckdb.DuckDBPyConnection, src: LocalSource) -> int | None:
    """Planner estimate from pg_class.reltuples — cheap, no table scan.
    Returns None when unknown (reltuples is -1 on never-analyzed tables)."""
    q = (
        "SELECT reltuples::bigint FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {_pg_lit(src.schema or 'public')} "
        f"AND c.relname = {_pg_lit(src.table or '')}"
    )
    try:
        row = _pg_query(con, src.alias or "", q).fetchone()
    except duckdb.Error:
        return None
    return int(row[0]) if row and row[0] is not None and row[0] >= 0 else None


def estimated_bytes(con: duckdb.DuckDBPyConnection, src: LocalSource) -> int | None:
    q = (
        "SELECT pg_total_relation_size((quote_ident("
        f"{_pg_lit(src.schema or 'public')}) || '.' || "
        f"quote_ident({_pg_lit(src.table or '')}))::regclass)"
    )
    try:
        row = _pg_query(con, src.alias or "", q).fetchone()
    except duckdb.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def check_row_guard(con: duckdb.DuckDBPyConnection, src: LocalSource) -> list[str]:
    """Admission caps. Returns warnings; raises on hard caps."""
    warnings: list[str] = []
    est = estimated_rows(con, src)
    if est is None:
        warnings.append(f"row_estimate_unavailable:{src.stem}")
    elif est > ROW_HARD_CAP:
        raise DatadifferError(
            f"{src.display} has ~{est:,} rows — over the {ROW_HARD_CAP:,} cross-source cap. "
            "Narrow the diff with --where."
        )
    elif est > ROW_WARN:
        warnings.append(f"large_pull:{src.stem}:~{est}")

    size = estimated_bytes(con, src)
    if size is None:
        warnings.append(f"byte_estimate_unavailable:{src.stem}")
    elif size > BYTE_HARD_CAP:
        raise DatadifferError(
            f"{src.display} is ~{size / 1024**3:.1f} GiB — over the "
            f"{BYTE_HARD_CAP / 1024**3:.0f} GiB cross-source cap. "
            "Narrow the diff with --where or exclude wide columns."
        )
    return warnings


def declared_key_sets_pg(
    con: duckdb.DuckDBPyConnection, src: LocalSource, warnings: list[str]
) -> list[list[str]]:
    """Declared PRIMARY KEY / UNIQUE column sets via information_schema.

    Casts to ::text — information_schema columns are domain types the DuckDB
    postgres extension would otherwise degrade to VARCHAR blobs. Join carries
    table_name: Postgres constraint names are unique per table, not schema.
    """
    q = (
        "SELECT tc.constraint_type::text, "
        "array_agg(kcu.column_name::text ORDER BY kcu.ordinal_position) AS cols "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON kcu.constraint_name = tc.constraint_name "
        "AND kcu.constraint_schema = tc.constraint_schema "
        "AND kcu.table_schema = tc.table_schema "
        "AND kcu.table_name = tc.table_name "
        f"WHERE tc.table_schema = {_pg_lit(src.schema or 'public')} "
        f"AND tc.table_name = {_pg_lit(src.table or '')} "
        "AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') "
        "GROUP BY tc.constraint_name, tc.constraint_type"
    )
    try:
        rows = _pg_query(con, src.alias or "", q).fetchall()
    except duckdb.Error:
        # Degradation must be visible: inference silently falling back from
        # 'declared' to name conventions is miserable to debug otherwise.
        warnings.append(f"declared_keys_unavailable:{src.stem}")
        return []
    rows.sort(key=lambda r: (r[0] != "PRIMARY KEY", list(r[1])))
    return [list(r[1]) for r in rows]


def _pg_lit(s: str) -> str:
    """A single-quoted literal INSIDE the postgres_query SQL string."""
    return "'" + s.replace("'", "''") + "'"
