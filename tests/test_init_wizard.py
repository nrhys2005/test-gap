from pathlib import Path

import pytest

from testgap.config.init_wizard import (
    analyze,
    build_config,
    ensure_gitignore_entry,
    provider_status,
    suggest_model,
    write_config,
)
from testgap.config.loader import CONFIG_FILENAME, load_config
from testgap.detect import OllamaScan
from testgap.detect import llm_provider as llm_provider_mod


def _fake_scan(*, pulled=(), reachable=False, binary=False):
    def fake(**kwargs):
        endpoint = kwargs.get("endpoint", "http://localhost:11434")
        return OllamaScan(
            binary_present=binary,
            endpoint=endpoint,
            server_reachable=reachable,
            pulled_models=tuple(pulled),
            error=None if reachable else "not installed (test isolation)",
        )

    return fake


@pytest.fixture
def _no_ollama(monkeypatch):
    """Force ``detect_llm_providers`` to see "no Ollama at all"."""
    monkeypatch.setattr(llm_provider_mod, "scan_ollama", _fake_scan())
    yield


@pytest.fixture
def _ollama_with_pulled_recommended(monkeypatch):
    """Simulate a working Ollama server with the top recommended model pulled."""
    monkeypatch.setattr(
        llm_provider_mod,
        "scan_ollama",
        _fake_scan(pulled=("qwen2.5-coder:7b",), reachable=True, binary=True),
    )
    yield


def _make_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    pkg = root / "src" / "myapp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()


def test_analyze_full_project(tmp_project: Path):
    _make_project(tmp_project)
    report = analyze(tmp_project)
    assert report.pytest_signals
    assert report.source_paths == ["src/"]
    assert report.test_paths == ["tests/"]
    assert report.layout_ambiguous is False


def test_analyze_empty_project(tmp_project: Path):
    report = analyze(tmp_project)
    assert report.pytest_signals == []
    assert report.source_paths == []
    assert report.test_paths == []


def test_build_and_write_config_roundtrip(tmp_project: Path):
    _make_project(tmp_project)
    config = build_config(
        source_paths=["src/"], test_paths=["tests/"], model="ollama/qwen2.5-coder"
    )
    path = write_config(config, tmp_project)

    assert path == tmp_project / CONFIG_FILENAME
    reloaded = load_config(path)
    assert reloaded.llm.model == "ollama/qwen2.5-coder"
    assert reloaded.project.source_paths == ["src/"]


def test_suggest_model_uses_anthropic_when_key_set(monkeypatch, _no_ollama):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert suggest_model().startswith("anthropic/")


def test_suggest_model_falls_back_when_no_env_no_ollama(monkeypatch, _no_ollama):
    """With neither API keys nor Ollama, the highest-priority row wins.

    Priority order (TG-401): KEY_MISSING (4) beats NOT_INSTALLED (5), so the
    Anthropic row is surfaced. The user is nudged toward the actionable hint
    ("set ANTHROPIC_API_KEY") rather than the un-installable Ollama default.
    """
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    result = suggest_model()
    # Any of the KEY_MISSING API providers is acceptable — the first one wins
    # via registration order but the test tolerates any API row.
    assert result.startswith(("anthropic/", "openai/", "gemini/"))


def test_suggest_model_prefers_pulled_ollama_over_missing_key(
    monkeypatch, _ollama_with_pulled_recommended
):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert suggest_model() == "ollama/qwen2.5-coder:7b"


def test_provider_status_returns_hint_from_detect(monkeypatch, _no_ollama):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rows = provider_status()
    assert rows, "expected at least one provider row"
    # Anthropic row's hint mentions the env var name.
    anthropic_row = next(r for r in rows if r[0].startswith("anthropic/"))
    assert "ANTHROPIC_API_KEY" in anthropic_row[1]


def test_ensure_gitignore_appends_once(tmp_project: Path):
    gi = tmp_project / ".gitignore"
    gi.write_text("__pycache__/\n", encoding="utf-8")

    assert ensure_gitignore_entry(tmp_project) is True
    assert ".testgap/" in gi.read_text(encoding="utf-8")

    assert ensure_gitignore_entry(tmp_project) is False


def test_ensure_gitignore_creates_when_missing(tmp_project: Path):
    gi = tmp_project / ".gitignore"
    assert not gi.exists()

    assert ensure_gitignore_entry(tmp_project) is True
    assert ".testgap/" in gi.read_text(encoding="utf-8")
