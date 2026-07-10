"""Tests for the ``python_executable`` parameter on both pytest runners (TG-417).

``subprocess.run`` is replaced by a capturing fake so no real interpreter is
spawned. The coverage fake writes a minimal ``coverage.json`` side-effect; the
validator fake produces no JSON report so the ``_fallback_parse`` path is used.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from testgap.coverage import runner as coverage_runner_mod
from testgap.coverage.runner import CoverageError, run_pytest_with_coverage
from testgap.validator import runner as validator_runner_mod
from testgap.validator.runner import ValidatorError, run_pytest_on_file


class _CaptureRun:
    """Record the cmd passed to ``subprocess.run``; optionally run a side-effect."""

    def __init__(self, side_effect=None):
        self.cmd: list[str] | None = None
        self.side_effect = side_effect

    def __call__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        if self.side_effect is not None:
            self.side_effect()
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


def _raise_file_not_found(cmd, **kwargs):
    raise FileNotFoundError(cmd[0])


# ---------------------------------------------------------------------------
# validator/runner.py
# ---------------------------------------------------------------------------


def test_validator_uses_custom_python(tmp_path: Path, monkeypatch):
    capture = _CaptureRun()
    monkeypatch.setattr(validator_runner_mod.subprocess, "run", capture)
    run_pytest_on_file(
        tmp_path / "test_x.py",
        project_root=tmp_path,
        python_executable="/x/bin/python",
    )
    assert capture.cmd is not None
    assert capture.cmd[0] == "/x/bin/python"


def test_validator_defaults_to_sys_executable(tmp_path: Path, monkeypatch):
    capture = _CaptureRun()
    monkeypatch.setattr(validator_runner_mod.subprocess, "run", capture)
    run_pytest_on_file(tmp_path / "test_x.py", project_root=tmp_path)
    assert capture.cmd is not None
    assert capture.cmd[0] == sys.executable


def test_validator_missing_python_error_contains_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(validator_runner_mod.subprocess, "run", _raise_file_not_found)
    with pytest.raises(ValidatorError, match="/x/bin/python"):
        run_pytest_on_file(
            tmp_path / "test_x.py",
            project_root=tmp_path,
            python_executable="/x/bin/python",
        )


# ---------------------------------------------------------------------------
# coverage/runner.py
# ---------------------------------------------------------------------------


def _coverage_json_side_effect(project_root: Path):
    def write():
        json_path = project_root / ".testgap" / "coverage.json"
        json_path.write_text('{"files": {}}', encoding="utf-8")

    return write


def test_coverage_uses_custom_python(tmp_path: Path, monkeypatch):
    capture = _CaptureRun(side_effect=_coverage_json_side_effect(tmp_path))
    monkeypatch.setattr(coverage_runner_mod.subprocess, "run", capture)
    run_pytest_with_coverage(tmp_path, ["src/"], python_executable="/x/bin/python")
    assert capture.cmd is not None
    assert capture.cmd[0] == "/x/bin/python"


def test_coverage_defaults_to_sys_executable(tmp_path: Path, monkeypatch):
    capture = _CaptureRun(side_effect=_coverage_json_side_effect(tmp_path))
    monkeypatch.setattr(coverage_runner_mod.subprocess, "run", capture)
    run_pytest_with_coverage(tmp_path, ["src/"])
    assert capture.cmd is not None
    assert capture.cmd[0] == sys.executable


def test_coverage_missing_python_error_contains_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(coverage_runner_mod.subprocess, "run", _raise_file_not_found)
    with pytest.raises(CoverageError, match="/x/bin/python"):
        run_pytest_with_coverage(tmp_path, ["src/"], python_executable="/x/bin/python")
