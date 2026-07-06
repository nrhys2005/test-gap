"""``testgap backfill`` — CLI dispatcher for :func:`testgap.backfill.run_backfill`.

Responsibilities:

* Parse CLI options and translate them to ``run_backfill`` kwargs.
* Load ``.testgap.yml`` (exit 1 on missing / invalid).
* Guard interactive mode against non-TTY stdin (exit 1).
* Open a session log via the shared ``open_session_log`` factory.
* Render the ``BackfillOutcome`` summary (``≈`` prefix on estimated coverage).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from testgap.backfill import BackfillOutcome, run_backfill
from testgap.config.loader import ConfigError, load_config
from testgap.generator import LLMClient, LLMError, summarize_llm_error
from testgap.session_logging import open_session_log

__all__ = ["run_backfill_cli"]


_ALLOWED_PRIORITY = {"impact", "coverage", "size"}


def run_backfill_cli(
    path: Path | None = typer.Option(
        None, "--path", "-p", file_okay=False, help="Project root (defaults to cwd)."
    ),
    target_coverage: float | None = typer.Option(
        None,
        "--target-coverage",
        help="Stop once estimated overall coverage reaches this percent.",
    ),
    max_functions: int | None = typer.Option(
        None,
        "--max-functions",
        "-n",
        help="Cap the number of functions processed.",
    ),
    below: float | None = typer.Option(
        None,
        "--below",
        help="Only backfill files with coverage below this percent.",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help=(
            "Non-interactive: apply when tests pass, skip when discarded. "
            "Interactive mode remains the default (5-choice prompt per function)."
        ),
    ),
    priority: str = typer.Option(
        "impact",
        "--priority",
        help="Worklist priority: impact | coverage | size.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Do everything except writing test files.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show full tracebacks and LiteLLM logs on error.",
    ),
    session_log: bool = typer.Option(
        True,
        "--session-log/--no-session-log",
        help=(
            "Record per-function LLM/pytest/backfill events under "
            ".testgap/logs/ (JSONL). Use --no-session-log to opt out."
        ),
    ),
) -> None:
    """Iterate through uncovered functions and backfill tests via LLM."""
    console = Console()
    if priority not in _ALLOWED_PRIORITY:
        console.print(
            f"[red]error:[/] --priority must be one of "
            f"{sorted(_ALLOWED_PRIORITY)}, got {priority!r}"
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

    if not auto and not sys.stdin.isatty():
        console.print(
            "[red]error:[/] interactive backfill requires a TTY. "
            "Re-run with --auto or from a real terminal."
        )
        raise typer.Exit(code=1)

    llm_client = LLMClient(
        model=config.llm.model,
        max_retries=config.llm.max_retries,
        verbose=verbose,
    )

    log = open_session_log(root, config, enabled=session_log)
    if log.path is not None:
        try:
            rel = log.path.relative_to(root)
        except ValueError:
            rel = log.path
        console.print(f"[dim]session log: {rel}[/]")

    console.print(
        f"[bold]Backfilling[/] {root} "
        f"[dim](priority={priority}, auto={auto}, dry_run={dry_run})[/]"
    )

    try:
        with log:
            outcome = run_backfill(
                project_root=root,
                config=config,
                llm_client=llm_client,
                target_coverage=target_coverage,
                max_functions=max_functions,
                below_pct=below,
                auto=auto,
                priority=priority,  # type: ignore[arg-type]
                dry_run=dry_run,
                console=console,
                session_log=log,
            )
            if outcome.quit_reason or outcome.provider_unhealthy:
                log.close(
                    quit_reason=outcome.quit_reason
                    or (
                        "provider_unhealthy"
                        if outcome.provider_unhealthy
                        else None
                    )
                )
    except LLMError as e:
        console.print(
            f"[red]:cross_mark: LLM error:[/] {escape(summarize_llm_error(e))}"
        )
        if verbose:
            console.print_exception()
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001 — surface coverage/git-layer errors
        console.print(f"[red]:cross_mark:[/] {escape(str(e))}")
        if verbose:
            console.print_exception()
        raise typer.Exit(code=1) from e

    _render_backfill_summary(outcome, console, project_root=root)


def _render_backfill_summary(
    outcome: BackfillOutcome,
    console: Console,
    project_root: Path | None = None,
) -> None:
    """Render the end-of-run summary block.

    Enforces the ``≈`` prefix on ``coverage_after`` when the heuristic is used
    (see BackfillOutcome.coverage_after_is_estimated). A future
    ``--verify-coverage`` flag will unset that flag after re-measuring.

    PR #12 review (gemini MED): applied file paths are now rendered relative
    to ``project_root`` when given. This shortens the CLI output on real
    projects and avoids leaking absolute directory structures in shared
    terminals.
    """
    console.print()
    console.print("[bold]Backfill summary[/]")

    after_label = _format_coverage_after(outcome)
    console.print(
        f"  coverage: {outcome.coverage_before}% → {after_label}"
    )
    console.print(
        f"  processed: {outcome.functions_processed}   "
        f"accepted: {outcome.functions_accepted}   "
        f"skipped: {outcome.functions_skipped}   "
        f"failed: {outcome.functions_failed}"
    )
    console.print(f"  [dim]cost: ${outcome.cost_total:.4f}   "
                  f"elapsed: {outcome.elapsed_seconds}s[/]")

    if outcome.applied:
        table = Table(
            show_header=True,
            header_style="bold",
            title="applied test files",
        )
        table.add_column("qualname")
        table.add_column("path")
        table.add_column("tests", justify="right")
        for af in outcome.applied:
            path_str = str(af.path)
            if project_root is not None:
                try:
                    path_str = str(af.path.relative_to(project_root))
                except ValueError:
                    # Path is outside project_root — leave absolute.
                    pass
            table.add_row(
                escape(af.function_qualname),
                escape(path_str),
                str(af.test_count),
            )
        console.print(table)

    if outcome.discarded_qualnames:
        console.print()
        console.print("[yellow]![/] discarded (no accepted cases):")
        for name in outcome.discarded_qualnames[:10]:
            console.print(f"    · {escape(name)}")
        remaining = len(outcome.discarded_qualnames) - 10
        if remaining > 0:
            console.print(f"    (+{remaining} more)")

    if outcome.quit_reason:
        console.print(f"[dim]stopped: {escape(outcome.quit_reason)}[/]")
    if outcome.provider_unhealthy:
        reason = outcome.unhealthy_reason or "consecutive LLM failures"
        console.print(
            f"[yellow]![/] provider unhealthy ({escape(reason)}). "
            "Try: testgap doctor"
        )


def _format_coverage_after(outcome: BackfillOutcome) -> str:
    prefix = "≈" if outcome.coverage_after_is_estimated else ""
    label = f"{prefix}{outcome.coverage_after}%"
    if outcome.dry_run:
        label += " [dim](dry-run)[/]"
    return label


