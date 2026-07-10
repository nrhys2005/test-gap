"""Deterministic resolution of the Python interpreter used for pytest subprocesses.

Priority: ``.testgap.yml pytest.python`` > ``$VIRTUAL_ENV`` > ``$CONDA_PREFIX``
> ``sys.executable``. No AI, no heuristics — env vars + file-existence checks
only (TG-417 / D11: testgap installed in an isolated venv, e.g. via pipx, must
run pytest with the *target project's* interpreter or every import fails).

Design notes
------------
* Takes a plain ``str | None`` (the value of ``config.pytest.python``) instead
  of ``TestGapConfig`` — ``config/init_wizard.py`` already imports
  ``testgap.detect``, so importing ``testgap.config`` here would create a
  circular import.
* ``env`` is injectable (defaults to ``os.environ``) following the
  ``detect_llm_providers(env=...)`` convention so tests pass plain dicts
  without monkeypatching.
* Windows layout helpers take an explicit ``windows`` keyword so the branch is
  unit-testable on POSIX CI.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PythonSource = Literal["config", "virtual_env", "conda_prefix", "sys_executable"]


class PytestPythonNotFoundError(Exception):
    """Raised when ``.testgap.yml pytest.python`` points to a non-existent file."""


@dataclass(frozen=True)
class ResolvedPython:
    """Interpreter chosen for pytest subprocesses, plus where it came from."""

    path: str
    source: PythonSource


def _venv_python_path(prefix: Path, *, windows: bool) -> Path:
    """Interpreter path for the standard venv layout (Windows: Scripts/python.exe)."""
    return prefix / ("Scripts/python.exe" if windows else "bin/python")


def _conda_python_path(prefix: Path, *, windows: bool) -> Path:
    """Conda layout — on Windows the interpreter sits at the prefix root."""
    return prefix / ("python.exe" if windows else "bin/python")


def resolve_pytest_python(
    configured: str | None,
    *,
    project_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedPython:
    """Decide which python runs pytest subprocesses.

    * ``configured`` (``config.pytest.python``) wins when set. It is
      ``expanduser``-ed and, if relative, resolved against ``project_root``.
      A missing file raises :class:`PytestPythonNotFoundError` with the
      original value, the resolved absolute path, and a fix hint — this fires
      *before* any LLM spend (fail-fast).
    * When unset, ``VIRTUAL_ENV`` then ``CONDA_PREFIX`` are probed. A candidate
      is adopted only if the derived interpreter actually exists; otherwise we
      fall through (stale env-var defence). ``VIRTUAL_ENV`` outranks
      ``CONDA_PREFIX`` because a venv activated on top of a conda base is the
      more specific environment.
    * Final fallback: ``sys.executable`` — identical to pre-TG-417 behaviour.
    """
    env_map = env if env is not None else os.environ
    windows = os.name == "nt"

    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            base = project_root if project_root is not None else Path.cwd()
            candidate = base / candidate
        # ``normpath`` cleans up ``.``/``..`` without following symlinks —
        # crucially, ``Path.resolve()`` would rewrite a venv's ``bin/python``
        # (a symlink to the base interpreter) into the base interpreter,
        # losing the venv's site-packages and re-introducing D11. ``is_file()``
        # still follows the symlink for the existence check, which is correct.
        candidate = Path(os.path.normpath(candidate))
        if not candidate.is_file():
            raise PytestPythonNotFoundError(
                f"pytest.python is set to {configured!r} but {candidate} does not exist. "
                f"Fix pytest.python in .testgap.yml."
            )
        return ResolvedPython(path=str(candidate), source="config")

    virtual_env = env_map.get("VIRTUAL_ENV")
    if virtual_env:
        candidate = _venv_python_path(Path(virtual_env), windows=windows)
        if candidate.is_file():
            return ResolvedPython(path=str(candidate), source="virtual_env")

    conda_prefix = env_map.get("CONDA_PREFIX")
    if conda_prefix:
        candidate = _conda_python_path(Path(conda_prefix), windows=windows)
        if candidate.is_file():
            return ResolvedPython(path=str(candidate), source="conda_prefix")

    return ResolvedPython(path=sys.executable, source="sys_executable")
