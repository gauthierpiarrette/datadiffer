"""Regression tests for previously fixed defects."""

import json

import duckdb
import pytest
from click.testing import CliRunner

import datadiffer
from datadiffer.cli import main
from datadiffer.errors import DatadifferError


def _write(tmp_path, name, sql):
    p = tmp_path / name
    con = duckdb.connect()
    con.execute(f"COPY ({sql}) TO '{p}' (FORMAT parquet)")
    con.close()
    return str(p)


def test_user_column_named_p_is_compared_not_corrupted(tmp_path):
    """A user column named _p must neither corrupt presence markers
    nor be silently dropped from comparison."""
    a = _write(tmp_path, "a.parquet",
               "SELECT range AS id, CAST(NULL AS BOOLEAN) AS _p, range AS v FROM range(4)")
    b = _write(tmp_path, "b.parquet",
               "SELECT range AS id, CAST(NULL AS BOOLEAN) AS _p, range AS v FROM range(4)")
    report = datadiffer.diff(a, b)
    assert not report.has_diff  # identical files must never report added/removed
    assert report.rows.matched == 4

    b2 = _write(tmp_path, "b2.parquet",
                "SELECT range AS id, TRUE AS _p, range AS v FROM range(4)")
    report2 = datadiffer.diff(a, b2)
    assert report2.column("_p").changed_rows == 4  # _p is a real, compared column


def test_key_named_added_works(tmp_path):
    a = _write(tmp_path, "a.parquet", "SELECT range AS _added, range AS v FROM range(3)")
    b = _write(tmp_path, "b.parquet", "SELECT range AS _added, range + 1 AS v FROM range(3)")
    report = datadiffer.diff(a, b, keys=["_added"])
    assert report.rows.matched == 3
    assert report.rows.modified == 3


def test_unknown_columns_flag_errors(fixture_pair):
    """A typo'd --columns must never silently pass CI."""
    a, b = fixture_pair
    with pytest.raises(DatadifferError, match="not present"):
        datadiffer.diff(a, b, columns=["amuont"])
    with pytest.raises(DatadifferError, match="not present"):
        datadiffer.diff(a, b, exclude_columns=["amuont"])
    result = CliRunner().invoke(main, ["diff", a, b, "--columns", "amuont"])
    assert result.exit_code == 2


def test_key_probe_respects_where(tmp_path):
    """Key unique within the --where slice must be accepted."""
    a = _write(tmp_path, "a.parquet",
               "SELECT range % 5 AS id, range // 5 AS day, range AS v FROM range(10)")
    report = datadiffer.diff(a, a, keys=["id"], where="day = 1")
    assert report.rows.matched == 5
    with pytest.raises(DatadifferError, match="not unique"):
        datadiffer.diff(a, a, keys=["id"])


def test_key_type_mismatch_is_operational_error(tmp_path):
    """BIGINT vs VARCHAR key must exit 2, not traceback with exit 1."""
    pa = tmp_path / "a.csv"
    pb = tmp_path / "b.csv"
    pa.write_text("id,v\n1,a\n2,b\n")
    pb.write_text("id,v\n01,a\nx2,b\n")
    result = CliRunner().invoke(main, ["diff", str(pa), str(pb), "--key", "id"])
    assert result.exit_code == 2
    assert "incomparable types" in result.output


def test_engine_errors_exit_2(tmp_path):
    corrupt = tmp_path / "corrupt.duckdb"
    corrupt.write_text("not a duckdb file")
    good = _write(tmp_path, "g.parquet", "SELECT 1 AS id")
    result = CliRunner().invoke(main, ["diff", f"{corrupt}:orders", good])
    assert result.exit_code == 2


def test_infinity_serializes_as_valid_json(tmp_path):
    a = _write(tmp_path, "a.parquet", "SELECT range AS id, 1.0::DOUBLE AS v FROM range(2)")
    b = _write(tmp_path, "b.parquet",
               "SELECT range AS id, CASE WHEN range = 0 THEN 'inf'::DOUBLE "
               "ELSE '-inf'::DOUBLE END AS v FROM range(2)")
    payload = datadiffer.diff(a, b).to_json()
    d = json.loads(payload)  # must be strict-parseable
    assert "Infinity" not in payload.replace('"Infinity"', "").replace('"-Infinity"', "")
    vals = {c["changes"]["v"]["b"] for c in d["samples"]["modified"]}
    assert vals == {"Infinity", "-Infinity"}


def test_duckdb_schema_qualified_table(tmp_path):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA staging")
    con.execute(
        "CREATE TABLE staging.orders AS SELECT range AS order_id, range AS v FROM range(3)"
    )
    con.execute(
        "CREATE TABLE main.orders AS SELECT range AS order_id, range + 1 AS v FROM range(3)"
    )
    con.close()
    report = datadiffer.diff(f"{db}:main.orders", f"{db}:staging.orders")
    assert report.rows.modified == 3


def test_declared_composite_pk_inferred(tmp_path):
    db = tmp_path / "pk.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE t (k1 INT, k2 INT, v INT, PRIMARY KEY (k1, k2));"
        "INSERT INTO t SELECT range // 2, range % 2, range FROM range(6)"
    )
    con.close()
    report = datadiffer.diff(f"{db}:t", f"{db}:t")
    assert report.primary_key.columns == ["k1", "k2"]
    assert report.primary_key.rule == "declared"


def test_stem_casing_table_id_rule(tmp_path):
    a = _write(tmp_path, "ORDERS.parquet",
               "SELECT range AS customer_id, range AS orders_id, range AS v FROM range(3)")
    report = datadiffer.diff(a, a)
    assert report.primary_key.columns == ["orders_id"]
    assert report.primary_key.rule == "table_id"


def test_where_literal_with_dashes_accepted(tmp_path):
    a = _write(tmp_path, "a.parquet", "SELECT range AS id, 'a--b' AS tag FROM range(3)")
    report = datadiffer.diff(a, a, where="tag = 'a--b'")
    assert report.rows.matched == 3


def test_ci_without_required_args_exits_2():
    assert CliRunner().invoke(main, ["ci"]).exit_code == 2  # missing --table-a


def test_bracketed_filename_renders_literally(tmp_path):
    a = _write(tmp_path, "data [red]x.parquet", "SELECT range AS id, range AS v FROM range(2)")
    result = CliRunner().invoke(main, ["diff", a, a])
    assert result.exit_code == 0
    assert "[red]x.parquet" in result.output
