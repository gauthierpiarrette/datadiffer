"""Segment attribution: which slice of the data changed.

Single execution path over the local join temp table. Scoring: support
(share of the status's rows), lift vs a STATUS-RELATIVE baseline, ranked by
Adtributor's bounded JS-surprise. Baselines: added/modified score against the
side-B population, removed against side-A, any_change against the exact union
(side-B rows + removed side-A rows), so every status's baseline is a superset of
its own rows, so lift stays finite. Dimension values are selected by ROW
PRESENCE (a value that changed to NULL attributes to NULL). Output language is
"over-represented", never causal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_NULL = "__dd_null__"  # sentinel: NULL is a first-class segment value

# Attribution-eligible DuckDB type prefixes: low-cardinality-able scalars.
_DIM_TYPES = ("VARCHAR", "BOOLEAN", "DATE", "TINYINT", "SMALLINT", "INTEGER",
              "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "ENUM")

_STATUSES = ("added", "removed", "modified", "any_change")
_BASELINE_BY_STATUS = {
    "added": "target", "modified": "target", "removed": "source", "any_change": "union",
}
@dataclass
class AttributionParams:
    max_cardinality: int = 50
    uniqueness_ratio: float = 0.2
    max_candidate_columns: int = 20
    top_k: int = 5
    min_segment_rows: int = 10
    min_support: float = 0.05
    min_lift: float = 1.5
    min_changed_rows: int = 30
    changed_rows_cap: int = 5_000_000  # above this, skip attribution (note in report)


@dataclass
class Segment:
    column: str
    value: str | None
    rows: int
    support: float
    baseline_share: float
    lift: float
    score: float


@dataclass
class StatusAttribution:
    total_rows: int
    note: str | None = None
    segments: list[Segment] = field(default_factory=list)


@dataclass
class Attribution:
    params: AttributionParams
    semantics: str
    baseline_by_status: dict[str, str]
    analyzed_columns: list[str]
    skipped_columns: list[dict[str, str]]
    by_status: dict[str, StatusAttribution]


def compute_attribution(
    ex,  # statement executor over the shared connection (counts statements)
    join_table: str,
    compare: list[tuple[str, str]],  # (column, mode) as materialized in the join table
    schema_a: dict[str, str],
    schema_b: dict[str, str],
    keys: list[str],
    status_totals: dict[str, int],  # added/removed/modified row counts
    params: AttributionParams | None = None,
) -> Attribution:
    p = params or AttributionParams()
    totals = dict(status_totals)
    totals["any_change"] = sum(status_totals.values())

    by_status: dict[str, StatusAttribution] = {}
    if totals["any_change"] > p.changed_rows_cap:
        for s in _STATUSES:
            if totals[s]:
                by_status[s] = StatusAttribution(totals[s], note="attribution_skipped_large")
        return _result(p, [], [], by_status)

    analyzed, skipped = _detect_candidates(ex, join_table, compare, schema_a, schema_b, keys, p)
    if not analyzed:
        for s in _STATUSES:
            if totals[s]:
                by_status[s] = StatusAttribution(totals[s], note="no_candidate_columns")
        return _result(p, analyzed, skipped, by_status)

    scored = _score(ex, join_table, analyzed, totals, p)
    for s in _STATUSES:
        if not totals[s]:
            continue
        if totals[s] < p.min_changed_rows:
            by_status[s] = StatusAttribution(totals[s], note="too_few_rows")
            continue
        segs = scored.get(s, [])
        by_status[s] = StatusAttribution(
            totals[s], note=None if segs else "uniform_change", segments=segs
        )
    return _result(p, analyzed, skipped, by_status)


def _result(p, analyzed, skipped, by_status) -> Attribution:
    return Attribution(
        params=p,
        semantics="after_side_by_presence",
        baseline_by_status=dict(_BASELINE_BY_STATUS),
        analyzed_columns=[c for c, _ in analyzed],
        skipped_columns=skipped,
        by_status=by_status,
    )


def _detect_candidates(ex, jt, compare, schema_a, schema_b, keys, p):
    """Type gate, then bounded exact distinct probes on BOTH sides (a dimension
    can be tame in B yet explosive in A; removed-row segments live in A)."""
    analyzed: list[tuple[str, int]] = []  # (column, index into compare)
    skipped: list[dict[str, str]] = []

    (n_a,) = ex(f"SELECT count(*) FROM {jt} WHERE NOT _dd_added").fetchone()
    (n_b,) = ex(f"SELECT count(*) FROM {jt} WHERE NOT _dd_removed").fetchone()
    n_max = max(int(n_a or 0), int(n_b or 0), 1)

    for i, (col, mode) in enumerate(compare):
        if col in keys:
            continue
        ta, tb = schema_a[col].upper(), schema_b[col].upper()
        if mode != "direct" or not (ta.startswith(_DIM_TYPES) and tb.startswith(_DIM_TYPES)):
            skipped.append({"column": col, "reason": "ineligible_type"})
            continue
        if len(analyzed) >= p.max_candidate_columns:
            skipped.append({"column": col, "reason": "max_candidate_columns"})
            continue
        limit = p.max_cardinality + 1
        (da,) = ex(
            f"SELECT count(*) FROM (SELECT DISTINCT _dd_a_{i} FROM {jt} "
            f"WHERE NOT _dd_added LIMIT {limit})"
        ).fetchone()
        (db,) = ex(
            f"SELECT count(*) FROM (SELECT DISTINCT _dd_b_{i} FROM {jt} "
            f"WHERE NOT _dd_removed LIMIT {limit})"
        ).fetchone()
        d = max(int(da), int(db))
        if d > p.max_cardinality:
            skipped.append({"column": col, "reason": "high_cardinality",
                            "distinct_count": f">{p.max_cardinality}"})
        elif d / n_max > p.uniqueness_ratio:
            skipped.append({"column": col, "reason": "quasi_unique", "distinct_count": str(d)})
        else:
            analyzed.append((col, i))
    return analyzed, skipped


def _score(ex, jt, analyzed, totals, p) -> dict[str, list[Segment]]:
    """One melted aggregation over the join table; scoring math in Python
    (statuses have different denominators, clearer here than nested SQL)."""

    def dim(i: int, side_case: str) -> str:
        # Escape real values equal to the sentinel so a literal '__dd_null__'
        # string never merges into the NULL segment.
        v = f"CAST({side_case} AS VARCHAR)"
        return (f"CASE WHEN {v} = '{_NULL}' THEN '{_NULL} (literal)' "
                f"WHEN {v} IS NULL THEN '{_NULL}' ELSE {v} END")

    presence = "CASE WHEN _dd_removed THEN _dd_a_{i} ELSE _dd_b_{i} END"
    melt_changed = " UNION ALL ".join(
        f"SELECT '{_esc(col)}' AS col, {dim(i, presence.format(i=i))} AS val, "
        f"CASE WHEN _dd_added THEN 'added' WHEN _dd_removed THEN 'removed' "
        f"ELSE 'modified' END AS status "
        f"FROM {jt} WHERE _dd_added OR _dd_removed OR (_dd_matched AND _dd_dirty)"
        for col, i in analyzed
    )
    melt_pops = " UNION ALL ".join(
        f"SELECT '{_esc(col)}' AS col, {dim(i, f'_dd_b_{i}')} AS val, 'target' AS pop "
        f"FROM {jt} WHERE NOT _dd_removed "
        f"UNION ALL "
        f"SELECT '{_esc(col)}', {dim(i, f'_dd_a_{i}')}, 'source' "
        f"FROM {jt} WHERE NOT _dd_added"
        for col, i in analyzed
    )
    rows = ex(f"""
        WITH changed AS ({melt_changed}),
        seg AS (
            SELECT col, val, status, count(*)::DOUBLE AS n FROM changed GROUP BY 1, 2, 3
            UNION ALL
            SELECT col, val, 'any_change', count(*)::DOUBLE FROM changed GROUP BY 1, 2
        ),
        base AS (
            SELECT col, val, pop, count(*)::DOUBLE AS n
            FROM ({melt_pops}) GROUP BY 1, 2, 3
        ),
        base_union AS (  -- exact union population: side-B rows + removed side-A rows.
            -- FULL OUTER: a value existing ONLY in removed rows must still be
            -- in the union baseline, or it disappears from the report.
            SELECT COALESCE(b.col, r.col) AS col, COALESCE(b.val, r.val) AS val,
                   COALESCE(b.n, 0) + COALESCE(r.n, 0) AS n
            FROM (SELECT col, val, n FROM base WHERE pop = 'target') b
            FULL OUTER JOIN (SELECT col, val, n FROM seg WHERE status = 'removed') r
              ON b.col = r.col AND b.val = r.val
        ),
        base_all AS (
            SELECT col, val, pop, n FROM base
            UNION ALL SELECT col, val, 'union', n FROM base_union
        ),
        pop_tot AS (SELECT pop, col, sum(n) AS n FROM base_all GROUP BY 1, 2)
        SELECT s.status, s.col, s.val, s.n, b.n AS base_n, pt.n AS pop_n
        FROM seg s
        JOIN base_all b ON b.col = s.col AND b.val = s.val AND b.pop = CASE s.status
            WHEN 'removed' THEN 'source'
            WHEN 'any_change' THEN 'union'
            ELSE 'target' END
        JOIN pop_tot pt ON pt.col = s.col AND pt.pop = b.pop
    """).fetchall()

    out: dict[str, list[Segment]] = {}
    for status, col, val, seg_n, base_n, pop_n in rows:
        total = totals.get(status, 0)
        if not total or total < p.min_changed_rows:
            continue
        support = seg_n / total
        baseline = (base_n / pop_n) if pop_n else 0.0
        if baseline <= 0:
            continue  # superset construction makes this unreachable; div-zero guard only
        lift = support / baseline
        if seg_n < p.min_segment_rows or support < p.min_support or lift < p.min_lift:
            continue
        pq = baseline + support
        score = 0.5 * (
            baseline * math.log(2 * baseline / pq) + support * math.log(2 * support / pq)
        )
        if score != score:  # NaN never ranks
            continue
        out.setdefault(status, []).append(Segment(
            column=col,
            value=None if val == _NULL else val,
            rows=int(seg_n),
            support=round(support, 4),
            baseline_share=round(baseline, 4),
            lift=round(lift, 2),
            score=round(score, 6),
        ))
    for segs in out.values():
        segs.sort(key=lambda s: (-s.score, s.column, str(s.value)))
        del segs[p.top_k:]
    return out


def _esc(s: str) -> str:
    return s.replace("'", "''")
