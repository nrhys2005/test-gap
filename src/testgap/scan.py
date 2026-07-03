"""Deterministic project-wide coverage scan (LLM-free).

``testgap scan`` runs pytest with coverage once against the whole source tree
and produces a per-file / per-function coverage report. No LLM calls, no diff
computation — purely a snapshot of what is uncovered *right now*.

The output feeds two consumers:

* ``testgap scan`` CLI (human-readable table or JSON) — see :mod:`testgap.cli_scan`.
* ``testgap backfill`` orchestrator — builds a worklist of uncovered functions
  and drives ``pipeline.process_function`` for each. See :mod:`testgap.backfill`.

**Determinism**: no timestamps affect logic; ``ScanReport.generated_at`` is set
last for observability only and is not part of sort keys. Sorting is done in
the render layer (:func:`sort_files`), not baked into raw scan output.

**Serialization safety** (P2): ``FunctionCoverage._underlying`` holds the raw
:class:`UncoveredFunction` for backfill reuse and MUST NOT leak into JSON.
Use :func:`report_to_dict` (whitelist) instead of ``dataclasses.asdict``.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from testgap.config.schema import TestGapConfig
from testgap.coverage import (
    UncoveredFunction,
    group_by_function,
    run_pytest_with_coverage,
)
from testgap.coverage.diff_coverage import UncoveredLine
from testgap.coverage.runner import CoverageRunResult

SCAN_SCHEMA_VERSION = 1

__all__ = [
    "SCAN_SCHEMA_VERSION",
    "FileCoverage",
    "FunctionCoverage",
    "ScanReport",
    "report_to_dict",
    "scan_project",
    "sort_files",
]


# ---------------------------------------------------------------------------
# public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FunctionCoverage:
    """Per-function coverage summary within a file.

    ``_underlying`` holds the raw :class:`UncoveredFunction` so that
    :mod:`testgap.backfill` can feed it back to ``pipeline.process_function``
    without re-parsing the source file. ``repr=False, compare=False`` keeps
    it invisible to ``__repr__`` / equality / ``asdict`` — serialization goes
    through the whitelist helper :func:`report_to_dict`.
    """

    qualname: str
    start_line: int
    end_line: int
    uncovered_lines: list[int] = field(default_factory=list)
    total_lines: int = 0  # executable statement count within function
    covered_lines: int = 0
    _underlying: UncoveredFunction | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def coverage_pct(self) -> float:
        if self.total_lines == 0:
            return 100.0
        return round((self.covered_lines / self.total_lines) * 100, 1)


@dataclass
class FileCoverage:
    """Per-file coverage summary."""

    path: Path  # absolute
    rel_path: str  # project-root-relative posix
    total_lines: int
    covered_lines: int
    uncovered_functions: list[FunctionCoverage] = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        if self.total_lines == 0:
            return 100.0
        return round((self.covered_lines / self.total_lines) * 100, 1)


@dataclass
class ScanReport:
    """Aggregate scan result across all in-scope files."""

    schema_version: int = SCAN_SCHEMA_VERSION
    project_root: Path = field(default_factory=Path)
    files: list[FileCoverage] = field(default_factory=list)
    total_lines: int = 0
    covered_lines: int = 0
    generated_at: str = ""  # ISO8601 UTC — observability only

    @property
    def overall_coverage_pct(self) -> float:
        if self.total_lines == 0:
            return 100.0
        return round((self.covered_lines / self.total_lines) * 100, 1)


# ---------------------------------------------------------------------------
# scan orchestration
# ---------------------------------------------------------------------------


CoverageRunner = Callable[..., CoverageRunResult]


def scan_project(
    project_root: Path,
    config: TestGapConfig,
    *,
    path_filter: Path | None = None,
    below_pct: float | None = None,
    coverage_runner: CoverageRunner = run_pytest_with_coverage,
) -> ScanReport:
    """Run coverage once and build a per-file/function :class:`ScanReport`.

    Parameters
    ----------
    project_root:
        Absolute path to the project root (contains ``.testgap.yml``).
    config:
        Loaded ``.testgap.yml``. Uses ``project.source_paths`` (coverage scope)
        and ``coverage.exclude`` (fnmatch filter).
    path_filter:
        Optional project-root-relative path prefix — only files whose rel_path
        starts with this prefix are kept.
    below_pct:
        Optional coverage cutoff — only files with ``coverage_pct < below_pct``
        are kept.
    coverage_runner:
        Injection point for tests. Defaults to :func:`run_pytest_with_coverage`.

    Notes
    -----
    * The report is NOT sorted here; sorting is a render-layer concern
      (:func:`sort_files`) so raw output is stable and reusable.
    * ``FunctionCoverage._underlying`` is populated so backfill can reuse the
      raw AST-grouped ``UncoveredFunction`` without re-parsing.
    """
    project_root = project_root.resolve()
    coverage_run = coverage_runner(project_root, config.project.source_paths)

    # 1. Compute uncovered lines per file (executable minus executed).
    uncovered_lines: list[UncoveredLine] = []
    per_file_executable: dict[Path, set[int]] = {}
    per_file_total: dict[Path, int] = {}
    for abs_path, summary in coverage_run.file_summaries.items():
        num_statements = int(summary.get("num_statements", 0) or 0)
        missing_lines_raw = summary.get("missing_lines") or []
        missing_lines = {int(n) for n in missing_lines_raw}
        # ``executable = executed ∪ missing`` per coverage.py semantics.
        executed_for_file = coverage_run.executed_lines.get(abs_path, frozenset())
        executable_set = set(executed_for_file) | missing_lines
        per_file_executable[abs_path] = executable_set
        per_file_total[abs_path] = num_statements or len(executable_set)
        for line in sorted(missing_lines):
            uncovered_lines.append(UncoveredLine(file=abs_path, line=line))

    # 2. Group uncovered lines into their enclosing function via AST.
    uncovered_functions = group_by_function(uncovered_lines)
    functions_by_file: dict[Path, list[UncoveredFunction]] = {}
    for uf in uncovered_functions:
        functions_by_file.setdefault(uf.file, []).append(uf)

    # 3. Assemble FileCoverage per file, filtered by exclude/path/below.
    exclude_patterns = list(config.coverage.exclude or [])
    files_out: list[FileCoverage] = []
    total_lines = 0
    covered_lines = 0
    for abs_path, executable_set in per_file_executable.items():
        rel_path = _safe_relative(abs_path, project_root)
        if _is_excluded(rel_path, exclude_patterns):
            continue

        executed_for_file = coverage_run.executed_lines.get(abs_path, frozenset())
        file_total = per_file_total.get(abs_path, 0)
        # ``covered = executable ∩ executed`` — clamp to num_statements when
        # coverage.py reports a discrepancy.
        covered_for_file = len(executable_set & set(executed_for_file))
        if file_total and covered_for_file > file_total:
            covered_for_file = file_total

        uncov_funcs_raw = functions_by_file.get(abs_path, [])
        uncov_funcs = [
            _summarize_function(uf, project_root) for uf in uncov_funcs_raw
        ]

        fc = FileCoverage(
            path=abs_path,
            rel_path=rel_path,
            total_lines=file_total,
            covered_lines=covered_for_file,
            uncovered_functions=uncov_funcs,
        )
        files_out.append(fc)
        total_lines += file_total
        covered_lines += covered_for_file

    # 4. Apply path_filter / below_pct on rel_path + file coverage_pct.
    if path_filter is not None:
        prefix = path_filter.as_posix().rstrip("/")
        files_out = [
            fc
            for fc in files_out
            if fc.rel_path == prefix or fc.rel_path.startswith(prefix + "/")
        ]
    if below_pct is not None:
        files_out = [fc for fc in files_out if fc.coverage_pct < below_pct]

    return ScanReport(
        schema_version=SCAN_SCHEMA_VERSION,
        project_root=project_root,
        files=files_out,
        total_lines=total_lines,
        covered_lines=covered_lines,
        generated_at=_utc_iso_now(),
    )


# ---------------------------------------------------------------------------
# helpers — impact score / sort / serialize
# ---------------------------------------------------------------------------


def _impact_score(func: FunctionCoverage) -> float:
    """Ratio of uncovered lines to raw function span.

    Uses ``max(end - start + 1, 1)`` (raw AST span) rather than executable
    statement count so single-line functions and docstring-heavy functions
    have a comparable denominator. Range ``[0.0, 1.0]``.
    """
    span = max(func.end_line - func.start_line + 1, 1)
    return len(func.uncovered_lines) / span


def _file_impact_score(fc: FileCoverage) -> float:
    return sum(_impact_score(f) for f in fc.uncovered_functions)


def sort_files(
    files: list[FileCoverage],
    *,
    sort_by: str = "coverage",
) -> list[FileCoverage]:
    """Return sorted files. ``sort_by`` ∈ {"coverage", "missing", "impact"}."""
    if sort_by == "coverage":
        return sorted(files, key=lambda fc: (fc.coverage_pct, fc.rel_path))
    if sort_by == "missing":
        return sorted(
            files,
            key=lambda fc: (-len(fc.uncovered_functions), fc.rel_path),
        )
    if sort_by == "impact":
        return sorted(
            files,
            key=lambda fc: (-_file_impact_score(fc), fc.rel_path),
        )
    raise ValueError(
        f"unknown sort_by={sort_by!r}; expected 'coverage'|'missing'|'impact'"
    )


def report_to_dict(report: ScanReport) -> dict:
    """JSON-safe whitelist serialization.

    Excludes ``FunctionCoverage._underlying`` and any raw source text — those
    fields are private orchestration data and must not leak into stdout /
    session logs.
    """
    return {
        "schema_version": report.schema_version,
        "project_root": str(report.project_root),
        "generated_at": report.generated_at,
        "total_lines": report.total_lines,
        "covered_lines": report.covered_lines,
        "overall_coverage_pct": report.overall_coverage_pct,
        "files": [
            {
                "rel_path": fc.rel_path,
                "total_lines": fc.total_lines,
                "covered_lines": fc.covered_lines,
                "coverage_pct": fc.coverage_pct,
                "uncovered_functions": [
                    {
                        "qualname": func.qualname,
                        "start_line": func.start_line,
                        "end_line": func.end_line,
                        "uncovered_lines": list(func.uncovered_lines),
                        "total_lines": func.total_lines,
                        "covered_lines": func.covered_lines,
                        "coverage_pct": func.coverage_pct,
                    }
                    for func in fc.uncovered_functions
                ],
            }
            for fc in report.files
        ],
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _summarize_function(
    uf: UncoveredFunction, project_root: Path
) -> FunctionCoverage:
    """Build a :class:`FunctionCoverage` from an AST-grouped ``UncoveredFunction``."""
    executable = _executable_lines_in_function(uf)
    total = len(executable)
    covered = max(total - len(uf.uncovered_lines), 0)
    return FunctionCoverage(
        qualname=uf.qualname,
        start_line=uf.start_line,
        end_line=uf.end_line,
        uncovered_lines=list(uf.uncovered_lines),
        total_lines=total,
        covered_lines=covered,
        _underlying=uf,
    )


def _executable_lines_in_function(uf: UncoveredFunction) -> set[int]:
    """Approximate executable-line count from the function's AST source.

    Parses ``uf.source`` (the raw function body text) and collects
    ``lineno`` of leaf statements. Uses the function's ``start_line`` as
    the offset so absolute line numbers line up with coverage output.
    """
    try:
        tree = ast.parse(uf.source)
    except SyntaxError:
        # Fall back to raw span when we cannot parse (e.g. decorator chains
        # split across lines that ``group_by_function`` sliced awkwardly).
        return {n for n in range(uf.start_line, uf.end_line + 1)}

    lines: set[int] = set()
    offset = uf.start_line - 1  # source[0] maps to uf.start_line
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and hasattr(node, "lineno"):
            lines.add(int(node.lineno) + offset)
    return lines


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pat) for pat in patterns)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


