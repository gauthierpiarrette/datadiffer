# Diffing dbt models in CI

datadiffer isn't a dbt tool — it doesn't read your manifest or care about your
project. But dbt already tells you which models a PR changed, and most dbt CI
setups already build them into a per-PR schema. That's everything needed to
diff each changed model against production automatically.

This recipe uses `dbt ls --select state:modified` to build a job matrix, then
runs one datadiffer step per changed model.

## What you need first

- A **production manifest** available to CI (`state:modified` compares against
  it). dbt Cloud exposes one; self-hosted setups usually download the manifest
  from the last successful `main` run's artifacts or from S3.
- A **per-PR schema** built by your existing CI job. dbt Cloud uses
  `dbt_cloud_pr_<job>_<pr>`; self-hosted conventions are usually `PR_<number>`
  or `<target>_pr_<number>`. Whatever yours is, you'll express it as a
  `schema-map`.
- A **read-only warehouse role** for datadiffer (below).

## The workflow

```yaml
name: data-diff
on: pull_request

permissions:
  contents: read
  pull-requests: write

jobs:
  # 1. Build the PR schema and list the models this PR touched.
  changed:
    runs-on: ubuntu-latest
    outputs:
      models: ${{ steps.list.outputs.models }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6

      - name: Install dbt
        run: uv tool install "dbt-snowflake>=1.8"

      - name: Fetch production manifest
        run: |
          mkdir -p prod-artifacts
          # however you publish it — S3, GH artifact, dbt Cloud API:
          aws s3 cp s3://my-bucket/dbt/manifest.json prod-artifacts/manifest.json

      - name: Build changed models into the PR schema
        env:
          DBT_SCHEMA: PR_${{ github.event.pull_request.number }}
          SNOWFLAKE_PRIVATE_KEY: ${{ secrets.SNOWFLAKE_PRIVATE_KEY }}
        run: dbt build --select state:modified+ --state prod-artifacts --target ci

      - name: List changed models as a JSON matrix
        id: list
        run: |
          # dbt ls emits one JSON object per line; --output-keys name gives
          # just the model names, which become the matrix entries.
          dbt ls --select state:modified --state prod-artifacts \
                 --resource-type model --output json --output-keys name \
            | jq -sc '[.[].name]' > models.json
          echo "models=$(cat models.json)" >> "$GITHUB_OUTPUT"
          cat models.json

  # 2. One diff per changed model, each with its own sticky comment.
  diff:
    needs: changed
    if: needs.changed.outputs.models != '[]'
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        model: ${{ fromJSON(needs.changed.outputs.models) }}
    steps:
      - uses: gauthierpiarrette/datadiffer-action@v1
        with:
          version: "0.1.0"
          config: datadiffer.toml     # defines the `wh` Snowflake connection
          table-a: "wh::ANALYTICS.${{ matrix.model }}"
          schema-map: "ANALYTICS=PR_${PR_NUMBER}"
          header: "${{ matrix.model }}"          # one comment per model
          exclude-columns: "updated_at,_loaded_at,dbt_updated_at"
          max-changed-rows-pct: "1.0"
        env:
          SNOWFLAKE_PRIVATE_KEY: ${{ secrets.SNOWFLAKE_PRIVATE_KEY }}
```

Three things make this work:

- **`header: ${{ matrix.model }}`** gives each model its own sticky comment, so
  re-runs update in place instead of stacking.
- **`schema-map`** rewrites only side B's schema, so `table-a` stays the
  production relation and side B becomes the PR build of the same model.
  `${PR_NUMBER}` is exported by the action.
- **`fail-fast: false`** so one model over threshold doesn't cancel the others.

## The read-only role

datadiffer only ever runs `SELECT` — no temp tables, no writes — so the role
needs nothing else:

```sql
CREATE ROLE IF NOT EXISTS DATADIFFER_READER;
GRANT USAGE ON WAREHOUSE DIFF_XS TO ROLE DATADIFFER_READER;
GRANT USAGE ON DATABASE ANALYTICS TO ROLE DATADIFFER_READER;
GRANT USAGE ON ALL SCHEMAS IN DATABASE ANALYTICS TO ROLE DATADIFFER_READER;
GRANT SELECT ON ALL TABLES IN DATABASE ANALYTICS TO ROLE DATADIFFER_READER;
GRANT SELECT ON FUTURE TABLES IN DATABASE ANALYTICS TO ROLE DATADIFFER_READER;
-- and the same two grants for VIEWS if you diff views
```

Use a key-pair service user (`TYPE=SERVICE`); Snowflake blocks password auth
for service users. Postgres equivalent: a `LOGIN` role with `CONNECT`, `USAGE`
on the schemas, `SELECT` on all tables, and matching default privileges.

## Tuning it for real projects

**Exclude the noise columns.** Most dbt models carry `updated_at`,
`_loaded_at`, or `dbt_updated_at`, which change on every build. Without
`--exclude-columns` every row looks modified and the report is useless.

**Diff a slice, not the warehouse.** For large incremental models, add a
`where` that bounds the comparison:

```yaml
where: "event_date >= current_date - 7"
```

**Pick a threshold you'd actually act on.** `max-changed-rows-pct: "1.0"` fails
the check when more than 1% of base rows changed. Start permissive; a gate that
cries wolf gets disabled in a week. `fail-on: never` gives you the comment with
no gate at all, which is a good first month.

## Why not just `audit_helper`?

[dbt-audit-helper](https://github.com/dbt-labs/dbt-audit-helper) is good, and
if it's working for you there's no reason to move. The differences:

- audit_helper is macros you run and read yourself; datadiffer is a CLI plus a
  PR comment, so the result shows up where review happens without anyone
  running anything.
- audit_helper compares within one warehouse; datadiffer also diffs across
  sources (warehouse vs. Parquet, Postgres vs. DuckDB).
- audit_helper tells you which rows differ; datadiffer additionally tells you
  *where the change is concentrated* ("98% of modified rows are `country =
  'DE'`"), which is usually the thing you actually wanted to know.
- audit_helper is dbt-native — it knows your project. datadiffer doesn't, which
  is why this page exists.

Use both if you like: audit_helper for ad-hoc investigation in your IDE,
datadiffer for the automatic gate on every PR.
