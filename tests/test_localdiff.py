"""Acceptance tests for the local diff engine."""

import duckdb
import pytest

import datadiffer
from datadiffer.errors import DatadifferError
from datadiffer.report.model import InvariantError, RowCounts


def test_counts_and_key_inference(fixture_pair):
    a, b = fixture_pair
    report = datadiffer.diff(a, b)

    assert report.rows.added == 12
    assert report.rows.removed == 5
    assert report.rows.modified == 20
    assert report.rows.matched == 995
    assert report.rows.unchanged == 975
    assert report.rows.a == 1000
    assert report.rows.b == 1007
    assert report.primary_key.columns == ["order_id"]
    assert report.primary_key.inferred is True
    assert report.has_diff


def test_untouched_columns_report_zero(fixture_pair):
    """The headline-number regression test: added/removed rows must never
    inflate per-column changed counts."""
    a, b = fixture_pair
    report = datadiffer.diff(a, b)

    assert report.column("amount").changed_rows == 20
    assert report.column("country").changed_rows == 0
    assert report.column("status").changed_rows == 0
    assert report.column("amount").change_rate == pytest.approx(20 / 995)


def test_identical_tables(fixture_pair):
    a, _ = fixture_pair
    report = datadiffer.diff(a, a)
    assert not report.has_diff
    assert report.rows.added == report.rows.removed == report.rows.modified == 0


def test_samples_shape(fixture_pair):
    a, b = fixture_pair
    report = datadiffer.diff(a, b, sample_limit=3)
    assert len(report.samples["added"]) == 3
    assert len(report.samples["removed"]) == 3
    mod = report.samples["modified"][0]
    assert set(mod["changes"].keys()) == {"amount"}
    assert mod["changes"]["amount"]["a"] != mod["changes"]["amount"]["b"]


def test_where_filters_both_sides(fixture_pair):
    a, b = fixture_pair
    report = datadiffer.diff(a, b, where="order_id < 5")
    # ids 0-4 exist only in A under this filter
    assert report.rows.removed == 5
    assert report.rows.added == 0
    assert report.rows.matched == 0


def test_where_rejects_subqueries(fixture_pair):
    a, b = fixture_pair
    with pytest.raises(DatadifferError, match="subqueries"):
        datadiffer.diff(a, b, where="(SELECT 1) IS NOT NULL")


def test_where_rejects_unknown_columns(fixture_pair):
    a, b = fixture_pair
    with pytest.raises(DatadifferError, match="not on both sides"):
        datadiffer.diff(a, b, where="nope = 1")


def test_duplicate_key_refused(tmp_path):
    p = tmp_path / "dup.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT range % 3 AS id, range AS v FROM range(9)) TO '{p}' (FORMAT parquet)"
    )
    con.close()
    with pytest.raises(DatadifferError, match="not unique"):
        datadiffer.diff(str(p), str(p), keys=["id"])


def test_no_inferable_key_refused(tmp_path):
    p = tmp_path / "nokey.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT range AS v FROM range(5)) TO '{p}' (FORMAT parquet)")
    con.close()
    with pytest.raises(DatadifferError, match="--key"):
        datadiffer.diff(str(p), str(p))


def test_exclude_columns(fixture_pair):
    a, b = fixture_pair
    report = datadiffer.diff(a, b, exclude_columns=["amount"])
    assert report.rows.modified == 0
    assert all(c.name != "amount" for c in report.columns)


def test_schema_diff_and_json_coercion(tmp_path):
    pa = tmp_path / "a.parquet"
    pb = tmp_path / "b.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT range AS id, [range, 1] AS tags, 'x' AS gone FROM range(4)) "
        f"TO '{pa}' (FORMAT parquet)"
    )
    con.execute(
        f"COPY (SELECT range AS id, [range, 2] AS tags, 1 AS fresh FROM range(4)) "
        f"TO '{pb}' (FORMAT parquet)"
    )
    con.close()
    report = datadiffer.diff(str(pa), str(pb))
    assert report.column("tags").compared_as == "json_text"
    assert report.column("tags").changed_rows == 4
    assert [c["name"] for c in report.schema_diff.columns_removed] == ["gone"]
    assert [c["name"] for c in report.schema_diff.columns_added] == ["fresh"]
    assert not report.schema_diff.identical


def test_report_invariants_enforced():
    with pytest.raises(InvariantError):
        RowCounts(a=10, b=10, matched=5, added=5, removed=4, modified=2, unchanged=3).validate()


def test_json_roundtrip(fixture_pair):
    import json

    a, b = fixture_pair
    d = json.loads(datadiffer.diff(a, b).to_json())
    assert d["report_schema_version"] == "1"
    assert d["rows"]["a"] == d["rows"]["matched"] + d["rows"]["removed"]
    assert d["rows"]["b"] == d["rows"]["matched"] + d["rows"]["added"]
    assert d["rows"]["matched"] == d["rows"]["modified"] + d["rows"]["unchanged"]
