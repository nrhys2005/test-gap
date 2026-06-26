import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from testgap.coverage.git_diff import FileLines


@dataclass(frozen=True)
class UncoveredLine:
    file: Path
    line: int


@dataclass
class DiffCoverageReport:
    base_ref: str
    head_ref: str
    uncovered: list[UncoveredLine] = field(default_factory=list)
    changed_total: int = 0
    covered_total: int = 0

    @property
    def diff_coverage_pct(self) -> float:
        if self.changed_total == 0:
            return 100.0
        return round((self.covered_total / self.changed_total) * 100, 1)


def compute_diff_coverage(
    *,
    diff: list[FileLines],
    executed: dict[Path, frozenset[int]],
    base_ref: str,
    head_ref: str = "HEAD",
    exclude_patterns: list[str] | None = None,
    project_root: Path,
) -> DiffCoverageReport:
    """Intersect git diff with coverage executed lines to find uncovered diff lines."""
    exclude_patterns = exclude_patterns or []
    report = DiffCoverageReport(base_ref=base_ref, head_ref=head_ref)

    for file_lines in diff:
        rel_path = _safe_relative(file_lines.path, project_root)
        if _is_excluded(rel_path, exclude_patterns):
            continue
        if not file_lines.path.is_file() or file_lines.path.suffix != ".py":
            continue

        executed_for_file = executed.get(file_lines.path.resolve(), frozenset())
        for line in sorted(file_lines.lines):
            report.changed_total += 1
            if line in executed_for_file:
                report.covered_total += 1
            else:
                report.uncovered.append(UncoveredLine(file=file_lines.path, line=line))

    return report


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pat) for pat in patterns)
