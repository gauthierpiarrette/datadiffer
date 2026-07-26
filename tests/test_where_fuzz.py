"""Fuzz suite for the --where gate: every known bypass class."""

import pytest

from datadiffer.api import _validate_where
from datadiffer.errors import DatadifferError

COMMON = {"id": "BIGINT", "amount": "DOUBLE", "country": "VARCHAR", "tag": "VARCHAR"}


def ok(where, strict=False):
    sql, cols = _validate_where(where, COMMON, strict=strict)
    return sql


def refuse(where, match, strict=False):
    with pytest.raises(DatadifferError, match=match):
        _validate_where(where, COMMON, strict=strict)


# --- both modes -------------------------------------------------------------

def test_multi_statement_refused():
    refuse("id > 0; DROP TABLE x", "single boolean")


def test_stacked_via_union_refused():
    refuse("id IN (SELECT 1 UNION SELECT 2)", "subqueries")


def test_subquery_and_exists_refused():
    refuse("(SELECT pg_sleep(3600)) IS NULL AND 1=1", "subqueries")
    refuse("EXISTS (SELECT 1)", "subqueries")


def test_foreign_and_qualified_columns_refused():
    refuse("other_table.id = 1", "qualify")
    refuse("secret_col = 1", "not on both sides")


def test_comment_smuggling_neutralized_by_rerender():
    # the gate returns the RE-RENDERED AST: comments never reach the engine
    rendered = ok("id > 0 -- DROP TABLE x")
    assert "DROP" not in rendered and "--" not in rendered
    rendered2 = ok("id > /* sneaky */ 0")
    assert "sneaky" not in rendered2


def test_literal_containing_dashes_allowed():
    assert "a--b" in ok("tag = 'a--b'")


# --- strict mode (WHERE_ALLOWLIST_V1, the MCP gate) --------------------------

def test_strict_allows_the_allowlist():
    ok("lower(country) = 'de' AND amount BETWEEN 1 AND 2", strict=True)
    ok("coalesce(tag, '') LIKE 'x%' OR id IN (1, 2, 3)", strict=True)
    ok("date_trunc('day', CAST(tag AS DATE)) IS NOT NULL", strict=True)
    ok("round(abs(amount), 2) > 1.5", strict=True)


def test_strict_refuses_unknown_functions():
    refuse("pg_sleep(9) IS NULL", "allowlist", strict=True)
    refuse("read_csv('x') IS NOT NULL", "subqueries|allowlist", strict=True)
    refuse("version() = 'x'", "allowlist", strict=True)


def test_strict_refuses_regex_operators():
    refuse("country ~ 'D.'", "allowlist", strict=True)
    refuse("regexp_matches(country, 'D.')", "allowlist", strict=True)


def test_strict_refuses_parameter_markers():
    refuse("id = ?", "single boolean|parameter", strict=True)
    refuse("id = $1", "parameter|allowlist|single boolean", strict=True)


def test_strict_refuses_nonscalar_casts():
    refuse("CAST(tag AS STRUCT(a INT)) IS NULL", "non-scalar", strict=True)


def test_strict_dialect_escape_attempts():
    refuse("country COLLATE NOCASE = 'de'", "allowlist", strict=True)
    refuse("list_contains([1,2], id)", "allowlist", strict=True)
