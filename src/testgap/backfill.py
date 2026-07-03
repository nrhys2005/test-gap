"""``testgap backfill`` — LLM-driven test generation across the whole repo.

Given the same scan output that powers :mod:`testgap.cli_scan`, ``backfill``
iterates through uncovered functions and drives ``pipeline.process_function``
for each, either interactively (``_review_one`` per function) or in ``--auto``
mode (accept-on-pass / skip-on-discard).

Design highlights (see ``.plans/TG-403-415.md``):

* **Priority ordering** (``--priority``): impact / coverage / size. All use
  stable alphabetical tie-breakers so the output is deterministic.
* **Progress ↔ prompt separation** (R7 option A): rich ``Progress`` is used
  *only* in ``--auto`` mode. Interactive mode prints a ``[i/N] Processing …``
  header per function to avoid Live/Prompt stdout collisions.
* **Auto-skip 3-way logging** (R2): console warn + ``user_action`` + one
  ``backfill_progress(action="auto_skip")`` per discarded function.
* **Provider-unhealthy** counter mirrors ``run_review_session`` /
  ``pipeline.run_diff`` — 2 consecutive LLM failures ⇒ stop early.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from testgap import pipeline
from testgap.config.schema import TestGapConfig
from testgap.cost import CostTracker
from testgap.coverage import UncoveredFunction
from testgap.generator import LLMClient
from testgap.pipeline import CONSECUTIVE_LLM_FAILURE_LIMIT, FunctionSuggestion
from testgap.scan import (
    FileCoverage,
    FunctionCoverage,
    ScanReport,
    scan_project,
)
from testgap.session_logging import (
    NoopSessionLog,
    SessionLogProtocol,
    log_file_rel,
)
from testgap.session_logging.events import (
    EVENT_BACKFILL_END,
    EVENT_BACKFILL_PROGRESS,
    EVENT_BACKFILL_START,
    EVENT_USER_ACTION,
)
from testgap.ui.interactive import (
    AppliedFile,
    EditorFn,
    PromptFn,
    _apply_to_disk,
    _format_suggestion_block,
    _review_one,
    default_editor_fn,
    default_prompt_fn,
)

BACKFILL_SCHEMA_VERSION = 1

__all__ = [
    "BACKFILL_SCHEMA_VERSION",
    "BackfillOutcome",
    "run_backfill",
]


# ---------------------------------------------------------------------------
# public dataclass
# ---------------------------------------------------------------------------


@dataclass
class BackfillOutcome:
    """End-of-run summary returned to the CLI.

    ``coverage_after`` is a *heuristic* — we do not re-run pytest at the end.
    ``coverage_after_is_estimated=True`` forces the CLI render helper to
    prefix the value with ``≈``. A future ``--verify-coverage`` flag will
    set the flag to ``False`` after re-measuring.
    """

    functions_processed: int = 0
    functions_accepted: int = 0
    functions_skipped: int = 0
    functions_failed: int = 0
    applied: list[AppliedFile] = field(default_factory=list)
    discarded_qualnames: list[str] = field(default_factory=list)
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    coverage_after_is_estimated: bool = True
    cost_total: float = 0.0
    elapsed_seconds: float = 0.0
    quit_reason: str | None = None
    provider_unhealthy: bool = False
    unhealthy_reason: str | None = None
    dry_run: bool = False
    schema_version: int = BACKFILL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# worklist helpers
# ---------------------------------------------------------------------------


@dataclass
class _WorkItem:
    """One row of the backfill worklist."""

    uf: UncoveredFunction  # feeds ``pipeline.process_function``
    fc: FunctionCoverage  # stats / display
    file: FileCoverage
    impact: float


def _to_underlying(func: FunctionCoverage, fc: FileCoverage) -> UncoveredFunction | None:
    """Recover the raw :class:`UncoveredFunction` embedded in ``FunctionCoverage``.

    ``FunctionCoverage._underlying`` was populated by :func:`scan_project` so
    that ``pipeline.process_function`` gets the exact same ``UncoveredFunction``
    it would have received via ``discover_targets`` — including source text
    and branch flag.
    """
    _ = fc  # signature kept in case future callers pass file context
    return func._underlying


def _build_worklist(
    scan: ScanReport, priority: str
) -> list[_WorkItem]:
    """Flatten scan → per-function worklist and sort by ``priority``.

    Sort keys always include ``rel_path`` and ``qualname`` as alphabetical
    tie-breakers so the ordering is deterministic across runs.
    """
    items: list[_WorkItem] = []
    for fc in scan.files:
        for func in fc.uncovered_functions:
            uf = _to_underlying(func, fc)
            if uf is None:
                # Defensive: scan_project always populates ``_underlying``,
                # but a hand-built ScanReport (rare — tests only) may skip it.
                continue
            impact = _impact_score(func)
            items.append(_WorkItem(uf=uf, fc=func, file=fc, impact=impact))

    if priority == "impact":
        items.sort(key=lambda w: (-w.impact, w.file.rel_path, w.fc.qualname))
    elif priority == "coverage":
        items.sort(
            key=lambda w: (w.file.coverage_pct, w.file.rel_path, w.fc.qualname)
        )
    elif priority == "size":
        items.sort(
            key=lambda w: (
                -(w.fc.end_line - w.fc.start_line),
                w.file.rel_path,
                w.fc.qualname,
            )
        )
    else:
        raise ValueError(
            f"unknown priority={priority!r}; expected 'impact'|'coverage'|'size'"
        )
    return items


def _impact_score(func: FunctionCoverage) -> float:
    """Same formula as :func:`testgap.scan._impact_score` — kept local so the
    backfill layer does not depend on scan's private symbol."""
    span = max(func.end_line - func.start_line + 1, 1)
    return len(func.uncovered_lines) / span


# ---------------------------------------------------------------------------
# auto-mode helper
# ---------------------------------------------------------------------------


@dataclass
class _AutoOutcome:
    """Result of ``_process_auto`` for one function."""

    applied_file: AppliedFile | None = None
    action: Literal["apply", "auto_skip", "error"] = "auto_skip"
    error: str | None = None


def _process_auto(
    *,
    suggestion: FunctionSuggestion,
    func: UncoveredFunction,
    project_root: Path,
    config: TestGapConfig,
    test_dirs: list[Path],
    console: Console,
    session_log: SessionLogProtocol,
    dry_run: bool,
) -> _AutoOutcome:
    """Non-interactive per-function branch.

    * accepted_cases exist → ``_apply_to_disk`` (skipped in dry-run).
    * discarded (no accepted cases) → warning + user_action(auto_skip).
    * ``_apply_to_disk`` exceptions → ``action="error"`` (backfill counts as failed).
    """
    if not suggestion.accepted_cases:
        console.print(
            f"[yellow]![/] discarded: {escape(func.qualname)} "
            f"(no accepted cases)"
        )
        session_log.record(
            EVENT_USER_ACTION,
            {
                "function_qualname": func.qualname,
                "action": "auto_skip",
                "applied_path": None,
                "reason": "no_accepted_cases",
            },
        )
        return _AutoOutcome(applied_file=None, action="auto_skip")

    if dry_run:
        return _AutoOutcome(applied_file=None, action="auto_skip")

    test_dir = test_dirs[0] if test_dirs else (project_root / "tests")
    try:
        applied = _apply_to_disk(
            suggestion,
            func=func,
            project_root=project_root,
            test_dir=test_dir,
            source_paths=config.project.source_paths,
        )
    except (OSError, ValueError) as e:
        console.print(
            f"[red]:cross_mark:[/] apply failed for {escape(func.qualname)}: "
            f"{escape(str(e))}"
        )
        return _AutoOutcome(applied_file=None, action="error", error=str(e))

    console.print(
        f"[green]:heavy_check_mark:[/] applied {escape(func.qualname)} "
        f"→ {escape(str(applied.path))} ({applied.test_count} tests)"
    )
    session_log.record(
        EVENT_USER_ACTION,
        {
            "function_qualname": func.qualname,
            "action": "apply",
            "applied_path": log_file_rel(applied.path, project_root),
        },
    )
    return _AutoOutcome(applied_file=applied, action="apply")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_backfill(
    *,
    project_root: Path,
    config: TestGapConfig,
    llm_client: LLMClient,
    target_coverage: float | None = None,
    max_functions: int | None = None,
    path_filter: Path | None = None,
    below_pct: float | None = None,
    auto: bool = False,
    priority: Literal["coverage", "impact", "size"] = "impact",
    dry_run: bool = False,
    console: Console | None = None,
    prompt_fn: PromptFn | None = None,
    editor_fn: EditorFn | None = None,
    session_log: SessionLogProtocol | None = None,
) -> BackfillOutcome:
    """Orchestrate an LLM-backed backfill session across the whole project."""
    console = console or Console()
    # R6: ``_review_one`` requires non-None prompt/editor callables. Fall back
    # to the same defaults ``run_review_session`` uses so callers can omit them.
    prompt_fn = prompt_fn or default_prompt_fn
    editor_fn = editor_fn or default_editor_fn
    log = session_log or NoopSessionLog()

    tracker = CostTracker(max_cost_per_run=config.llm.max_cost_per_run)
    test_dirs = pipeline.prepare_test_dirs(config, project_root)

    scan_report = scan_project(
        project_root,
        config,
        path_filter=path_filter,
        below_pct=below_pct,
    )
    full_worklist = _build_worklist(scan_report, priority)

    max_functions_capped = False
    if max_functions is not None and max_functions < len(full_worklist):
        worklist = full_worklist[: max(max_functions, 0)]
        max_functions_capped = True
    else:
        worklist = full_worklist

    coverage_before = scan_report.overall_coverage_pct
    outcome = BackfillOutcome(
        coverage_before=coverage_before,
        coverage_after=coverage_before,
        dry_run=dry_run,
    )

    log.record(
        EVENT_BACKFILL_START,
        {
            "worklist_size": len(worklist),
            "priority": priority,
            "target_coverage": target_coverage,
            "max_functions": max_functions,
            "dry_run": dry_run,
            "auto": auto,
        },
    )

    t_start = time.monotonic()
    total = len(worklist)
    if total == 0:
        outcome.elapsed_seconds = round(time.monotonic() - t_start, 3)
        _record_end(log, outcome)
        console.print("[green]:heavy_check_mark:[/] nothing to backfill")
        return outcome

    # ------------------------------------------------------------------
    # Progress ↔ interactive branch (R7 option A):
    #   auto     → wrap the loop in a rich ``Progress`` context.
    #   interactive → print a header line per function; NO Progress.
    # ------------------------------------------------------------------
    if auto:
        with _make_progress(console) as progress:
            task_id = progress.add_task(
                "[bold cyan]backfill[/]", total=total
            )
            _run_loop(
                worklist=worklist,
                total=total,
                outcome=outcome,
                project_root=project_root,
                config=config,
                llm_client=llm_client,
                tracker=tracker,
                test_dirs=test_dirs,
                console=console,
                prompt_fn=prompt_fn,
                editor_fn=editor_fn,
                session_log=log,
                auto=True,
                dry_run=dry_run,
                target_coverage=target_coverage,
                progress=progress,
                progress_task_id=task_id,
            )
    else:
        _run_loop(
            worklist=worklist,
            total=total,
            outcome=outcome,
            project_root=project_root,
            config=config,
            llm_client=llm_client,
            tracker=tracker,
            test_dirs=test_dirs,
            console=console,
            prompt_fn=prompt_fn,
            editor_fn=editor_fn,
            session_log=log,
            auto=False,
            dry_run=dry_run,
            target_coverage=target_coverage,
            progress=None,
            progress_task_id=None,
        )

    # If the loop finished naturally AND the worklist was capped by
    # --max-functions, surface that reason.
    if outcome.quit_reason is None and max_functions_capped:
        outcome.quit_reason = "max_functions"

    outcome.cost_total = tracker.spent
    outcome.elapsed_seconds = round(time.monotonic() - t_start, 3)
    _record_end(log, outcome)
    return outcome


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


def _run_loop(
    *,
    worklist: list[_WorkItem],
    total: int,
    outcome: BackfillOutcome,
    project_root: Path,
    config: TestGapConfig,
    llm_client: LLMClient,
    tracker: CostTracker,
    test_dirs: list[Path],
    console: Console,
    prompt_fn: PromptFn,
    editor_fn: EditorFn,
    session_log: SessionLogProtocol,
    auto: bool,
    dry_run: bool,
    target_coverage: float | None,
    progress: Progress | None,
    progress_task_id: int | None,
) -> None:
    """Iterate the worklist, dispatching to auto/interactive per function.

    Kept as a top-level helper so both branches (``auto`` wrapped in Progress,
    ``interactive`` without) share exactly one implementation of the
    provider-unhealthy / target-coverage / budget guards.
    """
    consecutive_llm_failures = 0
    covered_lines_est = int(
        outcome.coverage_before / 100.0
        * max(_scan_total_lines_placeholder(worklist), 1)
    )
    # Estimated total lines snapshot for target_coverage heuristic; falls back
    # to a placeholder when the worklist is empty (loop won't run anyway).
    total_lines_snapshot = _scan_total_lines_placeholder(worklist)

    for idx, work in enumerate(worklist):
        # Interactive: header line replaces Progress.
        if not auto:
            console.print(
                f"[bold cyan][{idx + 1}/{total}] Processing "
                f"{escape(work.fc.qualname)}[/]"
            )

        try:
            suggestion = pipeline.process_function(
                func=work.uf,
                project_root=project_root,
                config=config,
                llm_client=llm_client,
                tracker=tracker,
                test_dirs=test_dirs,
                session_log=session_log,
            )
        except KeyboardInterrupt:
            console.print(
                "\n[yellow]![/] interrupted; finalizing partial results"
            )
            outcome.quit_reason = outcome.quit_reason or "user_quit"
            break

        outcome.functions_processed += 1
        session_log.increment_functions()

        progress_action: str = "skip"
        gain_lines = 0

        # ------------------------------------------------------------------
        # dry-run: no apply, no interactive prompt.
        # ------------------------------------------------------------------
        if dry_run:
            outcome.functions_skipped += 1
            progress_action = "skip"
            gain_lines = 0
            _emit_progress(
                session_log,
                idx=idx,
                total=total,
                qualname=work.fc.qualname,
                file_rel=work.file.rel_path,
                action=progress_action,
                gain_lines=gain_lines,
            )
        elif auto:
            auto_out = _process_auto(
                suggestion=suggestion,
                func=work.uf,
                project_root=project_root,
                config=config,
                test_dirs=test_dirs,
                console=console,
                session_log=session_log,
                dry_run=False,
            )
            if auto_out.action == "apply" and auto_out.applied_file is not None:
                outcome.applied.append(auto_out.applied_file)
                outcome.functions_accepted += 1
                gain_lines = len(work.fc.uncovered_lines)
                covered_lines_est += gain_lines
                progress_action = "apply"
            elif auto_out.action == "auto_skip":
                outcome.functions_skipped += 1
                outcome.discarded_qualnames.append(work.fc.qualname)
                progress_action = "auto_skip"
            else:  # error
                outcome.functions_failed += 1
                progress_action = "error"
            _emit_progress(
                session_log,
                idx=idx,
                total=total,
                qualname=work.fc.qualname,
                file_rel=work.file.rel_path,
                action=progress_action,
                gain_lines=gain_lines,
            )
        else:
            # Interactive branch: mirrors run_review_session line 173-206.
            _format_suggestion_block(console, idx + 1, total, suggestion)
            one = _review_one(
                func=work.uf,
                suggestion=suggestion,
                project_root=project_root,
                config=config,
                llm_client=llm_client,
                tracker=tracker,
                test_dirs=test_dirs,
                console=console,
                prompt_fn=prompt_fn,
                editor_fn=editor_fn,
                session_log=session_log,
            )
            if one.action == "apply" and one.applied_file is not None:
                outcome.applied.append(one.applied_file)
                outcome.functions_accepted += 1
                gain_lines = len(work.fc.uncovered_lines)
                covered_lines_est += gain_lines
                progress_action = "apply"
                _emit_progress(
                    session_log,
                    idx=idx,
                    total=total,
                    qualname=work.fc.qualname,
                    file_rel=work.file.rel_path,
                    action=progress_action,
                    gain_lines=gain_lines,
                )
            elif one.action == "skip":
                outcome.functions_skipped += 1
                progress_action = "skip"
                _emit_progress(
                    session_log,
                    idx=idx,
                    total=total,
                    qualname=work.fc.qualname,
                    file_rel=work.file.rel_path,
                    action=progress_action,
                    gain_lines=0,
                )
            elif one.action == "quit":
                outcome.quit_reason = "user_quit"
                _emit_progress(
                    session_log,
                    idx=idx,
                    total=total,
                    qualname=work.fc.qualname,
                    file_rel=work.file.rel_path,
                    action="skip",
                    gain_lines=0,
                )
                break

        # Advance auto-mode progress bar only after emitting the event so the
        # per-function log ordering matches the visible tick.
        if progress is not None and progress_task_id is not None:
            progress.update(progress_task_id, advance=1)

        # Update running coverage_after heuristic (used for target check).
        if total_lines_snapshot:
            outcome.coverage_after = round(
                covered_lines_est / total_lines_snapshot * 100.0, 1
            )

        # --------------------------------------------------------------
        # Provider-unhealthy — 2 consecutive LLM failures without any
        # accepted case → early exit. Same as run_review_session.
        # --------------------------------------------------------------
        if suggestion.llm_failure_observed and not suggestion.accepted_cases:
            consecutive_llm_failures += 1
        else:
            consecutive_llm_failures = 0
        if consecutive_llm_failures >= CONSECUTIVE_LLM_FAILURE_LIMIT:
            outcome.provider_unhealthy = True
            outcome.unhealthy_reason = (
                f"{consecutive_llm_failures} consecutive LLM failures"
            )
            outcome.quit_reason = "provider_unhealthy"
            console.print(
                "[yellow]![/] provider unhealthy — stopping backfill. "
                "Try: testgap doctor"
            )
            break

        # --------------------------------------------------------------
        # target_coverage — stop as soon as the heuristic meets the target.
        # --------------------------------------------------------------
        if (
            target_coverage is not None
            and outcome.coverage_after >= target_coverage
        ):
            outcome.quit_reason = "target_reached"
            break

        if tracker.remaining <= 0 and idx + 1 < total:
            outcome.quit_reason = "budget_exhausted"
            break

    # Note: max_functions is enforced by the slice in ``run_backfill`` before
    # this loop runs, so a natural loop completion means either the sliced
    # cap fired or the full worklist was processed. We do not distinguish here
    # — ``quit_reason=None`` on natural completion is the normal success path.


def _scan_total_lines_placeholder(worklist: list[_WorkItem]) -> int:
    """Estimate total executable lines from the worklist's file coverage.

    Used only for the ``target_coverage`` heuristic. Deduplicates files by
    ``rel_path`` so a file with N uncovered functions counts once.
    """
    seen: dict[str, int] = {}
    for w in worklist:
        seen.setdefault(w.file.rel_path, w.file.total_lines)
    return sum(seen.values())


# ---------------------------------------------------------------------------
# progress helpers
# ---------------------------------------------------------------------------


def _make_progress(console: Console) -> Progress:
    """Rich Progress preset used only in ``--auto`` mode.

    Kept as a helper so tests can spy on the Progress class import path.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=False,
    )


def _emit_progress(
    session_log: SessionLogProtocol,
    *,
    idx: int,
    total: int,
    qualname: str,
    file_rel: str,
    action: str,
    gain_lines: int,
) -> None:
    """Emit exactly one ``backfill_progress`` event per function.

    ``action`` schema (v0.1 emit values):
      * apply / skip                    — interactive
      * apply / auto_skip / error       — auto
      * skip                            — dry_run
    ``regenerate`` / ``edit`` are reserved for v0.2.
    """
    session_log.record(
        EVENT_BACKFILL_PROGRESS,
        {
            "index": idx,
            "total": total,
            "qualname": qualname,
            "file": file_rel,
            "action": action,
            "estimated_gain_lines": gain_lines,
        },
    )


def _record_end(session_log: SessionLogProtocol, outcome: BackfillOutcome) -> None:
    session_log.record(
        EVENT_BACKFILL_END,
        {
            "functions_processed": outcome.functions_processed,
            "functions_accepted": outcome.functions_accepted,
            "quit_reason": outcome.quit_reason,
            "coverage_before": outcome.coverage_before,
            "coverage_after_est": outcome.coverage_after,
            "elapsed_seconds": outcome.elapsed_seconds,
        },
    )
