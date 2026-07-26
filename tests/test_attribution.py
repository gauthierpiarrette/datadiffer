"""Acceptance tests for segment attribution."""

import json

import duckdb
import pytest

import datadiffer


def _write(tmp_path, name, sql):
    p = tmp_path / name
    con = duckdb.connect()
    con.execute(f"COPY ({sql}) TO '{p}' (FORMAT parquet)")
    con.close()
    return str(p)


def test_planted_de_segment(tmp_path):
    """Modified rows concentrated in country='DE' must surface as top segment."""
    a = _write(tmp_path, "a.parquet", """
        SELECT range AS id, range * 1.0 AS amount,
               CASE WHEN range % 9 = 0 THEN 'DE' ELSE 'US' END AS country
        FROM range(10000)""")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id,
               CASE WHEN range % 9 = 0 AND range % 2 = 0 THEN range * 1.0 + 5
                    ELSE range * 1.0 END AS amount,
               CASE WHEN range % 9 = 0 THEN 'DE' ELSE 'US' END AS country
        FROM range(10000)""")
    report = datadiffer.diff(a, b)
    seg = report.attribution.by_status["modified"].segments[0]
    assert seg.column == "country"
    assert seg.value == "DE"
    assert seg.support == 1.0  # every modified row is DE
    assert seg.lift > 5  # DE is ~11% of the table
    assert seg.baseline_share == pytest.approx(1112 / 10000, abs=0.01)


def test_added_rows_null_segment(tmp_path):
    """NULL is a first-class segment value (sentinel-coalesced internally)."""
    a = _write(tmp_path, "a.parquet",
               "SELECT range AS id, 'pro' AS plan, range * 1.0 AS v FROM range(2000)")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id, 'pro' AS plan, range * 1.0 AS v FROM range(2000)
        UNION ALL
        SELECT 2000 + range,
               CASE WHEN range % 10 = 0 THEN 'pro' ELSE NULL END,
               range * 1.0
        FROM range(500)""")
    report = datadiffer.diff(a, b)
    seg = report.attribution.by_status["added"].segments[0]
    assert seg.column == "plan"
    assert seg.value is None
    assert seg.support == pytest.approx(0.9)


def test_removed_segment_scored_against_source(tmp_path):
    """A segment entirely removed must score against the SOURCE
    baseline — finite lift, never silently dropped."""
    a = _write(tmp_path, "a.parquet", """
        SELECT range AS id, CASE WHEN range < 3000 THEN 'X' ELSE 'Y' END AS seg,
               range * 1.0 AS v
        FROM range(10000)""")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id, CASE WHEN range < 3000 THEN 'X' ELSE 'Y' END AS seg,
               range * 1.0 AS v
        FROM range(10000) WHERE range >= 3000""")
    report = datadiffer.diff(a, b)
    seg = report.attribution.by_status["removed"].segments[0]
    assert seg.column == "seg"
    assert seg.value == "X"
    assert seg.support == 1.0
    assert seg.baseline_share == pytest.approx(0.3)
    assert seg.lift == pytest.approx(1 / 0.3, rel=0.02)


def test_uniform_removal_yields_no_segment(tmp_path):
    """Uniform removal across segments: lift ~1 everywhere -> uniform_change."""
    a = _write(tmp_path, "a.parquet", """
        SELECT range AS id, CASE WHEN range % 2 = 0 THEN 'X' ELSE 'Y' END AS seg,
               range * 1.0 AS v FROM range(4000)""")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id, CASE WHEN range % 2 = 0 THEN 'X' ELSE 'Y' END AS seg,
               range * 1.0 AS v FROM range(4000)
        WHERE range % 10 NOT IN (3, 4)""")  # removes 400 X + 400 Y — truly uniform
    report = datadiffer.diff(a, b)
    sa = report.attribution.by_status["removed"]
    assert sa.segments == []
    assert sa.note == "uniform_change"


def test_uniform_change_note(tmp_path):
    a = _write(tmp_path, "a.parquet", """
        SELECT range AS id, CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END AS country,
               range * 1.0 AS v FROM range(1000)""")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id, CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END AS country,
               range * 1.0 + 1 AS v FROM range(1000)""")
    report = datadiffer.diff(a, b)
    sa = report.attribution.by_status["modified"]
    assert sa.note == "uniform_change"
    assert sa.segments == []


def test_too_few_rows_note(tmp_path):
    a = _write(tmp_path, "a.parquet", """
        SELECT range AS id, 'DE' AS country, range * 1.0 AS v FROM range(1000)""")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id, 'DE' AS country,
               CASE WHEN range < 10 THEN range * 2.0 + 1 ELSE range * 1.0 END AS v
        FROM range(1000)""")
    report = datadiffer.diff(a, b)
    assert report.attribution.by_status["modified"].note == "too_few_rows"


def test_no_candidate_columns_note(tmp_path):
    a = _write(tmp_path, "a.parquet",
               "SELECT range AS id, range * 1.111 AS v FROM range(1000)")
    b = _write(tmp_path, "b.parquet",
               "SELECT range AS id, range * 1.111 + (CASE WHEN range < 100 "
               "THEN 1 ELSE 0 END) AS v FROM range(1000)")
    report = datadiffer.diff(a, b)
    assert report.attribution.analyzed_columns == []
    assert report.attribution.by_status["modified"].note == "no_candidate_columns"
    reasons = {s["column"]: s["reason"] for s in report.attribution.skipped_columns}
    assert reasons["v"] == "ineligible_type"


def test_dimension_flips_to_null_attributes_to_null(tmp_path):
    """Presence semantics: a dimension that changed to NULL attributes to NULL,
    never to its before-side value (the classic silent-regression shape)."""
    a = _write(tmp_path, "a.parquet", """
        SELECT range AS id, CASE WHEN range % 5 = 0 THEN 'DE' ELSE 'US' END AS country,
               range * 1.0 AS v FROM range(5000)""")
    b = _write(tmp_path, "b.parquet", """
        SELECT range AS id,
               CASE WHEN range % 5 = 0 THEN NULL ELSE 'US' END AS country,
               range * 1.0 AS v FROM range(5000)""")
    report = datadiffer.diff(a, b)
    seg = report.attribution.by_status["modified"].segments[0]
    assert seg.column == "country"
    assert seg.value is None  # NULL, not 'DE'


def test_attribution_off(tmp_path, fixture_pair):
    a, b = fixture_pair
    report = datadiffer.diff(a, b, attribution="off")
    assert report.attribution is None


def test_attribution_serializes_to_strict_json(fixture_pair):
    a, b = fixture_pair
    d = json.loads(datadiffer.diff(a, b).to_json())
    attr = d["attribution"]
    assert attr["semantics"] == "after_side_by_presence"
    assert attr["baseline_by_status"]["removed"] == "source"
    assert attr["baseline_by_status"]["any_change"] == "union"
    assert isinstance(attr["by_status"], dict)


def test_quasi_unique_column_skipped(tmp_path):
    a = _write(tmp_path, "a.parquet",
               "SELECT range AS id, 'u' || (range % 40) AS bucket, "
               "range * 1.0 AS v FROM range(100)")
    b = _write(tmp_path, "b.parquet",
               "SELECT range AS id, 'u' || (range % 40) AS bucket, "
               "CASE WHEN range < 50 THEN range * 2.0 + 1 ELSE range * 1.0 END AS v "
               "FROM range(100)")
    report = datadiffer.diff(a, b)
    reasons = {s["column"]: s["reason"] for s in report.attribution.skipped_columns}
    assert reasons.get("bucket") == "quasi_unique"  # 40 distinct / 100 rows > 0.2
