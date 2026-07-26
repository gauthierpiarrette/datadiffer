"""Regression tests for previously fixed defects."""

import duckdb
import pytest
from click.testing import CliRunner

import datadiffer
from datadiffer.cli import main
from datadiffer.config import load_config, probe_connection
from datadiffer.connect.postgres_ import open_postgres, redact_text
from datadiffer.errors import DatadifferError


def test_failed_connection_never_prints_password():
    """Driver error text embeds the raw DSN — must be scrubbed."""
    dsn = "postgresql://user:SUPERSECRET@localhost:1/db"
    ok, detail = probe_connection({"type": "postgres", "dsn": dsn})
    assert not ok
    assert "SUPERSECRET" not in detail

    con = duckdb.connect(":memory:")
    with pytest.raises(DatadifferError) as ei:
        open_postgres(con, dsn, "orders", "x")
    assert "SUPERSECRET" not in str(ei.value)
    con.close()


def test_redact_text_scrubs_embedded_dsn():
    msg = 'Unable to connect at "postgresql://u:pw@h/db?password=pw2"'
    out = redact_text(msg, "postgresql://u:pw@h/db?password=pw2")
    assert "pw" not in out.replace("password=***", "")


def test_probe_handles_quote_in_dsn():
    """Probe must quote like the real path — no parser error."""
    ok, detail = probe_connection(
        {"type": "postgres", "dsn": "postgresql://u:pa'ss@localhost:1/db"}
    )
    assert not ok
    assert "Parser Error" not in detail
    assert "pa'ss" not in detail


def test_unset_env_var_fails_loudly_on_use(tmp_path, monkeypatch):
    """${VAR} of an unset variable must never silently become '' —
    and (lazy contract) it fails when the CONNECTION IS USED, not on load."""
    from datadiffer.config import connection

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[connections.wh]\ntype = "postgres"\ndsn = "postgresql://u:${DD_MISSING_VAR}@h/db"\n'
        f'[connections.local]\ntype = "duckdb"\npath = "{tmp_path}/x.duckdb"\n'
    )
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    monkeypatch.delenv("DD_MISSING_VAR", raising=False)
    load_config()  # raw load must NOT raise
    with pytest.raises(DatadifferError, match=r"\$\{DD_MISSING_VAR\}"):
        connection("wh")


def test_broken_connection_does_not_brick_others(tmp_path, monkeypatch):
    """One unset ${VAR} on connection A must not brick connection B or the
    connections verbs ."""
    import duckdb as _duckdb

    db = tmp_path / "ok.duckdb"
    con = _duckdb.connect(str(db))
    con.execute("CREATE TABLE t AS SELECT range AS id FROM range(3)")
    con.close()
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[connections.broken]\ntype = "postgres"\ndsn = "postgresql://u:${DD_NOPE_VAR}@h/db"\n'
        f'[connections.ok]\ntype = "duckdb"\npath = "{db}"\n'
    )
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    monkeypatch.delenv("DD_NOPE_VAR", raising=False)

    report = datadiffer.diff("ok::t", "ok::t")  # unrelated connection works
    assert not report.has_diff

    listed = CliRunner().invoke(main, ["connections", "list"])
    assert listed.exit_code == 0
    assert "unresolved" in listed.output and "DD_NOPE_VAR" in listed.output

    tested = CliRunner().invoke(main, ["connections", "test"])
    assert tested.exit_code == 2  # broken conn FAILS, but the verb runs
    assert "ok" in tested.output


def test_env_config_beats_cwd_file(tmp_path, monkeypatch):
    """Spec: explicit $DATADIFFER_CONFIG wins over a stray ./datadiffer.toml."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "datadiffer.toml").write_text(
        '[connections.stray]\ntype = "duckdb"\npath = "x.duckdb"\n'
    )
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[connections.chosen]\ntype = "duckdb"\npath = "y.duckdb"\n')
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("DATADIFFER_CONFIG", str(explicit))
    cfg, path = load_config()
    assert path == explicit
    assert list(cfg["connections"]) == ["chosen"]


def test_no_config_at_all(tmp_path, monkeypatch):
    monkeypatch.delenv("DATADIFFER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # hide any real home config
    monkeypatch.chdir(tmp_path)
    cfg, path = load_config()
    assert cfg == {} and path is None
    result = CliRunner().invoke(main, ["connections", "list"])
    assert result.exit_code == 0
    assert "No connections configured" in result.output


def test_double_colon_table_ref_rejected(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE orders AS SELECT 1 AS id")
    con.close()
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'[connections.local]\ntype = "duckdb"\npath = "{db}"\n')
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    with pytest.raises(DatadifferError, match="Bad table reference"):
        datadiffer.diff("local::orders::x", "local::orders")


def test_bare_locator_error_mentions_conn_syntax():
    with pytest.raises(DatadifferError, match="connection>::<table"):
        datadiffer.diff("orders", "orders")


def test_diff_timeout_cancels(tmp_path):
    """The watchdog must interrupt a long diff and surface DiffTimeout."""
    p = tmp_path / "big.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT range AS id, range % 97 * 1.1 AS v, "
        f"'x' || (range % 1000) AS s FROM range(4000000)) TO '{p}' (FORMAT parquet)"
    )
    con.close()
    from datadiffer.errors import DiffTimeout

    with pytest.raises(DiffTimeout):
        datadiffer.diff(str(p), str(p), keys=["id"], timeout_seconds=0.02)
