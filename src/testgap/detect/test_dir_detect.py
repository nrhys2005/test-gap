from dataclasses import dataclass
from pathlib import Path

_CANDIDATE_NAMES = ("tests", "test")


@dataclass
class TestDirDetection:
    paths: list[Path]
    has_conftest: bool


def detect_test_dirs(root: Path) -> TestDirDetection:
    found: list[Path] = []
    has_conftest = False

    for name in _CANDIDATE_NAMES:
        candidate = root / name
        if candidate.is_dir():
            found.append(candidate)
            if (candidate / "conftest.py").is_file():
                has_conftest = True

    for child in root.iterdir():
        if not child.is_dir() or not (child / "__init__.py").is_file():
            continue
        for name in _CANDIDATE_NAMES:
            candidate = child / name
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)
                if (candidate / "conftest.py").is_file():
                    has_conftest = True

    if has_conftest:
        found.sort(key=lambda p: (not (p / "conftest.py").is_file(), str(p)))

    return TestDirDetection(paths=found, has_conftest=has_conftest)
