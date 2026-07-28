"""Local sources: Parquet, CSV, and DuckDB files opened into one shared connection."""

from __future__ import annotations

import os
from dataclasses import dataclass

import duckdb

from datadiffer.errors import DatadifferError


@dataclass
class LocalSource:
    rel: str  # FROM-able SQL fragment inside the shared connection
    display: str  # human label for the report
    stem: str  # table-ish name used by key inference (<stem>_id)
    kind: str  # parquet | csv | duckdb
    alias: str | None = None  # attach alias (duckdb kind)
    schema: str | None = None  # schema inside the attached db (duckdb kind)
    table: str | None = None  # table name (duckdb kind)


def open_source(con: duckdb.DuckDBPyConnection, locator: str, alias: str) -> LocalSource:
    """Resolve a locator into a relation inside `con`. Read-only."""
    if "://" in locator:
        raise DatadifferError(
            f"{locator!r} looks like a connection URI. Pass warehouse tables as "
            "<connection>::<table> using datadiffer.toml, or as a bare table name "
            "with --source. Local files: .parquet, .csv, file.duckdb:table."
        )
    if "::" in locator:
        raise DatadifferError(
            f"Unexpected '::' in locator {locator!r}: conn::table locators are "
            "resolved via datadiffer.toml (run `datadiffer connections list`)."
        )

    lower = locator.lower()
    if lower.endswith((".parquet", ".pq")):
        _require_file(locator)
        return LocalSource(
            rel=f"read_parquet({_sqlstr(locator)})",
            display=locator, stem=_stem(locator), kind="parquet",
        )
    if lower.endswith(".csv"):
        _require_file(locator)
        return LocalSource(
            rel=f"read_csv({_sqlstr(locator)})",
            display=locator, stem=_stem(locator), kind="csv",
        )
    if ".duckdb:" in lower:
        path, _, table = locator.rpartition(":")
        _require_file(path)
        parts = table.split(".")
        if len(parts) > 2 or not all(parts):
            raise DatadifferError(
                f"Bad table reference {table!r}, use <table> or <schema>.<table> "
                "(a literal dot in a table name is not supported by this locator form)"
            )
        try:
            con.execute(f"ATTACH {_sqlstr(path)} AS {alias} (READ_ONLY)")
        except duckdb.Error as e:
            raise DatadifferError(f"Cannot open DuckDB file {path!r}: {e}") from e
        schema = parts[0] if len(parts) == 2 else None
        rel = alias + "." + ".".join(_qident(p) for p in parts)
        return LocalSource(
            rel=rel, display=locator, stem=parts[-1], kind="duckdb",
            alias=alias, schema=schema, table=parts[-1],
        )
    if lower.endswith(".duckdb"):
        raise DatadifferError(f"Specify a table for DuckDB files: {locator}:<table>")
    raise DatadifferError(
        f"Cannot resolve table locator {locator!r}. "
        "Supported: file.parquet, file.csv, file.duckdb:table, "
        "<connection>::<table> from datadiffer.toml (see `datadiffer connections list`), "
        "or a bare [schema.]table name together with --source <postgresql://...>."
    )


def declared_key_sets(con: duckdb.DuckDBPyConnection, src: LocalSource) -> list[list[str]]:
    """Declared PRIMARY KEY / UNIQUE column sets for an attached DuckDB table."""
    if src.kind != "duckdb":
        return []
    sql = (
        "SELECT constraint_type, constraint_column_names FROM duckdb_constraints() "
        "WHERE database_name = ? AND table_name = ? "
        "AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')"
    )
    params: list[str] = [src.alias or "", src.table or ""]
    if src.schema:
        sql += " AND schema_name = ?"
        params.append(src.schema)
    rows = con.execute(sql, params).fetchall()
    rows.sort(key=lambda r: r[0] != "PRIMARY KEY")  # PK first
    return [list(r[1]) for r in rows]


def _require_file(path: str) -> None:
    if not os.path.exists(path):
        raise DatadifferError(f"File not found: {path}")


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _sqlstr(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
