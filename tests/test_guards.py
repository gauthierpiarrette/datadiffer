"""Unit tests for guards and redaction, pg-free (monkeypatched estimates)."""

import io

import duckdb
import pytest
from rich.console import Console

import datadiffer
from datadiffer.connect import postgres_
from datadiffer.connect.duckdb_ import LocalSource
from datadiffer.connect.postgres_ import check_row_guard, open_postgres, redact
from datadiffer.errors import DatadifferError
from datadiffer.report.render_text import render

SRC = LocalSource(rel="x", display="orders (postgresql://h/db)", stem="orders",
                  kind="postgres", alias="a", schema="public", table="orders")


def test_redact_covers_userinfo_and_query_params():
    assert redact("postgresql://u:s3cret@h:5432/db") == "postgresql://h:5432/db"
    assert (redact("postgresql://h/db?user=x&password=hunter2&sslmode=require")
            == "postgresql://h/db?user=x&password=***&sslmode=require")
    assert redact("host=h password=pw") == "host=h password=***"
    assert "sslkey=***" in redact("postgresql://h/db?sslkey=/tmp/k&password=p")


def test_row_guard_hard_cap(monkeypatch):
    monkeypatch.setattr(postgres_, "estimated_rows", lambda c, s: postgres_.ROW_HARD_CAP + 1)
    monkeypatch.setattr(postgres_, "estimated_bytes", lambda c, s: 1)
    with pytest.raises(DatadifferError, match="--where"):
        check_row_guard(duckdb.connect(), SRC)


def test_row_guard_warn_tier(monkeypatch):
    monkeypatch.setattr(postgres_, "estimated_rows", lambda c, s: postgres_.ROW_WARN + 1)
    monkeypatch.setattr(postgres_, "estimated_bytes", lambda c, s: 1)
    warnings = check_row_guard(duckdb.connect(), SRC)
    assert any(w.startswith("large_pull:orders") for w in warnings)


def test_row_guard_unavailable_estimates(monkeypatch):
    monkeypatch.setattr(postgres_, "estimated_rows", lambda c, s: None)
    monkeypatch.setattr(postgres_, "estimated_bytes", lambda c, s: None)
    warnings = check_row_guard(duckdb.connect(), SRC)
    assert "row_estimate_unavailable:orders" in warnings
    assert "byte_estimate_unavailable:orders" in warnings


def test_byte_guard_hard_cap(monkeypatch):
    monkeypatch.setattr(postgres_, "estimated_rows", lambda c, s: 100)
    monkeypatch.setattr(postgres_, "estimated_bytes", lambda c, s: postgres_.BYTE_HARD_CAP + 1)
    with pytest.raises(DatadifferError, match="GiB"):
        check_row_guard(duckdb.connect(), SRC)


def test_open_postgres_rejects_uri_as_table_ref():
    with pytest.raises(DatadifferError, match="connection URI"):
        open_postgres(duckdb.connect(), "postgresql://h/db", "postgresql://u@h/db", "x")


def test_warnings_visible_in_text_output(fixture_pair):
    a, b = fixture_pair
    report = datadiffer.diff(a, b)
    report.execution.warnings.append("unsnapshotted_multipass")
    buf = io.StringIO()
    render(report, Console(file=buf, force_terminal=False, width=100))
    assert "warning: unsnapshotted_multipass" in buf.getvalue()
