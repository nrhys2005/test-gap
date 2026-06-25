from pathlib import Path

from testgap.detect import detect_test_dirs


def test_tests_dir(tmp_project: Path):
    (tmp_project / "tests").mkdir()
    result = detect_test_dirs(tmp_project)
    assert len(result.paths) == 1
    assert result.paths[0].name == "tests"
    assert result.has_conftest is False


def test_conftest_promotes_dir(tmp_project: Path):
    (tmp_project / "test").mkdir()
    (tmp_project / "tests").mkdir()
    (tmp_project / "tests" / "conftest.py").write_text("", encoding="utf-8")

    result = detect_test_dirs(tmp_project)
    assert result.has_conftest is True
    assert result.paths[0].name == "tests"


def test_in_package_tests(tmp_project: Path):
    pkg = tmp_project / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "tests").mkdir()

    result = detect_test_dirs(tmp_project)
    paths = [p.relative_to(tmp_project).as_posix() for p in result.paths]
    assert "myapp/tests" in paths


def test_no_test_dirs(tmp_project: Path):
    result = detect_test_dirs(tmp_project)
    assert result.paths == []
    assert result.has_conftest is False
