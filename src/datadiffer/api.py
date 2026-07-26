"""Public API: datadiffer.diff(a, b, ...) -> DiffReport."""

from __future__ import annotations

import threading

import duckdb
import sqlglot
from sqlglot import exp

from datadiffer.connect.duckdb_ import LocalSource, declared_key_sets, open_source
from datadiffer.connect.postgres_ import (
    check_row_guard,
    declared_key_sets_pg,
    open_postgres,
)
from datadiffer.core.keys import resolve_keys
from datadiffer.core.localdiff import read_schema, run_localdiff
from datadiffer.errors import DatadifferError, DiffTimeout
from datadiffer.report.model import DiffReport


def diff(
    a: str,
    b: str,
    *,
    source: str | None = None,
    target: str | None = None,
    keys: list[str] | None = None,
    columns: list[str] | None = None,
    exclude_columns: list[str] | None = None,
    where: str | None = None,
    sample_limit: int = 10,
    attribution: str = "auto",
    strict_where: bool = False,
    timeout_seconds: float | None = None,
) -> DiffReport:
    """Diff two tables and return a semantic change report.

    ``a`` / ``b`` are table locators: a local file (``file.parquet``,
    ``file.csv``, ``file.duckdb:table``) or a bare ``[schema.]table`` name
    resolved against ``source`` / ``target`` (``postgresql://`` URIs; ``target``
    defaults to ``source``). Postgres is attached READ-ONLY and pulled through
    the local engine.

    ``columns`` selects what to compare (applied first); ``exclude_columns``
    then subtracts. Unknown names in either are an error, never ignored.
    """
    if attribution not in ("auto", "off"):
        raise DatadifferError(f"attribution must be 'auto' or 'off', got {attribution!r}")
    con = duckdb.connect(":memory:")
    watchdog: threading.Timer | None = None
    handles: list = []
    if timeout_seconds:
        # DuckDB has no statement_timeout setting — a watchdog
        # thread interrupts the connection so the query is actually cancelled.
        watchdog = threading.Timer(timeout_seconds, con.interrupt)
        watchdog.daemon = True
        watchdog.start()
    try:
        warnings: list[str] = []
        raw_a = _open_side(con, a, source, "src_a", warnings, handles, timeout_seconds)
        raw_b = _open_side(con, b, target or source, "src_b", warnings, handles, timeout_seconds)

        schema_a = _side_schema(con, raw_a, "a")
        schema_b = _side_schema(con, raw_b, "b")
        common = {c: t for c, t in schema_a.items() if c in schema_b}
        if not common:
            raise DatadifferError("Tables share no columns — nothing to diff.")

        # Column filters are validated against the ORIGINAL common set, before
        # projection removes excluded columns from pulled copies.
        _validate_col_filters(common, columns, exclude_columns)
        where_sql, where_cols = (
            _validate_where(where, common, strict=strict_where) if where else (None, set())
        )

        # Snowflake sides materialize now: projected pull (excluded columns
        # never cross the wire), one SELECT = one consistent snapshot. The
        # engine then works on the LOCAL schemas (DuckDB types).
        src_a, engine_a = _materialize(con, raw_a, schema_a, common, keys, columns,
                                       exclude_columns, where_cols, "src_a")
        src_b, engine_b = _materialize(con, raw_b, schema_b, common, keys, columns,
                                       exclude_columns, where_cols, "src_b")
        engine_common = {c: t for c, t in engine_a.items() if c in engine_b}
        snapshot = _snapshot_label(raw_a, raw_b)
        include_p = [c for c in columns or [] if c in engine_common] or None
        exclude_p = [c for c in exclude_columns or [] if c in engine_common] or None

        declared = _shared_declared_keys(con, src_a, src_b, warnings, raw_a, raw_b)
        key_cols, inferred, rule, probes = resolve_keys(
            con, src_a.rel, src_b.rel, engine_common, keys,
            (src_a.stem, src_b.stem), where_sql, declared,
        )
        # Guard against include/exclude leaving nothing to compare — checked
        # against the ORIGINAL pre-pull common so pruning cannot mask it.
        if columns or exclude_columns:
            candidates = [c for c in common if c not in key_cols]
            survivors = [
                c for c in candidates
                if (not columns or c in columns)
                and not (exclude_columns and c in exclude_columns)
            ]
            if candidates and not survivors:
                raise DatadifferError("--columns/--exclude-columns left no columns to compare")
        return run_localdiff(
            con,
            src_a.rel,
            src_b.rel,
            (src_a.display, src_b.display),
            engine_a,
            engine_b,
            key_cols,
            (inferred, rule),
            where_sql,
            include_p,
            exclude_p,
            sample_limit=sample_limit,
            base_statements=2 + probes,  # the two DESCRIBEs + key probes
            extra_warnings=warnings,
            attribution=attribution != "off",
            snapshot=snapshot,
            source_schemas=(schema_a, schema_b),
        )
    except duckdb.Error as e:
        # Operational failures must surface as exit 2, never as "diff found".
        if "interrupt" in str(e).lower() or isinstance(
            e, getattr(duckdb, "InterruptException", ())
        ):
            raise DiffTimeout(
                f"Diff timed out after {timeout_seconds}s and the query was cancelled."
            ) from e
        raise DatadifferError(f"Engine error: {e}") from e
    finally:
        if watchdog is not None:
            watchdog.cancel()
        for h in handles:
            h.close()
        con.close()


def _open_side(
    con, locator: str, uri: str | None, alias: str, warnings: list[str],
    handles: list | None = None, timeout_seconds: float | None = None,
) -> LocalSource:
    if "::" in locator:
        from datadiffer.config import connection

        name, _, table = locator.partition("::")
        if "::" in table or not table:
            raise DatadifferError(f"Bad table reference {table!r}.")
        conn = connection(name)
        ctype = conn.get("type")
        if ctype == "postgres":
            src = open_postgres(con, conn.get("dsn", ""), table, alias)
            warnings.extend(check_row_guard(con, src))
            _mark_unsnapshotted(warnings)
            return src
        if ctype == "duckdb":
            path = conn.get("path", "")
            if not path.lower().endswith(".duckdb"):
                raise DatadifferError(
                    f"Connection {name!r}: path must end in .duckdb (got {path!r})"
                )
            return open_source(con, f"{path}:{table}", alias)
        if ctype == "snowflake":
            from datadiffer.connect.snowflake_ import SnowflakeHandle

            handle = SnowflakeHandle.open(conn, table, timeout_seconds)
            if handles is not None:
                # Register for cleanup BEFORE guard() — any failure from here
                # on must not leak a live warehouse session.
                handles.append(handle)
            warnings.extend(handle.guard())
            return handle
        raise DatadifferError(
            f"Connection {name!r} has unsupported type {ctype!r} "
            "(supported: postgres, duckdb, snowflake)."
        )
    if uri and not _looks_local(locator):
        if not uri.startswith(("postgresql://", "postgres://")):
            raise DatadifferError(
                "--source accepts postgresql:// URIs. For Snowflake, define a "
                "connection in datadiffer.toml and use <connection>::<table>."
            )
        src = open_postgres(con, uri, locator, alias)
        warnings.extend(check_row_guard(con, src))
        _mark_unsnapshotted(warnings)
        return src
    return open_source(con, locator, alias)


def _mark_unsnapshotted(warnings: list[str]) -> None:
    # The Postgres ATTACH path is unsnapshotted — the extension
    # opens its own scan connections and every pass rescans the live table.
    if "unsnapshotted_multipass" not in warnings:
        warnings.append("unsnapshotted_multipass")


def _looks_local(locator: str) -> bool:
    low = locator.lower()
    return low.endswith((".parquet", ".pq", ".csv", ".duckdb")) or ".duckdb:" in low


def _side_schema(con, raw, side: str) -> dict[str, str]:
    from datadiffer.connect.snowflake_ import SnowflakeHandle

    if isinstance(raw, SnowflakeHandle):
        return raw.schema_map()
    return read_schema(con, raw.rel, f"{raw.display} (side {side})")


def _snapshot_label(raw_a, raw_b) -> str:
    """Honest consistency label for execution.snapshot:
    pg attach rescans a live table every pass -> unsnapshotted; a Snowflake
    pull is one SELECT copied locally -> pulled_copy; pure-local reads share
    one connection."""
    from datadiffer.connect.snowflake_ import SnowflakeHandle

    kinds = set()
    for raw in (raw_a, raw_b):
        if isinstance(raw, SnowflakeHandle):
            kinds.add("snowflake")
        else:
            kinds.add(raw.kind)
    if "postgres" in kinds:
        return "unsnapshotted"
    if "snowflake" in kinds:
        return "pulled_copy"
    return "single_connection"


def _validate_col_filters(common, include, exclude) -> None:
    for flag, names in (("--columns", include), ("--exclude-columns", exclude)):
        unknown = [n for n in names or [] if n not in common]
        if unknown:
            raise DatadifferError(
                f"{flag} names not present on both sides: {', '.join(unknown)}. "
                f"Common columns: {', '.join(common)}"
            )


def _materialize(con, raw, schema, common, keys, include, exclude, where_cols, alias):
    """Returns (source, engine_schema). Snowflake handles pull a projected
    copy; the engine schema is re-read locally (DuckDB types)."""
    from datadiffer.connect.snowflake_ import SnowflakeHandle

    if not isinstance(raw, SnowflakeHandle):
        return raw, schema
    declared = raw.declared_key_sets()  # pre-pull: unconventional PK names
    needed = _needed_columns(schema, common, keys, include, exclude,
                             where_cols, declared)
    src = raw.pull(con, needed, alias)
    return src, read_schema(con, src.rel, f"{src.display} (pulled)")


def _needed_columns(schema, common, keys, include, exclude, where_cols, declared) -> list[str]:
    """Projection for cross-source pulls: compared columns + every possible
    key column (name candidates + declared constraints) + columns referenced
    only by --where (the filter must bind on the pulled copy)."""
    compared = {
        c for c in common
        if (not include or c in include) and not (exclude and c in exclude)
    }
    key_pool = set(keys or [])
    for cols in declared or []:
        key_pool.update(cols)
    for c in common:
        low = c.lower()
        if low == "id" or low.endswith(("_id", "_key")):
            key_pool.add(c)
    needed = compared | key_pool | set(where_cols or [])
    return [c for c in schema if c in needed]


def _shared_declared_keys(con, src_a, src_b, warnings: list[str], raw_a=None, raw_b=None):
    from datadiffer.connect.snowflake_ import SnowflakeHandle

    def sets_for(src: LocalSource | None, raw) -> list[list[str]]:
        if isinstance(raw, SnowflakeHandle):
            return raw.declared_key_sets()
        tgt = src if src is not None else raw
        if tgt is None:
            return []
        if tgt.kind == "duckdb":
            return declared_key_sets(con, tgt)
        if tgt.kind == "postgres":
            return declared_key_sets_pg(con, tgt, warnings)
        return []

    sets_a = sets_for(src_a, raw_a)
    if not sets_a:
        return []
    sets_b = sets_for(src_b, raw_b)
    return [cols for cols in sets_a if cols in sets_b]


# WHERE_ALLOWLIST_V1 — an exact, versioned contract.
# Additions require a version bump plus new fuzz cases. Enforced on the MCP
# path (strict=True); the CLI keeps the parse/subquery/column gates only.
WHERE_ALLOWLIST_V1_FUNCS = frozenset({
    "lower", "upper", "trim", "ltrim", "rtrim", "substr", "substring", "length",
    "abs", "round", "coalesce", "date_trunc", "current_date", "current_timestamp",
})
_ALLOWED_NODES = (
    exp.Column, exp.Identifier, exp.Literal, exp.Boolean, exp.Null, exp.Paren,
    exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE,
    exp.In, exp.Between, exp.Like, exp.ILike, exp.Is, exp.Tuple, exp.Neg,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Cast, exp.DataType, exp.Interval, exp.Var,
)


def _validate_where(where: str, common: dict[str, str], strict: bool = False) -> str:
    """Single boolean expression; no statements, no subqueries; columns must
    resolve on both sides. Returns the RE-RENDERED expression — literals are
    re-emitted by sqlglot, which neutralizes smuggled text.
    ``strict`` additionally enforces WHERE_ALLOWLIST_V1 (the MCP gate)."""
    try:
        cond = sqlglot.parse_one(where, read="duckdb", into=exp.Condition)
    except Exception as e:  # sqlglot raises several error types
        raise DatadifferError(f"--where must be a single boolean SQL expression: {e}") from e
    if cond.find(exp.Select, exp.Subquery, exp.Exists):
        raise DatadifferError("--where must not contain subqueries")
    for col in cond.find_all(exp.Column):
        if col.table:
            raise DatadifferError(f"--where must not qualify columns: {col.sql()}")
        if col.name not in common:
            raise DatadifferError(
                f"--where references {col.name!r}, which is not on both sides"
            )
    if strict:
        _enforce_where_allowlist(cond)
    # sqlglot carries comments through the re-render; strip them so the SQL we
    # emit contains nothing but the validated expression.
    for node in cond.walk():
        node.comments = None
    cols = {c.name for c in cond.find_all(exp.Column)}
    return cond.sql(dialect="duckdb"), cols


_NONSCALAR_TYPES = (
    exp.DataType.Type.STRUCT, exp.DataType.Type.ARRAY, exp.DataType.Type.MAP,
    exp.DataType.Type.JSON, exp.DataType.Type.OBJECT,
)


def _enforce_where_allowlist(cond: exp.Expression) -> None:
    """WHERE_ALLOWLIST_V1. Node classes are checked first — several allowed
    operators (AND/OR/CAST/...) are also exp.Func subclasses in sqlglot, so
    the function check must run only on nodes NOT already allowed by class."""
    for node in cond.walk():
        if isinstance(node, (exp.Placeholder, exp.Parameter)):
            raise DatadifferError("where: parameter markers are not allowed")
        if isinstance(node, exp.Cast):
            to = node.args.get("to")
            if to is not None and to.this in _NONSCALAR_TYPES:
                raise DatadifferError("where: casts to non-scalar types are not allowed")
            continue
        if isinstance(node, _ALLOWED_NODES):
            continue
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name not in WHERE_ALLOWLIST_V1_FUNCS:
                raise DatadifferError(
                    f"where: function {name!r} is not in the allowlist "
                    f"(allowed: {', '.join(sorted(WHERE_ALLOWLIST_V1_FUNCS))})"
                )
            continue
        raise DatadifferError(
            f"where: {node.__class__.__name__} is not allowed by WHERE_ALLOWLIST_V1"
        )
