from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path

from testgap.detect._toml import load_toml


@dataclass
class PytestDetection:
    detected: bool
    signals: list[str] = field(default_factory=list)


_DEP_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "Pipfile",
    "poetry.lock",
)


def detect_pytest(root: Path) -> PytestDetection:
    signals: list[str] = []

    pyproject = load_toml(root / "pyproject.toml")
    if "tool" in pyproject and "pytest" in pyproject.get("tool", {}):
        signals.append("pyproject.toml:[tool.pytest.ini_options]")

    if (root / "pytest.ini").is_file():
        signals.append("pytest.ini")

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            if parser.has_section("tool:pytest"):
                signals.append("setup.cfg:[tool:pytest]")
        except (OSError, UnicodeDecodeError):
            pass

    tox_ini = root / "tox.ini"
    if tox_ini.is_file():
        parser = ConfigParser()
        try:
            parser.read(tox_ini, encoding="utf-8")
            if parser.has_section("pytest"):
                signals.append("tox.ini:[pytest]")
        except (OSError, UnicodeDecodeError):
            pass

    for conftest in (root / "conftest.py", root / "tests" / "conftest.py"):
        if conftest.is_file():
            signals.append(f"conftest.py at {conftest.relative_to(root)}")
            break

    if _has_pytest_in_dependencies(root, pyproject):
        signals.append("pytest in dependencies")

    if _has_test_files(root):
        signals.append("test_*.py files present")

    return PytestDetection(detected=bool(signals), signals=signals)


def _has_pytest_in_dependencies(root: Path, pyproject: dict) -> bool:
    project_deps = pyproject.get("project", {}).get("dependencies", []) or []
    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {}) or {}
    poetry_deps = (
        pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    )
    poetry_groups = pyproject.get("tool", {}).get("poetry", {}).get("group", {})
    poetry_dev = poetry_groups.get("dev", {}).get("dependencies", {}) or {}

    haystacks: list[str] = []
    haystacks.extend(str(d) for d in project_deps)
    for group_deps in optional_deps.values():
        haystacks.extend(str(d) for d in group_deps)
    haystacks.extend(poetry_deps.keys())
    haystacks.extend(poetry_dev.keys())

    if any("pytest" in h.lower() for h in haystacks):
        return True

    for filename in _DEP_FILES:
        path = root / filename
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "pytest" in content.lower():
                return True

    return False


def _has_test_files(root: Path) -> bool:
    for tests_dir_name in ("tests", "test"):
        tests_dir = root / tests_dir_name
        if not tests_dir.is_dir():
            continue
        for pattern in ("test_*.py", "*_test.py"):
            if any(tests_dir.rglob(pattern)):
                return True
    return False
