"""Snowflake source tests. Gated on DATADIFFER_TEST_SNOWFLAKE_ACCOUNT (+ the
key/user/warehouse/database env vars). Each run seeds its own schema so
parallel CI matrix jobs never race, and drops it on teardown."""

import os
import sys

import duckdb
import pytest

import datadiffer

ACCOUNT = os.environ.get("DATADIFFER_TEST_SNOWFLAKE_ACCOUNT")
pytestmark = pytest.mark.skipif(not ACCOUNT, reason="DATADIFFER_TEST_SNOWFLAKE_* not set")

SCHEMA = f"FX_{os.environ.get('GITHUB_RUN_ID', 'LOCAL')}_{sys.version_info.minor}"


def _cfg() -> dict:
    return {
        "type": "snowflake",
        "account": ACCOUNT,
        "user": os.environ["DATADIFFER_TEST_SNOWFLAKE_USER"],
        "private_key_path": os.environ["DATADIFFER_TEST_SNOWFLAKE_KEY"],
        "warehouse": os.environ["DATADIFFER_TEST_SNOWFLAKE_WAREHOUSE"],
        "database": os.environ["DATADIFFER_TEST_SNOWFLAKE_DATABASE"],
        "schema": SCHEMA,
    }


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    from datadiffer.connect.snowflake_ import _connect

    con = _connect(_cfg())
    cur = con.cursor()
    cur.execute(f"CREATE OR REPLACE SCHEMA {SCHEMA}")
    cur.execute(f"""
        CREATE TABLE {SCHEMA}.ORDERS AS
        SELECT ROW_NUMBER() OVER (ORDER BY seq4()) AS ORDER_ID,
               (ROW_NUMBER() OVER (ORDER BY seq4()) * 1.5)::DOUBLE AS AMOUNT,
               IFF(ROW_NUMBER() OVER (ORDER BY seq4()) % 3 = 0, 'DE', 'US') AS COUNTRY
        FROM TABLE(GENERATOR(ROWCOUNT => 200))
    """)
    cur.execute(f"""
        CREATE TABLE {SCHEMA}.ORDERS_V2 AS
        SELECT ROW_NUMBER() OVER (ORDER BY seq4()) AS ORDER_ID,
               IFF(ROW_NUMBER() OVER (ORDER BY seq4()) % 3 = 0
                   AND ROW_NUMBER() OVER (ORDER BY seq4()) <= 120,
                   (ROW_NUMBER() OVER (ORDER BY seq4()) * 1.5 + 1.0)::DOUBLE,
                   (ROW_NUMBER() OVER (ORDER BY seq4()) * 1.5)::DOUBLE) AS AMOUNT,
               IFF(ROW_NUMBER() OVER (ORDER BY seq4()) % 3 = 0, 'DE', 'US') AS COUNTRY
        FROM TABLE(GENERATOR(ROWCOUNT => 210))
    """)
    cur.execute(f"ALTER TABLE {SCHEMA}.ORDERS ADD PRIMARY KEY (ORDER_ID)")
    cur.execute(f"ALTER TABLE {SCHEMA}.ORDERS_V2 ADD PRIMARY KEY (ORDER_ID)")
    con.close()

    cfg_dir = tmp_path_factory.mktemp("sfcfg")
    cfg_file = cfg_dir / "cfg.toml"
    lines = ["[connections.sf]"] + [
        f'{k} = "{v}"' for k, v in _cfg().items()
    ]
    cfg_file.write_text("\n".join(lines) + "\n")
    os.environ["DATADIFFER_CONFIG"] = str(cfg_file)
    yield str(cfg_file)
    del os.environ["DATADIFFER_CONFIG"]
    con = _connect(_cfg())
    con.cursor().execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    con.close()


def test_sf_to_sf_diff_with_declared_pk(seeded):
    report = datadiffer.diff("sf::orders", "sf::orders_v2")
    assert report.rows.added == 10
    assert report.rows.removed == 0
    assert report.rows.modified == 40  # DE rows (%3==0) with id <= 120
    assert report.column("amount").changed_rows == 40
    assert report.column("country").changed_rows == 0
    assert report.primary_key.columns == ["order_id"]
    assert report.primary_key.rule == "declared"


def test_sf_attribution_headline(seeded):
    report = datadiffer.diff("sf::orders", "sf::orders_v2")
    seg = report.attribution.by_status["modified"].segments[0]
    assert seg.column == "country"
    assert seg.value == "DE"
    assert seg.support == 1.0


def test_sf_to_parquet_identical(seeded, tmp_path):
    """Case folding: SF's uppercase unquoted identifiers must match lowercase
    parquet columns; identical data must report no diff."""
    p = tmp_path / "orders.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT range AS order_id, CAST(range * 1.5 AS DOUBLE) AS amount,
                     CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END AS country
              FROM range(1, 201)) TO '{p}' (FORMAT parquet)
    """)
    con.close()
    report = datadiffer.diff("sf::orders", str(p))
    assert not report.has_diff
    assert report.rows.matched == 200


def test_sf_projection_excludes_columns(seeded):
    report = datadiffer.diff("sf::orders", "sf::orders_v2", exclude_columns=["amount"])
    assert report.rows.modified == 0  # only amount changed
    assert all(c.name != "amount" for c in report.columns)


def test_sf_decimal_fidelity(seeded, tmp_path):
    """A last-decimal NUMBER(18,4) change at 14 integer
    digits must be flagged — float64 pulls silently missed it."""
    from datadiffer.connect.snowflake_ import _connect

    cur = _connect(_cfg()).cursor()
    cur.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.MONEY_A (ID INT, V NUMBER(18,4))")
    cur.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.MONEY_B (ID INT, V NUMBER(18,4))")
    cur.execute(f"INSERT INTO {SCHEMA}.MONEY_A VALUES (1, 12345678901234.5678)")
    cur.execute(f"INSERT INTO {SCHEMA}.MONEY_B VALUES (1, 12345678901234.5679)")
    cur.connection.close()
    report = datadiffer.diff("sf::money_a", "sf::money_b", keys=["id"])
    assert report.rows.modified == 1
    assert report.column("v").changed_rows == 1


def test_sf_empty_side(seeded):
    from datadiffer.connect.snowflake_ import _connect

    cur = _connect(_cfg()).cursor()
    cur.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.EMPTY_T "
                f"(ORDER_ID INT, AMOUNT DOUBLE, COUNTRY TEXT)")
    cur.connection.close()
    report = datadiffer.diff("sf::orders", "sf::empty_t", keys=["order_id"])
    assert report.rows.removed == 200  # everything gone; must not crash


def test_sf_guard_estimates_fold_case(seeded):
    """Guard must see stats for lowercase-typed refs (information_schema
    stores folded names)."""
    from datadiffer.connect.snowflake_ import SnowflakeHandle

    h = SnowflakeHandle.open(_cfg(), "orders")
    try:
        assert h.row_estimate() == 200
        assert h.byte_estimate() is not None
    finally:
        h.close()


def test_sf_variant_vs_local_json_no_false_positive(seeded, tmp_path):
    """VARIANT pretty-printing must not diff against compact local JSON."""
    import duckdb as _d

    from datadiffer.connect.snowflake_ import _connect

    cur = _connect(_cfg()).cursor()
    cur.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.JS_T (ID INT, J VARIANT)")
    cur.execute(
        f"INSERT INTO {SCHEMA}.JS_T SELECT 1, PARSE_JSON('{{\"a\": 1, \"b\": [2, 3]}}')"
    )
    cur.connection.close()
    p = tmp_path / "js.parquet"
    con = _d.connect()
    con.execute(
        f"""COPY (SELECT 1 AS id, '{{"a":1,"b":[2,3]}}'::JSON AS j) TO '{p}' (FORMAT parquet)"""
    )
    con.close()
    report = datadiffer.diff("sf::js_t", str(p), keys=["id"])
    assert not report.has_diff
    assert report.column("j").compared_as == "json_text"


def test_sf_timestamp_cross_source(seeded, tmp_path):
    """Ns-precision pulls broke comparability; a real 1h change must be seen."""
    import duckdb as _d

    from datadiffer.connect.snowflake_ import _connect

    cur = _connect(_cfg()).cursor()
    cur.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.TS_T (ID INT, TS TIMESTAMP_NTZ)")
    cur.execute(f"INSERT INTO {SCHEMA}.TS_T VALUES (1, '2026-01-01 10:00:00')")
    cur.connection.close()
    p = tmp_path / "ts.parquet"
    con = _d.connect()
    con.execute(
        f"COPY (SELECT 1 AS id, TIMESTAMP '2026-01-01 11:00:00' AS ts) TO '{p}' (FORMAT parquet)"
    )
    con.close()
    report = datadiffer.diff("sf::ts_t", str(p), keys=["id"])
    assert report.rows.modified == 1
    assert not report.schema_diff.columns_skipped  # no type_mismatch skip


def test_sf_casual_capitalization_resolves(seeded):
    """Sf::Orders (unquoted-style caps) must resolve like Snowflake SQL would."""
    report = datadiffer.diff("sf::Orders", "sf::ORDERS")
    assert not report.has_diff


def test_sf_schema_diff_reports_source_columns(seeded, tmp_path):
    """A column existing only on the SF side must appear in columns_removed
    even though projection never pulls it."""
    import duckdb as _d

    from datadiffer.connect.snowflake_ import _connect

    cur = _connect(_cfg()).cursor()
    cur.execute(
        f"CREATE OR REPLACE TABLE {SCHEMA}.XSRC AS "
        f"SELECT 1 AS ID, 2 AS V, 3 AS W, 'only-here' AS EXTRA_SF_ONLY"
    )
    cur.connection.close()
    p = tmp_path / "xsrc.parquet"
    con = _d.connect()
    con.execute(f"COPY (SELECT 1 AS id, 2 AS v, 3 AS w) TO '{p}' (FORMAT parquet)")
    con.close()
    report = datadiffer.diff("sf::xsrc", str(p), keys=["id"])
    assert [c["name"] for c in report.schema_diff.columns_removed] == ["extra_sf_only"]

    report2 = datadiffer.diff("sf::xsrc", str(p), keys=["id"], exclude_columns=["v"])
    assert report2.schema_diff.columns_added == []  # excluded != "added"
