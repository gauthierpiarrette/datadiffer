"""Config file, init/connections verbs, and conn::table locators."""

import os

import duckdb
import pytest
from click.testing import CliRunner

import datadiffer
from datadiffer.cli import main
from datadiffer.config import connection, load_config
from datadiffer.errors import DatadifferError


def test_init_scaffolds_and_refuses_overwrite():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])
        assert result.exit_code == 0
        assert oct(os.stat("datadiffer.toml").st_mode & 0o777) == "0o600"
        assert "connections test" in result.output
        again = runner.invoke(main, ["init"])
        assert again.exit_code == 2
        assert "not overwriting" in again.output


def test_env_var_config_and_interpolation(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[connections.wh]\ntype = "postgres"\ndsn = "postgresql://u:${DD_TEST_PW}@h/db"\n'
    )
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    monkeypatch.setenv("DD_TEST_PW", "s3cret")
    assert connection("wh")["dsn"] == "postgresql://u:s3cret@h/db"


def test_unknown_connection_lists_available(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[connections.prod]\ntype = "postgres"\ndsn = "postgresql://h/db"\n')
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    with pytest.raises(DatadifferError, match="Available: prod"):
        connection("dev")


def test_conn_locator_duckdb_end_to_end(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t AS SELECT range AS id, range AS v FROM range(5)")
    con.close()
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'[connections.local]\ntype = "duckdb"\npath = "{db}"\n')
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    report = datadiffer.diff("local::t", "local::t")
    assert not report.has_diff
    assert report.rows.matched == 5


def test_conn_locator_bad_type(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[connections.bq]\ntype = "bigquery"\nproject = "x"\n')
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    with pytest.raises(DatadifferError, match="unsupported type"):
        datadiffer.diff("bq::orders", "bq::orders")


def test_connections_list_and_test(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    duckdb.connect(str(db)).close()
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        f'[connections.local]\ntype = "duckdb"\npath = "{db}"\n'
        '[connections.gone]\ntype = "duckdb"\npath = "/nope/missing.duckdb"\n'
    )
    monkeypatch.setenv("DATADIFFER_CONFIG", str(cfg))
    runner = CliRunner()
    listed = runner.invoke(main, ["connections", "list"])
    assert listed.exit_code == 0
    assert "local" in listed.output and "duckdb" in listed.output
    tested = runner.invoke(main, ["connections", "test"])
    assert tested.exit_code == 2  # 'gone' fails
    assert "FAIL" in tested.output and "ok" in tested.output


def test_no_config_message(monkeypatch, tmp_path):
    monkeypatch.delenv("DATADIFFER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg, path = load_config()
    assert cfg == {} or path is not None  # home config may exist on dev machines
    result = CliRunner().invoke(main, ["connections", "list"])
    assert result.exit_code == 0
