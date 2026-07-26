"""CLI smoke tests: verbs wire up, exit codes honor the contract."""

import json

from click.testing import CliRunner

import datadiffer
from datadiffer.cli import main


def test_version():
    assert datadiffer.__version__


def test_cli_no_args_shows_usage_and_examples():
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert "Examples" in result.output
    assert "datadiffer diff prod.parquet dev.parquet" in result.output


def test_cli_help_lists_all_verbs():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for verb in ("diff", "ci", "mcp", "init", "connections", "demo"):
        assert verb in result.output


def test_diff_missing_file_exits_2():
    result = CliRunner().invoke(main, ["diff", "missing_a.parquet", "missing_b.parquet"])
    assert result.exit_code == 2
    assert "File not found" in result.output


def test_diff_exit_codes(fixture_pair):
    a, b = fixture_pair
    runner = CliRunner()
    assert runner.invoke(main, ["diff", a, b, "-q"]).exit_code == 1
    assert runner.invoke(main, ["diff", a, a, "-q"]).exit_code == 0
    assert runner.invoke(main, ["diff", a, b, "-q", "--fail-on", "never"]).exit_code == 0


def test_diff_json_output(fixture_pair):
    a, b = fixture_pair
    result = CliRunner().invoke(main, ["diff", a, b, "--format", "json", "--fail-on", "never"])
    assert result.exit_code == 0
    d = json.loads(result.output)
    assert d["rows"]["modified"] == 20


def test_diff_text_output_has_verdict(fixture_pair):
    a, b = fixture_pair
    result = CliRunner().invoke(main, ["diff", a, b])
    assert result.exit_code == 1
    assert "DIFF FOUND" in result.output
    assert "inferred" in result.output
