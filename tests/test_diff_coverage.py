from pathlib import Path

from testgap.coverage.diff_coverage import compute_diff_coverage
from testgap.coverage.git_diff import FileLines


def _setup_file(project_root: Path, name: str = "mod.py") -> Path:
    path = project_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    return path


def test_uncovered_when_executed_empty(tmp_project: Path):
    py = _setup_file(tmp_project)
    diff = [FileLines(py, frozenset({1, 2}))]
    report = compute_diff_coverage(
        diff=diff,
        executed={},
        base_ref="origin/main",
        project_root=tmp_project,
    )
    assert report.changed_total == 2
    assert report.covered_total == 0
    assert {(u.file, u.line) for u in report.uncovered} == {(py, 1), (py, 2)}


def test_partial_coverage(tmp_project: Path):
    py = _setup_file(tmp_project)
    diff = [FileLines(py, frozenset({1, 2, 3}))]
    executed = {py.resolve(): frozenset({1, 2})}
    report = compute_diff_coverage(
        diff=diff,
        executed=executed,
        base_ref="origin/main",
        project_root=tmp_project,
    )
    assert report.covered_total == 2
    assert {u.line for u in report.uncovered} == {3}
    assert report.diff_coverage_pct == 66.7


def test_full_coverage(tmp_project: Path):
    py = _setup_file(tmp_project)
    diff = [FileLines(py, frozenset({1, 2}))]
    executed = {py.resolve(): frozenset({1, 2})}
    report = compute_diff_coverage(
        diff=diff,
        executed=executed,
        base_ref="origin/main",
        project_root=tmp_project,
    )
    assert report.uncovered == []
    assert report.diff_coverage_pct == 100.0


def test_excludes_glob_patterns(tmp_project: Path):
    py = _setup_file(tmp_project, "myapp/__init__.py")
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("\n", encoding="utf-8")
    diff = [FileLines(py, frozenset({1}))]
    report = compute_diff_coverage(
        diff=diff,
        executed={},
        base_ref="origin/main",
        exclude_patterns=["**/__init__.py"],
        project_root=tmp_project,
    )
    assert report.changed_total == 0


def test_skips_non_python_files(tmp_project: Path):
    non_py = tmp_project / "README.md"
    non_py.write_text("# hello\n", encoding="utf-8")
    diff = [FileLines(non_py, frozenset({1}))]
    report = compute_diff_coverage(
        diff=diff,
        executed={},
        base_ref="origin/main",
        project_root=tmp_project,
    )
    assert report.changed_total == 0
