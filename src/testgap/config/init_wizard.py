import os
from dataclasses import dataclass
from pathlib import Path

from testgap.config.loader import CONFIG_FILENAME, dump_config
from testgap.config.schema import (
    CoverageConfig,
    GenerationConfig,
    LLMConfig,
    ProjectConfig,
    TestGapConfig,
)
from testgap.detect import detect_layout, detect_pytest, detect_source_paths, detect_test_dirs


@dataclass
class DetectionReport:
    pytest_signals: list[str]
    source_paths: list[str]
    test_paths: list[str]
    layout_kind: str
    layout_ambiguous: bool
    has_git: bool


def analyze(root: Path) -> DetectionReport:
    pytest = detect_pytest(root)
    layout = detect_layout(root)
    test_dirs = detect_test_dirs(root)

    source_paths = detect_source_paths(root)
    ambiguous = layout.kind.value == "flat" and len(layout.candidates) > 1

    test_paths = sorted({f"{p.relative_to(root).as_posix()}/" for p in test_dirs.paths})

    return DetectionReport(
        pytest_signals=pytest.signals,
        source_paths=source_paths,
        test_paths=test_paths,
        layout_kind=layout.kind.value,
        layout_ambiguous=ambiguous,
        has_git=(root / ".git").exists(),
    )


_KNOWN_PROVIDERS = (
    ("anthropic/claude-sonnet-4-6", "ANTHROPIC_API_KEY"),
    ("openai/gpt-4o", "OPENAI_API_KEY"),
    ("gemini/gemini-2.0-flash", "GEMINI_API_KEY"),
    ("ollama/qwen2.5-coder", None),
)


def suggest_model() -> str:
    """Pick the first provider whose API key is set in env; fall back to Ollama."""
    for model, env_var in _KNOWN_PROVIDERS:
        if env_var is None:
            continue
        if os.environ.get(env_var):
            return model
    return "ollama/qwen2.5-coder"


def provider_status() -> list[tuple[str, str]]:
    """Return (model, status) pairs for display in the wizard."""
    rows: list[tuple[str, str]] = []
    for model, env_var in _KNOWN_PROVIDERS:
        if env_var is None:
            rows.append((model, "local model"))
        elif os.environ.get(env_var):
            rows.append((model, f"{env_var} found"))
        else:
            rows.append((model, f"{env_var} not set"))
    return rows


def build_config(
    *,
    source_paths: list[str],
    test_paths: list[str],
    model: str,
) -> TestGapConfig:
    return TestGapConfig(
        project=ProjectConfig(
            source_paths=source_paths or ["src/"],
            test_paths=test_paths or ["tests/"],
        ),
        coverage=CoverageConfig(),
        llm=LLMConfig(model=model),
        generation=GenerationConfig(),
    )


def write_config(config: TestGapConfig, root: Path) -> Path:
    path = root / CONFIG_FILENAME
    dump_config(config, path)
    return path


def ensure_gitignore_entry(root: Path, entry: str = ".testgap/") -> bool:
    """Append entry to .gitignore if missing. Returns True if file was modified."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return False
    content = gitignore.read_text(encoding="utf-8")
    lines = {line.strip() for line in content.splitlines()}
    if entry.strip() in lines or entry.rstrip("/") in lines:
        return False
    suffix = "" if content.endswith("\n") or not content else "\n"
    with gitignore.open("a", encoding="utf-8") as f:
        f.write(f"{suffix}\n# TestGap\n{entry}\n")
    return True
