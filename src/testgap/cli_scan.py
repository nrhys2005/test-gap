"""``testgap scan`` — LLM-free per-file coverage report.

Runs :func:`testgap.scan.scan_project` and renders either a rich table (default)
or JSON (``--json``) to stdout. No LLM calls are made; ``testgap doctor`` /
``testgap init`` remain the only bootstrap prerequisites.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from testgap.config.loader import ConfigError, load_config
from testgap.scan import (
    FileCoverage,
    ScanReport,
    report_to_dict,
    scan_project,
    sort_files,
)

__all__ = ["run_scan"]


_ALLOWED_SORT = {"coverage", "missing", "impact"}


def run_scan(
    path: Path | None = typer.Option(
        None, "--path", "-p", file_okay=False, help="Project root (defaults to cwd)."
    ),
    below: float | None = typer.Option(
        None,
        "--below",
        help="Show only files with coverage below this percent.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON to stdout."
    ),
    sort_by: str = typer.Option(
        "coverage",
        "--sort-by",
        help="Sort key: coverage | missing | impact.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show full tracebacks on error."
    ),
) -> None:
    """Report per-file coverage without invoking any LLM."""
    console = Console()
    if sort_by not in _ALLOWED_SORT:
        console.print(
            f"[red]error:[/] --sort-by must be one of "
            f"{sorted(_ALLOWED_SORT)}, got {sort_by!r}"
        )
        raise typer.Exit(code=1)

    root = (path or Path.cwd()).resolve()

    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]:cross_mark:[/] {escape(str(e))}")
        if verbose:
            console.print_exception()
        raise typer.Exit(code=1) from e

    try:
        report = scan_project(root, config, below_pct=below)
    except Exception as e:  # noqa: BLE001 — surface coverage/subprocess errors
        console.print(f"[red]:cross_mark:[/] {escape(str(e))}")
        if verbose:
            console.print_exception()
        raise typer.Exit(code=1) from e

    if json_out:
        # Whitelist serialization — ``report_to_dict`` excludes ``_underlying``
        # and raw source text so JSON stdout never leaks prompt context.
        console.print_json(json.dumps(report_to_dict(report)))
        return

    _render_scan_table(report, console, sort_by=sort_by)


def _render_scan_table(
    report: ScanReport, console: Console, *, sort_by: str
) -> None:
    files = sort_files(report.files, sort_by=sort_by)

    console.print(
        f"[bold]testgap scan[/] — {report.overall_coverage_pct}% overall "
        f"({report.covered_lines}/{report.total_lines} lines) "
        f"[dim]sorted by {sort_by}[/]"
    )

    if not files:
        console.print("[green]:heavy_check_mark:[/] no files to display")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("coverage", justify="right")
    table.add_column("uncovered fns", justify="right")
    table.add_column("top hotspots")

    for fc in files:
        table.add_row(
            escape(fc.rel_path),
            _fmt_pct(fc.coverage_pct),
            str(len(fc.uncovered_functions)),
            _fmt_hotspots(fc),
        )
    console.print(table)


def _fmt_pct(pct: float) -> str:
    if pct >= 80:
        return f"[green]{pct}%[/]"
    if pct >= 50:
        return f"[yellow]{pct}%[/]"
    return f"[red]{pct}%[/]"


def _fmt_hotspots(fc: FileCoverage, *, top: int = 3) -> str:
    if not fc.uncovered_functions:
        return "-"
    # Show top-N by descending impact (matches --sort-by impact intuition).
    ranked = sorted(
        fc.uncovered_functions,
        key=lambda f: (
            -(len(f.uncovered_lines)),
            f.start_line,
            f.qualname,
        ),
    )
    parts = [
        f"{escape(f.qualname)} ({len(f.uncovered_lines)}L)" for f in ranked[:top]
    ]
    if len(ranked) > top:
        parts.append(f"+{len(ranked) - top} more")
    return ", ".join(parts)
