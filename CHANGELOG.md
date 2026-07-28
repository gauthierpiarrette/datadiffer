# Changelog

All notable changes to this project are documented here.
This project follows [semantic versioning](https://semver.org/).

## [0.1.0] - 2026-07-26

First working release.

### Added

- **`datadiffer diff`**: semantic change report between two tables: rows
  added / removed / modified / unchanged, per-column change rates, schema diff,
  and samples. Exit codes follow GNU diff (0 clean, 1 diff, 2 error).
- **Segment attribution**: "98.6% of modified rows have `country = 'DE'`
  (8.9× over-represented)". Single-column, categorical, status-relative
  baselines (added/modified vs. target, removed vs. source), ranked by
  Adtributor's Jensen-Shannon surprise.
- **Sources**: Parquet, CSV, DuckDB files, Postgres (read-only ATTACH),
  Snowflake (key-pair auth, projected Arrow pulls). Cross-source diffs pull
  through local DuckDB with row/byte admission caps.
- **Key inference**: declared PRIMARY KEY / UNIQUE constraints first, then
  conventional names (`id`, `<table>_id`, `*_id`, `*_key`); every candidate is
  verified non-null and unique on both filtered sides.
- **`datadiffer demo`**: offline seeded fixtures with planted regressions,
  so the tool demonstrates itself in 60 seconds with no credentials.
- **MCP server** (`datadiffer mcp`): stdio, four read-only tools
  (`list_connections`, `schema_diff`, `diff_summary`, `diff_tables`).
  Credentials never pass through the model; over-cap requests return
  structured refusals with a remedy.
- **`datadiffer ci`** and the companion
  [GitHub Action](https://github.com/gauthierpiarrette/datadiffer-action):
  sticky PR comments with the attribution callout, job summary, JSON artifact,
  and policy gates (`fail-on`, `max-changed-rows-pct`, `fail-on-schema-change`).
- **`datadiffer init` / `connections list|test`** and `datadiffer.toml`, one
  config shared by the CLI, the MCP server, and the Action.
- **Report JSON schema v1**, frozen: additive changes only, with contract tests
  guarding the field set that CI pipelines and agents depend on.

### Security

- `--where` is parsed, validated, and re-rendered from the AST: no subqueries,
  no foreign columns, no comments reaching the engine. The MCP path adds
  `WHERE_ALLOWLIST_V1`, an explicit function allowlist.
- Warehouse credentials are scrubbed from every display string, log line, and
  driver error message.
- Snowflake sessions open with `ABORT_DETACHED_QUERY=TRUE` and a statement
  timeout, so an abandoned client can never keep burning credits.

### Known limitations

- No checksum-bisection hashdiff yet, so cross-source diffs are capped at 50M
  rows / 10 GiB per side. Planned for v0.2, ported with credit to
  [reladiff](https://github.com/erezsh/reladiff).
- BigQuery lands in v0.1.1.
- Postgres reads are unsnapshotted across passes (reported in
  `execution.snapshot`).
- Attribution is single-column and categorical; numeric bucketing and
  multi-column interactions are v0.2.
