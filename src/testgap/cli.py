from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from testgap import __version__
from testgap.config.init_wizard import (
    analyze,
    build_config,
    ensure_gitignore_entry,
    provider_status,
    suggest_model,
    write_config,
)
from testgap.config.loader import CONFIG_FILENAME, ConfigError, load_config
from testgap.generator import LLMClient
from testgap.pipeline import DiffRunReport, FunctionSuggestion, run_diff

app = typer.Typer(
    name="testgap",
    help="AI-powered test generator that closes coverage gaps in your PRs.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


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
    pass


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
) -> None:
    """Analyze the diff and propose tests for uncovered changes (non-interactive)."""
    root = (path or Path.cwd()).resolve()

    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]✗[/] {escape(str(e))}")
        raise typer.Exit(code=1) from e

    console.print(f"[bold]Analyzing diff[/] in {root}")

    llm_client = LLMClient(model=config.llm.model, max_retries=config.llm.max_retries)

    try:
        report = run_diff(
            project_root=root,
            config=config,
            llm_client=llm_client,
            base_ref=base,
            head_ref=head,
            max_functions=max_functions,
        )
    except Exception as e:  # surface user-facing errors from coverage/git layers
        console.print(f"[red]✗[/] {escape(str(e))}")
        raise typer.Exit(code=1) from e

    _print_diff_report(report)

    if report.suggestions and not all(s.succeeded for s in report.suggestions):
        raise typer.Exit(code=1)


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

    passed_n = len(s.validator_result.passed)
    failed_n = len(s.validator_result.failed)
    total_n = len(s.validator_result.cases)
    cost_label = f"${s.cost_usd:.4f}" if s.cost_usd > 0 else "$0 (cost unknown)"

    if s.succeeded:
        console.print(
            f"  [green]✓[/] {passed_n}/{total_n} tests passed   {cost_label}"
        )
    else:
        console.print(
            f"  [yellow]![/] {passed_n} pass / {failed_n} fail of {total_n}   {cost_label}"
        )

    for case in s.validator_result.failed[:3]:
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
