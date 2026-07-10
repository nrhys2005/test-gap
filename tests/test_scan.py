"""Unit tests for :mod:`testgap.scan`.

Coverage runner is always injected — no subprocess / pytest is spawned so
these tests are deterministic across hosts.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from testgap.config.schema import (
    CoverageConfig,
    ProjectConfig,
    TestGapConfig,
)
from testgap.coverage.runner import CoverageRunResult
from testgap.scan import (
    SCAN_SCHEMA_VERSION,
    FileCoverage,
    FunctionCoverage,
    ScanReport,
    _impact_score,
    report_to_dict,
    scan_project,
    sort_files,
)

# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------


def _config(exclude: list[str] | None = None) -> TestGapConfig:
    return TestGapConfig(
        project=ProjectConfig(source_paths=["src/"], test_paths=["tests/"]),
        coverage=CoverageConfig(exclude=exclude if exclude is not None else []),
    )


def _write_source(
    project_root: Path, rel_path: str, body: str
) -> Path:
    p = project_root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p.resolve()


def _fake_runner(
    executed_lines: dict[Path, frozenset[int]],
    file_summaries: dict[Path, dict],
) -> Callable[..., CoverageRunResult]:
    def runner(project_root: Path, source_paths: list[str], **kwargs):
        _ = (project_root, source_paths, kwargs)
        return CoverageRunResult(
            coverage_json_path=Path("/tmp/coverage.json"),
            executed_lines=executed_lines,
            raw_pytest_exit_code=0,
            file_summaries=file_summaries,
        )

    return runner


# ---------------------------------------------------------------------------
# dataclass defaults
# ---------------------------------------------------------------------------


def test_scan_report_dataclass_defaults():
    report = ScanReport()
    assert report.schema_version == SCAN_SCHEMA_VERSION
    assert report.files == []
    assert report.total_lines == 0
    assert report.covered_lines == 0
    assert report.overall_coverage_pct == 100.0


def test_file_coverage_pct_zero_total_returns_100():
    fc = FileCoverage(
        path=Path("/tmp/x.py"), rel_path="x.py", total_lines=0, covered_lines=0
    )
    assert fc.coverage_pct == 100.0


def test_function_coverage_pct_zero_total_returns_100():
    func = FunctionCoverage(qualname="f", start_line=1, end_line=1)
    assert func.coverage_pct == 100.0


# ---------------------------------------------------------------------------
# scan_project
# ---------------------------------------------------------------------------


def _demo_source(project_root: Path) -> Path:
    body = (
        "def covered():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def uncovered():\n"
        "    x = 2\n"
        "    return x\n"
    )
    return _write_source(project_root, "src/demo/mod.py", body)


def test_scan_project_passes_resolved_python_to_runner(tmp_project: Path):
    """TG-417: scan resolves ``config.pytest.python`` and forwards it to the runner."""
    src = _demo_source(tmp_project)
    venv_python = tmp_project / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    received: dict = {}

    def runner(project_root: Path, source_paths: list[str], **kwargs):
        received.update(kwargs)
        return CoverageRunResult(
            coverage_json_path=Path("/tmp/coverage.json"),
            executed_lines={src: frozenset({1, 2})},
            raw_pytest_exit_code=0,
            file_summaries={src: {"num_statements": 4, "missing_lines": [5, 6, 7]}},
        )

    config = _config()
    config.pytest.python = ".venv/bin/python"
    scan_project(tmp_project, config, coverage_runner=runner)
    # ``str(venv_python)`` (NOT ``.resolve()``) — configured paths preserved
    # verbatim so venv symlinks survive (TG-417 review 🔴-1).
    assert received["python_executable"] == str(venv_python)


def test_scan_project_with_fake_runner(tmp_project: Path):
    src = _demo_source(tmp_project)
    executed = {src: frozenset({1, 2})}
    summaries = {
        src: {"num_statements": 4, "missing_lines": [5, 6, 7]},
    }
    report = scan_project(
        tmp_project,
        _config(),
        coverage_runner=_fake_runner(executed, summaries),
    )
    assert report.schema_version == SCAN_SCHEMA_VERSION
    assert report.total_lines == 4
    assert len(report.files) == 1
    fc = report.files[0]
    assert fc.rel_path == "src/demo/mod.py"
    assert fc.total_lines == 4
    assert fc.covered_lines == 2
    assert len(fc.uncovered_functions) == 1
    assert fc.uncovered_functions[0].qualname == "uncovered"
    assert fc.uncovered_functions[0].uncovered_lines == [5, 6, 7]


def test_scan_project_path_filter(tmp_project: Path):
    a = _write_source(tmp_project, "src/app/services/mod.py", "def f():\n    return 1\n")
    b = _write_source(tmp_project, "src/app/utils/other.py", "def g():\n    return 2\n")
    executed: dict[Path, frozenset[int]] = {a: frozenset(), b: frozenset()}
    summaries = {
        a: {"num_statements": 2, "missing_lines": [1, 2]},
        b: {"num_statements": 2, "missing_lines": [1, 2]},
    }
    report = scan_project(
        tmp_project,
        _config(),
        path_filter=Path("src/app/services"),
        coverage_runner=_fake_runner(executed, summaries),
    )
    rel_paths = {fc.rel_path for fc in report.files}
    assert rel_paths == {"src/app/services/mod.py"}


def test_scan_project_below_pct_filters_out_high_coverage(tmp_project: Path):
    a = _write_source(tmp_project, "src/high.py", "def f():\n    return 1\n")
    b = _write_source(tmp_project, "src/low.py", "def g():\n    return 2\n")
    executed = {a: frozenset({1, 2}), b: frozenset()}
    summaries = {
        a: {"num_statements": 2, "missing_lines": []},
        b: {"num_statements": 2, "missing_lines": [1, 2]},
    }
    report = scan_project(
        tmp_project,
        _config(),
        below_pct=80,
        coverage_runner=_fake_runner(executed, summaries),
    )
    rel_paths = {fc.rel_path for fc in report.files}
    assert rel_paths == {"src/low.py"}


def test_scan_project_excludes_configured_patterns(tmp_project: Path):
    a = _write_source(tmp_project, "src/pkg/__init__.py", "\n")
    b = _write_source(tmp_project, "src/pkg/mod.py", "def f():\n    return 1\n")
    executed: dict[Path, frozenset[int]] = {a: frozenset(), b: frozenset()}
    summaries = {
        a: {"num_statements": 1, "missing_lines": [1]},
        b: {"num_statements": 2, "missing_lines": [1, 2]},
    }
    report = scan_project(
        tmp_project,
        _config(exclude=["**/__init__.py"]),
        coverage_runner=_fake_runner(executed, summaries),
    )
    rel_paths = {fc.rel_path for fc in report.files}
    assert rel_paths == {"src/pkg/mod.py"}


def test_scan_project_generated_at_is_iso(tmp_project: Path):
    from datetime import datetime

    src = _demo_source(tmp_project)
    executed = {src: frozenset()}
    summaries = {src: {"num_statements": 4, "missing_lines": [1, 2, 5, 6, 7]}}
    report = scan_project(
        tmp_project,
        _config(),
        coverage_runner=_fake_runner(executed, summaries),
    )
    parsed = datetime.fromisoformat(report.generated_at)
    assert parsed is not None


def test_scan_project_covered_functions_excluded(tmp_project: Path):
    src = _demo_source(tmp_project)
    # All lines executed → no uncovered functions expected.
    executed = {src: frozenset({1, 2, 5, 6, 7})}
    summaries = {src: {"num_statements": 4, "missing_lines": []}}
    report = scan_project(
        tmp_project,
        _config(),
        coverage_runner=_fake_runner(executed, summaries),
    )
    fc = report.files[0]
    assert fc.uncovered_functions == []


# ---------------------------------------------------------------------------
# sort_files
# ---------------------------------------------------------------------------


def _fc(
    rel_path: str,
    *,
    covered: int,
    total: int,
    funcs: list[tuple[str, int, int, list[int]]] | None = None,
) -> FileCoverage:
    fc_funcs = [
        FunctionCoverage(
            qualname=q,
            start_line=s,
            end_line=e,
            uncovered_lines=lines,
            total_lines=e - s + 1,
            covered_lines=max((e - s + 1) - len(lines), 0),
        )
        for q, s, e, lines in (funcs or [])
    ]
    return FileCoverage(
        path=Path(f"/{rel_path}"),
        rel_path=rel_path,
        total_lines=total,
        covered_lines=covered,
        uncovered_functions=fc_funcs,
    )


def test_sort_files_by_coverage_low_first():
    files = [
        _fc("b.py", covered=8, total=10),  # 80%
        _fc("a.py", covered=2, total=10),  # 20%
        _fc("c.py", covered=5, total=10),  # 50%
    ]
    sorted_files = sort_files(files, sort_by="coverage")
    assert [fc.rel_path for fc in sorted_files] == ["a.py", "c.py", "b.py"]


def test_sort_files_by_missing():
    files = [
        _fc("a.py", covered=10, total=10, funcs=[("f", 1, 2, [1])]),
        _fc(
            "b.py",
            covered=10,
            total=10,
            funcs=[("f", 1, 2, [1]), ("g", 3, 4, [3])],
        ),
    ]
    sorted_files = sort_files(files, sort_by="missing")
    assert [fc.rel_path for fc in sorted_files] == ["b.py", "a.py"]


def test_sort_files_by_impact():
    small_hot = _fc(
        "small.py", covered=0, total=2, funcs=[("f", 1, 2, [1, 2])]
    )  # impact = 1.0
    big_cold = _fc(
        "big.py",
        covered=0,
        total=100,
        funcs=[("g", 1, 100, [1])],
    )  # impact ≈ 0.01
    sorted_files = sort_files([big_cold, small_hot], sort_by="impact")
    assert [fc.rel_path for fc in sorted_files] == ["small.py", "big.py"]


def test_sort_files_unknown_raises():
    with pytest.raises(ValueError):
        sort_files([], sort_by="nope")


# ---------------------------------------------------------------------------
# _impact_score edge cases (P3)
# ---------------------------------------------------------------------------


def test_impact_score_short_function():
    func = FunctionCoverage(
        qualname="f",
        start_line=1,
        end_line=3,
        uncovered_lines=[1, 2, 3],
    )
    assert _impact_score(func) == 1.0


def test_impact_score_single_line_function():
    func = FunctionCoverage(
        qualname="f",
        start_line=42,
        end_line=42,
        uncovered_lines=[42],
    )
    # No divide-by-zero: max(end - start + 1, 1) = 1.
    assert _impact_score(func) == 1.0


def test_impact_score_no_uncovered_lines():
    func = FunctionCoverage(
        qualname="f",
        start_line=1,
        end_line=10,
        uncovered_lines=[],
    )
    assert _impact_score(func) == 0.0


# ---------------------------------------------------------------------------
# report_to_dict (P2 — no _underlying / source leak)
# ---------------------------------------------------------------------------


def test_report_to_dict_excludes_underlying(tmp_project: Path):
    src = _demo_source(tmp_project)
    executed = {src: frozenset({1, 2})}
    summaries = {src: {"num_statements": 4, "missing_lines": [5, 6, 7]}}
    report = scan_project(
        tmp_project,
        _config(),
        coverage_runner=_fake_runner(executed, summaries),
    )
    assert report.files[0].uncovered_functions[0]._underlying is not None

    dumped = report_to_dict(report)

    # Recursively scan dumped dict for banned keys / prompt-source text.
    banned_keys = {"_underlying", "source"}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in banned_keys, f"leaked key: {k}"
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(dumped)


# ---------------------------------------------------------------------------
# PR #12 review regressions (gemini CRITICAL scan.py:366)
# ---------------------------------------------------------------------------


def test_executable_lines_skips_def_line_for_uncovered_function():
    """The ``def`` line runs at import time so coverage.py never marks it
    missed. Counting it as an executable statement inflated coverage for
    functions where the body itself was never executed. Regression: the
    body-only walk must return zero coverage when every body line is missing.
    """
    from testgap.coverage.ast_grouping import UncoveredFunction
    from testgap.scan import _executable_lines_in_function

    source = (
        "def compute(name: str) -> str:\n"
        "    if not name:\n"
        "        raise ValueError('empty')\n"
        "    return name\n"
    )
    uf = UncoveredFunction(
        file=Path("m.py"),
        qualname="compute",
        start_line=10,
        end_line=13,
        source=source,
        uncovered_lines=[11, 12, 13],
    )
    lines = _executable_lines_in_function(uf)
    # Body lines: 11 (if), 12 (raise), 13 (return). ``def`` line (10) MUST
    # NOT appear here — that's exactly the fix.
    assert 10 not in lines, "def line 10 leaked into executable set"
    assert 11 in lines
    assert 12 in lines
    assert 13 in lines


def test_executable_lines_handles_async_def():
    """Same behavior for ``async def``. Guards the isinstance(..., AsyncFunctionDef)
    branch introduced in the fix.
    """
    from testgap.coverage.ast_grouping import UncoveredFunction
    from testgap.scan import _executable_lines_in_function

    source = (
        "async def worker(payload):\n"
        "    result = await process(payload)\n"
        "    return result\n"
    )
    uf = UncoveredFunction(
        file=Path("m.py"),
        qualname="worker",
        start_line=5,
        end_line=7,
        source=source,
        uncovered_lines=[6, 7],
    )
    lines = _executable_lines_in_function(uf)
    assert 5 not in lines  # async def line skipped
    assert 6 in lines
    assert 7 in lines
