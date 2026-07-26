"""Terminal renderer (rich). Degrades to plain text on non-TTY; honors NO_COLOR.

User-controlled strings (paths, column names, values) are always escaped or
printed with markup disabled — bracketed text must render literally.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from datadiffer.report.model import DiffReport


def render(report: DiffReport, console: Console | None = None) -> None:
    c = console or Console()
    r = report.rows

    c.print(
        f"[bold]{escape(report.tables['a'])}[/bold] vs [bold]{escape(report.tables['b'])}[/bold]"
    )
    pk = report.primary_key
    rule_names = {
        "declared": "declared primary key",
        "exact_id": "column named 'id'",
        "table_id": "<table>_id convention",
        "suffix": "*_id naming",
    }
    key_note = (
        f" (inferred from {rule_names.get(pk.rule or '', pk.rule)})" if pk.inferred else ""
    )
    c.print(f"key: {', '.join(pk.columns)}{key_note}", markup=False)
    c.print(
        f"rows: [green]+{r.added} added[/green]  [red]-{r.removed} removed[/red]  "
        f"[yellow]~{r.modified} modified[/yellow]  {r.unchanged} unchanged"
    )
    for w in report.execution.warnings:
        c.print(f"[yellow]warning:[/yellow] {escape(w)}")

    changed_cols = [col for col in report.columns if col.changed_rows]
    if changed_cols:
        t = Table(title=None, pad_edge=False)
        t.add_column("column")
        t.add_column("changed rows", justify="right")
        t.add_column("% of matched", justify="right")
        for col in sorted(changed_cols, key=lambda x: -x.changed_rows)[:15]:
            rate = f"{col.change_rate:.2%}" if col.change_rate is not None else "-"
            name = col.name + (" (json)" if col.compared_as == "json_text" else "")
            t.add_row(escape(name), f"{col.changed_rows:,}", rate)
        c.print(t)

    sd = report.schema_diff
    if not sd.identical:
        parts = []
        if sd.columns_added:
            parts.append(f"+{len(sd.columns_added)} column(s) added")
        if sd.columns_removed:
            parts.append(f"-{len(sd.columns_removed)} removed")
        if sd.columns_type_changed:
            parts.append(f"{len(sd.columns_type_changed)} retyped")
        if parts:
            c.print(f"schema: {', '.join(parts)}", markup=False)
    for skipped in sd.columns_skipped:
        c.print(f"[dim]skipped {escape(skipped['column'])}: {escape(skipped['reason'])}[/dim]")

    _render_attribution(c, report)

    mods = report.samples.get("modified", [])
    if mods:
        c.print("sample changes:")
        for m in mods[:5]:
            key = ", ".join(f"{k}={v}" for k, v in m["key"].items())
            chg = "  ".join(f"{col}: {v['a']} -> {v['b']}" for col, v in m["changes"].items())
            c.print(f"  [{key}]  {chg}", markup=False)

    if report.has_diff:
        c.print("[bold red]DIFF FOUND[/bold red]")
    else:
        c.print("[bold green]NO DIFFERENCES[/bold green]")


def _render_attribution(c: Console, report: DiffReport) -> None:
    attr = report.attribution
    if attr is None:
        return
    # Headline: the highest-scoring segment, preferring the most tellable status.
    lines: list[tuple[str, object]] = []
    for status in ("modified", "added", "removed"):
        sa = attr.by_status.get(status)
        if sa and sa.segments:
            lines.append((status, sa.segments[0]))
    if not lines:
        return
    headline_first = True
    for status, seg in lines:
        val = "NULL" if seg.value is None else f"'{seg.value}'"
        text = (
            f"{seg.support:.1%} of {status} rows have {seg.column} = {val} "
            f"({seg.lift:g}x over-represented)"
        )
        if headline_first:
            c.print(f"[bold cyan]-> {escape(text)}[/bold cyan]")
            headline_first = False
        else:
            c.print(f"   {text}", markup=False)
