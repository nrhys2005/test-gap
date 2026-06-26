from pathlib import Path

from testgap.config.init_wizard import (
    analyze,
    build_config,
    ensure_gitignore_entry,
    suggest_model,
    write_config,
)
from testgap.config.loader import CONFIG_FILENAME, load_config


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


def test_suggest_model_uses_anthropic_when_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert suggest_model().startswith("anthropic/")


def test_suggest_model_falls_back_to_ollama(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert suggest_model().startswith("ollama/")


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
