import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class CoverageError(Exception):
    pass


@dataclass
class CoverageRunResult:
    coverage_json_path: Path
    executed_lines: dict[Path, frozenset[int]]
    raw_pytest_exit_code: int


def run_pytest_with_coverage(
    project_root: Path,
    source_paths: list[str],
    extra_pytest_args: list[str] | None = None,
    timeout_seconds: int = 300,
) -> CoverageRunResult:
    """Run pytest under coverage.py, return per-file executed line sets."""
    output_dir = project_root / ".testgap"
    output_dir.mkdir(exist_ok=True)
    json_path = output_dir / "coverage.json"
    if json_path.exists():
        json_path.unlink()

    source_args = []
    for path in source_paths:
        source_args.extend(["--cov", path.rstrip("/")])

    cmd = [
        sys.executable,
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
        raise CoverageError("python executable not found on PATH") from e

    if not json_path.is_file():
        raise CoverageError(
            f"coverage.json was not produced. pytest stderr:\n{result.stderr.strip()}"
        )

    executed = _parse_coverage_json(json_path, project_root)
    return CoverageRunResult(
        coverage_json_path=json_path,
        executed_lines=executed,
        raw_pytest_exit_code=result.returncode,
    )


def _parse_coverage_json(json_path: Path, project_root: Path) -> dict[Path, frozenset[int]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CoverageError(f"failed to read {json_path}: {e}") from e

    files = data.get("files", {})
    out: dict[Path, frozenset[int]] = {}
    for raw_path, info in files.items():
        abs_path = (project_root / raw_path).resolve()
        executed = info.get("executed_lines", []) or []
        out[abs_path] = frozenset(int(n) for n in executed)
    return out
