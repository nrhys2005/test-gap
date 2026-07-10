import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class CoverageError(Exception):
    pass


@dataclass
class CoverageRunResult:
    coverage_json_path: Path
    executed_lines: dict[Path, frozenset[int]]
    raw_pytest_exit_code: int
    # Per-file raw ``summary`` block from coverage.json (num_statements,
    # missing_lines, etc.). Populated by :func:`_parse_coverage_json`. Consumers
    # that only need executed lines can ignore it; ``testgap.scan`` uses it to
    # compute per-file totals without re-parsing coverage.json. Kept as
    # ``default_factory=dict`` so existing fake constructors stay valid without
    # setting the new field.
    file_summaries: dict[Path, dict] = field(default_factory=dict)


def run_pytest_with_coverage(
    project_root: Path,
    source_paths: list[str],
    extra_pytest_args: list[str] | None = None,
    timeout_seconds: int = 300,
    python_executable: str | None = None,
) -> CoverageRunResult:
    """Run pytest under coverage.py, return per-file executed line sets.

    ``python_executable`` selects the interpreter for the pytest subprocess
    (TG-417); ``None`` keeps the historical ``sys.executable`` behaviour. The
    runner stays dumb — resolution logic lives in
    :func:`testgap.detect.resolve_pytest_python` at the call sites.
    """
    python = python_executable or sys.executable
    output_dir = project_root / ".testgap"
    output_dir.mkdir(exist_ok=True)
    json_path = output_dir / "coverage.json"
    if json_path.exists():
        json_path.unlink()

    source_args = []
    for path in source_paths:
        source_args.extend(["--cov", path.rstrip("/")])

    cmd = [
        python,
        "-m",
        "pytest",
        *source_args,
        "--cov-report",
        f"json:{json_path}",
        "-q",
        "--no-header",
    ]
    if extra_pytest_args:
        cmd.extend(extra_pytest_args)

    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise CoverageError(f"pytest timed out after {timeout_seconds}s") from e
    except FileNotFoundError as e:
        raise CoverageError(f"python executable not found: {python}") from e

    if not json_path.is_file():
        raise CoverageError(
            f"coverage.json was not produced. pytest stderr:\n{result.stderr.strip()}"
        )

    executed, summaries = _parse_coverage_json(json_path, project_root)
    return CoverageRunResult(
        coverage_json_path=json_path,
        executed_lines=executed,
        raw_pytest_exit_code=result.returncode,
        file_summaries=summaries,
    )


def _parse_coverage_json(
    json_path: Path, project_root: Path
) -> tuple[dict[Path, frozenset[int]], dict[Path, dict]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CoverageError(f"failed to read {json_path}: {e}") from e

    files = data.get("files", {})
    executed: dict[Path, frozenset[int]] = {}
    summaries: dict[Path, dict] = {}
    for raw_path, info in files.items():
        abs_path = (project_root / raw_path).resolve()
        exec_lines = info.get("executed_lines", []) or []
        executed[abs_path] = frozenset(int(n) for n in exec_lines)
        summary = info.get("summary")
        if isinstance(summary, dict):
            summaries[abs_path] = dict(summary)
    return executed, summaries
