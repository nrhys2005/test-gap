"""Integration tests wiring :mod:`testgap.session_logging` into pipeline / cli.

Scope (three cases per the plan):

1. ``run_diff`` forwards ``llm_call`` + ``pytest_run`` events to the session log
   in the correct order.
2. ``run_review_session`` (via ``_review_one``) forwards user actions.
3. The ``--no-session-log`` CLI flag prevents any ``.testgap/logs/`` files.

Kept in its own file because ``test_pipeline.py`` / ``test_ui_interactive.py``
are already large and unrelated. External boundaries (LLM, prompt, editor)
use the same injection patterns as those files — no real LLM traffic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from testgap import cli as cli_mod
from testgap.cli import app
from testgap.config.schema import (
    GenerationConfig,
    LLMConfig,
    ProjectConfig,
    TestGapConfig,
)
from testgap.coverage import UncoveredFunction
from testgap.generator import LLMClient
from testgap.pipeline import run_diff
from testgap.session_logging import SessionLog
from testgap.ui.interactive import _review_one

# Fixtures + helpers from the existing pipeline / interactive tests.
from tests.test_pipeline import (  # noqa: E402
    _payload,
    _queued_completion,
    _test_entry,
    demo_project,  # noqa: F401 — fixture re-export
)
from tests.test_ui_interactive import (  # noqa: E402
    make_prompt_queue,
)


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


class _RecordingLog:
    """In-memory session log used to assert event sequences.

    Structural type — satisfies :class:`SessionLogProtocol` without inheriting.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.function_ticks = 0
        self.closed = False
        self.quit_reason: str | None = None
        self._path: Path | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    def record(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    def increment_functions(self, n: int = 1) -> None:
        self.function_ticks += n

    def close(self, *, quit_reason: str | None = None) -> None:
        self.closed = True
        self.quit_reason = quit_reason

    def __enter__(self) -> _RecordingLog:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Mirror SessionLog.__exit__ semantics — infer reason then close.
        if exc_type is None:
            reason = None
        elif issubclass(exc_type, KeyboardInterrupt):
            reason = "keyboard_interrupt"
        else:
            reason = "exception"
        self.close(quit_reason=reason)


# ---------------------------------------------------------------------------
# 11. run_diff forwards llm_call + pytest_run in order
# ---------------------------------------------------------------------------


def test_run_diff_forwards_events_to_session_log(demo_project: Path) -> None:
    """Batch pipeline should emit ``llm_call`` then ``pytest_run`` per round.

    Verifies both an in-memory recorder (for exact ordering assertions) and a
    real ``SessionLog`` (for the JSONL persistence contract).
    """
    fn, _calls = _queued_completion([_pass_payload()])
    client = LLMClient(model="fake/model", completion_fn=fn)

    # (a) Recorder-based ordering check.
    recorder = _RecordingLog()
    run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
        session_log=recorder,
    )
    kinds = [e for e, _ in recorder.events]
    assert kinds == ["llm_call", "pytest_run"]
    assert recorder.function_ticks == 1

    llm_payload = recorder.events[0][1]
    assert llm_payload["function_qualname"].endswith("sub")
    assert llm_payload["attempt"] == 1
    assert llm_payload["model"] == "fake/model"
    assert llm_payload["cost_usd"] > 0

    pytest_payload = recorder.events[1][1]
    assert pytest_payload["exit_code"] == 0
    assert pytest_payload["pass_count"] >= 1
    assert pytest_payload["fail_count"] == 0

    # (b) Real SessionLog persistence check — reopen the JSONL and re-parse.
    fn2, _ = _queued_completion([_pass_payload()])
    client2 = LLMClient(model="fake/model", completion_fn=fn2)
    log = SessionLog.start(demo_project, _config())
    log_path = log.path
    with log:
        run_diff(
            project_root=demo_project,
            config=_config(),
            llm_client=client2,
            base_ref="main",
            session_log=log,
        )
    assert log_path is not None
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds_persisted = [e["event"] for e in events]
    assert kinds_persisted[0] == "session_start"
    assert kinds_persisted[-1] == "session_end"
    # There must be at least one llm_call and one pytest_run between them.
    assert "llm_call" in kinds_persisted
    assert "pytest_run" in kinds_persisted
    # session_end aggregates line up with what we saw:
    end = events[-1]
    assert end["functions_processed"] == 1
    assert end["total_cost"] > 0
    assert end["tests_accepted"] >= 1


# ---------------------------------------------------------------------------
# 12. _review_one records user actions per choice
# ---------------------------------------------------------------------------


def _make_uncovered(demo_project: Path) -> UncoveredFunction:
    return UncoveredFunction(
        file=demo_project / "src" / "demo" / "calc.py",
        qualname="sub",
        start_line=4,
        end_line=5,
        source="def sub(a, b):\n    return a - b\n",
        uncovered_lines=[5],
    )


def _run_review_one(
    demo_project: Path,
    suggestion,
    recorder: _RecordingLog,
    choices: list[str],
):
    from testgap.cost import CostTracker

    tracker = CostTracker(max_cost_per_run=1.0)
    test_dir = demo_project / "tests"
    from rich.console import Console

    console = Console(quiet=True)
    return _review_one(
        func=_make_uncovered(demo_project),
        suggestion=suggestion,
        project_root=demo_project,
        config=_config(),
        llm_client=LLMClient(model="fake/model", completion_fn=lambda **_k: None),
        tracker=tracker,
        test_dirs=[test_dir],
        console=console,
        prompt_fn=make_prompt_queue(choices),
        editor_fn=lambda _p: None,
        session_log=recorder,
    )


def test_review_session_records_user_actions(demo_project: Path) -> None:
    """Each choice from ``_review_one`` should log a ``user_action`` event."""
    from testgap.generator import GeneratedTest, GeneratedTestSet
    from testgap.pipeline import FunctionSuggestion
    from testgap.validator.result import (
        TestCaseResult,
        TestOutcome,
        ValidatorResult,
    )

    generated = GeneratedTestSet(
        imports=["from demo.calc import sub"],
        tests=[
            GeneratedTest(
                name="test_sub_ok",
                purpose="happy path",
                code=(
                    "def test_sub_ok():\n"
                    "    from demo.calc import sub\n"
                    "    assert sub(5, 3) == 2\n"
                ),
            )
        ],
    )
    passing_case = TestCaseResult(name="test_sub_ok", outcome=TestOutcome.PASS)
    vr = ValidatorResult(cases=[passing_case], exit_code=0)

    def _fresh_suggestion() -> FunctionSuggestion:
        return FunctionSuggestion(
            function=_make_uncovered(demo_project),
            generated=generated,
            validator_result=vr,
            accepted_cases=[passing_case],
            attempts=1,
        )

    # --- apply
    rec_apply = _RecordingLog()
    _run_review_one(demo_project, _fresh_suggestion(), rec_apply, ["a"])
    kinds = [e for e, _ in rec_apply.events]
    assert kinds == ["user_action"]
    assert rec_apply.events[0][1]["action"] == "apply"
    assert rec_apply.events[0][1]["applied_path"] is not None

    # --- skip
    rec_skip = _RecordingLog()
    _run_review_one(demo_project, _fresh_suggestion(), rec_skip, ["s"])
    assert rec_skip.events[-1][1]["action"] == "skip"

    # --- quit
    rec_quit = _RecordingLog()
    _run_review_one(demo_project, _fresh_suggestion(), rec_quit, ["q"])
    assert rec_quit.events[-1][1]["action"] == "quit"


# ---------------------------------------------------------------------------
# 13. --no-session-log CLI flag prevents log creation
# ---------------------------------------------------------------------------


def _write_min_config(root: Path) -> None:
    (root / ".testgap.yml").write_text(
        "version: 1\n"
        "project:\n  source_paths: [src/]\n  test_paths: [tests/]\n"
        "llm:\n  model: fake/model\n  max_cost_per_run: 1.0\n",
        encoding="utf-8",
    )


def test_no_session_log_flag_disables_logging(
    tmp_path: Path, monkeypatch
) -> None:
    """``--no-session-log`` must skip ``.testgap/logs/`` creation entirely."""
    _write_min_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    class _FakeReport:
        suggestions: list = []
        skipped_reason: str | None = "no diff"
        base_ref = "main"
        head_ref = "HEAD"
        diff_coverage_pct = 100.0
        changed_total = 0
        covered_total = 0
        cost_total = 0.0

    captured: dict[str, Any] = {}

    def fake_run_diff(**kwargs):
        # Capture the log the CLI injected so we can assert its type.
        captured["session_log"] = kwargs.get("session_log")
        return _FakeReport()

    monkeypatch.setattr(cli_mod, "run_diff", fake_run_diff)

    runner = CliRunner()
    result = runner.invoke(
        app, ["diff", "--path", str(tmp_path), "--no-session-log"]
    )
    assert result.exit_code == 0

    logs_dir = tmp_path / ".testgap" / "logs"
    assert not logs_dir.exists()
    # The CLI should not print the session-log announcement line.
    assert "session log:" not in result.stdout

    # Confirm the CLI injected a Noop-shaped log (path is None).
    injected = captured.get("session_log")
    assert injected is not None
    assert injected.path is None


def test_default_session_log_flag_creates_log_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """Complement to the previous test — the default flag should log."""
    _write_min_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    class _FakeReport:
        suggestions: list = []
        skipped_reason: str | None = "no diff"
        base_ref = "main"
        head_ref = "HEAD"
        diff_coverage_pct = 100.0
        changed_total = 0
        covered_total = 0
        cost_total = 0.0

    monkeypatch.setattr(cli_mod, "run_diff", lambda **_k: _FakeReport())
    runner = CliRunner()
    result = runner.invoke(app, ["diff", "--path", str(tmp_path)])
    assert result.exit_code == 0

    logs_dir = tmp_path / ".testgap" / "logs"
    assert logs_dir.exists()
    entries = list(logs_dir.iterdir())
    assert entries, "expected at least one .jsonl file"
    assert entries[0].suffix == ".jsonl"
    # Announcement line should be present.
    assert "session log:" in result.stdout


def test_diff_help_lists_session_log_option() -> None:
    """The ``--no-session-log`` flag is documented in the CLI help."""
    runner = CliRunner()
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0
    # Typer renders combined flags; test for the opt-out form specifically.
    assert "--no-session-log" in result.stdout
