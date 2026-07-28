"""MCP tool contract tests: impl functions directly; server build if fastmcp
is installed. Refusals must be structured results with a remedy, never
exceptions (agents recover from typed errors)."""

import duckdb
import pytest

from datadiffer.errors import DatadifferError
from datadiffer.mcp_server import (
    impl_diff_summary,
    impl_diff_tables,
    impl_list_connections,
    impl_schema_diff,
)


@pytest.fixture
def wh(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE orders AS SELECT range AS order_id, CAST(range AS DOUBLE) AS amount, "
        "CASE WHEN range % 4 = 0 THEN 'DE' ELSE 'US' END AS country FROM range(400)"
    )
    con.execute(
        "CREATE TABLE orders_v2 AS SELECT range AS order_id, "
        "CAST(CASE WHEN range % 4 = 0 AND range < 320 THEN range + 9 ELSE range END AS DOUBLE) "
        "AS amount, CASE WHEN range % 4 = 0 THEN 'DE' ELSE 'US' END AS country "
        "FROM range(410)"
    )
    con.execute("CREATE TABLE nokey AS SELECT range % 3 AS k, range AS v FROM range(9)")
    con.close()
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'[connections.wh]\ntype = "duckdb"\npath = "{db}"\n')
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    return "wh"


def test_list_connections(wh):
    out = impl_list_connections(probe=True)
    assert out["connections"][0]["name"] == "wh"
    assert out["connections"][0]["reachable"] is True


def test_list_connections_empty_has_remedy(tmp_path, monkeypatch):
    cfg = tmp_path / "empty.toml"
    cfg.write_text("")
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    out = impl_list_connections()
    assert "datadiffer init" in out["remedy"]


def test_schema_diff(wh):
    out = impl_schema_diff("wh", "orders", "orders_v2")
    assert out["identical"] is True
    assert out["columns_common"] == 3


def test_diff_summary_recommends(wh):
    out = impl_diff_summary("wh", "orders", "orders_v2")
    assert out["rows_a"] == 400 and out["rows_b"] == 410
    assert out["primary_key"]["usable"] is True
    assert out["primary_key"]["columns"] == ["order_id"]
    assert out["within_caps"] is True
    assert out["recommendation"] == "proceed with diff_tables"


def test_diff_tables_full_report(wh):
    out = impl_diff_tables("wh", "orders", "orders_v2")
    assert out["status"] == "ok"
    assert out["rows"]["added"] == 10
    assert out["rows"]["modified"] == 80
    assert "98" in out["summary_markdown"] or "100" in out["summary_markdown"]
    seg = out["attribution"]["by_status"]["modified"]["segments"][0]
    assert seg["column"] == "country" and seg["value"] == "DE"


def test_refused_no_key_is_structured(wh):
    out = impl_diff_tables("wh", "nokey", "nokey")
    assert out["status"] == "refused_no_key"
    assert "keys" in out["refusal"]["remedy"]


def test_refused_cap_and_force_large(wh, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg2.toml"
    db = tmp_path / "wh.duckdb"
    cfg.write_text(
        f'[connections.wh]\ntype = "duckdb"\npath = "{db}"\n'
        "[mcp]\nmax_rows_per_side = 100\n"
    )
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    out = impl_diff_tables("wh", "orders", "orders_v2")
    assert out["status"] == "refused_cap"
    assert "force_large" in out["refusal"]["remedy"]
    ok = impl_diff_tables("wh", "orders", "orders_v2", force_large=True)
    assert ok["status"] == "ok"


def test_dsn_arguments_rejected(wh):
    with pytest.raises(DatadifferError, match="named connection"):
        impl_diff_tables("postgresql://u:p@h/db", "orders", "orders_v2")
    with pytest.raises(DatadifferError, match="available = \\[wh\\]"):
        impl_schema_diff("wh", "postgresql://u:p@h/db", "orders")


def test_strict_where_allowlist(wh):
    ok = impl_diff_tables("wh", "orders", "orders_v2", where="lower(country) = 'de'")
    assert ok["status"] == "ok"
    with pytest.raises(DatadifferError, match="allowlist"):
        impl_diff_tables("wh", "orders", "orders_v2", where="pg_sleep(9) IS NULL")
    with pytest.raises(DatadifferError, match="allowlist"):
        impl_diff_tables("wh", "orders", "orders_v2", where="country ~ 'D.'")


def test_server_builds_with_four_tools():
    fastmcp = pytest.importorskip("fastmcp")  # noqa: F841
    import asyncio

    from datadiffer.mcp_server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"list_connections", "schema_diff", "diff_summary", "diff_tables"}
