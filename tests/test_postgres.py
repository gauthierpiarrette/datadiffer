"""Postgres source tests. Gated on DATADIFFER_TEST_PG (a postgresql:// DSN);
CI provides a postgres service container. Seeding goes through DuckDB's
postgres extension so the tests need no extra driver."""

import os

import duckdb
import pytest

import datadiffer
from datadiffer.errors import DatadifferError

DSN = os.environ.get("DATADIFFER_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="DATADIFFER_TEST_PG not set")


@pytest.fixture(scope="module")
def seeded_pg():
    con = duckdb.connect()
    con.execute(f"ATTACH '{DSN}' AS seed (TYPE postgres)")
    con.execute("CALL postgres_execute('seed', 'DROP TABLE IF EXISTS orders')")
    con.execute("CALL postgres_execute('seed', 'DROP TABLE IF EXISTS orders_v2')")
    con.execute("CALL postgres_execute('seed', "
                "'CREATE TABLE orders (order_id int PRIMARY KEY, "
                "amount double precision, country text)')")
    con.execute("CALL postgres_execute('seed', "
                "'CREATE TABLE orders_v2 (order_id int PRIMARY KEY, "
                "amount double precision, country text)')")
    # Out-of-band DDL: the extension caches schema info — refresh before using
    # the catalog path for INSERTs.
    con.execute("CALL pg_clear_cache()")
    con.execute(
        "INSERT INTO seed.public.orders "
        "SELECT range, range * 1.5, CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END "
        "FROM range(200)"
    )
    con.execute(
        # +1.0 so ALL 20 rows genuinely differ (0 * 2.5 == 0 * 1.5 fixed point)
        "INSERT INTO seed.public.orders_v2 "
        "SELECT range, CASE WHEN range < 20 THEN range * 2.5 + 1.0 ELSE range * 1.5 END, "
        "CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END "
        "FROM range(210)"
    )
    # Fresh statistics so the row/byte guards exercise their estimate branches in CI.
    con.execute("CALL postgres_execute('seed', 'ANALYZE')")
    con.close()
    yield DSN


def test_pg_to_pg_diff(seeded_pg):
    report = datadiffer.diff("orders", "orders_v2", source=seeded_pg)
    assert report.rows.added == 10
    assert report.rows.removed == 0
    assert report.rows.modified == 20
    assert report.column("country").changed_rows == 0


def test_pg_declared_pk_inferred(seeded_pg):
    report = datadiffer.diff("orders", "orders_v2", source=seeded_pg)
    assert report.primary_key.columns == ["order_id"]
    assert report.primary_key.rule == "declared"


def test_pg_to_parquet_cross_source(seeded_pg, tmp_path):
    p = tmp_path / "orders.parquet"
    con = duckdb.connect()
    con.execute(f"ATTACH '{DSN}' AS s (TYPE postgres, READ_ONLY)")
    con.execute(f"COPY (SELECT * FROM s.public.orders) TO '{p}' (FORMAT parquet)")
    con.close()
    report = datadiffer.diff("orders", str(p), source=seeded_pg)
    assert not report.has_diff


def test_pg_bad_dsn_exits_operationally():
    with pytest.raises(DatadifferError, match="Cannot connect"):
        datadiffer.diff("orders", "orders", source="postgresql://nope:1/void")
