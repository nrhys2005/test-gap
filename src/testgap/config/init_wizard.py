"""Bootstrap helpers for ``testgap init`` (project analysis + config write).

Provider inspection is delegated to ``testgap.detect.llm_provider`` so the same
detection powers the wizard, ``testgap doctor`` and future v0.3 hardware checks.
The public ``suggest_model()`` / ``provider_status()`` signatures are preserved
for CLI / test back-compat.
"""

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
from testgap.detect import (
    Provider,
    detect_layout,
    detect_llm_providers,
    detect_pytest,
    detect_source_paths,
    detect_test_dirs,
)

FALLBACK_MODEL = "ollama/qwen2.5-coder:7b"


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


def suggest_model(project_root: Path | None = None) -> str:
    """Return the highest-priority provider's model id.

    ``project_root`` is accepted for signature stability (v0.3 may cache probes
    per-project); it is unused today because provider detection is optimistic
    (no live runnability probe — see :func:`detect_providers_for_ui`).
    """
    _ = project_root  # unused — signature kept for back-compat
    providers = detect_llm_providers()
    if not providers:
        return FALLBACK_MODEL
    return providers[0].model


def provider_status() -> list[tuple[str, str]]:
    """Return ``(model, hint)`` pairs for the wizard's rendered table."""
    return [(p.model, p.hint) for p in detect_llm_providers()]


def detect_providers_for_ui(project_root: Path | None = None) -> list[Provider]:
    """Wizard/doctor-facing provider list. Optimistic — no live probe.

    Historically this issued a ``probe_model_runnable`` call against
    ``/api/show`` and cached the result. The TG-401 review round 1 concluded
    that the probe was unreliable in practice (wrong HTTP verb + Ollama
    version drift both surface as false-negatives) and that the pipeline /
    review-session consecutive-failure guards are the right place to catch
    runtime failures. We therefore default to "pulled ⇒ runnable" and skip
    the probe entirely.

    ``project_root`` is accepted for signature stability but unused.
    """
    _ = project_root  # unused — signature kept for back-compat
    return detect_llm_providers(env=dict(os.environ))


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
        gitignore.write_text(f"# TestGap\n{entry}\n", encoding="utf-8")
        return True
    content = gitignore.read_text(encoding="utf-8")
    lines = {line.strip() for line in content.splitlines()}
    if entry.strip() in lines or entry.rstrip("/") in lines:
        return False
    suffix = "" if content.endswith("\n") or not content else "\n"
    with gitignore.open("a", encoding="utf-8") as f:
        f.write(f"{suffix}\n# TestGap\n{entry}\n")
    return True
