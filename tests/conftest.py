import duckdb
import pytest


@pytest.fixture
def fixture_pair(tmp_path):
    """Two parquet files with a planted diff.

    Base: 1000 rows (order_id 0..999). Target: removes ids 0-4 (5 removed),
    adds ids 1000-1011 (12 added), modifies amount for ids 10-29 (20 modified).
    `status` and `country` never change, which the per-column zero assertion relies on.
    """
    a = tmp_path / "prod.parquet"
    b = tmp_path / "dev.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (
          SELECT range AS order_id,
                 (range % 7)::DOUBLE + 0.5 AS amount,
                 CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END AS country,
                 'shipped' AS status
          FROM range(1000)
        ) TO '{a}' (FORMAT parquet)
    """)
    con.execute(f"""
        COPY (
          SELECT range AS order_id,
                 CASE WHEN range BETWEEN 10 AND 29
                      THEN (range % 7)::DOUBLE + 1.5
                      ELSE (range % 7)::DOUBLE + 0.5 END AS amount,
                 CASE WHEN range % 3 = 0 THEN 'DE' ELSE 'US' END AS country,
                 'shipped' AS status
          FROM range(1012)
          WHERE range >= 5
        ) TO '{b}' (FORMAT parquet)
    """)
    con.close()
    return str(a), str(b)
