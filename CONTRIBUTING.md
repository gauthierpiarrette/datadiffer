# Contributing

Small, focused pull requests are easiest to merge. If a change is bigger than
a bug fix, open an issue first so the shape is agreed before you write it.

## Setup

```bash
git clone https://github.com/gauthierpiarrette/datadiffer && cd datadiffer
uv sync --group dev
uv run pytest -q          # warehouse tests skip unless credentials are set
uv run ruff check .
```

Warehouse tests are environment-gated: set `DATADIFFER_TEST_PG` for Postgres
and `DATADIFFER_TEST_SNOWFLAKE_*` for Snowflake, and they run for real. CI
runs Postgres on every push; Snowflake on `main`.

## What the tests protect

- **`tests/test_contract.py`** covers the frozen report schema v1 and the MCP tool
  surface. If you break these, CI pipelines and agents downstream break too.
  Changes within v1 are additive only.
- **`tests/test_regressions_*.py`** holds one case per defect that shipped once.
  They look arbitrary in isolation; each is load-bearing. Don't delete them.
- **`tests/test_where_fuzz.py`** covers the `--where` gate. Every named bypass class
  has a case; new gate behavior needs a new case.

## Standards

- Ruff clean, 100-column lines, Python 3.10+ (`tomli` fallback for 3.10).
- New comparison behavior needs a test with real fixture data, not a mock.
- Warehouse-specific SQL belongs in `dialects/` or the connector, never inline
  in the engine.
- Comments explain constraints the code can't show. Don't narrate the obvious.

## Good first issues

Issues labeled `good first issue` are real and self-contained. The connectors
(`src/datadiffer/connect/`) are the most approachable area: each one implements
a small protocol, and there are working examples for three engines.

## Reporting a security issue

Use GitHub's private vulnerability reporting (Security → Report a vulnerability)
rather than a public issue, and please include a reproducer. The gate
around `--where`, credential redaction, and the MCP tool surface are the areas
where a report is most valuable.
