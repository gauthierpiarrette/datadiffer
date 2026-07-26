"""Contract-freeze tests. If any of these fail, you are breaking the
frozen v1 contract that CLI JSON, MCP structuredContent, and the Action
artifact all share — stop and think, don't update the snapshot casually."""

import json

import pytest

import datadiffer
from datadiffer.report.json_schema import validate_report


def test_report_conforms_to_frozen_schema(fixture_pair):
    a, b = fixture_pair
    d = json.loads(datadiffer.diff(a, b).to_json())
    assert validate_report(d) == []


def test_report_key_sets_frozen(fixture_pair):
    a, b = fixture_pair
    d = json.loads(datadiffer.diff(a, b).to_json())

    assert sorted(d.keys()) == sorted([
        "report_schema_version", "status", "strategy", "tables", "primary_key",
        "rows", "schema_diff", "columns", "samples", "execution", "attribution",
    ])
    assert sorted(d["rows"].keys()) == sorted(
        ["a", "b", "matched", "added", "removed", "modified", "unchanged"]
    )
    assert sorted(d["execution"].keys()) == sorted(
        ["elapsed_seconds", "statements", "snapshot", "warnings", "capped"]
    )
    assert sorted(d["primary_key"].keys()) == sorted(["columns", "inferred", "rule"])
    seg = d["attribution"]["by_status"]["modified"]["segments"]
    if seg:
        assert sorted(seg[0].keys()) == sorted(
            ["column", "value", "rows", "support", "baseline_share", "lift", "score"]
        )


def test_report_version_is_1(fixture_pair):
    a, b = fixture_pair
    assert datadiffer.diff(a, b).to_dict()["report_schema_version"] == "1"


def test_mcp_tool_contract_frozen():
    pytest.importorskip("fastmcp")
    import asyncio

    from datadiffer.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    assert sorted(tools) == ["diff_summary", "diff_tables", "list_connections", "schema_diff"]

    frozen_params = {
        "list_connections": {"probe"},
        "schema_diff": {"connection", "table_a", "table_b", "connection_b"},
        "diff_summary": {"connection", "table_a", "table_b", "connection_b",
                         "keys", "where"},
        "diff_tables": {"connection", "table_a", "table_b", "connection_b", "keys",
                        "columns_exclude", "where", "sample_limit",
                        "timeout_seconds", "force_large"},
    }
    for name, params in frozen_params.items():
        props = set(tools[name].parameters.get("properties", {}))
        # Frozen params must all exist; NEW params may be added (additive-only).
        missing = params - props
        assert not missing, f"{name} lost frozen params: {missing}"
