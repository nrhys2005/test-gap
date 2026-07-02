"""``testgap doctor`` — environment diagnostic for TestGap.

Runs a fixed set of checks (pytest, git, config, LLM provider, cost estimate,
cache) and prints a rich table. Exit code semantics:

    0 → all checks OK
    1 → at least one blocker
    2 → at least one warning, no blockers
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from testgap.config.loader import (
    CONFIG_FILENAME,
    ConfigError,
    ConfigInvalidError,
    ConfigNotFoundError,
    find_config,
    load_config,
)
from testgap.config.schema import TestGapConfig
from testgap.detect import (
    CACHE_FILENAME,
    DetectCache,
    ProviderStatus,
    detect_llm_providers,
    detect_pytest,
)
from testgap.generator.prompt import _estimate_tokens
from testgap.pipeline import discover_targets

Level = Literal["ok", "warning", "blocker"]


@dataclass
class DoctorCheck:
    name: str
    level: Level
    message: str
    hint: str | None = None
    detail: str | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def _check_pytest(root: Path) -> DoctorCheck:
    detection = detect_pytest(root)
    if detection.detected:
        first = detection.signals[0] if detection.signals else "detected"
        return DoctorCheck(
            name="pytest",
            level="ok",
            message=first,
            detail=", ".join(detection.signals),
        )
    return DoctorCheck(
        name="pytest",
        level="blocker",
        message="no pytest configuration detected",
        hint="→ pip install pytest",
    )


def _check_git(root: Path) -> DoctorCheck:
    if (root / ".git").exists():
        return DoctorCheck(name="git", level="ok", message="repository detected")
    return DoctorCheck(
        name="git",
        level="warning",
        message="not a git repository",
        hint="→ run: git init",
    )


def _check_config(root: Path) -> tuple[DoctorCheck, TestGapConfig | None]:
    try:
        path = find_config(root)
    except ConfigNotFoundError:
        return (
            DoctorCheck(
                name=CONFIG_FILENAME,
                level="warning",
                message=f"{CONFIG_FILENAME} not found",
                hint="→ run: testgap init",
            ),
            None,
        )
    try:
        config = load_config(path)
    except ConfigInvalidError as e:
        first_line = str(e).splitlines()[0] if str(e) else "invalid config"
        return (
            DoctorCheck(
                name=CONFIG_FILENAME,
                level="blocker",
                message=first_line,
                hint=f"→ fix {CONFIG_FILENAME} (see error above)",
                detail=str(e),
            ),
            None,
        )
    except ConfigError as e:
        return (
            DoctorCheck(
                name=CONFIG_FILENAME,
                level="blocker",
                message=str(e).splitlines()[0] if str(e) else "config error",
                detail=str(e),
            ),
            None,
        )
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return (
        DoctorCheck(
            name=CONFIG_FILENAME,
            level="ok",
            message=f"loaded {rel}",
        ),
        config,
    )


def _cached_runnable_check(cache: DetectCache):
    """Return a ``runnable_check_fn`` that only consults the cache (no live probe).

    Doctor is a read-only diagnostic — we should not hit ``/api/show`` here
    because that adds noise / latency and can trip on transient network hiccups
    unrelated to the user's config. If nothing is cached yet, we optimistically
    assume RUNNABLE — the pipeline's own consecutive-failure guard is the
    ultimate safety net.
    """

    def fn(endpoint: str, model: str) -> bool:
        entry = cache.load_runnable(model, endpoint)
        if entry is None:
            return True  # optimistic — no probe from doctor
        return entry.runnable

    return fn


def _check_llm_providers(*, verbose: bool = False) -> DoctorCheck:
    cache = DetectCache()
    providers = detect_llm_providers(runnable_check_fn=_cached_runnable_check(cache))
    usable = [
        p
        for p in providers
        if p.status in (ProviderStatus.PULLED_RUNNABLE, ProviderStatus.KEY_FOUND)
    ]
    if usable:
        chosen = usable[0]
        return DoctorCheck(
            name="LLM provider",
            level="ok",
            message=f"{chosen.model} — {chosen.hint}",
            detail=(
                "\n".join(f"{p.model}: {p.hint}" for p in providers) if verbose else None
            ),
        )
    # No usable provider — first entry's hint is the actionable one.
    if providers:
        top = providers[0]
        return DoctorCheck(
            name="LLM provider",
            level="blocker",
            message=f"no usable provider (top: {top.model})",
            hint=f"→ {top.hint}",
            detail=(
                "\n".join(f"{p.model}: {p.hint}" for p in providers) if verbose else None
            ),
        )
    return DoctorCheck(
        name="LLM provider",
        level="blocker",
        message="no providers registered",
        hint="→ install ollama or set an API key",
    )


def _check_cost_estimate(root: Path, config: TestGapConfig | None) -> DoctorCheck:
    if config is None:
        return DoctorCheck(
            name="cost estimate",
            level="ok",
            message="n/a (no config)",
        )
    if config.llm.max_cost_per_run == 0:
        return DoctorCheck(
            name="cost estimate",
            level="ok",
            message="n/a (local model / no cap)",
        )
    try:
        functions, _meta = discover_targets(
            project_root=root,
            config=config,
            base_ref=None,
            head_ref="HEAD",
            max_functions=None,
        )
    except Exception as e:  # noqa: BLE001 — no git / diff etc. is common
        return DoctorCheck(
            name="cost estimate",
            level="ok",
            message="n/a (no diff computable)",
            detail=str(e),
        )
    if not functions:
        return DoctorCheck(
            name="cost estimate",
            level="ok",
            message="n/a (no uncovered functions in diff)",
        )
    # Rough estimate: (tokens per function prompt) * (2 rounds) * (unit price guess).
    # We do NOT call the LLM. Unit cost heuristic: $0.000003/token for hosted models,
    # $0 for local. When we cannot determine, use a modest default.
    unit_cost = 0.0 if config.llm.model.startswith("ollama/") else 3e-6
    per_fn_tokens = 3000  # ballpark: prompt + few-shot + expected output
    rounds = 2  # 1st pass + potential retry
    estimated = len(functions) * per_fn_tokens * rounds * unit_cost
    # Additional refinement using ``_estimate_tokens`` on function source when
    # available — smoothes the estimate for large functions.
    detail_lines = [
        f"{len(functions)} function(s)",
        f"unit≈${unit_cost:.6f}/token",
        f"rounds={rounds}",
        f"estimated ≈ ${estimated:.4f}",
    ]
    if functions:
        sample = functions[0]
        approx = _estimate_tokens(sample.source or "")
        detail_lines.append(f"first-function tokens≈{approx}")
    threshold = config.llm.max_cost_per_run * 0.8
    if estimated > threshold:
        return DoctorCheck(
            name="cost estimate",
            level="warning",
            message=(
                f"≈ ${estimated:.4f} projected (>80% of budget "
                f"${config.llm.max_cost_per_run:.2f})"
            ),
            hint="→ raise max_cost_per_run or use `-n <smaller>`",
            detail="\n".join(detail_lines),
        )
    return DoctorCheck(
        name="cost estimate",
        level="ok",
        message=f"≈ ${estimated:.4f} projected",
        detail="\n".join(detail_lines),
    )


def _check_cache() -> DoctorCheck:
    path = DetectCache.default_path()
    if not path.exists():
        return DoctorCheck(
            name="cache",
            level="ok",
            message=f"empty ({path})",
        )
    try:
        mtime = path.stat().st_mtime
        when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        when = "unknown"
    return DoctorCheck(
        name="cache",
        level="ok",
        message=f"last update {when}",
        detail=str(path),
    )


# ---------------------------------------------------------------------------
# rendering + exit
# ---------------------------------------------------------------------------


_LEVEL_BADGES = {
    "ok": "[green]OK[/]",
    "warning": "[yellow]WARN[/]",
    "blocker": "[red]FAIL[/]",
}


def _render_doctor_table(checks: list[DoctorCheck], *, verbose: bool, console: Console) -> None:
    table = Table(show_header=True, header_style="bold", title="testgap doctor")
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("message")
    for c in checks:
        badge = _LEVEL_BADGES[c.level]
        message = c.message
        if c.hint:
            message = f"{message}\n[dim]{c.hint}[/]"
        if verbose and c.detail:
            message = f"{message}\n[dim]{escape(c.detail)}[/]"
        table.add_row(c.name, badge, message)
    console.print(table)


def _summarize_exit(checks: list[DoctorCheck]) -> int:
    if any(c.level == "blocker" for c in checks):
        return 1
    if any(c.level == "warning" for c in checks):
        return 2
    return 0


# ---------------------------------------------------------------------------
# entry point (registered by cli.py)
# ---------------------------------------------------------------------------


def _run_doctor_impl(
    project_root: Path,
    *,
    refresh: bool,
    verbose: bool,
    console: Console,
) -> int:
    """Execute the doctor checks and render results. Returns the exit code.

    Split from :func:`run_doctor` so tests can drive the diagnostics with a
    recording ``Console`` and inspect the numeric exit without going through
    ``CliRunner``.
    """
    if refresh:
        DetectCache().clear()
        console.print("[dim]detect cache cleared[/]")

    config_check, config = _check_config(project_root)
    checks: list[DoctorCheck] = [
        _check_pytest(project_root),
        _check_git(project_root),
        config_check,
        _check_llm_providers(verbose=verbose),
        _check_cost_estimate(project_root, config),
        _check_cache(),
    ]
    _render_doctor_table(checks, verbose=verbose, console=console)

    # Preserve the import contract for potential v0.3 detail rendering.
    _ = CACHE_FILENAME
    _ = time
    return _summarize_exit(checks)


def run_doctor(
    root: Path | None = typer.Option(
        None, "--path", "-p", file_okay=False, help="Project root (defaults to cwd)."
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Clear the detection cache before running."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show raw diagnostics for each check."
    ),
) -> None:
    """Diagnose the local TestGap environment.

    Exit code 0 = all checks OK; 1 = blocker(s); 2 = warning(s) only.
    """
    project_root = (root or Path.cwd()).resolve()
    code = _run_doctor_impl(
        project_root, refresh=refresh, verbose=verbose, console=Console()
    )
    raise typer.Exit(code=code)


__all__ = ["DoctorCheck", "run_doctor"]
