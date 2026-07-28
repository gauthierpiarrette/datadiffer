"""MCP server: stdio, exactly four read-only tools.

Credentials never flow through the model: tool inputs accept CONNECTION NAMES
from datadiffer.toml, never DSNs. Refusals are structured results with a
remedy, not exceptions; agents recover better from typed errors. diff_tables
runs the diff_summary preflight internally, so correct behavior never depends
on the agent's call ordering.

The impl_* functions are the testable surface; FastMCP wiring is a thin shim.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from datadiffer import api
from datadiffer.config import conn_label, load_config, probe_connection
from datadiffer.errors import DatadifferError, DiffTimeout

_INSTRUCTIONS = (
    "Read-only table diffing across configured warehouse connections. "
    "Call diff_summary before diff_tables on unfamiliar tables."
)


@dataclass
class Caps:
    max_rows_per_side: int = 10_000_000
    force_large_cap: int = 50_000_000
    timeout_seconds: int = 300
    timeout_max: int = 1800
    sample_limit: int = 10
    sample_max: int = 100


def load_caps() -> Caps:
    from datadiffer.config import interpolate_conn

    cfg, _ = load_config()
    mcp = interpolate_conn(cfg.get("mcp", {}), "[mcp] section")
    caps = Caps()
    try:
        caps.max_rows_per_side = int(mcp.get("max_rows_per_side", caps.max_rows_per_side))
        caps.timeout_seconds = int(mcp.get("timeout_seconds", caps.timeout_seconds))
        caps.sample_limit = int(mcp.get("sample_limit", caps.sample_limit))
    except (TypeError, ValueError) as e:
        raise DatadifferError(f"[mcp] caps must be integers: {e}") from e
    return caps


def _refusal(status: str, reason: str, remedy: str) -> dict:
    return {"status": status, "refusal": {"reason": reason, "remedy": remedy}}


def _reject_dsn(*values: str | None) -> None:
    for v in values:
        if v and ("://" in v or v.lower().startswith(("host=", "dbname="))):
            cfg, _ = load_config()
            available = ", ".join(sorted(cfg.get("connections", {}))) or "(none)"
            raise DatadifferError(
                f"DSNs are not accepted as tool arguments. Use a named connection: "
                f"available = [{available}]. To add one, run `datadiffer init` and "
                "edit datadiffer.toml."
            )


def _locator(connection: str, table: str) -> str:
    return f"{connection}::{table}"


def impl_list_connections(probe: bool = False) -> dict:
    cfg, path = load_config()
    conns = cfg.get("connections", {})
    out = []
    for name, c in sorted(conns.items()):
        entry = {"name": name, "type": c.get("type", "?"), "target": conn_label(c)}
        if probe:
            ok, detail = probe_connection(c)
            entry["reachable"] = ok
            if not ok:
                entry["detail"] = detail
        out.append(entry)
    result = {"connections": out, "config_path": str(path) if path else None}
    if not out:
        result["remedy"] = (
            "No connections configured. Run `datadiffer init` and add a "
            "connection to datadiffer.toml."
        )
    return result


def _close_raws(*raws) -> None:
    for r in raws:
        if hasattr(r, "close"):
            r.close()


def _raw_rows(con, raw) -> int:
    """Row count for preflight: metadata estimate for warehouses (falling back
    to a remote COUNT(*)), exact local count otherwise."""
    from datadiffer.connect.snowflake_ import SnowflakeHandle

    if isinstance(raw, SnowflakeHandle):
        est = raw.row_estimate()
        if est is not None:
            return est
        (n,) = raw._cur().execute(f"SELECT count(*) FROM {raw._fqn()}").fetchone()
        return int(n)
    (n,) = con.execute(f"SELECT count(*) FROM {raw.rel}").fetchone()
    return int(n)


def impl_schema_diff(
    connection: str, table_a: str, table_b: str, connection_b: str | None = None
) -> dict:
    _reject_dsn(connection, table_a, table_b, connection_b)
    con = duckdb.connect(":memory:")
    raw_a = raw_b = None
    try:
        warnings: list[str] = []
        raw_a = api._open_side(con, _locator(connection, table_a), None, "src_a", warnings)
        raw_b = api._open_side(
            con, _locator(connection_b or connection, table_b), None, "src_b", warnings
        )
        from datadiffer.core.localdiff import _comparable

        sa = api._side_schema(con, raw_a, "a")
        sb = api._side_schema(con, raw_b, "b")
        type_changed = [
            {"name": c, "type_a": sa[c], "type_b": sb[c], "comparable": _comparable(sa[c], sb[c])}
            for c in sa if c in sb and sa[c] != sb[c]
        ]
        return {
            "columns_added": [{"name": c, "type": t} for c, t in sb.items() if c not in sa],
            "columns_removed": [{"name": c, "type": t} for c, t in sa.items() if c not in sb],
            "columns_type_changed": type_changed,
            "columns_common": sum(1 for c in sa if c in sb),
            "identical": sa == sb,
        }
    finally:
        _close_raws(raw_a, raw_b)
        con.close()


def impl_diff_summary(
    connection: str,
    table_a: str,
    table_b: str,
    connection_b: str | None = None,
    keys: list[str] | None = None,
    where: str | None = None,
) -> dict:
    _reject_dsn(connection, table_a, table_b, connection_b)
    caps = load_caps()
    con = duckdb.connect(":memory:")
    raw_a = raw_b = None
    try:
        warnings: list[str] = []
        raw_a = api._open_side(con, _locator(connection, table_a), None, "src_a", warnings)
        raw_b = api._open_side(
            con, _locator(connection_b or connection, table_b), None, "src_b", warnings
        )
        from datadiffer.connect.snowflake_ import SnowflakeHandle
        from datadiffer.core.keys import resolve_keys

        sa = api._side_schema(con, raw_a, "a")
        sb = api._side_schema(con, raw_b, "b")
        common = {c: t for c, t in sa.items() if c in sb}
        rows_a = _raw_rows(con, raw_a)
        rows_b = _raw_rows(con, raw_b)

        where_sql = (
            api._validate_where(where, common, strict=True)[0] if where else None
        )
        key_info: dict = {}
        if any(isinstance(r, SnowflakeHandle) for r in (raw_a, raw_b)):
            # Preflight stays CHEAP: declared keys come from warehouse metadata;
            # uniqueness probes would scan; they run inside diff_tables.
            declared = api._shared_declared_keys(con, None, None, warnings, raw_a, raw_b)
            if keys:
                key_info = {"columns": keys, "inferred": False, "rule": None, "usable": True}
            elif declared:
                key_info = {"columns": declared[0], "inferred": True,
                            "rule": "declared", "usable": True}
            else:
                key_info = {"usable": False,
                            "reason": "no declared key found; pass keys=[...]"}
        else:
            try:
                cols, inferred, rule, _ = resolve_keys(
                    con, raw_a.rel, raw_b.rel, common, keys,
                    (raw_a.stem, raw_b.stem), where_sql,
                    api._shared_declared_keys(con, raw_a, raw_b, warnings, raw_a, raw_b),
                )
                key_info = {"columns": cols, "inferred": inferred, "rule": rule, "usable": True}
            except DatadifferError as e:
                key_info = {"usable": False, "reason": str(e)}

        within = rows_a <= caps.max_rows_per_side and rows_b <= caps.max_rows_per_side
        cost: dict = {"rows_a": int(rows_a), "rows_b": int(rows_b), "source": "exact_count"}
        for side, raw in (("a", raw_a), ("b", raw_b)):
            if isinstance(raw, SnowflakeHandle):
                cost["source"] = "warehouse_metadata"
                size = raw.byte_estimate()
                if size is not None:
                    cost[f"bytes_{side}"] = size
        return {
            "rows_a": int(rows_a),
            "rows_b": int(rows_b),
            "estimated_cost": cost,
            "primary_key": key_info,
            "schema_identical": sa == sb,
            "schema_changes": {
                "added": sum(1 for c in sb if c not in sa),
                "removed": sum(1 for c in sa if c not in sb),
                "type_changed": sum(1 for c in common if sa[c] != sb[c]),
            },
            "estimated_strategy": "localdiff",
            "within_caps": within,
            "warnings": warnings,
            "recommendation": (
                "proceed with diff_tables" if within and key_info.get("usable")
                else "resolve the key first (pass keys)" if not key_info.get("usable")
                else "over caps: pass force_large to diff_tables or narrow with where"
            ),
        }
    finally:
        _close_raws(raw_a, raw_b)
        con.close()


def impl_diff_tables(
    connection: str,
    table_a: str,
    table_b: str,
    connection_b: str | None = None,
    keys: list[str] | None = None,
    columns_exclude: list[str] | None = None,
    where: str | None = None,
    sample_limit: int = 10,
    timeout_seconds: int | None = None,
    force_large: bool = False,
) -> dict:
    _reject_dsn(connection, table_a, table_b, connection_b)
    caps = load_caps()
    sample_limit = min(sample_limit, caps.sample_max)
    timeout = min(timeout_seconds or caps.timeout_seconds, caps.timeout_max)

    # Internal preflight (normative): correct behavior never depends on the
    # agent having called diff_summary first.
    summary = impl_diff_summary(connection, table_a, table_b, connection_b, keys, where)
    if not summary["primary_key"].get("usable"):
        return _refusal(
            "refused_no_key",
            summary["primary_key"].get("reason", "no usable key"),
            "Pass keys=[...] with the primary key column(s).",
        )
    cap = caps.force_large_cap if force_large else caps.max_rows_per_side
    if summary["rows_a"] > cap or summary["rows_b"] > cap:
        return _refusal(
            "refused_cap",
            f"table exceeds {cap:,} rows per side",
            "Narrow with where=..., or pass force_large=true (hard cap "
            f"{caps.force_large_cap:,}).",
        )

    try:
        report = api.diff(
            _locator(connection, table_a),
            _locator(connection_b or connection, table_b),
            keys=keys,
            exclude_columns=columns_exclude,
            where=where,
            sample_limit=sample_limit,
            strict_where=True,
            timeout_seconds=timeout,
        )
    except DiffTimeout as e:
        return _refusal("timeout", str(e), "Narrow with where=... or raise timeout_seconds.")
    result = report.to_dict()
    result["summary_markdown"] = _short_markdown(report)
    return result


def _short_markdown(report) -> str:
    r = report.rows
    lines = [
        f"**{report.tables['a']}** vs **{report.tables['b']}**: "
        f"+{r.added} added, -{r.removed} removed, ~{r.modified} modified, "
        f"{r.unchanged} unchanged."
    ]
    if report.attribution:
        for status in ("modified", "added", "removed"):
            sa = report.attribution.by_status.get(status)
            if sa and sa.segments:
                s = sa.segments[0]
                val = "NULL" if s.value is None else repr(s.value)
                lines.append(
                    f"{s.support:.1%} of {status} rows have {s.column} = {val} "
                    f"({s.lift:g}x over-represented)."
                )
    return "\n".join(lines)


def build_server():
    from fastmcp import FastMCP

    try:
        from mcp.types import ToolAnnotations

        ro = ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
        kw = {"annotations": ro}
    except Exception:  # annotations are a hint, never load-bearing
        kw = {}

    mcp = FastMCP("datadiffer", instructions=_INSTRUCTIONS)

    @mcp.tool(**kw)
    def list_connections(probe: bool = False) -> dict:
        """List configured warehouse connections (names only; credentials never
        pass through tools). Cheap."""
        return impl_list_connections(probe)

    @mcp.tool(**kw)
    def schema_diff(
        connection: str, table_a: str, table_b: str, connection_b: str | None = None
    ) -> dict:
        """Compare two tables' schemas only (columns added/removed/retyped).
        Metadata-only, always cheap."""
        return impl_schema_diff(connection, table_a, table_b, connection_b)

    @mcp.tool(**kw)
    def diff_summary(
        connection: str,
        table_a: str,
        table_b: str,
        connection_b: str | None = None,
        keys: list[str] | None = None,
        where: str | None = None,
    ) -> dict:
        """Cheap, counts-only preflight: row counts, key uniqueness, schema-change
        summary, and whether a full diff fits within caps. Run this before
        diff_tables on unfamiliar tables."""
        return impl_diff_summary(connection, table_a, table_b, connection_b, keys, where)

    @mcp.tool(**kw)
    def diff_tables(
        connection: str,
        table_a: str,
        table_b: str,
        connection_b: str | None = None,
        keys: list[str] | None = None,
        columns_exclude: list[str] | None = None,
        where: str | None = None,
        sample_limit: int = 10,
        timeout_seconds: int | None = None,
        force_large: bool = False,
    ) -> dict:
        """EXPENSIVE: full semantic diff, may scan both tables end to end.
        Returns rows added/removed/modified, per-column change rates, segment
        attribution, and samples. Runs the diff_summary preflight internally and
        refuses with a remedy if caps would be exceeded."""
        return impl_diff_tables(
            connection, table_a, table_b, connection_b, keys,
            columns_exclude, where, sample_limit, timeout_seconds, force_large,
        )

    return mcp


def main() -> None:
    build_server().run()  # stdio


if __name__ == "__main__":
    main()
