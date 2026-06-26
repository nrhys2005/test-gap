"""Unit tests for `testgap.ui.interactive.run_review_session` and helpers.

All external boundaries (LLM, prompt, editor, console) are injectable. We reuse
the `demo_project` fixture pattern from `test_pipeline.py` for a realistic
git+coverage setup, and the `_queued_completion` helper for deterministic LLM
responses across multiple calls.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from rich.console import Console

from testgap.config.schema import (
    GenerationConfig,
    LLMConfig,
    ProjectConfig,
    TestGapConfig,
)
from testgap.coverage import UncoveredFunction
from testgap.generator import LLMClient
from testgap.ui import run_review_session
from testgap.ui.interactive import (
    AppliedFile,
    ReviewOutcome,
    _apply_to_disk,
    _ensure_testgap_conftest,
    _estimate_next_call_cost,
    _resolve_target_path,
)

# Reuse helpers from test_pipeline.py (same package: tests/)
from tests.test_pipeline import (  # noqa: E402
    _payload,
    _queued_completion,
    _test_entry,
    demo_project,  # noqa: F401 — fixture re-export
)

# ---------------------------------------------------------------------------
# helpers (queued prompt + scripted editor)
# ---------------------------------------------------------------------------


def make_prompt_queue(choices: list[str]) -> Callable[..., str]:
    """Build a prompt_fn that returns scripted answers in order."""
    it = iter(choices)

    def fn(message: str, *, choices: list[str], default: str) -> str:
        try:
            return next(it)
        except StopIteration as e:
            raise AssertionError(
                f"prompt_fn called more times than scripted "
                f"(message={message!r}, choices={choices}, default={default!r})"
            ) from e

    return fn


def make_editor_writer(new_text: str | None) -> Callable[[Path], None]:
    """Build an editor_fn that overwrites the temp file (or leaves it if None)."""

    def fn(path: Path) -> None:
        if new_text is not None:
            path.write_text(new_text, encoding="utf-8")

    return fn


def _config(max_cost: float = 1.0) -> TestGapConfig:
    return TestGapConfig(
        project=ProjectConfig(source_paths=["src/"], test_paths=["tests/"]),
        llm=LLMConfig(model="fake/model", max_cost_per_run=max_cost),
        generation=GenerationConfig(test_timeout_seconds=30),
    )


def _pass_payload() -> str:
    return _payload(
        [
            _test_entry(
                "test_sub_returns_difference",
                "from demo.calc import sub\n    assert sub(5, 3) == 2",
            )
        ]
    )


def _partial_payload() -> str:
    return _payload(
        [
            _test_entry(
                "test_sub_pass",
                "from demo.calc import sub\n    assert sub(5, 3) == 2",
            ),
            _test_entry("test_sub_fail", "assert False, 'intentional'"),
        ]
    )


def _all_fail_payload() -> str:
    return _payload(
        [
            _test_entry("test_sub_only_fail", "assert False, 'always'"),
        ]
    )


# ---------------------------------------------------------------------------
# 1. apply / skip / quit basics
# ---------------------------------------------------------------------------


def test_apply_writes_file_and_returns_outcome(demo_project: Path):
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    console = Console(record=True, force_terminal=False, width=120)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["a"]),
        editor_fn=make_editor_writer(None),
    )

    assert outcome.processed == 1
    assert outcome.quit_early is False
    assert len(outcome.applied) == 1
    af = outcome.applied[0]
    assert af.path.exists()
    # mirrored under tests/demo/test_calc.py (src/ stripped)
    rel = af.path.relative_to(demo_project)
    assert rel.parts[0] == "tests"
    assert rel.name == "test_calc.py"
    # content matches generated.to_source()
    assert "test_sub_returns_difference" in af.path.read_text(encoding="utf-8")


def test_skip_records_skipped(demo_project: Path):
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["s"]),
        editor_fn=make_editor_writer(None),
    )

    assert outcome.processed == 1
    assert not outcome.applied
    assert len(outcome.skipped) == 1
    # qualname should be 'sub' for the demo project
    assert outcome.skipped[0] == "sub"


def test_quit_exits_loop_with_remaining_functions(demo_project: Path, monkeypatch):
    """When only one function is in the diff, quit-after-first sets pending=0.

    To simulate multiple pending functions we monkeypatch `discover_targets` to
    return three fakes; the loop processes the first and quits.
    """
    fn, _calls = _queued_completion([_pass_payload(), _pass_payload(), _pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    from testgap import pipeline as pipeline_mod

    # Build 3 stub UncoveredFunctions all pointing at the same real file/qualname
    real_funcs, meta = pipeline_mod.discover_targets(
        project_root=demo_project,
        config=_config(),
        base_ref="main",
        head_ref="HEAD",
        max_functions=None,
    )
    assert len(real_funcs) == 1
    base = real_funcs[0]
    triple = [
        UncoveredFunction(
            file=base.file,
            qualname=f"sub_{i}",
            start_line=base.start_line,
            end_line=base.end_line,
            source=base.source,
            uncovered_lines=base.uncovered_lines,
            has_branch=base.has_branch,
        )
        for i in range(3)
    ]
    # process_function inspects qualname; the same file's `sub` is the only real
    # function in src/demo/calc.py so set them all to `sub` for validation to pass.
    for t in triple:
        t.qualname = "sub"

    monkeypatch.setattr(
        pipeline_mod, "discover_targets", lambda **kw: (triple, meta)
    )

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["q"]),
        editor_fn=make_editor_writer(None),
    )

    assert outcome.quit_early is True
    # User quit at 1st of 3 → processed counts the one shown, pending = remaining.
    assert outcome.processed == 1
    assert outcome.pending == 2


# ---------------------------------------------------------------------------
# 2. regenerate
# ---------------------------------------------------------------------------


def test_regenerate_then_apply(demo_project: Path):
    """r -> a where the second LLM call passes fully."""
    fn, calls = _queued_completion([_partial_payload(), _pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["r", "a"]),
        editor_fn=make_editor_writer(None),
    )

    # 1st call (partial) -> retry (fail-fail merged) -> r -> 2nd full call
    # process_function may make up to 2 calls per invocation;
    # partial first round triggers retry inside the call, so total calls >= 3
    assert calls["n"] >= 2
    assert outcome.cost_total > 0
    assert len(outcome.applied) == 1


def test_regenerate_twice_then_apply(demo_project: Path):
    """r -> r -> a, tracker.spent accumulates across 3+ LLM calls."""
    fn, calls = _queued_completion(
        [_partial_payload(), _partial_payload(), _pass_payload()]
    )
    client = LLMClient(model="fake/model", completion_fn=fn)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(max_cost=5.0),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["r", "r", "a"]),
        editor_fn=make_editor_writer(None),
    )

    assert len(outcome.applied) == 1
    # 3 user-visible LLM calls; pipeline.process_function may internally do 1
    # extra retry for partial passes so total>=3.
    assert calls["n"] >= 3
    # Each LLM response is priced at $0.001; spent should be at least 3*0.001.
    assert outcome.cost_total >= 0.003


def test_regenerate_blocked_by_budget(demo_project: Path):
    """Budget guard fires when remaining < estimated cost on r."""
    # Choose budget so 1st call (response_cost=0.001) succeeds, but the budget
    # guard's `would_exceed` (avg per-call 0.001) trips on the regenerate
    # attempt because there's no headroom left.
    fn, calls = _queued_completion(
        [_pass_payload()],
        hidden_params=[{"response_cost": 0.001}],
        usages=[(100, 80)],
    )
    client = LLMClient(model="fake/model", completion_fn=fn)
    cfg = _config(max_cost=0.0015)

    outcome = run_review_session(
        project_root=demo_project,
        config=cfg,
        llm_client=client,
        base_ref="main",
        # r should be blocked, then s to exit cleanly
        prompt_fn=make_prompt_queue(["r", "s"]),
        editor_fn=make_editor_writer(None),
    )

    assert calls["n"] == 1  # regenerate never executed
    assert outcome.skipped == ["sub"]
    assert not outcome.applied


def test_regenerate_failure_preserves_previous_current(
    demo_project: Path, monkeypatch
):
    """When regen returns error+accepted=0, previous `current` is preserved.

    We monkeypatch `pipeline.process_function` so the FIRST call returns a real
    partial-pass suggestion, and the SECOND (regen) returns one with an error
    and no accepted cases. The previous result must survive so `a` can apply it.
    """
    from testgap import pipeline as pipeline_mod
    from testgap.pipeline import FunctionSuggestion

    real_pf = pipeline_mod.process_function
    counter = {"n": 0}

    def stub_process_function(**kwargs):
        counter["n"] += 1
        if counter["n"] == 1:
            return real_pf(**kwargs)
        # 2nd call (regen): return synthetic error-with-no-accepted suggestion
        return FunctionSuggestion(
            function=kwargs["func"],
            generated=None,
            validator_result=None,
            cost_usd=0.0,
            error="budget: simulated exhaustion",
            attempts=0,
        )

    monkeypatch.setattr(pipeline_mod, "process_function", stub_process_function)
    # Also patch the symbol imported inside ui.interactive's module namespace.
    from testgap.ui import interactive as ui_mod

    monkeypatch.setattr(ui_mod.pipeline, "process_function", stub_process_function)

    fn, _calls = _queued_completion([_partial_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    console = Console(record=True, force_terminal=False, width=120)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(max_cost=1.0),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["r", "a"]),
        editor_fn=make_editor_writer(None),
    )

    text = console.export_text()
    assert "regeneration failed" in text
    # The previous partial-pass suggestion survives → apply succeeds.
    assert len(outcome.applied) == 1
    body = outcome.applied[0].path.read_text(encoding="utf-8")
    # The 1st-round passing case must still be present.
    assert "test_sub_pass" in body


# ---------------------------------------------------------------------------
# 3. edit
# ---------------------------------------------------------------------------


def test_edit_no_changes_keeps_last_result(demo_project: Path):
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["e", "a"]),
        editor_fn=make_editor_writer(None),  # leave file untouched
    )

    assert len(outcome.applied) == 1
    body = outcome.applied[0].path.read_text(encoding="utf-8")
    # Original generated text preserved.
    assert "test_sub_returns_difference" in body


def test_edit_modifies_and_revalidates_pass(demo_project: Path):
    fn, _calls = _queued_completion([_all_fail_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    new_code = "def test_edited_pass():\n    assert 1 + 1 == 2\n"

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["e", "a"]),
        editor_fn=make_editor_writer(new_code),
    )

    assert len(outcome.applied) == 1
    body = outcome.applied[0].path.read_text(encoding="utf-8")
    assert "test_edited_pass" in body


def test_edit_modifies_and_fails_pytest(demo_project: Path):
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    bad_code = "def test_edited_fail():\n    assert False\n"

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        # After edit (all fail), [a] should be refused → [s].
        prompt_fn=make_prompt_queue(["e", "a", "s"]),
        editor_fn=make_editor_writer(bad_code),
    )

    assert not outcome.applied
    assert outcome.skipped == ["sub"]


def test_keyboard_interrupt_during_edit(demo_project: Path):
    """editor_fn raising KeyboardInterrupt does NOT bubble up to outer loop."""
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    def raising_editor(path: Path) -> None:
        raise KeyboardInterrupt()

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        # e (cancelled) → a (still applies prior suggestion)
        prompt_fn=make_prompt_queue(["e", "a"]),
        editor_fn=raising_editor,
    )

    assert outcome.quit_early is False
    assert len(outcome.applied) == 1


def test_editor_executable_not_found(demo_project: Path):
    """editor_fn raising FileNotFoundError shows message and keeps prompt loop."""
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    console = Console(record=True, force_terminal=False, width=120)

    def missing_editor(path: Path) -> None:
        raise FileNotFoundError("vi")

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["e", "a"]),
        editor_fn=missing_editor,
    )

    text = console.export_text()
    assert "editor failed" in text
    assert len(outcome.applied) == 1


# ---------------------------------------------------------------------------
# 4. apply edge cases
# ---------------------------------------------------------------------------


def test_apply_refused_when_no_accepted_cases(demo_project: Path):
    fn, _calls = _queued_completion([_all_fail_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    console = Console(record=True, force_terminal=False, width=120)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["a", "s"]),
        editor_fn=make_editor_writer(None),
    )

    text = console.export_text()
    assert "nothing to apply" in text
    assert not outcome.applied


def test_apply_refused_on_environment_error(demo_project: Path, monkeypatch):
    """When the suggestion has accepted_cases but environment_error, [a] is refused.

    We monkeypatch process_function to return a fake suggestion with one accepted
    case AND environment_error set so the env-error guard fires before "nothing
    to apply".
    """
    from testgap import pipeline as pipeline_mod
    from testgap.generator import GeneratedTest, GeneratedTestSet
    from testgap.pipeline import FunctionSuggestion
    from testgap.validator.result import TestCaseResult, TestOutcome, ValidatorResult

    def stub_process_function(**kwargs):
        func = kwargs["func"]
        case = TestCaseResult(name="test_x", outcome=TestOutcome.PASS)
        vr = ValidatorResult(
            cases=[case],
            duration_seconds=0.0,
            raw_stdout="",
            raw_stderr="",
            exit_code=2,
            environment_error="pytest collection blew up",
        )
        return FunctionSuggestion(
            function=func,
            generated=GeneratedTestSet(
                imports=[],
                tests=[GeneratedTest(name="test_x", purpose="x", code="def test_x():\n    pass")],
            ),
            validator_result=vr,
            cost_usd=0.001,
            accepted_cases=[case],
            attempts=1,
        )

    monkeypatch.setattr(pipeline_mod, "process_function", stub_process_function)
    from testgap.ui import interactive as ui_mod

    monkeypatch.setattr(ui_mod.pipeline, "process_function", stub_process_function)

    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)
    console = Console(record=True, force_terminal=False, width=120)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["a", "s"]),
        editor_fn=make_editor_writer(None),
    )

    text = console.export_text()
    assert "environment" in text.lower()
    assert not outcome.applied


def test_apply_avoids_overwriting_existing_file(demo_project: Path):
    """Pre-existing `tests/demo/test_calc.py` → secondary path picked."""
    primary = demo_project / "tests" / "demo" / "test_calc.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("# pre-existing\n", encoding="utf-8")

    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["a"]),
        editor_fn=make_editor_writer(None),
    )

    assert len(outcome.applied) == 1
    af = outcome.applied[0]
    assert af.path != primary
    assert af.path.name == "test_calc_sub.py"
    # merge_hint must point at the primary
    assert af.merge_hint == primary
    # original file untouched
    assert primary.read_text(encoding="utf-8") == "# pre-existing\n"


def test_apply_third_collision_gets_numeric_suffix(demo_project: Path):
    base_dir = demo_project / "tests" / "demo"
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "test_calc.py").write_text("# 1\n", encoding="utf-8")
    (base_dir / "test_calc_sub.py").write_text("# 2\n", encoding="utf-8")

    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=make_prompt_queue(["a"]),
        editor_fn=make_editor_writer(None),
    )

    af = outcome.applied[0]
    assert af.path.name == "test_calc_sub_2.py"
    assert af.merge_hint == base_dir / "test_calc.py"


# ---------------------------------------------------------------------------
# 5. summary / cost output
# ---------------------------------------------------------------------------


def test_apply_then_quit_summary(demo_project: Path, monkeypatch):
    fn, _calls = _queued_completion([_pass_payload(), _pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    from testgap import pipeline as pipeline_mod

    real_funcs, meta = pipeline_mod.discover_targets(
        project_root=demo_project,
        config=_config(),
        base_ref="main",
        head_ref="HEAD",
        max_functions=None,
    )
    base = real_funcs[0]
    pair = [
        UncoveredFunction(
            file=base.file,
            qualname="sub",
            start_line=base.start_line,
            end_line=base.end_line,
            source=base.source,
            uncovered_lines=base.uncovered_lines,
            has_branch=base.has_branch,
        )
        for _ in range(2)
    ]
    monkeypatch.setattr(
        pipeline_mod, "discover_targets", lambda **kw: (pair, meta)
    )

    console = Console(record=True, force_terminal=False, width=120)
    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["a", "q"]),
        editor_fn=make_editor_writer(None),
    )

    text = console.export_text()
    assert "applied: 1" in text
    assert outcome.quit_early is True
    # Both functions were "processed" (LLM call + prompt shown); the 2nd hit q.
    assert outcome.processed == 2
    assert outcome.pending == 0
    assert outcome.cost_total > 0
    # applied path file name shows up in summary (full path may be line-wrapped)
    assert outcome.applied[0].path.name in text


def test_summary_lists_applied_paths_and_cost(demo_project: Path, monkeypatch):
    fn, _calls = _queued_completion([_pass_payload(), _pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    from testgap import pipeline as pipeline_mod

    real_funcs, meta = pipeline_mod.discover_targets(
        project_root=demo_project,
        config=_config(),
        base_ref="main",
        head_ref="HEAD",
        max_functions=None,
    )
    base = real_funcs[0]
    pair = [
        UncoveredFunction(
            file=base.file,
            qualname="sub",
            start_line=base.start_line,
            end_line=base.end_line,
            source=base.source,
            uncovered_lines=base.uncovered_lines,
            has_branch=base.has_branch,
        )
        for _ in range(2)
    ]
    monkeypatch.setattr(
        pipeline_mod, "discover_targets", lambda **kw: (pair, meta)
    )

    console = Console(record=True, force_terminal=False, width=120)
    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        console=console,
        prompt_fn=make_prompt_queue(["a", "a"]),
        editor_fn=make_editor_writer(None),
    )

    text = console.export_text()
    assert len(outcome.applied) == 2
    for af in outcome.applied:
        # Full path may be wrapped; assert file name (and parent dir) appears.
        assert af.path.name in text
    assert "Total cost: $" in text


# ---------------------------------------------------------------------------
# 6. KeyboardInterrupt at outer prompt
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_graceful_exit(demo_project: Path):
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    def boom(message, *, choices, default):
        raise KeyboardInterrupt()

    outcome = run_review_session(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        prompt_fn=boom,
        editor_fn=make_editor_writer(None),
    )

    assert outcome.quit_early is True
    assert outcome.cost_total > 0  # 1st-round LLM call ran before prompt
    assert outcome.processed == 0
    assert outcome.pending == 1


# ---------------------------------------------------------------------------
# 7. conftest helper (unit)
# ---------------------------------------------------------------------------


def test_ensure_testgap_conftest_creates_file(tmp_path: Path):
    scratch = tmp_path / ".testgap"
    scratch.mkdir()
    _ensure_testgap_conftest(scratch)
    body = (scratch / "conftest.py").read_text(encoding="utf-8")
    assert 'collect_ignore_glob = ["*"]' in body


def test_ensure_testgap_conftest_preserves_custom_warns(tmp_path: Path):
    scratch = tmp_path / ".testgap"
    scratch.mkdir()
    custom = "# my custom conftest\n"
    (scratch / "conftest.py").write_text(custom, encoding="utf-8")
    console = Console(record=True, force_terminal=False, width=120)
    _ensure_testgap_conftest(scratch, console=console)
    # original content preserved
    assert (scratch / "conftest.py").read_text(encoding="utf-8") == custom
    text = console.export_text()
    assert "conftest.py exists" in text


# ---------------------------------------------------------------------------
# 8. pure unit tests for path resolution and cost estimate
# ---------------------------------------------------------------------------


def test_resolve_target_path_primary(tmp_path: Path):
    project_root = tmp_path
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    file = tmp_path / "src" / "app" / "user.py"
    file.parent.mkdir(parents=True)
    file.write_text("def f(): pass\n", encoding="utf-8")
    func = UncoveredFunction(
        file=file,
        qualname="f",
        start_line=1,
        end_line=1,
        source="def f(): pass",
    )
    chosen, hint = _resolve_target_path(test_dir, func, project_root, ["src/"])
    assert hint is None
    assert chosen == test_dir / "app" / "test_user.py"


def test_resolve_target_path_outside_source_root(tmp_path: Path):
    project_root = tmp_path
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    file = tmp_path / "lib" / "helpers.py"
    file.parent.mkdir(parents=True)
    file.write_text("def g(): pass\n", encoding="utf-8")
    func = UncoveredFunction(
        file=file,
        qualname="g",
        start_line=1,
        end_line=1,
        source="def g(): pass",
    )
    chosen, hint = _resolve_target_path(test_dir, func, project_root, ["src/"])
    # rel path is `lib/helpers.py`; src/ does not match so parts kept as-is.
    assert hint is None
    assert chosen == test_dir / "lib" / "test_helpers.py"


def test_estimate_next_call_cost_avg():
    from testgap.pipeline import FunctionSuggestion

    func = UncoveredFunction(
        file=Path("x.py"), qualname="x", start_line=1, end_line=1, source=""
    )
    s = FunctionSuggestion(function=func, cost_usd=0.004, attempts=2)
    tracker_stub = object()
    assert _estimate_next_call_cost(s, tracker_stub) == pytest.approx(0.002)


def test_estimate_next_call_cost_zero_when_no_cost():
    from testgap.pipeline import FunctionSuggestion

    func = UncoveredFunction(
        file=Path("x.py"), qualname="x", start_line=1, end_line=1, source=""
    )
    s = FunctionSuggestion(function=func, cost_usd=0.0, attempts=1)
    assert _estimate_next_call_cost(s, object()) == 0.0


# ---------------------------------------------------------------------------
# 9. AppliedFile + ReviewOutcome shape sanity
# ---------------------------------------------------------------------------


def test_review_outcome_defaults():
    outcome = ReviewOutcome()
    assert outcome.applied == []
    assert outcome.skipped == []
    assert outcome.quit_early is False
    assert outcome.cost_total == 0.0
    assert outcome.processed == 0
    assert outcome.pending == 0


def test_applied_file_shape(tmp_path: Path):
    af = AppliedFile(
        function_qualname="m.f",
        path=tmp_path / "test_f.py",
        test_count=3,
        merge_hint=None,
    )
    assert af.test_count == 3


# ---------------------------------------------------------------------------
# 10. direct _apply_to_disk (no LLM)
# ---------------------------------------------------------------------------


def test_apply_to_disk_writes_generated_to_source(tmp_path: Path):
    from testgap.generator import GeneratedTest, GeneratedTestSet
    from testgap.pipeline import FunctionSuggestion

    project_root = tmp_path
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    file = tmp_path / "src" / "demo.py"
    file.parent.mkdir(parents=True)
    file.write_text("def h(): pass\n", encoding="utf-8")
    func = UncoveredFunction(
        file=file, qualname="h", start_line=1, end_line=1, source="def h(): pass"
    )
    gs = GeneratedTestSet(
        imports=["from demo import h"],
        tests=[GeneratedTest(name="test_h", purpose="auto", code="def test_h():\n    h()")],
    )
    suggestion = FunctionSuggestion(function=func, generated=gs)

    af = _apply_to_disk(
        suggestion,
        func=func,
        project_root=project_root,
        test_dir=test_dir,
        source_paths=["src/"],
    )

    assert af.path.exists()
    body = af.path.read_text(encoding="utf-8")
    assert "test_h" in body
    assert "from demo import h" in body
    assert af.test_count == 1
