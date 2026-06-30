"""CLI integration for `testgap diff --review`.

Scope (per plan):
1. TTY guard regression (non-TTY → exit 1).
2. `run_review_session` dispatch verification — `--review` triggers it.
3. Non-review behaviour is unchanged.

The interactive flow itself (5-choice prompt, edit, etc.) lives in
``test_ui_interactive.py`` with prompt_fn injection. CliRunner cannot satisfy
both TTY-True and scripted stdin cleanly, so CLI tests deliberately stop at
"correct dispatch happened".
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from testgap import cli as cli_mod
from testgap.cli import app
from testgap.ui import ReviewOutcome

runner = CliRunner()


def _write_min_config(root: Path) -> None:
    (root / ".testgap.yml").write_text(
        "version: 1\n"
        "project:\n  source_paths: [src/]\n  test_paths: [tests/]\n"
        "llm:\n  model: fake/model\n  max_cost_per_run: 1.0\n",
        encoding="utf-8",
    )


def test_diff_review_requires_tty(tmp_path: Path, monkeypatch):
    _write_min_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    result = runner.invoke(app, ["diff", "--review", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "requires a TTY" in result.stdout


class _FakeTTYStdin:
    """Minimal stand-in for sys.stdin reporting isatty()==True.

    CliRunner replaces ``sys.stdin`` with StringIO at invoke-time, so we patch
    the module-level ``sys`` import inside ``cli.py`` to bypass that wrapping.
    """

    def isatty(self) -> bool:
        return True


def _patch_tty(monkeypatch, *, tty: bool) -> None:
    import sys as _sys

    fake_module = type(_sys)("fake_sys_for_cli")
    fake_module.stdin = _FakeTTYStdin() if tty else type("F", (), {"isatty": lambda self: False})()
    monkeypatch.setattr(cli_mod, "sys", fake_module)


def test_diff_review_runs_session(tmp_path: Path, monkeypatch):
    """When stdin is a TTY and --review is passed, the session function is invoked."""
    _write_min_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_tty(monkeypatch, tty=True)

    calls = {"n": 0}

    def fake_session(**kwargs):
        calls["n"] += 1
        # sanity-check critical kwargs from cli.diff
        assert kwargs["base_ref"] is None
        assert kwargs["head_ref"] == "HEAD"
        return ReviewOutcome()

    monkeypatch.setattr(cli_mod, "run_review_session", fake_session)

    result = runner.invoke(app, ["diff", "--review", "--path", str(tmp_path)])
    assert calls["n"] == 1, f"session not invoked. stdout={result.stdout!r}"
    assert result.exit_code == 0


def test_diff_review_session_exception_returns_one(tmp_path: Path, monkeypatch):
    """Exceptions raised by run_review_session surface as exit 1."""
    _write_min_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_tty(monkeypatch, tty=True)

    def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli_mod, "run_review_session", boom)

    result = runner.invoke(app, ["diff", "--review", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "kaboom" in result.stdout


def test_diff_without_review_keeps_existing_behavior(tmp_path: Path, monkeypatch):
    """`--review` absent → existing non-interactive `run_diff` path runs."""
    _write_min_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    sentinel = {"n": 0}

    class _FakeReport:
        suggestions: list = []
        skipped_reason: str | None = "no diff"
        base_ref = "main"
        head_ref = "HEAD"
        diff_coverage_pct = 100.0
        changed_total = 0
        covered_total = 0
        cost_total = 0.0

    def fake_run_diff(**kwargs):
        sentinel["n"] += 1
        return _FakeReport()

    monkeypatch.setattr(cli_mod, "run_diff", fake_run_diff)

    result = runner.invoke(app, ["diff", "--path", str(tmp_path)])
    assert sentinel["n"] == 1
    assert result.exit_code == 0


def test_diff_help_lists_review_option():
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0
    assert "--review" in result.stdout
    assert "Interactively review" in result.stdout
