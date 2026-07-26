"""`datadiffer ci` — the GitHub Action entry point.

Runs one single-table diff, writes report.json / comment.md / summary.md /
GITHUB_OUTPUT entries, and returns the policy exit code (0 within policy,
1 over policy, 2 operational). The Action's shell layer never parses our
output — everything it needs lands in files and step outputs.

Interpolation contract: a CLOSED set of variables — ${PR_NUMBER}, ${BRANCH},
${SHA_SHORT} — is substituted into table refs and schema-map from the
environment; ${OTHER_VARS} in --source/--target resolve from the environment
too (secrets ride env, never argv).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from datadiffer import api
from datadiffer.errors import DatadifferError

_REF_VARS = ("PR_NUMBER", "BRANCH", "SHA_SHORT")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def run_ci(
    *,
    table_a: str,
    table_b: str,
    source: str | None = None,
    target: str | None = None,
    schema_map: str | None = None,
    keys: list[str] | None = None,
    where: str | None = None,
    exclude_columns: list[str] | None = None,
    fail_on: str = "threshold",
    max_changed_rows_pct: float | None = None,
    fail_on_schema_change: bool = False,
    sample_rows: int = 5,
    header: str = "default",
    report_json: str | None = None,
    comment_md: str | None = None,
    summary_md: str | None = None,
    github_output: str | None = None,
) -> int:
    table_b = _interp_ref(table_b)
    table_a = _interp_ref(table_a)
    if schema_map:
        table_b = _apply_schema_map(table_b, _interp_ref(schema_map))
    source = _interp_env(source)
    target = _interp_env(target)

    report = api.diff(
        table_a, table_b,
        source=source, target=target,
        keys=keys, exclude_columns=exclude_columns, where=where,
        sample_limit=sample_rows,
    )

    changed = report.rows.added + report.rows.removed + report.rows.modified
    pct = (changed / report.rows.a * 100) if report.rows.a else (100.0 if changed else 0.0)

    failures: list[str] = []
    if fail_on == "any-diff" and report.has_diff:
        failures.append("differences found (fail-on: any-diff)")
    if fail_on == "threshold" and max_changed_rows_pct is not None and pct > max_changed_rows_pct:
        failures.append(
            f"max-changed-rows-pct exceeded: {pct:.2f}% > {max_changed_rows_pct:g}%"
        )
    if fail_on_schema_change and not report.schema_diff.identical:
        failures.append("schema changed (fail-on-schema-change)")
    exit_code = 1 if failures else 0
    policy_note = "; ".join(failures) if failures else _policy_ok_note(
        fail_on, max_changed_rows_pct
    )

    reproduce = _reproduce_command(table_a, table_b, source, target, keys, exclude_columns, where)

    if report_json:
        Path(report_json).write_text(report.to_json())
    if comment_md or summary_md:
        from datadiffer.report.render_markdown import render_comment, render_summary

        if comment_md:
            Path(comment_md).write_text(
                render_comment(report, header=header, reproduce=reproduce,
                               policy_note=policy_note)
            )
        if summary_md:
            Path(summary_md).write_text(
                render_summary(report, reproduce=reproduce, policy_note=policy_note)
            )
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"has-diff={'true' if report.has_diff else 'false'}\n")
            f.write(f"rows-added={report.rows.added}\n")
            f.write(f"rows-removed={report.rows.removed}\n")
            f.write(f"rows-modified={report.rows.modified}\n")
            f.write(f"changed-rows-pct={pct:.4f}\n")
            if report_json:
                f.write(f"report-path={report_json}\n")
    return exit_code


def _policy_ok_note(fail_on: str, threshold: float | None) -> str | None:
    if fail_on == "threshold" and threshold is not None:
        return f"within max-changed-rows-pct: {threshold:g}% ✅"
    if fail_on == "never":
        return "report-only (fail-on: never)"
    return None


def _interp_ref(value: str) -> str:
    """Closed-set interpolation for table refs / schema-map (${PR_NUMBER}...)."""
    def sub(m):
        name = m.group(1)
        if name not in _REF_VARS:
            raise DatadifferError(
                f"Unknown variable ${{{name}}} in table reference — "
                f"allowed: {', '.join('${' + v + '}' for v in _REF_VARS)}"
            )
        if name not in os.environ:
            raise DatadifferError(
                f"${{{name}}} referenced but not set in the environment "
                "(the Action exports it on pull_request events)"
            )
        return os.environ[name]
    return _ENV_REF.sub(sub, value)


def _interp_env(value: str | None) -> str | None:
    """Open interpolation for connection URIs — secrets ride env, not argv."""
    if value is None:
        return None
    missing: list[str] = []

    def sub(m):
        name = m.group(1)
        if name not in os.environ:
            missing.append(name)
            return ""
        return os.environ[name]

    out = _ENV_REF.sub(sub, value)
    if missing:
        raise DatadifferError(
            f"--source/--target references unset environment variable(s): "
            f"{', '.join('${' + v + '}' for v in missing)}"
        )
    return out


def _apply_schema_map(table_ref: str, schema_map: str) -> str:
    """`PROD_SCHEMA=DEV_SCHEMA[,...]` rewrites the schema part of table_b."""
    rules = {}
    for pair in schema_map.split(","):
        left, sep, right = pair.strip().partition("=")
        if not sep or not left or not right:
            raise DatadifferError(
                f"Bad schema-map entry {pair.strip()!r} — use OLD_SCHEMA=NEW_SCHEMA"
            )
        rules[left.lower()] = right
    # Split off any locator prefix first: conn::<ref> or file.duckdb:<ref>
    # (the file path itself contains dots).
    prefix = ""
    ref = table_ref
    if "::" in table_ref:
        head, _, ref = table_ref.partition("::")
        prefix = head + "::"
    elif ".duckdb:" in table_ref.lower():
        head, _, ref = table_ref.rpartition(":")
        prefix = head + ":"
    parts = ref.split(".")
    if len(parts) >= 2 and parts[-2].lower() in rules:
        parts[-2] = rules[parts[-2].lower()]
    return prefix + ".".join(parts)


def _reproduce_command(table_a, table_b, source, target, keys, exclude, where) -> str:
    bits = ["uvx datadiffer diff", table_a, table_b]
    if source:
        bits.append('--source "$SOURCE_URL"')
    if target:
        bits.append('--target "$TARGET_URL"')
    if keys:
        bits.append(f"--key {','.join(keys)}")
    if exclude:
        bits.append(f"--exclude-columns {','.join(exclude)}")
    if where:
        bits.append(f'--where "{where}"')
    return " ".join(bits)
