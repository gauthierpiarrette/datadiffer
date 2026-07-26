"""The demo must tell the planted story exactly (it is also the README GIF)."""

import duckdb
from click.testing import CliRunner

import datadiffer
from datadiffer import demo
from datadiffer.cli import main


def test_demo_generates_planted_regressions(tmp_path):
    a, b = demo.generate(str(tmp_path / "demo"))
    report = datadiffer.diff(a, b)

    # Deterministic generator: pin EXACT values so any edit that drifts the
    # planted story (the README's "~98% in DE" claim) fails the suite.
    assert report.rows.a == 50_000
    assert report.rows.added == 800
    assert report.rows.removed == 250  # order_id % 200 == 7
    assert report.rows.modified == 432  # 428 DE (lcm 9,13) - 2 removed + 6 noise
    assert report.column("amount").changed_rows == 432
    assert report.column("country").changed_rows == 0
    assert report.primary_key.columns == ["order_id"]

    # The planted concentration: modified rows are almost all country='DE'.
    con = duckdb.connect()
    (de_share,) = con.execute(f"""
        SELECT avg(CASE WHEN a.country = 'DE' THEN 1.0 ELSE 0.0 END)
        FROM read_parquet('{a}') a JOIN read_parquet('{b}') b USING (order_id)
        WHERE a.amount IS DISTINCT FROM b.amount
    """).fetchone()
    con.close()
    assert de_share > 0.98  # 426/432 = 98.6% — the attribution headline number

    # The NULL-plan story on added rows.
    con = duckdb.connect()
    (null_share,) = con.execute(f"""
        SELECT avg(CASE WHEN b.plan IS NULL THEN 1.0 ELSE 0.0 END)
        FROM read_parquet('{b}') b
        WHERE b.order_id >= 50000
    """).fetchone()
    con.close()
    assert null_share == 746 / 800  # plan NULL unless range % 15 == 0 (54 rows)


def test_demo_attribution_headline(tmp_path):
    a, b = demo.generate(str(tmp_path / "demo"))
    report = datadiffer.diff(a, b)
    top = report.attribution.by_status["modified"].segments[0]
    assert top.column == "country"
    assert top.value == "DE"
    assert top.support > 0.98
    added_top = report.attribution.by_status["added"].segments[0]
    assert added_top.column == "plan"
    assert added_top.value is None  # the NULL-plan story


def test_demo_is_deterministic(tmp_path):
    a1, b1 = demo.generate(str(tmp_path / "d1"))
    a2, b2 = demo.generate(str(tmp_path / "d2"))
    con = duckdb.connect()
    for f1, f2 in ((a1, a2), (b1, b2)):
        (n,) = con.execute(
            f"SELECT count(*) FROM (SELECT * FROM read_parquet('{f1}') "
            f"EXCEPT SELECT * FROM read_parquet('{f2}'))"
        ).fetchone()
        assert n == 0
    con.close()


def test_demo_verb_exits_zero_and_teaches_the_command():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["demo"])
        assert result.exit_code == 0
        assert "$ datadiffer diff" in result.output
        assert "DIFF FOUND" in result.output
        assert "exit 1" in result.output  # teaches the real contract
