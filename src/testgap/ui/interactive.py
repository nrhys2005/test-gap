"""Interactive `testgap diff --review` session.

The session iterates over each uncovered function discovered by ``discover_targets``,
runs the standard ``pipeline.process_function`` once, then offers the user a 5-way
prompt: apply / skip / regenerate / edit / quit.

All inputs that touch the outside world (console, prompt, editor) are injectable
so the module is fully unit-testable without a TTY / subprocess.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from testgap import pipeline
from testgap.config.schema import TestGapConfig
from testgap.cost import CostTracker
from testgap.coverage import UncoveredFunction
from testgap.generator import GeneratedTest, GeneratedTestSet, LLMClient
from testgap.pipeline import DiffMetadata, FunctionSuggestion
from testgap.validator import run_pytest_on_file
from testgap.validator.runner import ValidatorError

# ---------------------------------------------------------------------------
# public data model
# ---------------------------------------------------------------------------


@dataclass
class AppliedFile:
    """A single test file written to disk during the session."""

    function_qualname: str
    path: Path
    test_count: int
    merge_hint: Path | None = None  # primary `test_<module>.py` when we had to pick a
                                     # secondary path; summary uses this to nudge merge.


@dataclass
class ReviewOutcome:
    """End-of-session summary returned to the CLI."""

    applied: list[AppliedFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    quit_early: bool = False
    cost_total: float = 0.0
    processed: int = 0
    pending: int = 0


PromptFn = Callable[..., str]
EditorFn = Callable[[Path], None]


# ---------------------------------------------------------------------------
# defaults (real implementations of injectable hooks)
# ---------------------------------------------------------------------------


def default_prompt_fn(message: str, *, choices: list[str], default: str) -> str:
    """Wrapper around ``rich.prompt.Prompt.ask`` matching the injected signature."""
    return Prompt.ask(message, choices=choices, default=default)


def default_editor_fn(path: Path) -> None:
    """Open the user's ``$EDITOR`` (or ``vi``) on ``path``."""
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(path)], check=False)


# ---------------------------------------------------------------------------
# inner-loop dataclass
# ---------------------------------------------------------------------------


@dataclass
class _OneOutcome:
    action: Literal["apply", "skip", "quit"]
    applied_file: AppliedFile | None = None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_review_session(
    *,
    project_root: Path,
    config: TestGapConfig,
    llm_client: LLMClient,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
    max_functions: int | None = None,
    console: Console | None = None,
    prompt_fn: PromptFn | None = None,
    editor_fn: EditorFn | None = None,
) -> ReviewOutcome:
    """Run an interactive review session for the diff.

    Returns a :class:`ReviewOutcome` summarising applied / skipped / cost.
    The CLI prints the summary; this function only returns data.
    """
    console = console or Console()
    prompt_fn = prompt_fn or default_prompt_fn
    editor_fn = editor_fn or default_editor_fn

    functions, diff_meta = pipeline.discover_targets(
        project_root=project_root,
        config=config,
        base_ref=base_ref,
        head_ref=head_ref,
        max_functions=max_functions,
    )

    _print_header(console, diff_meta)
    if not functions:
        msg = diff_meta.skipped_reason or "nothing to do"
        console.print(f"[green]:heavy_check_mark:[/] {escape(msg)}")
        return ReviewOutcome(cost_total=0.0)

    tracker = CostTracker(max_cost_per_run=config.llm.max_cost_per_run)
    test_dirs = pipeline.prepare_test_dirs(config, project_root)
    outcome = ReviewOutcome()

    total = len(functions)
    for i, func in enumerate(functions, 1):
        suggestion = pipeline.process_function(
            func=func,
            project_root=project_root,
            config=config,
            llm_client=llm_client,
            tracker=tracker,
            test_dirs=test_dirs,
        )
        _format_suggestion_block(console, i, total, suggestion)

        try:
            one = _review_one(
                func=func,
                suggestion=suggestion,
                project_root=project_root,
                config=config,
                llm_client=llm_client,
                tracker=tracker,
                test_dirs=test_dirs,
                console=console,
                prompt_fn=prompt_fn,
                editor_fn=editor_fn,
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]![/] interrupted; finalizing partial results")
            outcome.quit_early = True
            outcome.pending = total - i + 1
            break

        outcome.processed += 1
        if one.action == "apply" and one.applied_file is not None:
            outcome.applied.append(one.applied_file)
        elif one.action == "skip":
            outcome.skipped.append(func.qualname)
        elif one.action == "quit":
            outcome.quit_early = True
            outcome.pending = total - i
            break

        if tracker.remaining <= 0 and i < total:
            console.print(
                "[yellow]![/] budget exhausted; stopping further generation"
            )
            outcome.pending = total - i
            break

    outcome.cost_total = tracker.spent
    _print_session_summary(console, outcome)
    return outcome


# ---------------------------------------------------------------------------
# inner loop (per-function 5-choice prompt)
# ---------------------------------------------------------------------------


def _review_one(
    *,
    func: UncoveredFunction,
    suggestion: FunctionSuggestion,
    project_root: Path,
    config: TestGapConfig,
    llm_client: LLMClient,
    tracker: CostTracker,
    test_dirs: list[Path],
    console: Console,
    prompt_fn: PromptFn,
    editor_fn: EditorFn,
) -> _OneOutcome:
    current = suggestion
    choices = ["a", "s", "r", "e", "q"]
    prompt_msg = "[a]pply / [s]kip / [r]egenerate / [e]dit / [q]uit"

    while True:
        default = "a" if current.accepted_cases else "s"
        choice = prompt_fn(prompt_msg, choices=choices, default=default)

        if choice == "a":
            if not current.accepted_cases:
                console.print(
                    "[yellow]![/] nothing to apply (no accepted cases). "
                    "Try [r] or [e]."
                )
                continue
            if (
                current.validator_result is not None
                and current.validator_result.environment_error
            ):
                console.print(
                    "[yellow]![/] pytest environment error; cannot safely apply."
                )
                continue
            test_dir = test_dirs[0] if test_dirs else (project_root / "tests")
            applied = _apply_to_disk(
                current,
                func=func,
                project_root=project_root,
                test_dir=test_dir,
                source_paths=config.project.source_paths,
            )
            console.print(
                f"[green]:heavy_check_mark:[/] wrote {escape(str(applied.path))} "
                f"({applied.test_count} tests)"
            )
            return _OneOutcome(action="apply", applied_file=applied)

        if choice == "s":
            console.print("[dim]skipped[/]")
            return _OneOutcome(action="skip")

        if choice == "q":
            return _OneOutcome(action="quit")

        if choice == "r":
            estimated = _estimate_next_call_cost(current, tracker)
            if tracker.would_exceed(estimated):
                console.print(
                    "[red]:cross_mark:[/] Budget exhausted — cannot regenerate. "
                    "Choose a/s/e/q."
                )
                continue
            console.print("[dim]regenerating...[/]")
            new_current = _regenerate(
                func=func,
                tracker=tracker,
                llm_client=llm_client,
                config=config,
                test_dirs=test_dirs,
                project_root=project_root,
            )
            if new_current.error and not new_current.accepted_cases:
                console.print(
                    f"[yellow]![/] regeneration failed: {escape(new_current.error)}; "
                    f"keeping previous result"
                )
            else:
                current = new_current
            _format_suggestion_block(console, None, None, current)
            continue

        if choice == "e":
            try:
                current, edit_msg = _edit_and_revalidate(
                    suggestion=current,
                    project_root=project_root,
                    config=config,
                    editor_fn=editor_fn,
                )
            except ValidatorError as e:
                console.print(f"[red]:cross_mark:[/] validator error: {escape(str(e))}")
                continue
            if edit_msg:
                console.print(f"[dim]{escape(edit_msg)}[/]")
            _format_suggestion_block(console, None, None, current)
            continue

        # unreachable — Prompt.ask constrains to choices, but keep defensive guard
        console.print(f"[yellow]?[/] unknown choice: {escape(choice)}")


# ---------------------------------------------------------------------------
# regeneration
# ---------------------------------------------------------------------------


def _regenerate(
    *,
    func: UncoveredFunction,
    tracker: CostTracker,
    llm_client: LLMClient,
    config: TestGapConfig,
    test_dirs: list[Path],
    project_root: Path,
) -> FunctionSuggestion:
    """Re-run ``pipeline.process_function`` (sharing the same tracker)."""
    return pipeline.process_function(
        func=func,
        project_root=project_root,
        config=config,
        llm_client=llm_client,
        tracker=tracker,
        test_dirs=test_dirs,
    )


def _estimate_next_call_cost(prev: FunctionSuggestion, tracker: CostTracker) -> float:
    """Rough per-call cost estimate for the budget guard.

    NOTE: this heuristic has known limits.
      1) When ``prev.attempts == 2`` (1st-round partial pass + retry), the
         average undercounts a true single call's tokens.
      2) ``process_function`` itself may internally fire a 2nd LLM call after
         partial-pass on the next round, doubling the worst-case spend.
      3) v0.1 uses the simple average and relies on ``tracker.record`` raising
         :class:`BudgetExceeded` as the ultimate guardrail.
    """
    _ = tracker  # unused; kept in signature for future tuning
    if prev.cost_usd > 0 and prev.attempts > 0:
        return prev.cost_usd / prev.attempts
    return 0.0


# ---------------------------------------------------------------------------
# edit + revalidate
# ---------------------------------------------------------------------------


_CONFTEST_BODY = 'collect_ignore_glob = ["*"]\n'


def _ensure_testgap_conftest(scratch_dir: Path, *, console: Console | None = None) -> None:
    """Drop a conftest under ``.testgap/`` that hides temp files from pytest."""
    conftest = scratch_dir / "conftest.py"
    if not conftest.exists():
        conftest.write_text(_CONFTEST_BODY, encoding="utf-8")
        return
    existing = conftest.read_text(encoding="utf-8")
    if existing.strip() != _CONFTEST_BODY.strip() and console is not None:
        console.print(
            "[yellow]![/] .testgap/conftest.py exists with custom content; "
            "ensure it contains `collect_ignore_glob = [\"*\"]` to prevent "
            "test collection of temporary validator files."
        )


def _edit_and_revalidate(
    *,
    suggestion: FunctionSuggestion,
    project_root: Path,
    config: TestGapConfig,
    editor_fn: EditorFn,
) -> tuple[FunctionSuggestion, str | None]:
    """Open the user's editor on the current generated code and re-validate."""
    base_code = (
        suggestion.generated.to_source() if suggestion.generated else "# (empty)\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_test.py", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(base_code)
        tmp_path = Path(tf.name)

    try:
        before = tmp_path.read_text(encoding="utf-8")
        try:
            editor_fn(tmp_path)
        except FileNotFoundError as e:
            return suggestion, f"editor failed: {e}"
        except KeyboardInterrupt:
            return suggestion, "editor cancelled"
        after = tmp_path.read_text(encoding="utf-8")
        if before == after:
            return suggestion, "no changes; using last result"

        scratch_dir = project_root / ".testgap"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        _ensure_testgap_conftest(scratch_dir)
        scratch_path = scratch_dir / f"validator_edit_{tmp_path.stem}.py"
        scratch_path.write_text(after, encoding="utf-8")
        try:
            vr = run_pytest_on_file(
                scratch_path,
                project_root=project_root,
                timeout_seconds=config.generation.test_timeout_seconds,
            )
        finally:
            scratch_path.unlink(missing_ok=True)

        edited_set = GeneratedTestSet(
            imports=[],
            tests=[
                GeneratedTest(
                    name="edited", purpose="user-edited", code=after.rstrip()
                )
            ],
        )
        accepted = list(vr.passed)
        discarded = list(vr.failed)
        new_suggestion = FunctionSuggestion(
            function=suggestion.function,
            generated=edited_set,
            validator_result=vr,
            cost_usd=suggestion.cost_usd,
            attempts=suggestion.attempts,
            accepted_cases=accepted,
            discarded_cases=discarded,
        )
        return new_suggestion, None
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# apply (write to disk)
# ---------------------------------------------------------------------------


def _resolve_target_path(
    test_dir: Path,
    func: UncoveredFunction,
    project_root: Path,
    source_paths: list[str],
) -> tuple[Path, Path | None]:
    """Decide the final on-disk path for an applied test file.

    Returns ``(chosen_path, merge_hint)`` where ``merge_hint`` is the primary
    `test_<module>.py` path when we had to fall back to a secondary name —
    used by the summary output to suggest merging.
    """
    try:
        rel = func.file.resolve().relative_to(project_root.resolve())
    except ValueError:
        module_dir_parts: list[str] = []
        module_stem = func.file.stem
    else:
        parts = list(rel.parts)
        for src in source_paths:
            prefix = src.rstrip("/").split("/")
            if parts[: len(prefix)] == prefix:
                parts = parts[len(prefix):]
                break
        if not parts:
            module_dir_parts = []
            module_stem = func.file.stem
        else:
            module_dir_parts = parts[:-1]
            module_stem = Path(parts[-1]).stem

    target_dir = test_dir.joinpath(*module_dir_parts) if module_dir_parts else test_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    primary = target_dir / f"test_{module_stem}.py"
    if not primary.exists():
        return primary, None

    func_suffix = func.qualname.replace(".", "_")
    secondary = target_dir / f"test_{module_stem}_{func_suffix}.py"
    if not secondary.exists():
        return secondary, primary

    n = 2
    while True:
        cand = target_dir / f"test_{module_stem}_{func_suffix}_{n}.py"
        if not cand.exists():
            return cand, primary
        n += 1


def _apply_to_disk(
    suggestion: FunctionSuggestion,
    *,
    func: UncoveredFunction,
    project_root: Path,
    test_dir: Path,
    source_paths: list[str],
) -> AppliedFile:
    """Write the accepted tests to a new file (never overwriting existing)."""
    if suggestion.generated is None:
        raise ValueError("cannot apply suggestion with no generated content")

    path, merge_hint = _resolve_target_path(test_dir, func, project_root, source_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(suggestion.generated.to_source(), encoding="utf-8")

    return AppliedFile(
        function_qualname=func.qualname,
        path=path,
        test_count=len(suggestion.generated.tests),
        merge_hint=merge_hint,
    )


# ---------------------------------------------------------------------------
# console output helpers
# ---------------------------------------------------------------------------


def _print_header(console: Console, meta: DiffMetadata) -> None:
    console.print(f"[dim]base[/] {escape(meta.base_ref)} -> [dim]head[/] {escape(meta.head_ref)}")
    if meta.skipped_reason is None:
        console.print(
            f"changed lines: {meta.changed_total}   covered: {meta.covered_total}   "
            f"diff coverage: {meta.diff_coverage_pct}%"
        )


def _format_suggestion_block(
    console: Console,
    idx: int | None,
    total: int | None,
    s: FunctionSuggestion,
) -> None:
    """Render a single function's diagnostic block (mirrors cli._print_suggestion)."""
    file_label = escape(f"{s.function.file.name}::{s.function.qualname}")
    if idx is not None and total is not None:
        header = f"[{idx}/{total}] {file_label}"
    else:
        header = file_label
    console.print(f"[bold]{header}[/]")

    lines_str = ", ".join(str(n) for n in s.function.uncovered_lines[:8])
    if len(s.function.uncovered_lines) > 8:
        lines_str += ", ..."
    console.print(f"  uncovered lines: {lines_str}")

    if s.error:
        console.print(f"  [red]:cross_mark:[/] {escape(s.error)}")
        return

    if s.validator_result is None or s.generated is None:
        console.print("  [yellow]![/] no result captured")
        return

    if s.validator_result.environment_error:
        console.print(
            f"  [red]:cross_mark:[/] {escape(s.validator_result.environment_error)}"
        )
        return

    accepted_n = len(s.accepted_cases)
    discarded_n = len(s.discarded_cases)
    total_n = accepted_n + discarded_n
    cost_label = f"${s.cost_usd:.4f}" if s.cost_usd > 0 else "$0 (cost unknown)"
    retried_marker = " [retried]" if s.attempts == 2 else ""

    if s.fully_passed:
        console.print(
            f"  [green]:heavy_check_mark:[/] {accepted_n}/{total_n} tests passed   "
            f"{cost_label}{retried_marker}"
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
        console.print(
            f"  [yellow]![/] retry skipped: {escape(s.retry_skipped_reason)}"
        )

    for case in s.discarded_cases[:3]:
        console.print(f"    [red]·[/] {escape(case.name)}")


def _print_session_summary(console: Console, outcome: ReviewOutcome) -> None:
    console.print()
    console.print("[bold]Session summary[/]")
    console.print(
        f"  applied: {len(outcome.applied)}   "
        f"skipped: {len(outcome.skipped)}   "
        f"processed: {outcome.processed}   "
        f"pending: {outcome.pending}"
    )
    for af in outcome.applied:
        line = f"  [green]:heavy_check_mark:[/] {escape(str(af.path))} ({af.test_count} tests)"
        console.print(line)
        if af.merge_hint is not None:
            console.print(
                f"      [dim]consider merging into {escape(str(af.merge_hint))}[/]"
            )
    console.print(f"  [dim]Total cost: ${outcome.cost_total:.4f}[/]")
    if outcome.quit_early:
        console.print("  [yellow]![/] session ended early")
