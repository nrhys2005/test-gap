"""Tests for ``testgap.detect.python_env`` (TG-417).

All env-var scenarios inject a plain ``env`` dict — no monkeypatching of
``os.environ`` — so tests stay deterministic regardless of whether the dev
shell has a venv/conda activated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from testgap.detect import (
    PytestPythonNotFoundError,
    resolve_pytest_python,
)
from testgap.detect.python_env import _conda_python_path, _venv_python_path


def _make_fake_venv(prefix: Path) -> Path:
    """Create a minimal POSIX venv layout; return the interpreter path."""
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    return python


# ---------------------------------------------------------------------------
# configured (config.pytest.python) — highest priority
# ---------------------------------------------------------------------------


def test_configured_absolute_path_wins(tmp_path: Path):
    python = _make_fake_venv(tmp_path / "venv")
    resolved = resolve_pytest_python(str(python), project_root=tmp_path, env={})
    assert resolved.source == "config"
    # ``str(python)`` (NOT ``.resolve()``) — the configured path must be
    # preserved verbatim so a venv symlink is not rewritten (see the symlink
    # regression test below).
    assert resolved.path == str(python)


def test_configured_relative_resolved_against_project_root(tmp_path: Path):
    python = _make_fake_venv(tmp_path / ".venv")
    resolved = resolve_pytest_python(".venv/bin/python", project_root=tmp_path, env={})
    assert resolved.source == "config"
    assert resolved.path == str(python)


def test_configured_expands_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    python = _make_fake_venv(tmp_path / "myenv")
    resolved = resolve_pytest_python("~/myenv/bin/python", project_root=tmp_path, env={})
    assert resolved.source == "config"
    assert resolved.path == str(python)


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv symlink layout")
def test_configured_symlink_path_is_preserved(tmp_path: Path):
    """A configured venv interpreter that is a symlink must NOT be resolved to
    its target — doing so runs the base interpreter and loses venv
    site-packages, re-introducing D11 (TG-417 review 🔴-1)."""
    real = tmp_path / "real-python"
    real.touch()
    link = tmp_path / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    resolved = resolve_pytest_python(".venv/bin/python", project_root=tmp_path, env={})
    assert resolved.source == "config"
    # The symlink path itself, not the resolved target.
    assert resolved.path == str(link)
    assert resolved.path != str(real)


def test_configured_missing_raises_with_clear_message(tmp_path: Path):
    with pytest.raises(PytestPythonNotFoundError) as excinfo:
        resolve_pytest_python(".venv/bin/python", project_root=tmp_path, env={})
    message = str(excinfo.value)
    assert ".venv/bin/python" in message  # the original configured value
    assert str(tmp_path) in message  # the resolved absolute path
    assert "pytest.python" in message  # the fix hint


def test_configured_beats_virtual_env(tmp_path: Path):
    configured = _make_fake_venv(tmp_path / "configured")
    venv = tmp_path / "activated"
    _make_fake_venv(venv)
    resolved = resolve_pytest_python(
        str(configured), project_root=tmp_path, env={"VIRTUAL_ENV": str(venv)}
    )
    assert resolved.source == "config"
    assert resolved.path == str(configured.resolve())


# ---------------------------------------------------------------------------
# auto-detection — VIRTUAL_ENV > CONDA_PREFIX > sys.executable
# ---------------------------------------------------------------------------


def test_virtual_env_detected(tmp_path: Path):
    venv = tmp_path / "venv"
    python = _make_fake_venv(venv)
    resolved = resolve_pytest_python(None, env={"VIRTUAL_ENV": str(venv)})
    assert resolved.source == "virtual_env"
    assert resolved.path == str(python)


def test_virtual_env_stale_falls_through(tmp_path: Path):
    """Env var set but the interpreter is gone → next candidate (sys.executable)."""
    resolved = resolve_pytest_python(None, env={"VIRTUAL_ENV": str(tmp_path / "gone")})
    assert resolved.source == "sys_executable"
    assert resolved.path == sys.executable


def test_conda_prefix_detected(tmp_path: Path):
    conda = tmp_path / "conda-env"
    python = _make_fake_venv(conda)  # POSIX conda layout is also bin/python
    resolved = resolve_pytest_python(None, env={"CONDA_PREFIX": str(conda)})
    assert resolved.source == "conda_prefix"
    assert resolved.path == str(python)


def test_virtual_env_beats_conda_prefix(tmp_path: Path):
    venv = tmp_path / "venv"
    venv_python = _make_fake_venv(venv)
    conda = tmp_path / "conda-env"
    _make_fake_venv(conda)
    resolved = resolve_pytest_python(
        None, env={"VIRTUAL_ENV": str(venv), "CONDA_PREFIX": str(conda)}
    )
    assert resolved.source == "virtual_env"
    assert resolved.path == str(venv_python)


def test_no_env_falls_back_to_sys_executable():
    resolved = resolve_pytest_python(None, env={})
    assert resolved.source == "sys_executable"
    assert resolved.path == sys.executable


# ---------------------------------------------------------------------------
# Windows layouts (pure path helpers, testable on POSIX CI)
# ---------------------------------------------------------------------------


def test_windows_layouts(tmp_path: Path):
    assert _venv_python_path(tmp_path, windows=True) == tmp_path / "Scripts/python.exe"
    assert _venv_python_path(tmp_path, windows=False) == tmp_path / "bin/python"
    assert _conda_python_path(tmp_path, windows=True) == tmp_path / "python.exe"
    assert _conda_python_path(tmp_path, windows=False) == tmp_path / "bin/python"
