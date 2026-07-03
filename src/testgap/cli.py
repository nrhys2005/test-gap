import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from testgap import __version__
from testgap.cli_doctor import run_doctor
from testgap.config.init_wizard import (
    analyze,
    build_config,
    ensure_gitignore_entry,
    provider_status,
    suggest_model,
    write_config,
)
from testgap.config.loader import CONFIG_FILENAME, ConfigError, load_config
from testgap.generator import LLMClient, LLMError, summarize_llm_error
from testgap.pipeline import DiffRunReport, FunctionSuggestion, run_diff
from testgap.ui import run_review_session

app = typer.Typer(
    name="testgap",
    help="AI-powered test generator that closes coverage gaps in your PRs.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()

# Loggers whose default INFO/WARN levels bury useful output at test-generation time.
# ``main()`` silences them once per process — verbose mode lifts the gag.
_NOISY_LITELLM_LOGGERS = ("LiteLLM", "litellm", "httpx", "urllib3.connectionpool")


def _setup_litellm_logging(*, verbose: bool = False) -> None:
    """Quiet LiteLLM's chatty loggers unless the user asked for verbose output.

    Called once from the ``@app.callback`` — see the plan's D4 rationale.
    """
    level = logging.DEBUG if verbose else logging.ERROR
    for name in _NOISY_LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(level)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"testgap {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    _setup_litellm_logging(verbose=False)


@app.command()
def init(
    path: Path | None = typer.Option(
        None, "--path", "-p", help="Project root to initialize.", file_okay=False
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Accept all detected defaults without prompts."
    ),
) -> None:
    """Initialize TestGap in the current project (creates .testgap.yml)."""
    root = (path or Path.cwd()).resolve()
    if not root.is_dir():
        console.print(f"[red]✗[/] {root} is not a directory")
        raise typer.Exit(code=1)

    console.print(f"[bold]Analyzing[/] {root}")
    report = analyze(root)

    if not report.pytest_signals:
        console.print("[red]✗[/] No pytest project detected.")
        console.print("  Install pytest first: [cyan]pip install pytest[/]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/] pytest detected ({escape(report.pytest_signals[0])})")

    if not report.has_git:
        console.print(
            "[yellow]![/] Not a git repository — `testgap diff` will not work until you `git init`."
        )

    existing = root / CONFIG_FILENAME
    if existing.is_file() and not yes:
        action = Prompt.ask(
            f"[yellow]{CONFIG_FILENAME} already exists.[/] Action?",
            choices=["overwrite", "backup", "cancel"],
            default="cancel",
        )
        if action == "cancel":
            console.print("Aborted.")
            raise typer.Exit(code=0)
        if action == "backup":
            backup_path = existing.with_suffix(existing.suffix + ".bak")
            backup_path.write_bytes(existing.read_bytes())
            console.print(f"  Backed up to {backup_path.name}")

    source_paths = _choose_source_paths(report, yes=yes)
    test_paths = report.test_paths or ["tests/"]
    if not report.test_paths:
        console.print(f"[yellow]![/] No tests/ directory found — defaulting to {test_paths[0]}")
    else:
        console.print(f"[green]✓[/] test directory: {test_paths[0]}")

    model = _choose_model(yes=yes)

    config = build_config(source_paths=source_paths, test_paths=test_paths, model=model)
    config_path = write_config(config, root)
    console.print(f"[green]✓[/] wrote {config_path.relative_to(root)}")

    if ensure_gitignore_entry(root):
        console.print("[green]✓[/] added .testgap/ to .gitignore")

    console.print()
    console.print("[bold]Next steps:[/]")
    console.print("  [cyan]testgap diff --review[/]   suggest tests for uncovered changes")


def _choose_source_paths(report, *, yes: bool) -> list[str]:
    if report.source_paths and not report.layout_ambiguous:
        console.print(f"[green]✓[/] source path: {report.source_paths[0]}")
        return report.source_paths

    if report.layout_ambiguous and not yes:
        console.print("[yellow]?[/] multiple source candidates found:")
        for i, p in enumerate(report.source_paths, 1):
            console.print(f"   [{i}] {p}")
        choice = Prompt.ask(
            "  pick one",
            choices=[str(i) for i in range(1, len(report.source_paths) + 1)],
            default="1",
        )
        return [report.source_paths[int(choice) - 1]]

    if not report.source_paths:
        if yes:
            console.print("[yellow]![/] no source layout detected — defaulting to src/")
            return ["src/"]
        custom = Prompt.ask(
            "[yellow]?[/] no source layout detected. Source path?", default="src/"
        )
        return [custom]

    return report.source_paths


@app.command()
def diff(
    base: str | None = typer.Option(
        None, "--base", "-b", help="Base git ref. Defaults to origin/HEAD then main/master."
    ),
    head: str = typer.Option("HEAD", "--head", help="Head ref. Defaults to HEAD."),
    max_functions: int | None = typer.Option(
        None, "--max-functions", "-n", help="Limit number of functions processed."
    ),
    path: Path | None = typer.Option(None, "--path", "-p", file_okay=False),
    review: bool = typer.Option(
        False,
        "--review",
        help=(
            "Interactively review generated tests and apply them to disk. "
            "Exits 0 even on quit (only exceptions return 1)."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show full tracebacks and LiteLLM logs on error.",
    ),
) -> None:
    """Analyze the diff and propose tests for uncovered changes.

    Without ``--review``: non-interactive batch report (exit 1 if any
    suggestion has no accepted cases).
    With ``--review``: per-function 5-choice prompt (apply/skip/regenerate/
    edit/quit). Exit 0 even on quit; only exceptions surface as exit 1.
    """
    if verbose:
        _setup_litellm_logging(verbose=True)

    root = (path or Path.cwd()).resolve()

    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]✗[/] {escape(str(e))}")
        raise typer.Exit(code=1) from e

    llm_client = LLMClient(
        model=config.llm.model,
        max_retries=config.llm.max_retries,
        verbose=verbose,
    )

    if review:
        if not sys.stdin.isatty():
            console.print(
                "[red]error:[/] --review requires a TTY (stdin must be interactive)"
            )
            raise typer.Exit(code=1)
        console.print(f"[bold]Reviewing diff[/] in {root}")
        try:
            run_review_session(
                project_root=root,
                config=config,
                llm_client=llm_client,
                base_ref=base,
                head_ref=head,
                max_functions=max_functions,
                console=console,
            )
        except LLMError as e:
            console.print(
                f"[red]✗ LLM error:[/] {escape(summarize_llm_error(e))}"
            )
            if verbose:
                console.print_exception()
            raise typer.Exit(code=1) from e
        except Exception as e:
            console.print(f"[red]✗[/] {escape(str(e))}")
            if verbose:
                console.print_exception()
            raise typer.Exit(code=1) from e
        return

    console.print(f"[bold]Analyzing diff[/] in {root}")

    try:
        report = run_diff(
            project_root=root,
            config=config,
            llm_client=llm_client,
            base_ref=base,
            head_ref=head,
            max_functions=max_functions,
        )
    except LLMError as e:
        console.print(f"[red]✗ LLM error:[/] {escape(summarize_llm_error(e))}")
        if verbose:
            console.print_exception()
        raise typer.Exit(code=1) from e
    except Exception as e:  # surface user-facing errors from coverage/git layers
        console.print(f"[red]✗[/] {escape(str(e))}")
        if verbose:
            console.print_exception()
        raise typer.Exit(code=1) from e

    _print_diff_report(report)

    if report.suggestions and not all(s.succeeded for s in report.suggestions):
        raise typer.Exit(code=1)


# Register ``doctor`` from the dedicated module. ``run_doctor`` accepts the
# typer options directly so we bind it as-is.
app.command(name="doctor", help="Diagnose the local TestGap environment.")(run_doctor)


def _print_diff_report(report: DiffRunReport) -> None:
    console.print(f"[dim]base[/] {report.base_ref} → [dim]head[/] {report.head_ref}")

    if report.skipped_reason:
        console.print(f"[green]✓[/] {report.skipped_reason}")
        return

    summary = (
        f"changed lines: {report.changed_total}   "
        f"covered: {report.covered_total}   "
        f"diff coverage: {report.diff_coverage_pct}%"
    )
    console.print(summary)
    console.print()

    for i, suggestion in enumerate(report.suggestions, 1):
        _print_suggestion(i, len(report.suggestions), suggestion)

    console.print()
    console.print(f"[dim]LLM cost this run:[/] ${report.cost_total:.4f}")
    if report.provider_unhealthy:
        reason = report.unhealthy_reason or "consecutive LLM failures"
        console.print(
            f"[yellow]![/] provider unhealthy — skipped remaining functions "
            f"({escape(reason)}). Try: testgap doctor"
        )


def _print_suggestion(idx: int, total: int, s: FunctionSuggestion) -> None:
    file_label = escape(f"{s.function.file.name}::{s.function.qualname}")
    header = f"[{idx}/{total}] {file_label}"
    console.print(f"[bold]{header}[/]")
    lines_str = ", ".join(str(n) for n in s.function.uncovered_lines[:8])
    if len(s.function.uncovered_lines) > 8:
        lines_str += ", …"
    console.print(f"  uncovered lines: {lines_str}")

    if s.error:
        console.print(f"  [red]✗[/] {escape(s.error)}")
        return

    if s.validator_result is None or s.generated is None:
        console.print("  [yellow]![/] no result captured")
        return

    if s.validator_result.environment_error:
        console.print(f"  [red]✗[/] {escape(s.validator_result.environment_error)}")
        return

    accepted_n = len(s.accepted_cases)
    discarded_n = len(s.discarded_cases)
    total_n = accepted_n + discarded_n
    cost_label = f"${s.cost_usd:.4f}" if s.cost_usd > 0 else "$0 (cost unknown)"
    retried_marker = " [retried]" if s.attempts == 2 else ""

    if s.fully_passed:
        console.print(
            f"  [green]✓[/] {accepted_n}/{total_n} tests passed   {cost_label}{retried_marker}"
        )
    elif s.succeeded:
        console.print(
            f"  [yellow]![/] {accepted_n} kept / {discarded_n} discarded   "
            f"{cost_label}{retried_marker}"
        )
    else:
        console.print(
            f"  [yellow]![/] {accepted_n} pass / {discarded_n} fail of {total_n}   "
            f"{cost_label}{retried_marker}"
        )

    if s.retry_skipped_reason:
        console.print(f"  [yellow]![/] retry skipped: {escape(s.retry_skipped_reason)}")

    for case in s.discarded_cases[:3]:
        console.print(f"    [red]·[/] {escape(case.name)}")


def _choose_model(*, yes: bool) -> str:
    suggested = suggest_model()
    if yes:
        return suggested

    table = Table(show_header=True, header_style="bold", title="Available LLM providers")
    table.add_column("model")
    table.add_column("status")
    rows = provider_status()
    options: list[str] = []
    for model, status in rows:
        marker = "→" if model == suggested else " "
        table.add_row(f"{marker} {model}", status)
        options.append(model)
    console.print(table)

    use_default = Confirm.ask(f"Use suggested model [cyan]{suggested}[/]?", default=True)
    if use_default:
        return suggested
    choice = Prompt.ask("Enter model id", default=suggested)
    return choice or suggested
