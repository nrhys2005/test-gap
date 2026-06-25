from pathlib import Path

from testgap.detect import detect_pytest


def test_no_signals(tmp_project: Path):
    result = detect_pytest(tmp_project)
    assert result.detected is False
    assert result.signals == []


def test_pyproject_tool_pytest(tmp_project: Path):
    (tmp_project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    result = detect_pytest(tmp_project)
    assert result.detected is True
    assert any("pyproject.toml" in s for s in result.signals)


def test_pytest_ini(tmp_project: Path):
    (tmp_project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result = detect_pytest(tmp_project)
    assert result.detected is True
    assert "pytest.ini" in result.signals


def test_setup_cfg_tool_pytest(tmp_project: Path):
    (tmp_project / "setup.cfg").write_text("[tool:pytest]\n", encoding="utf-8")
    result = detect_pytest(tmp_project)
    assert result.detected is True


def test_conftest_at_root(tmp_project: Path):
    (tmp_project / "conftest.py").write_text("# conftest\n", encoding="utf-8")
    result = detect_pytest(tmp_project)
    assert result.detected is True


def test_pytest_in_dependencies(tmp_project: Path):
    (tmp_project / "requirements.txt").write_text("pytest>=7\n", encoding="utf-8")
    result = detect_pytest(tmp_project)
    assert result.detected is True


def test_test_files_present(tmp_project: Path):
    tests_dir = tmp_project / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_thing.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    result = detect_pytest(tmp_project)
    assert result.detected is True
