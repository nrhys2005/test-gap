"""CLI tests for ``testgap backfill``.

``run_backfill`` is monkeypatched in :mod:`testgap.cli_backfill` so the CLI
tests focus on option parsing / config guarding / TTY guards without touching
the LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from testgap.backfill import BackfillOutcome
from testgap.cli import app

runner = CliRunner()


def _write_config(root: Path) -> None:
    (root / ".testgap.yml").write_text(
        "version: 1\n"
        "project:\n"
        "  source_paths: [src/]\n"
        "  test_paths: [tests/]\n"
        "llm:\n"
        "  model: fake/model\n"
        "  max_cost_per_run: 0\n",
        encoding="utf-8",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stub_outcome() -> BackfillOutcome:
    return BackfillOutcome(
        functions_processed=1,
        functions_accepted=1,
        coverage_before=50.0,
        coverage_after=55.5,
        coverage_after_is_estimated=True,
    )


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


def test_backfill_help_lists_options():
    result = runner.invoke(app, ["backfill", "--help"])
    assert result.exit_code == 0
    for opt in [
        "--target-coverage",
        "--max-functions",
        "--path",
        "--auto",
        "--below",
        "--priority",
        "--dry-run",
    ]:
        assert opt in result.output


def test_top_level_help_shows_backfill():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "backfill" in result.output


# ---------------------------------------------------------------------------
# option dispatch
# ---------------------------------------------------------------------------


def test_backfill_auto_flag_dispatches_auto_mode(project: Path, monkeypatch):
    """--auto flows to run_backfill(auto=True)."""
    seen: dict = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return _stub_outcome()

    monkeypatch.setattr("testgap.cli_backfill.run_backfill", fake_run)
    result = runner.invoke(app, ["backfill", "--auto", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert seen["auto"] is True
    assert seen["dry_run"] is True


def test_backfill_dry_run_exits_0(project: Path, monkeypatch):
    def fake_run(**kwargs):
        return _stub_outcome()

    monkeypatch.setattr("testgap.cli_backfill.run_backfill", fake_run)
    result = runner.invoke(app, ["backfill", "--auto", "--dry-run"])
    assert result.exit_code == 0


def test_backfill_summary_uses_approx_prefix(project: Path, monkeypatch):
    """R1: ``coverage_after_is_estimated=True`` → renders with ≈ prefix."""

    def fake_run(**kwargs):
        return _stub_outcome()

    monkeypatch.setattr("testgap.cli_backfill.run_backfill", fake_run)
    result = runner.invoke(app, ["backfill", "--auto"])
    assert result.exit_code == 0
    assert "≈55.5%" in result.output


# ---------------------------------------------------------------------------
# error / guard paths
# ---------------------------------------------------------------------------


def test_backfill_missing_config_exits_1(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["backfill", "--auto"])
    assert result.exit_code == 1


def test_backfill_invalid_priority_exits_1(project: Path):
    result = runner.invoke(app, ["backfill", "--auto", "--priority", "nope"])
    assert result.exit_code == 1


def test_backfill_interactive_without_tty_exits_1(project: Path, monkeypatch):
    """Interactive mode requires a TTY. CliRunner.invoke provides non-TTY stdin."""
    result = runner.invoke(app, ["backfill"])
    assert result.exit_code == 1
    assert "TTY" in result.output or "tty" in result.output


# ---------------------------------------------------------------------------
# _render_backfill_summary directly (R1 guarantee)
# ---------------------------------------------------------------------------


def test_render_backfill_summary_uses_approx_prefix():
    from rich.console import Console

    from testgap.cli_backfill import _render_backfill_summary

    outcome = BackfillOutcome(
        coverage_before=40.0,
        coverage_after=42.7,
        coverage_after_is_estimated=True,
    )
    console = Console(record=True, force_terminal=False, width=120)
    _render_backfill_summary(outcome, console)
    text = console.export_text()
    assert "≈42.7%" in text


def test_render_backfill_summary_no_prefix_when_verified():
    from rich.console import Console

    from testgap.cli_backfill import _render_backfill_summary

    outcome = BackfillOutcome(
        coverage_before=40.0,
        coverage_after=42.7,
        coverage_after_is_estimated=False,
    )
    console = Console(record=True, force_terminal=False, width=120)
    _render_backfill_summary(outcome, console)
    text = console.export_text()
    assert "≈42.7%" not in text
    assert "42.7%" in text
