---
name: Bug report
about: Something produced a wrong number, crashed, or refused when it shouldn't
labels: bug
---

**What you ran**

```bash
datadiffer diff ...
```

**What you expected vs. what happened**

**Sources** (Parquet / CSV / DuckDB / Postgres / Snowflake, and whether the two
sides are the same engine)

**Report output** (`--format json` is ideal). Redact freely; the row counts,
`execution` block, and `schema_diff` are the useful parts.

**Version:** `datadiffer --version`, Python version, OS.

> Migrating from data-diff or reladiff and something broke? Say so, that is
> the most useful bug report this project can get right now.
