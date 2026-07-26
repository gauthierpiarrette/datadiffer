"""`datadiffer ci` contract tests: artifacts, outputs, policy exit codes."""

import json

from click.testing import CliRunner

from datadiffer.cli import main
from datadiffer.report.json_schema import validate_report


def _run(args, tmp_path, fixture_pair, extra=()):
    a, b = fixture_pair
    out = tmp_path / "gh_output"
    base = [
        "ci", "--table-a", a, "--table-b", b,
        "--report-json", str(tmp_path / "report.json"),
        "--comment-md", str(tmp_path / "comment.md"),
        "--summary-md", str(tmp_path / "summary.md"),
        "--github-output", str(out),
    ]
    result = CliRunner().invoke(main, base + list(args), env=dict(extra))
    return result, tmp_path, out


def test_ci_writes_all_artifacts(tmp_path, fixture_pair):
    result, d, out = _run(["--fail-on", "never"], tmp_path, fixture_pair)
    assert result.exit_code == 0

    report = json.loads((d / "report.json").read_text())
    assert validate_report(report) == []

    comment = (d / "comment.md").read_text()
    assert comment.startswith("<!-- datadiffer-report:default -->")
    assert "differences found" in comment
    assert "over-represented" not in comment or "concentrated" in comment
    assert "Reproduce locally" in comment
    assert len(comment) <= 60_000

    outputs = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    assert outputs["has-diff"] == "true"
    assert outputs["rows-modified"] == "20"
    assert outputs["exit-code"] == "0"


def test_ci_threshold_pass_and_fail(tmp_path, fixture_pair):
    ok, *_ = _run(["--max-changed-rows-pct", "10"], tmp_path, fixture_pair)
    assert ok.exit_code == 0  # 3.7% of 1000 base rows changed

    bad, d, out = _run(["--max-changed-rows-pct", "1"], tmp_path, fixture_pair)
    assert bad.exit_code == 1
    assert "exceeded" in (d / "comment.md").read_text()


def test_ci_any_diff_and_schema_change(tmp_path, fixture_pair):
    r, *_ = _run(["--fail-on", "any-diff"], tmp_path, fixture_pair)
    assert r.exit_code == 1

    r2, *_ = _run(["--fail-on", "never", "--fail-on-schema-change"], tmp_path, fixture_pair)
    assert r2.exit_code == 0  # fixture schemas are identical


def test_ci_operational_error_exits_2(tmp_path):
    out = tmp_path / "gh_output"
    result = CliRunner().invoke(main, [
        "ci", "--table-a", "missing.parquet", "--table-b", "missing.parquet",
        "--github-output", str(out),
    ])
    assert result.exit_code == 2
    assert "exit-code=2" in out.read_text()


def test_ci_interpolates_pr_number(tmp_path, fixture_pair, monkeypatch):
    import duckdb

    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA analytics_pr_42")
    con.execute("CREATE SCHEMA analytics")
    con.execute("CREATE TABLE analytics.orders AS SELECT range AS id, range AS v FROM range(5)")
    con.execute(
        "CREATE TABLE analytics_pr_42.orders AS SELECT range AS id, range + 1 AS v FROM range(5)"
    )
    con.close()
    monkeypatch.setenv("PR_NUMBER", "42")
    result = CliRunner().invoke(main, [
        "ci",
        "--table-a", f"{db}:analytics.orders",
        "--table-b", f"{db}:analytics.orders",
        "--schema-map", "analytics=analytics_pr_${PR_NUMBER}",
        "--fail-on", "never",
        "--report-json", str(tmp_path / "r.json"),
    ])
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["rows"]["modified"] == 5  # hit the PR schema, not prod


def test_ci_unknown_ref_variable_refused(tmp_path, fixture_pair):
    a, b = fixture_pair
    result = CliRunner().invoke(main, [
        "ci", "--table-a", a, "--table-b", b, "--schema-map", "x=${EVIL_VAR}",
    ])
    assert result.exit_code == 2
    assert "PR_NUMBER" in result.output  # error lists the allowed set


def test_ci_source_env_interpolation_missing_var(tmp_path, fixture_pair):
    a, b = fixture_pair
    result = CliRunner().invoke(main, [
        "ci", "--table-a", "orders", "--table-b", "orders",
        "--source", "postgresql://u:${DD_CI_UNSET_PW}@h/db",
    ])
    assert result.exit_code == 2
    assert "DD_CI_UNSET_PW" in result.output


def test_comment_truncation_tiers(tmp_path):
    """A wide table must never exceed the GitHub comment cap."""
    import duckdb

    import datadiffer
    from datadiffer.report.render_markdown import render_comment

    cols = ", ".join(f"range * {i} AS col_{i:03d}" for i in range(1, 120))
    pa_, pb_ = tmp_path / "wa.parquet", tmp_path / "wb.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT range AS id, {cols} FROM range(200)) TO '{pa_}' (FORMAT parquet)")
    cols_b = ", ".join(f"range * {i} + 1 AS col_{i:03d}" for i in range(1, 120))
    con.execute(f"COPY (SELECT range AS id, {cols_b} FROM range(200)) TO '{pb_}' (FORMAT parquet)")
    con.close()
    report = datadiffer.diff(str(pa_), str(pb_))
    text = render_comment(report)
    assert len(text) <= 60_000
    assert "more |" in text  # truncation tier visibly engaged
