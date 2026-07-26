"""Markdown renderers: sticky PR comment + job summary.

The comment is the product's most-viewed artifact. Hard budget 60,000 chars
(GitHub caps issue comments at 65,536); truncation tiers: drop samples first,
then cap the column table harder, never the attribution callout. Full fidelity
always lives in the job summary (1 MiB budget) and the JSON artifact.
"""

from __future__ import annotations

import datetime as _dt

from datadiffer import __version__
from datadiffer.report.model import DiffReport

COMMENT_BUDGET = 60_000
SUMMARY_BUDGET = 900_000


def render_comment(
    report: DiffReport,
    header: str = "default",
    reproduce: str | None = None,
    policy_note: str | None = None,
) -> str:
    for columns_cap, samples_cap in ((15, 5), (15, 0), (5, 0)):
        text = _render(report, header, reproduce, policy_note,
                       columns_cap, samples_cap, marker=True)
        if len(text) <= COMMENT_BUDGET:
            return text
    return text[:COMMENT_BUDGET]  # pathological column names; hard floor


def render_summary(
    report: DiffReport,
    reproduce: str | None = None,
    policy_note: str | None = None,
) -> str:
    text = _render(report, "", reproduce, policy_note,
                   columns_cap=200, samples_cap=20, marker=False)
    return text if len(text) <= SUMMARY_BUDGET else text[:SUMMARY_BUDGET]


def _render(report, header, reproduce, policy_note, columns_cap, samples_cap, marker):
    r = report.rows
    changed = r.added + r.removed + r.modified
    pct = (changed / r.a * 100) if r.a else (100.0 if changed else 0.0)
    verdict = "differences found ❌" if report.has_diff else "no differences ✅"

    out: list[str] = []
    if marker:
        out.append(f"<!-- datadiffer-report:{header} -->")
    out.append(
        f"### datadiffer: `{report.tables['a']}` vs `{report.tables['b']}` — {verdict}"
    )
    out.append("")
    out.append(
        f"**+{r.added:,} added · −{r.removed:,} removed · ~{r.modified:,} modified · "
        f"{r.unchanged:,} unchanged** ({pct:.2f}% of base rows affected)"
    )
    if policy_note:
        out.append(f"Policy: {policy_note}")
    for w in report.execution.warnings:
        out.append(f"⚠️ `{w}`")

    changed_cols = sorted(
        (c for c in report.columns if c.changed_rows), key=lambda c: -c.changed_rows
    )
    if changed_cols:
        out.append("")
        out.append("| Column | Changed rows | % of matched |")
        out.append("|---|---:|---:|")
        for c in changed_cols[:columns_cap]:
            rate = f"{c.change_rate:.2%}" if c.change_rate is not None else "—"
            suffix = " (json)" if c.compared_as == "json_text" else ""
            out.append(f"| `{c.name}`{suffix} | {c.changed_rows:,} | {rate} |")
        if len(changed_cols) > columns_cap:
            out.append(f"| …{len(changed_cols) - columns_cap} more | | |")

    sd = report.schema_diff
    if not sd.identical:
        bits = []
        if sd.columns_added:
            bits.append(f"+{len(sd.columns_added)} added")
        if sd.columns_removed:
            bits.append(f"−{len(sd.columns_removed)} removed")
        if sd.columns_type_changed:
            bits.append(f"{len(sd.columns_type_changed)} retyped")
        if sd.columns_skipped:
            bits.append(f"{len(sd.columns_skipped)} not compared")
        out.append("")
        out.append(f"**Schema:** {', '.join(bits)}")

    attr_lines = _attribution_lines(report)
    if attr_lines:
        out.append("")
        for i, line in enumerate(attr_lines):
            prefix = "> **Where it's concentrated:** " if i == 0 else "> "
            out.append(prefix + line)

    if samples_cap and report.samples.get("modified"):
        out.append("")
        out.append("<details><summary>Sample changes</summary>")
        out.append("")
        for m in report.samples["modified"][:samples_cap]:
            key = ", ".join(f"{k}={v}" for k, v in m["key"].items())
            chg = " · ".join(
                f"`{col}`: {v['a']} → {v['b']}" for col, v in m["changes"].items()
            )
            out.append(f"- [{key}] {chg}")
        out.append("")
        out.append("</details>")

    out.append("")
    out.append("---")
    footer = [f"datadiffer v{__version__}"]
    if reproduce:
        footer.insert(0, f"Reproduce locally: `{reproduce}`")
    footer.append(
        _dt.datetime.now(_dt.timezone.utc).strftime("compared at %Y-%m-%d %H:%M UTC")
    )
    out.append(f"<sub>{' · '.join(footer)}</sub>")
    return "\n".join(out)


def _attribution_lines(report) -> list[str]:
    attr = report.attribution
    if attr is None:
        return []
    lines = []
    for status in ("modified", "added", "removed"):
        sa = attr.by_status.get(status)
        if sa and sa.segments:
            s = sa.segments[0]
            val = "NULL" if s.value is None else f"'{s.value}'"
            lines.append(
                f"{s.support:.1%} of {status} rows have `{s.column} = {val}` "
                f"({s.lift:g}× over-represented)"
            )
    return lines
