import json
import subprocess
import sys
import time
from pathlib import Path

from testgap.validator.result import TestCaseResult, TestOutcome, ValidatorResult


class ValidatorError(Exception):
    pass


def run_pytest_on_file(
    test_file: Path,
    *,
    project_root: Path,
    timeout_seconds: int = 30,
    python_executable: str | None = None,
) -> ValidatorResult:
    """Run pytest against a single file using the JSON report, return per-case results.

    ``python_executable`` selects the interpreter for the pytest subprocess
    (TG-417); ``None`` keeps the historical ``sys.executable`` behaviour. The
    runner stays dumb — resolution logic lives in
    :func:`testgap.detect.resolve_pytest_python` at the call sites.
    """
    python = python_executable or sys.executable
    report_path = project_root / ".testgap" / f"validator_{test_file.stem}.json"
    report_path.parent.mkdir(exist_ok=True)
    if report_path.exists():
        report_path.unlink()

    cmd = [
        python,
        "-m",
        "pytest",
        str(test_file),
        "--no-header",
        "--tb=short",
        "-q",
        "-p",
        "no:cacheprovider",
        "--json-report",
        f"--json-report-file={report_path}",
    ]

    start = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return ValidatorResult(
            duration_seconds=timeout_seconds,
            raw_stderr=str(e),
            exit_code=-1,
            environment_error=f"pytest timed out after {timeout_seconds}s",
        )
    except FileNotFoundError as e:
        raise ValidatorError(f"python executable not found: {python}") from e

    duration = time.monotonic() - start

    if not report_path.is_file():
        return _fallback_parse(completed, duration)

    return _parse_json_report(report_path, completed, duration)


def _parse_json_report(
    report_path: Path,
    completed: subprocess.CompletedProcess[str],
    duration: float,
) -> ValidatorResult:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fallback_parse(completed, duration)

    cases: list[TestCaseResult] = []
    for test in data.get("tests", []):
        nodeid = test.get("nodeid", "<unknown>")
        outcome_raw = test.get("outcome", "error")
        outcome = _map_outcome(outcome_raw)
        message = ""
        for phase in ("setup", "call", "teardown"):
            phase_data = test.get(phase, {}) or {}
            if phase_data.get("outcome") in ("failed", "error"):
                message = (phase_data.get("longrepr") or "").strip()
                break
        cases.append(TestCaseResult(name=nodeid, outcome=outcome, message=message))

    return ValidatorResult(
        cases=cases,
        duration_seconds=round(duration, 3),
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
        exit_code=completed.returncode,
    )


def _fallback_parse(
    completed: subprocess.CompletedProcess[str], duration: float
) -> ValidatorResult:
    """When json-report plugin isn't available, do best-effort text parsing."""
    text = completed.stdout + "\n" + completed.stderr
    env_err: str | None = None
    if "ERRORS" in text or "ERROR collecting" in text or "ImportError" in text:
        env_err = "pytest reported an environment/collection error"

    cases: list[TestCaseResult] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("PASSED"):
            name = line[len("PASSED"):].strip() or line
            cases.append(TestCaseResult(name=name, outcome=TestOutcome.PASS))
        elif line.startswith("FAILED"):
            name = line[len("FAILED"):].strip() or line
            cases.append(TestCaseResult(name=name, outcome=TestOutcome.FAIL))
        elif line.startswith("ERROR"):
            name = line[len("ERROR"):].strip() or line
            cases.append(TestCaseResult(name=name, outcome=TestOutcome.ERROR))

    return ValidatorResult(
        cases=cases,
        duration_seconds=round(duration, 3),
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
        exit_code=completed.returncode,
        environment_error=env_err,
    )


_OUTCOME_MAP = {
    "passed": TestOutcome.PASS,
    "failed": TestOutcome.FAIL,
    "error": TestOutcome.ERROR,
    "skipped": TestOutcome.SKIP,
}


def _map_outcome(raw: str) -> TestOutcome:
    return _OUTCOME_MAP.get(raw.lower(), TestOutcome.ERROR)
