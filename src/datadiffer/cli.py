"""datadiffer command-line interface.

Verbs: diff, ci, mcp, init, connections, demo.
Exit codes (GNU-diff convention): 0 = no diff / within policy,
1 = diff over policy, 2 = operational error.
"""

from __future__ import annotations

import click

from datadiffer import __version__
from datadiffer.errors import DatadifferError

_EXAMPLES = """\
Examples:

  # try it in 60 seconds - generates its own data, no credentials
  datadiffer demo

  # local files - no credentials, no config
  datadiffer diff prod.parquet dev.parquet

  # DuckDB tables
  datadiffer diff prod.duckdb:orders dev.duckdb:orders

  # Postgres (attached read-only)
  datadiffer diff orders orders_v2 --source "postgresql://user@host:5432/db"

  # Snowflake (key-pair auth, via datadiffer.toml — run `datadiffer init`)
  datadiffer diff wh::orders wh::orders_v2

  # explicit key, JSON report
  datadiffer diff a.parquet b.parquet --key order_id --format json
"""

@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="datadiffer")
@click.pass_context
def main(ctx: click.Context) -> None:
    """datadiffer — diff two tables and see which slice of your data changed."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        click.echo()
        click.echo(_EXAMPLES)


@main.command()
@click.argument("table_a")
@click.argument("table_b")
@click.option("--source", help="Connection URI for bare table names (side A default).")
@click.option("--target", help="Connection URI for side B (defaults to --source).")
@click.option("--key", "keys", multiple=True,
              help="Primary key column (repeatable or comma-separated).")
@click.option("--where", help="SQL boolean predicate applied to both sides.")
@click.option("--columns", help="Comma-separated columns to compare (default: all common).")
@click.option("--exclude-columns", help="Comma-separated columns to ignore.")
@click.option("--sample-limit", default=10, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option("-q", "--quiet", is_flag=True, help="Suppress report; exit code only.")
@click.option("--no-attribution", is_flag=True, help="Skip segment attribution.")
@click.option("--fail-on", type=click.Choice(["any-diff", "never"]), default="any-diff",
              show_default=True)
def diff(table_a, table_b, source, target, keys, where, columns,
         exclude_columns, sample_limit, fmt, quiet, no_attribution, fail_on) -> None:
    """Diff two tables and print a semantic change report."""
    from datadiffer import api

    try:
        report = api.diff(
            table_a, table_b,
            source=source, target=target,
            keys=_split(keys) or None,
            columns=_split_str(columns),
            exclude_columns=_split_str(exclude_columns),
            where=where,
            sample_limit=sample_limit,
            attribution="off" if no_attribution else "auto",
        )
    except DatadifferError as e:
        _fail(e)

    if not quiet:
        if fmt == "json":
            click.echo(report.to_json())
        else:
            from datadiffer.report.render_text import render
            render(report)

    if report.has_diff and fail_on == "any-diff":
        raise SystemExit(1)


def _split(values: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for v in values:
        out.extend(part.strip() for part in v.split(",") if part.strip())
    return out


def _split_str(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _fail(e: DatadifferError) -> None:
    click.echo(f"error: {e}", err=True)
    raise SystemExit(2) from e


@main.command()
@click.option("--table-a", required=True)
@click.option("--table-b", required=True)
@click.option("--source", help="Connection URI; ${ENV_VAR} refs resolve from the environment.")
@click.option("--target")
@click.option("--schema-map", help="OLD_SCHEMA=NEW_SCHEMA[,...]; supports ${PR_NUMBER}.")
@click.option("--key", "keys", multiple=True)
@click.option("--where")
@click.option("--exclude-columns")
@click.option("--fail-on", type=click.Choice(["threshold", "any-diff", "never"]),
              default="threshold", show_default=True)
@click.option("--max-changed-rows-pct", type=float)
@click.option("--fail-on-schema-change", is_flag=True)
@click.option("--sample-rows", default=5, show_default=True)
@click.option("--header", default="default", show_default=True)
@click.option("--report-json")
@click.option("--comment-md")
@click.option("--summary-md")
@click.option("--github-output", help="Path to append step outputs (GITHUB_OUTPUT).")
def ci(table_a, table_b, source, target, schema_map, keys, where, exclude_columns,
       fail_on, max_changed_rows_pct, fail_on_schema_change, sample_rows, header,
       report_json, comment_md, summary_md, github_output) -> None:
    """CI entry point: one single-table diff -> report.json, comment.md,
    summary.md, and step outputs. Exit codes: 0 within policy, 1 over policy,
    2 operational error."""
    from datadiffer.ci import run_ci

    try:
        code = run_ci(
            table_a=table_a, table_b=table_b, source=source, target=target,
            schema_map=schema_map, keys=_split(keys) or None, where=where,
            exclude_columns=_split_str(exclude_columns), fail_on=fail_on,
            max_changed_rows_pct=max_changed_rows_pct,
            fail_on_schema_change=fail_on_schema_change, sample_rows=sample_rows,
            header=header, report_json=report_json, comment_md=comment_md,
            summary_md=summary_md, github_output=github_output,
        )
    except DatadifferError as e:
        if github_output:
            with open(github_output, "a") as f:
                f.write("has-diff=unknown\nexit-code=2\n")
        _fail(e)
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"exit-code={code}\n")
    if code:
        raise SystemExit(code)


@main.command()
def mcp() -> None:
    """Start the MCP server (stdio) so AI agents can verify data changes."""
    try:
        from datadiffer.mcp_server import main as mcp_main
    except ImportError as e:
        click.echo(
            f"error: MCP support requires the extra: pip install 'datadiffer[mcp]' ({e})",
            err=True,
        )
        raise SystemExit(2) from e
    mcp_main()


@main.command()
def init() -> None:
    """Scaffold a commented datadiffer.toml with example connections."""
    from pathlib import Path

    from datadiffer.config import write_scaffold

    path = Path("datadiffer.toml")
    try:
        write_scaffold(path)
    except DatadifferError as e:
        _fail(e)
    click.echo(f"Wrote {path} (mode 600). Edit it, then:")
    click.echo("  datadiffer connections test")
    click.echo("  datadiffer diff prod::orders local::orders")
    click.echo("To expose it to an MCP client, add this server entry:")
    click.echo('  {"command": "datadiffer", "args": ["mcp"]}')
    click.echo('  or, without installing: '
               '{"command": "uvx", "args": ["--from", "datadiffer[mcp]", "datadiffer", "mcp"]}')


@main.group()
def connections() -> None:
    """List or test configured connections."""


@connections.command("list")
def connections_list() -> None:
    """List connections from datadiffer.toml."""
    from datadiffer.config import conn_label, load_config

    try:
        cfg, path = load_config()
    except DatadifferError as e:
        _fail(e)
    conns = cfg.get("connections", {})
    if not conns:
        click.echo("No connections configured. Run `datadiffer init` and edit datadiffer.toml.")
        return
    click.echo(f"config: {path}")
    for name, c in sorted(conns.items()):
        click.echo(f"  {name}  type={c.get('type', '?')}  {conn_label(c)}")


@connections.command("test")
def connections_test() -> None:
    """Test reachability of configured connections."""
    from datadiffer.config import load_config, probe_connection

    try:
        cfg, path = load_config()
    except DatadifferError as e:
        _fail(e)
    conns = cfg.get("connections", {})
    if not conns:
        click.echo("No connections configured. Run `datadiffer init` and edit datadiffer.toml.")
        return
    failures = 0
    for name, c in sorted(conns.items()):
        ok, detail = probe_connection(c)
        mark = "ok" if ok else "FAIL"
        failures += 0 if ok else 1
        click.echo(f"  {name}  {mark}  {detail}")
    if failures:
        raise SystemExit(2)


@main.command()
def demo() -> None:
    """Generate seeded demo data and diff it (offline, no credentials).

    Exits 0 even though a diff is found — it is a demonstration, not a gate
    (operational errors still exit 2).
    """
    import duckdb

    from datadiffer import api
    from datadiffer import demo as demo_mod
    from datadiffer.report.render_text import render

    try:
        a, b = demo_mod.generate()
    except (OSError, duckdb.Error) as e:
        click.echo(
            f"error: cannot write ./{demo_mod.DEMO_DIR}/ here ({e}) — "
            "cd to a writable directory and retry.",
            err=True,
        )
        raise SystemExit(2) from e
    click.echo(f"Generated {a} and {b} ({demo_mod._N:,} orders, planted regressions).")
    click.echo(f"$ datadiffer diff {a} {b}")
    click.echo()
    try:
        report = api.diff(a, b)
    except DatadifferError as e:
        _fail(e)
    render(report)
    click.echo()
    click.echo("A real `datadiffer diff` run would exit 1 here (diff found).")
    click.echo(f"Explore further:  datadiffer diff {a} {b} --format json")


if __name__ == "__main__":
    main()
