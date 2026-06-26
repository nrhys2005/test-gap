from configparser import ConfigParser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from testgap.detect._toml import load_toml

_EXCLUDE_DIRS = {
    "tests", "test", "docs", "doc", "examples", "example",
    ".venv", "venv", "env", ".env", "build", "dist",
    "node_modules", ".git", ".tox", ".mypy_cache", ".pytest_cache",
    "__pycache__", ".ruff_cache",
}


class LayoutKind(str, Enum):
    SRC = "src"
    FLAT = "flat"
    UNKNOWN = "unknown"


@dataclass
class LayoutDetection:
    kind: LayoutKind
    candidates: list[Path]


def detect_layout(root: Path) -> LayoutDetection:
    src_candidates = _detect_src_layout(root)
    if src_candidates:
        return LayoutDetection(kind=LayoutKind.SRC, candidates=src_candidates)

    flat_candidates = _detect_flat_layout(root)
    if flat_candidates:
        return LayoutDetection(kind=LayoutKind.FLAT, candidates=flat_candidates)

    return LayoutDetection(kind=LayoutKind.UNKNOWN, candidates=[])


def detect_source_paths(root: Path) -> list[str]:
    """Return source paths relative to root, ready for config.project.source_paths."""
    detection = detect_layout(root)
    if detection.kind == LayoutKind.SRC:
        return ["src/"]
    if detection.kind == LayoutKind.FLAT:
        return sorted({f"{p.name}/" for p in detection.candidates})
    return []


def _detect_src_layout(root: Path) -> list[Path]:
    candidates: list[Path] = []

    src_dir = root / "src"
    if src_dir.is_dir():
        for child in src_dir.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                candidates.append(child)

    pyproject = load_toml(root / "pyproject.toml")
    if _toml_indicates_src(pyproject):
        if src_dir.is_dir() and src_dir not in candidates:
            candidates.append(src_dir)

    if _setup_cfg_indicates_src(root / "setup.cfg") and src_dir.is_dir():
        candidates.append(src_dir)

    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _detect_flat_layout(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if child.name in _EXCLUDE_DIRS:
            continue
        if (child / "__init__.py").is_file():
            candidates.append(child)
    return candidates


def _toml_indicates_src(pyproject: dict) -> bool:
    if not pyproject:
        return False

    tool = pyproject.get("tool", {})

    setuptools = tool.get("setuptools", {})
    if setuptools.get("package-dir", {}).get("", "") == "src":
        return True
    pkg_find = setuptools.get("packages", {}).get("find", {})
    if pkg_find.get("where") == ["src"] or pkg_find.get("where") == "src":
        return True

    hatch = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    packages = hatch.get("packages", [])
    if any(str(p).startswith("src/") for p in packages):
        return True

    poetry_pkgs = tool.get("poetry", {}).get("packages", [])
    if any(p.get("from") == "src" for p in poetry_pkgs if isinstance(p, dict)):
        return True

    return False


def _setup_cfg_indicates_src(setup_cfg: Path) -> bool:
    if not setup_cfg.is_file():
        return False
    parser = ConfigParser()
    try:
        parser.read(setup_cfg, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if parser.has_option("options", "package_dir"):
        raw = parser.get("options", "package_dir")
        if "=src" in raw.replace(" ", ""):
            return True
    if parser.has_option("options.packages.find", "where"):
        if "src" in parser.get("options.packages.find", "where"):
            return True
    return False
