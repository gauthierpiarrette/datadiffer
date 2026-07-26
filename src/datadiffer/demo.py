"""`datadiffer demo` — seeded fixtures with planted regressions, run offline.

Generates two parquet files into ./datadiffer-demo/ and diffs them, printing
the exact command it ran so the grammar is taught. Deterministic (pure
arithmetic, no RNG). Planted story: an agent's refactor changed tax handling —
amount shifts concentrated almost entirely in country='DE', a batch of new
orders arrived with plan=NULL, and a slice of old rows was dropped.
"""

from __future__ import annotations

import os

import duckdb

from datadiffer.connect.duckdb_ import _sqlstr

DEMO_DIR = "datadiffer-demo"
_N = 50_000

_BASE = """
    SELECT range AS order_id,
           CAST(round((range % 97) * 1.37 + 5, 2) AS DOUBLE) AS amount,
           CASE WHEN range % 9 = 0 THEN 'DE'
                WHEN range % 9 < 5 THEN 'US'
                WHEN range % 9 < 7 THEN 'FR'
                ELSE 'GB' END AS country,
           CASE WHEN range % 4 = 0 THEN 'basic' ELSE 'pro' END AS plan,
           'shipped' AS status
    FROM range({n})
"""


def generate(directory: str = DEMO_DIR) -> tuple[str, str]:
    """Write orders_prod.parquet / orders_dev.parquet; return their paths."""
    os.makedirs(directory, exist_ok=True)
    a = os.path.join(directory, "orders_prod.parquet")
    b = os.path.join(directory, "orders_dev.parquet")
    base = _BASE.format(n=_N)
    con = duckdb.connect()
    con.execute(f"COPY ({base}) TO {_sqlstr(a)} (FORMAT parquet)")
    con.execute(f"""
        COPY (
          WITH base AS ({base})
          -- removed: a slice of old rows is gone
          SELECT order_id,
                 -- modified: tax refactor shifts amount, concentrated in DE
                 CAST(CASE WHEN country = 'DE' AND order_id % 13 = 0 THEN amount + 2.10
                           WHEN order_id % 9999 = 1 THEN amount + 0.01
                           ELSE amount END AS DOUBLE) AS amount,
                 country, plan, status
          FROM base
          WHERE NOT (order_id % 200 = 7)
          UNION ALL
          -- added: new orders, most arriving with plan = NULL
          SELECT {_N} + range,
                 CAST(round((range % 83) * 1.19 + 7, 2) AS DOUBLE),
                 CASE WHEN range % 9 = 0 THEN 'DE' ELSE 'US' END,
                 CASE WHEN range % 15 = 0 THEN 'pro' ELSE NULL END,
                 'shipped'
          FROM range(800)
        ) TO {_sqlstr(b)} (FORMAT parquet)
    """)
    con.close()
    return a, b
