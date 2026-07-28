# Migrating from data-diff or reladiff

[data-diff](https://github.com/datafold/data-diff) was archived in May 2024.
[reladiff](https://github.com/erezsh/reladiff), Erez Shinan's fork, last
released in March 2025. If you're still installing either in CI, this page
gets you across, or tells you plainly to stay.

## Should you switch?

**Stay on reladiff for now if** you diff more than ~50M rows across two
different databases. That's checksum-bisection territory ("1B rows in ~5
minutes"), and datadiffer does not have it yet. The port is the top item on
the v0.2 roadmap and will credit its author.

**Stay on reladiff for now if** you need MySQL, Oracle, ClickHouse, Trino,
Vertica, or Databricks. datadiffer v0.1 speaks Snowflake, Postgres, DuckDB,
Parquet, and CSV. BigQuery lands in v0.1.1.

**Switch if** you want a maintained tool that explains *where* the change is
concentrated, runs in CI with a real PR comment, exposes an MCP server for
agents, and is tested on Python 3.10–3.13.

## Command mapping

```bash
# data-diff / reladiff
data-diff postgresql://u@h/db orders postgresql://u@h/db orders_v2 -k order_id

# datadiffer
datadiffer diff orders orders_v2 --source "postgresql://u@h/db" --key order_id
```

| data-diff / reladiff | datadiffer | Notes |
|---|---|---|
| positional `DB1 table1 DB2 table2` | `diff <a> <b>` + `--source` / `--target` | `--target` defaults to `--source` |
| `-k`, `--key-columns` | `--key` | repeatable, or comma-separated; inferred when omitted |
| `-c`, `--columns` | `--columns` | unknown names are an **error**, never ignored |
| (no equivalent) | `--exclude-columns` | for `updated_at`, `_loaded_at`, and friends |
| `-w`, `--where` | `--where` | validated: single boolean expression, no subqueries |
| `-l`, `--limit` | `--sample-limit` | samples shown per status |
| `--json` | `--format json` | stable [schema v1](../src/datadiffer/report/json_schema.py) |
| `-t`, `--update-column` | `--where "updated_at < now() - interval '1 hour'"` | see replication lag below |
| `--bisection-factor`, `--bisection-threshold` | n/a | no hashdiff in v0.1 |
| `-d`, `--debug` | n/a | errors are explicit; open an issue if one is unclear |

## Behavior differences worth knowing

**Keys are inferred.** Omit `--key` and datadiffer uses declared PRIMARY KEY /
UNIQUE constraints first, then conventional names (`id`, `<table>_id`, `*_id`,
`*_key`), verifying non-null and unique on *both* sides, under your `--where`
filter. It refuses rather than guessing when nothing qualifies.

**Exit codes are a contract.** `0` no differences, `1` differences found,
`2` operational error. data-diff conflated the last two; CI can now tell "the
data changed" from "the connection broke."

**Typos fail loudly.** A misspelled column in `--columns` is an error. In a CI
gate, silently comparing nothing and reporting success is the worst outcome
available.

**Reports are machine-readable and frozen.** `--format json` emits schema v1,
which is contract-tested. It is the same payload the MCP server and the GitHub
Action produce. Changes within v1 are additive only.

## Replication lag

data-diff had `--min-age`. Use `--where` with your update column:

```bash
datadiffer diff orders orders_replica \
  --source "$PROD" --target "$REPLICA" \
  --where "updated_at < now() - interval '5 minutes'"
```

The filter is applied to both sides before the join, and to the key-uniqueness
probes, so partitioned append-only tables work correctly.

## Where the CI job goes

If you were running data-diff in CI, the [GitHub
Action](https://github.com/gauthierpiarrette/datadiffer-action) replaces the
shell step and adds the PR comment:

```yaml
- uses: gauthierpiarrette/datadiffer-action@v1
  with:
    source: ${{ secrets.WAREHOUSE_URL }}    # postgresql:// URI
    table-a: ANALYTICS.ORDERS
    schema-map: "ANALYTICS=ANALYTICS_PR_${PR_NUMBER}"
    max-changed-rows-pct: "0.5"
```

## If something is missing

Open an issue. The gaps above are tracked, and "I moved from data-diff and X
broke" is the single most useful bug report this project can receive right now.
