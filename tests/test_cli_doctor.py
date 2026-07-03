"""Tests for ``testgap doctor``.

Doctor calls ``detect_llm_providers`` internally; every test that touches the
LLM check monkeypatches ``scan_ollama`` (via ``llm_provider.scan_ollama``) and
clears API-key env vars so results are deterministic on any host.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from testgap.cli import app
from testgap.cli_doctor import _run_doctor_impl
from testgap.detect import OllamaScan
from testgap.detect import llm_provider as llm_provider_mod

runner = CliRunner()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_scan(
    *,
    pulled=(),
    reachable=False,
    binary=False,
    binary_source="missing",
    binary_path=None,
):
    def fake(**kwargs):
        endpoint = kwargs.get("endpoint", "http://localhost:11434")
        return OllamaScan(
            binary_present=binary,
            endpoint=endpoint,
            server_reachable=reachable,
            pulled_models=tuple(pulled),
            binary_source=binary_source,
            binary_path=binary_path,
        )

    return fake


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Clear API key env vars + isolate the XDG cache so doctor is deterministic."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


def _write_min_pytest_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths=["tests"]\n', encoding="utf-8"
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_smoke.py").write_text("def test_ok(): pass\n", encoding="utf-8")


def _write_config(root: Path, *, model: str = "ollama/qwen2.5-coder:7b") -> None:
    (root / ".testgap.yml").write_text(
        "version: 1\n"
        "project:\n  source_paths: [src/]\n  test_paths: [tests/]\n"
        f"llm:\n  model: {model}\n  max_cost_per_run: 0\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# exit-code scenarios
# ---------------------------------------------------------------------------


def test_doctor_all_ok_exit_0(tmp_path: Path, monkeypatch):
    """pytest + git + config + one usable provider → exit 0."""
    _write_min_pytest_project(tmp_path)
    _write_config(tmp_path)
    (tmp_path / ".git").mkdir()

    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )

    console = Console(record=True, force_terminal=False, width=120)
    code = _run_doctor_impl(tmp_path, refresh=False, verbose=False, console=console)
    assert code == 0
    text = console.export_text()
    assert "pytest" in text
    assert "OK" in text


def test_doctor_no_pytest_exit_1(tmp_path: Path, monkeypatch):
    """pytest absence is a blocker → exit 1."""
    (tmp_path / ".git").mkdir()
    _write_config(tmp_path)
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )

    console = Console(record=True, force_terminal=False, width=120)
    code = _run_doctor_impl(tmp_path, refresh=False, verbose=False, console=console)
    assert code == 1
    text = console.export_text()
    assert "FAIL" in text


def test_doctor_no_config_exit_2(tmp_path: Path, monkeypatch):
    """Missing ``.testgap.yml`` is a warning → exit 2 (blocker-free)."""
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )
    console = Console(record=True, force_terminal=False, width=120)
    code = _run_doctor_impl(tmp_path, refresh=False, verbose=False, console=console)
    assert code == 2
    text = console.export_text()
    assert "testgap init" in text


def test_doctor_invalid_config_exit_1(tmp_path: Path, monkeypatch):
    """Malformed YAML in ``.testgap.yml`` is a blocker → exit 1."""
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".testgap.yml").write_text("::: not yaml :::", encoding="utf-8")
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )
    console = Console(record=True, force_terminal=False, width=120)
    code = _run_doctor_impl(tmp_path, refresh=False, verbose=False, console=console)
    assert code == 1


def test_doctor_no_provider_exit_1(tmp_path: Path, monkeypatch):
    """No API key + no Ollama = provider blocker → exit 1."""
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    _write_config(tmp_path)
    monkeypatch.setattr(
        llm_provider_mod, "scan_ollama", _fake_scan(reachable=False, binary=False)
    )
    console = Console(record=True, force_terminal=False, width=120)
    code = _run_doctor_impl(tmp_path, refresh=False, verbose=False, console=console)
    assert code == 1


# ---------------------------------------------------------------------------
# --refresh / --verbose flags
# ---------------------------------------------------------------------------


def test_doctor_refresh_clears_cache(tmp_path: Path, monkeypatch):
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    _write_config(tmp_path)
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )

    from testgap.detect.cache import DetectCache, RunnableCacheEntry

    cache = DetectCache()
    cache.store_runnable(
        RunnableCacheEntry(
            model="ollama/qwen2.5-coder:7b",
            endpoint="http://localhost:11434",
            runnable=True,
            checked_at=0.0,
        )
    )
    assert cache.path.exists()

    console = Console(record=True, force_terminal=False, width=120)
    _run_doctor_impl(tmp_path, refresh=True, verbose=False, console=console)
    # After --refresh the cache file must be gone. Doctor performs no live
    # probe (review round 1 removed the ``/api/show`` probe entirely — see
    # llm_provider.probe_model_runnable docstring), so the file stays absent.
    assert not cache.path.exists()
    text = console.export_text()
    assert "detect cache cleared" in text


def test_doctor_verbose_shows_provider_detail(tmp_path: Path, monkeypatch):
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    _write_config(tmp_path)
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )

    console = Console(record=True, force_terminal=False, width=140)
    _run_doctor_impl(tmp_path, refresh=False, verbose=True, console=console)
    text = console.export_text()
    # Verbose exposes every registered provider row.
    assert "anthropic/" in text
    assert "openai/" in text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_doctor_help_lists_options():
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--refresh" in result.stdout
    assert "--verbose" in result.stdout


def test_top_level_help_shows_doctor():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_doctor_cli_returns_provider_blocker(tmp_path: Path, monkeypatch):
    """End-to-end CLI invocation exits 1 when provider is absent."""
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    _write_config(tmp_path)
    monkeypatch.setattr(
        llm_provider_mod, "scan_ollama", _fake_scan(reachable=False, binary=False)
    )

    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# TG-414: Ollama.app hint propagation
# ---------------------------------------------------------------------------


def test_doctor_app_broken_shows_menubar_hint(tmp_path: Path, monkeypatch):
    """When the recommended model is pulled but broken and the binary lives in
    Ollama.app, the doctor's LLM-provider row must surface the menu-bar
    upgrade guidance (TG-414 D9)."""
    _write_min_pytest_project(tmp_path)
    (tmp_path / ".git").mkdir()
    _write_config(tmp_path)
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(
            pulled=("qwen2.5-coder:7b",),
            reachable=True,
            binary=True,
            binary_source="app",
            binary_path="/Applications/Ollama.app/Contents/Resources/ollama",
        ),
    )
    # Force PULLED_BROKEN by patching the runnable check to always report False.
    from testgap.detect import llm_provider as lp

    original_entries = lp._ollama_provider_entries

    def broken_entries(scan, *, runnable_check_fn):
        return original_entries(scan, runnable_check_fn=lambda ep, m: False)

    monkeypatch.setattr(lp, "_ollama_provider_entries", broken_entries)

    console = Console(record=True, force_terminal=False, width=200)
    code = _run_doctor_impl(tmp_path, refresh=False, verbose=False, console=console)
    text = console.export_text()
    # With PULLED_BROKEN the LLM provider row is a blocker (no PULLED_RUNNABLE
    # and no API key), and the actionable hint must mention the menu-bar
    # upgrade path.
    assert code == 1
    assert "Upgrade via menu bar" in text
