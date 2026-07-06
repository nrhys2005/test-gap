"""Unit tests for :mod:`testgap.backfill`.

All external boundaries — LLM, session log, scan_project — are injected so no
subprocess / real LLM is spawned. The tests focus on:

* worklist priority ordering (impact / coverage / size)
* auto-mode apply / discard flows (3-way log recording)
* dry-run guards
* target_coverage / max_functions / provider_unhealthy break conditions
* backfill_progress event schema
* default prompt/editor fallback (R6)
* interactive Progress avoidance (R7)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from testgap import backfill as backfill_mod
from testgap.backfill import (
    _build_worklist,
    _process_auto,
    run_backfill,
)
from testgap.config.schema import (
    GenerationConfig,
    LLMConfig,
    ProjectConfig,
    TestGapConfig,
)
from testgap.coverage import UncoveredFunction
from testgap.pipeline import FunctionSuggestion
from testgap.scan import FileCoverage, FunctionCoverage, ScanReport
from testgap.session_logging.events import (
    EVENT_BACKFILL_END,
    EVENT_BACKFILL_PROGRESS,
    EVENT_BACKFILL_START,
    EVENT_USER_ACTION,
)
from testgap.ui.interactive import AppliedFile, _OneOutcome

# ---------------------------------------------------------------------------
# spies / fakes
# ---------------------------------------------------------------------------


class SpyLog:
    """Session-log spy that records every event and impersonates the protocol."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.functions_incremented = 0
        self.closed = False
        self.close_reason: str | None = None

    def record(self, event: str, payload: dict[str, Any]) -> None:
        self.records.append((event, payload))

    def close(self, *, quit_reason: str | None = None) -> None:
        self.closed = True
        self.close_reason = quit_reason

    def increment_functions(self, n: int = 1) -> None:
        self.functions_incremented += n

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    @property
    def path(self) -> Path | None:
        return None

    def events_by_type(self, event: str) -> list[dict[str, Any]]:
        return [p for e, p in self.records if e == event]


def _config() -> TestGapConfig:
    return TestGapConfig(
        project=ProjectConfig(source_paths=["src/"], test_paths=["tests/"]),
        llm=LLMConfig(model="fake/model", max_cost_per_run=1.0),
        generation=GenerationConfig(test_timeout_seconds=30),
    )


def _make_uf(qualname: str, file: Path, start: int = 1, end: int = 3) -> UncoveredFunction:
    return UncoveredFunction(
        file=file,
        qualname=qualname,
        start_line=start,
        end_line=end,
        source="def x():\n    return 1\n",
        uncovered_lines=list(range(start, end + 1)),
    )


def _make_fc(
    qualname: str, *, start: int = 1, end: int = 3, uncov: list[int] | None = None
) -> FunctionCoverage:
    return FunctionCoverage(
        qualname=qualname,
        start_line=start,
        end_line=end,
        uncovered_lines=uncov if uncov is not None else list(range(start, end + 1)),
        total_lines=end - start + 1,
        covered_lines=0,
    )


def _make_file_coverage(
    rel_path: str,
    *,
    covered: int,
    total: int,
    funcs: list[tuple[str, int, int, list[int]]] | None = None,
    project_root: Path,
) -> FileCoverage:
    file_path = project_root / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("def x():\n    return 1\n", encoding="utf-8")
    fc_funcs = []
    for q, s, e, lines in funcs or []:
        uf = _make_uf(q, file_path.resolve(), s, e)
        uf.uncovered_lines = list(lines)
        func = FunctionCoverage(
            qualname=q,
            start_line=s,
            end_line=e,
            uncovered_lines=list(lines),
            total_lines=e - s + 1,
            covered_lines=max((e - s + 1) - len(lines), 0),
            _underlying=uf,
        )
        fc_funcs.append(func)
    return FileCoverage(
        path=file_path.resolve(),
        rel_path=rel_path,
        total_lines=total,
        covered_lines=covered,
        uncovered_functions=fc_funcs,
    )


def _scan_report(
    project_root: Path,
    files: list[FileCoverage],
    *,
    total_lines: int | None = None,
    covered_lines: int | None = None,
) -> ScanReport:
    """Build a ScanReport for tests.

    ``total_lines`` / ``covered_lines`` override the aggregation so we can
    simulate reports that include fully-covered files (which the worklist
    itself wouldn't count).
    """
    return ScanReport(
        project_root=project_root,
        files=files,
        total_lines=(
            total_lines if total_lines is not None else sum(fc.total_lines for fc in files)
        ),
        covered_lines=(
            covered_lines
            if covered_lines is not None
            else sum(fc.covered_lines for fc in files)
        ),
        generated_at="2026-07-03T00:00:00+00:00",
    )


def _fake_suggestion(
    uf: UncoveredFunction,
    *,
    accepted: bool = True,
    llm_failure: bool = False,
) -> FunctionSuggestion:
    """Minimal FunctionSuggestion — accepted_cases populated when ``accepted``."""
    from testgap.generator import GeneratedTest, GeneratedTestSet
    from testgap.validator import TestCaseResult, TestOutcome, ValidatorResult

    generated = GeneratedTestSet(
        imports=[],
        tests=[GeneratedTest(name="t", purpose="p", code="def t():\n    pass")],
    )
    if accepted:
        accepted_cases = [TestCaseResult(name="t", outcome=TestOutcome.PASS)]
        vr = ValidatorResult(cases=accepted_cases, exit_code=0)
    else:
        accepted_cases = []
        vr = ValidatorResult(cases=[], exit_code=1)

    return FunctionSuggestion(
        function=uf,
        generated=generated,
        validator_result=vr,
        cost_usd=0.001,
        attempts=1,
        accepted_cases=accepted_cases,
        discarded_cases=[],
        llm_failure_observed=llm_failure,
    )


# ---------------------------------------------------------------------------
# _build_worklist priority ordering
# ---------------------------------------------------------------------------


def test_build_worklist_priority_impact(tmp_project: Path):
    small_hot = _make_file_coverage(
        "small.py",
        covered=0,
        total=2,
        funcs=[("hot", 1, 2, [1, 2])],  # impact = 1.0
        project_root=tmp_project,
    )
    big_cold = _make_file_coverage(
        "big.py",
        covered=0,
        total=100,
        funcs=[("cold", 1, 100, [1])],  # impact ≈ 0.01
        project_root=tmp_project,
    )
    report = _scan_report(tmp_project, [big_cold, small_hot])
    worklist = _build_worklist(report, "impact")
    assert [w.fc.qualname for w in worklist] == ["hot", "cold"]


def test_build_worklist_priority_coverage(tmp_project: Path):
    low = _make_file_coverage(
        "low.py",
        covered=1,
        total=10,  # 10%
        funcs=[("f", 1, 3, [1, 2, 3])],
        project_root=tmp_project,
    )
    high = _make_file_coverage(
        "high.py",
        covered=8,
        total=10,  # 80%
        funcs=[("g", 1, 3, [1, 2, 3])],
        project_root=tmp_project,
    )
    report = _scan_report(tmp_project, [high, low])
    worklist = _build_worklist(report, "coverage")
    assert [w.file.rel_path for w in worklist] == ["low.py", "high.py"]


def test_build_worklist_priority_size(tmp_project: Path):
    big = _make_file_coverage(
        "big.py",
        covered=0,
        total=10,
        funcs=[("large", 1, 20, [1])],
        project_root=tmp_project,
    )
    small = _make_file_coverage(
        "small.py",
        covered=0,
        total=10,
        funcs=[("tiny", 1, 3, [1])],
        project_root=tmp_project,
    )
    report = _scan_report(tmp_project, [small, big])
    worklist = _build_worklist(report, "size")
    assert [w.fc.qualname for w in worklist] == ["large", "tiny"]


def test_build_worklist_stable_tiebreak_alphabetical(tmp_project: Path):
    # Two functions in the same file with the same impact → qualname alpha
    fc = _make_file_coverage(
        "a.py",
        covered=0,
        total=6,
        funcs=[
            ("zebra", 1, 3, [1, 2, 3]),
            ("alpha", 4, 6, [4, 5, 6]),
        ],
        project_root=tmp_project,
    )
    report = _scan_report(tmp_project, [fc])
    worklist = _build_worklist(report, "impact")
    assert [w.fc.qualname for w in worklist] == ["alpha", "zebra"]


def test_build_worklist_unknown_priority_raises(tmp_project: Path):
    report = _scan_report(tmp_project, [])
    with pytest.raises(ValueError):
        _build_worklist(report, "nope")


# ---------------------------------------------------------------------------
# _process_auto
# ---------------------------------------------------------------------------


def test_process_auto_apply_when_accepted(tmp_project: Path):
    tests_dir = tmp_project / "tests"
    tests_dir.mkdir()
    src = tmp_project / "src" / "demo"
    src.mkdir(parents=True)
    file_path = src / "mod.py"
    file_path.write_text("def sub():\n    return 1\n", encoding="utf-8")
    uf = _make_uf("sub", file_path.resolve())
    suggestion = _fake_suggestion(uf, accepted=True)
    log = SpyLog()

    out = _process_auto(
        suggestion=suggestion,
        func=uf,
        project_root=tmp_project,
        config=_config(),
        test_dirs=[tests_dir],
        console=Console(record=True, force_terminal=False, width=120),
        session_log=log,
        dry_run=False,
    )
    assert out.action == "apply"
    assert out.applied_file is not None
    assert out.applied_file.path.exists()
    # user_action(apply) event recorded
    apply_events = [
        p for e, p in log.records if e == EVENT_USER_ACTION and p["action"] == "apply"
    ]
    assert len(apply_events) == 1


def test_process_auto_skip_when_discarded(tmp_project: Path):
    tests_dir = tmp_project / "tests"
    tests_dir.mkdir()
    file_path = tmp_project / "src" / "mod.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("def x():\n    return 1\n", encoding="utf-8")
    uf = _make_uf("x", file_path.resolve())
    suggestion = _fake_suggestion(uf, accepted=False)
    log = SpyLog()
    console = Console(record=True, force_terminal=False, width=120)

    out = _process_auto(
        suggestion=suggestion,
        func=uf,
        project_root=tmp_project,
        config=_config(),
        test_dirs=[tests_dir],
        console=console,
        session_log=log,
        dry_run=False,
    )
    assert out.action == "auto_skip"
    assert out.applied_file is None

    # user_action(auto_skip) with reason == no_accepted_cases (R2)
    skip_events = [
        p for e, p in log.records if e == EVENT_USER_ACTION and p["action"] == "auto_skip"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["reason"] == "no_accepted_cases"

    # Console warning line printed (R2)
    text = console.export_text()
    assert "discarded" in text


# ---------------------------------------------------------------------------
# run_backfill helpers
# ---------------------------------------------------------------------------


def _patch_scan(
    monkeypatch,
    tmp_project: Path,
    funcs: list[tuple[str, int, int, list[int]]],
    *,
    total_lines: int | None = None,
    covered_lines: int | None = None,
) -> None:
    """Monkeypatch ``backfill.scan_project`` to return a preset ScanReport.

    ``total_lines`` / ``covered_lines`` override the report's aggregates so
    tests can simulate reports that include fully-covered files outside
    the worklist (PR #12 review regression).
    """
    fc = _make_file_coverage(
        "src/demo/mod.py",
        covered=0,
        total=len(funcs) * 3,
        funcs=funcs,
        project_root=tmp_project,
    )
    report = _scan_report(
        tmp_project, [fc], total_lines=total_lines, covered_lines=covered_lines
    )

    def fake_scan_project(project_root, config, **kwargs):
        _ = (project_root, config, kwargs)
        return report

    monkeypatch.setattr(backfill_mod, "scan_project", fake_scan_project)


def _patch_pipeline_process_function(monkeypatch, *, accepted: bool = True) -> dict:
    """Return a call-count dict; ``pipeline.process_function`` is replaced with
    a fake that returns a stubbed FunctionSuggestion for each call."""
    calls: dict[str, int] = {"n": 0}

    def fake_process(**kwargs):
        calls["n"] += 1
        return _fake_suggestion(kwargs["func"], accepted=accepted)

    from testgap import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "process_function", fake_process)
    return calls


def _patch_prepare_test_dirs(monkeypatch, tmp_project: Path) -> Path:
    """Ensure prepare_test_dirs returns an existing directory."""
    tests_dir = tmp_project / "tests"
    tests_dir.mkdir(exist_ok=True)

    def fake_prepare(config, project_root):
        _ = (config, project_root)
        return [tests_dir]

    from testgap import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "prepare_test_dirs", fake_prepare)
    return tests_dir


# ---------------------------------------------------------------------------
# run_backfill flow tests
# ---------------------------------------------------------------------------


def test_run_backfill_dry_run_does_not_apply(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("f", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    # Spy on _apply_to_disk to make sure dry-run skips it.
    from testgap.ui import interactive as inter_mod
    calls: list[str] = []
    orig = inter_mod._apply_to_disk

    def spy_apply(*args, **kwargs):
        calls.append("apply")
        return orig(*args, **kwargs)

    monkeypatch.setattr(inter_mod, "_apply_to_disk", spy_apply)
    monkeypatch.setattr(backfill_mod, "_apply_to_disk", spy_apply)

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,  # not called — process_function is stubbed
        auto=True,
        dry_run=True,
        session_log=log,
    )
    assert calls == []
    assert outcome.dry_run is True
    assert outcome.functions_processed == 1
    # dry-run counts as skip
    assert outcome.functions_skipped == 1


def test_run_backfill_auto_apply_appends_applied(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("f", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    assert outcome.functions_accepted == 1
    assert len(outcome.applied) == 1


def test_run_backfill_auto_discard_records_both_events(tmp_project: Path, monkeypatch):
    """R2: discarded auto function → user_action(auto_skip) + backfill_progress(auto_skip)."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("f", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=False)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    ua_events = [p for e, p in log.records if e == EVENT_USER_ACTION and p["action"] == "auto_skip"]
    bp_events = [
        p for e, p in log.records
        if e == EVENT_BACKFILL_PROGRESS and p["action"] == "auto_skip"
    ]
    assert len(ua_events) == 1
    assert ua_events[0]["reason"] == "no_accepted_cases"
    assert len(bp_events) == 1
    assert outcome.functions_skipped == 1
    assert outcome.discarded_qualnames == ["f"]


def test_run_backfill_target_coverage_stops(tmp_project: Path, monkeypatch):
    """target_coverage is a heuristic based on covered_lines_est."""
    # 3 functions, each with 3 uncovered lines, file total 9 lines
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
        ("c", 7, 9, [7, 8, 9]),
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        target_coverage=40.0,  # 40% target → stops after 4/9 ≈ 44%
        session_log=log,
    )
    assert outcome.quit_reason == "target_reached"
    assert outcome.functions_processed < 3


def test_run_backfill_max_functions_caps_worklist(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
        ("c", 7, 9, [7, 8, 9]),
    ])
    calls = _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        max_functions=1,
        session_log=log,
    )
    assert calls["n"] == 1
    assert outcome.functions_processed == 1
    assert outcome.quit_reason == "max_functions"


def test_run_backfill_provider_unhealthy_stops(tmp_project: Path, monkeypatch):
    """Two consecutive LLM failures without accepted → provider_unhealthy."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
        ("c", 7, 9, [7, 8, 9]),
    ])

    from testgap import pipeline as pipeline_mod

    def failing_process(**kwargs):
        return _fake_suggestion(kwargs["func"], accepted=False, llm_failure=True)

    monkeypatch.setattr(pipeline_mod, "process_function", failing_process)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    assert outcome.provider_unhealthy is True
    assert outcome.quit_reason == "provider_unhealthy"


def test_run_backfill_records_start_end_events(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("f", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    events = {e for e, _p in log.records}
    assert EVENT_BACKFILL_START in events
    assert EVENT_BACKFILL_PROGRESS in events
    assert EVENT_BACKFILL_END in events


def test_run_backfill_uses_default_prompt_fn_when_none(tmp_project: Path, monkeypatch):
    """R6: prompt_fn=None / editor_fn=None → default_prompt_fn / default_editor_fn used."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("f", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    prompt_called: list[str] = []
    editor_called: list[str] = []

    from testgap.ui import interactive as inter_mod

    def spy_prompt(*args, **kwargs):
        prompt_called.append("!")
        return "s"  # skip to exit loop cleanly

    def spy_editor(path):
        editor_called.append(str(path))

    monkeypatch.setattr(inter_mod, "default_prompt_fn", spy_prompt)
    monkeypatch.setattr(inter_mod, "default_editor_fn", spy_editor)
    monkeypatch.setattr(backfill_mod, "default_prompt_fn", spy_prompt)
    monkeypatch.setattr(backfill_mod, "default_editor_fn", spy_editor)

    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=False,  # interactive so prompt_fn is invoked
        prompt_fn=None,
        editor_fn=None,
        session_log=log,
    )
    assert prompt_called, "default_prompt_fn was not used as fallback"


# ---------------------------------------------------------------------------
# _review_one integration in interactive mode (P4)
# ---------------------------------------------------------------------------


def _patch_review_one(monkeypatch, outcomes: list[_OneOutcome]) -> dict:
    """Replace ``backfill._review_one`` with a scripted sequence.

    Records suggestion.function.qualname per call so callers can assert order.
    """
    seen: dict[str, list] = {"funcs": [], "n": 0}

    def fake_review_one(*, func, suggestion, **kwargs):
        idx = seen["n"]
        seen["n"] += 1
        seen["funcs"].append(func.qualname)
        return outcomes[idx]

    monkeypatch.setattr(backfill_mod, "_review_one", fake_review_one)
    return seen


def _patch_format_suggestion_block(monkeypatch) -> list[tuple[int, int]]:
    """Return a list capturing (idx, total) each call."""
    calls: list[tuple[int, int]] = []

    def fake_fmt(console, idx, total, suggestion):
        calls.append((idx, total))

    monkeypatch.setattr(backfill_mod, "_format_suggestion_block", fake_fmt)
    return calls


def test_run_backfill_user_quit_breaks_loop(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)
    _patch_format_suggestion_block(monkeypatch)
    seen = _patch_review_one(monkeypatch, [
        _OneOutcome(action="quit", applied_file=None),
    ])

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=False,
        session_log=log,
    )
    assert outcome.quit_reason == "user_quit"
    # Only 1 review call → 2nd function was skipped
    assert seen["n"] == 1
    # backfill_progress emitted for the quit function (skip action)
    bp = [p for e, p in log.records if e == EVENT_BACKFILL_PROGRESS]
    assert bp[-1]["action"] == "skip"


def test_run_backfill_apply_appends_applied_file(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)
    _patch_format_suggestion_block(monkeypatch)
    af = AppliedFile(function_qualname="a", path=tmp_project / "tests" / "test_x.py", test_count=1)
    _patch_review_one(monkeypatch, [
        _OneOutcome(action="apply", applied_file=af),
    ])

    log = SpyLog()
    outcome = run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=False,
        session_log=log,
    )
    assert len(outcome.applied) == 1
    assert outcome.functions_accepted == 1


def test_run_backfill_calls_format_suggestion_block_with_idx(tmp_project: Path, monkeypatch):
    """P4/R7: _format_suggestion_block called with idx+1 (1-based) prior to _review_one."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)
    fmt_calls = _patch_format_suggestion_block(monkeypatch)
    _patch_review_one(monkeypatch, [
        _OneOutcome(action="skip", applied_file=None),
        _OneOutcome(action="skip", applied_file=None),
    ])

    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=False,
        session_log=log,
    )
    assert fmt_calls == [(1, 2), (2, 2)]


# ---------------------------------------------------------------------------
# Progress ↔ interactive branching (R7)
# ---------------------------------------------------------------------------


def test_run_backfill_interactive_does_not_use_progress(tmp_project: Path, monkeypatch):
    """R7: Interactive backfill uses console.print header, no rich Progress."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)
    _patch_format_suggestion_block(monkeypatch)
    _patch_review_one(monkeypatch, [
        _OneOutcome(action="skip", applied_file=None),
    ])

    progress_calls: list[str] = []

    def spy_progress(console):
        progress_calls.append("!")
        return backfill_mod._make_progress.__wrapped__(console)  # type: ignore

    # If _make_progress is called in interactive mode, we'd see progress_calls > 0.
    # Instead we monkeypatch it to raise so any accidental call surfaces.
    def raiser(console):
        raise AssertionError("_make_progress must not be called in interactive mode")

    monkeypatch.setattr(backfill_mod, "_make_progress", raiser)

    console = Console(record=True, force_terminal=False, width=120)
    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=False,
        console=console,
        session_log=log,
    )
    text = console.export_text()
    assert "[1/1] Processing" in text


def test_run_backfill_auto_uses_progress(tmp_project: Path, monkeypatch):
    """R7: --auto mode wraps the loop in rich Progress."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3])
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    calls: list[str] = []
    orig_make = backfill_mod._make_progress

    def spy_make(console):
        calls.append("!")
        return orig_make(console)

    monkeypatch.setattr(backfill_mod, "_make_progress", spy_make)

    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    assert calls == ["!"]


# ---------------------------------------------------------------------------
# backfill_progress action schema (R8)
# ---------------------------------------------------------------------------


_VALID_ACTIONS = {"apply", "skip", "auto_skip", "regenerate", "error", "edit"}


def test_backfill_progress_action_schema(tmp_project: Path, monkeypatch):
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=False)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    bp = [p for e, p in log.records if e == EVENT_BACKFILL_PROGRESS]
    for payload in bp:
        assert payload["action"] in _VALID_ACTIONS


def test_backfill_progress_recorded_per_function(tmp_project: Path, monkeypatch):
    """R8: worklist N → exactly N backfill_progress events."""
    _patch_scan(tmp_project=tmp_project, monkeypatch=monkeypatch, funcs=[
        ("a", 1, 3, [1, 2, 3]),
        ("b", 4, 6, [4, 5, 6]),
        ("c", 7, 9, [7, 8, 9]),
    ])
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    log = SpyLog()
    run_backfill(
        project_root=tmp_project,
        config=_config(),
        llm_client=None,
        auto=True,
        session_log=log,
    )
    bp = [p for e, p in log.records if e == EVENT_BACKFILL_PROGRESS]
    assert len(bp) == 3


# ---------------------------------------------------------------------------
# PR #12 review regression (gemini HIGH): totals come from scan_report,
# not from a worklist-only placeholder. Fully-covered files must appear
# in the denominator or projected coverage exceeds 100%.
# ---------------------------------------------------------------------------


def test_placeholder_helper_removed():
    """The old ``_scan_total_lines_placeholder`` helper was deleted. Import
    must fail so we notice if anyone reintroduces it under the same name.
    """
    from testgap import backfill as bf

    assert not hasattr(bf, "_scan_total_lines_placeholder"), (
        "the worklist-only placeholder must remain removed"
    )


def test_run_loop_uses_scan_totals(tmp_project: Path, monkeypatch):
    """The projected coverage denominator equals ``scan_report.total_lines``.

    Set up a scan where the worklist only has small files but the report
    total is large. Before the fix, the loop used a worklist-only sum and
    over-projected coverage. Now the report's totals are threaded through
    ``_run_loop`` and any projection ends up bounded by them.
    """
    from unittest.mock import patch

    _patch_scan(
        tmp_project=tmp_project,
        monkeypatch=monkeypatch,
        funcs=[("a", 1, 3, [1, 2, 3])],
        total_lines=1000,       # includes fully-covered files
        covered_lines=500,
    )
    _patch_pipeline_process_function(monkeypatch, accepted=True)
    _patch_prepare_test_dirs(monkeypatch, tmp_project)

    seen: dict[str, int] = {}
    real_run_loop = __import__("testgap.backfill", fromlist=["_run_loop"])._run_loop

    def spy(*args, **kwargs):
        seen["total_lines"] = kwargs.get("total_lines")
        seen["covered_lines"] = kwargs.get("covered_lines")
        return real_run_loop(*args, **kwargs)

    with patch("testgap.backfill._run_loop", spy):
        run_backfill(
            project_root=tmp_project,
            config=_config(),
            llm_client=None,
            auto=True,
            session_log=SpyLog(),
        )

    assert seen["total_lines"] == 1000
    assert seen["covered_lines"] == 500
