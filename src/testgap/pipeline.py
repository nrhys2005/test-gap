"""High-level orchestration that ties coverage → generator → validator together."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from testgap.config.schema import TestGapConfig
from testgap.cost import BudgetExceeded, CostTracker
from testgap.coverage import (
    UncoveredFunction,
    changed_lines,
    compute_diff_coverage,
    group_by_function,
    resolve_base_ref,
    run_pytest_with_coverage,
)
from testgap.generator import (
    GeneratedTest,
    GeneratedTestSet,
    LLMClient,
    LLMError,
    LLMResponse,
    ParseError,
    build_messages,
    find_few_shot_examples,
    parse_response,
)
from testgap.generator.prompt import PreviousFailure, PromptContext
from testgap.validator import TestCaseResult, ValidatorResult, run_pytest_on_file


@dataclass
class FunctionSuggestion:
    function: UncoveredFunction
    generated: GeneratedTestSet | None = None
    validator_result: ValidatorResult | None = None
    cost_usd: float = 0.0
    error: str | None = None
    attempts: int = 0
    accepted_cases: list[TestCaseResult] = field(default_factory=list)
    discarded_cases: list[TestCaseResult] = field(default_factory=list)
    retry_skipped_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        """True when at least one generated test was accepted.

        BREAKING (internal): previously meant "all tests passed". The strict
        all-passed semantics now live on :attr:`fully_passed`.
        """
        return bool(self.accepted_cases)

    @property
    def fully_passed(self) -> bool:
        """True only when every generated test passed on the first round.

        Returns False whenever a discarded case exists, an error was recorded,
        or ``environment_error`` was set (because in that case ``accepted_cases``
        is empty).
        """
        if self.error is not None:
            return False
        if not self.accepted_cases or self.discarded_cases:
            return False
        if self.validator_result is None:
            return False
        return self.validator_result.environment_error is None


@dataclass
class DiffRunReport:
    base_ref: str
    head_ref: str
    diff_coverage_pct: float
    changed_total: int
    covered_total: int
    suggestions: list[FunctionSuggestion] = field(default_factory=list)
    cost_total: float = 0.0
    skipped_reason: str | None = None


@dataclass
class _CallFailure:
    kind: Literal["llm", "parse", "budget"]
    message: str


@dataclass
class _CallSuccess:
    generated: GeneratedTestSet
    validator_result: ValidatorResult
    response: LLMResponse


def run_diff(
    *,
    project_root: Path,
    config: TestGapConfig,
    llm_client: LLMClient,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
    max_functions: int | None = None,
) -> DiffRunReport:
    resolved_base = resolve_base_ref(project_root, base_ref)
    diff = changed_lines(project_root, resolved_base, head_ref)

    if not diff:
        return DiffRunReport(
            base_ref=resolved_base,
            head_ref=head_ref,
            diff_coverage_pct=100.0,
            changed_total=0,
            covered_total=0,
            skipped_reason="no changed Python lines in diff",
        )

    coverage_run = run_pytest_with_coverage(project_root, config.project.source_paths)

    diff_report = compute_diff_coverage(
        diff=diff,
        executed=coverage_run.executed_lines,
        base_ref=resolved_base,
        head_ref=head_ref,
        exclude_patterns=config.coverage.exclude,
        project_root=project_root,
    )

    if not diff_report.uncovered:
        return DiffRunReport(
            base_ref=resolved_base,
            head_ref=head_ref,
            diff_coverage_pct=diff_report.diff_coverage_pct,
            changed_total=diff_report.changed_total,
            covered_total=diff_report.covered_total,
            skipped_reason="all changed lines are covered",
        )

    functions = group_by_function(diff_report.uncovered)
    if max_functions is not None:
        functions = functions[:max_functions]

    tracker = CostTracker(max_cost_per_run=config.llm.max_cost_per_run)
    test_dirs = [project_root / p.rstrip("/") for p in config.project.test_paths]

    suggestions: list[FunctionSuggestion] = []
    for func in functions:
        suggestion = _process_function(
            func=func,
            project_root=project_root,
            config=config,
            llm_client=llm_client,
            tracker=tracker,
            test_dirs=test_dirs,
        )
        suggestions.append(suggestion)
        if tracker.remaining <= 0:
            break

    return DiffRunReport(
        base_ref=resolved_base,
        head_ref=head_ref,
        diff_coverage_pct=diff_report.diff_coverage_pct,
        changed_total=diff_report.changed_total,
        covered_total=diff_report.covered_total,
        suggestions=suggestions,
        cost_total=tracker.spent,
    )


def _process_function(
    *,
    func: UncoveredFunction,
    project_root: Path,
    config: TestGapConfig,
    llm_client: LLMClient,
    tracker: CostTracker,
    test_dirs: list[Path],
) -> FunctionSuggestion:
    suggestion = FunctionSuggestion(function=func)

    few_shot = find_few_shot_examples(
        test_dirs=test_dirs,
        target_module_path=func.file,
        project_root=project_root,
    )
    module_import = _module_import_path(func.file, project_root, config.project.source_paths)
    test_dir = test_dirs[0] if test_dirs else (project_root / "tests")

    base_ctx_kwargs = dict(
        function=func,
        module_import_path=module_import,
        few_shot_examples=few_shot,
        max_tests=config.generation.max_tests_per_function,
    )

    # --- 1st round generation + validation ---
    first_msgs = build_messages(PromptContext(**base_ctx_kwargs))
    first_round = _call_and_validate(
        messages=first_msgs,
        func=func,
        tracker=tracker,
        llm_client=llm_client,
        test_dir=test_dir,
        project_root=project_root,
        config=config,
        suggestion=suggestion,
    )

    # The 1st-round _CallFailure branch is the ONLY legitimate path that does not
    # reach `_finalize` — there is nothing to finalize because no validator result
    # exists. `s.error` carries the user-facing reason; CLI handles `s.error` early.
    if isinstance(first_round, _CallFailure):
        suggestion.error = f"{first_round.kind}: {first_round.message}"
        return suggestion

    generated_first = first_round.generated
    vr_first = first_round.validator_result
    accepted = list(vr_first.passed)
    failed = list(vr_first.failed)

    # No retry when environment broke or everything already passed.
    if vr_first.environment_error or not failed:
        _finalize(
            suggestion,
            generated=generated_first,
            vrs=[vr_first],
            accepted=accepted,
            discarded=failed,
        )
        return suggestion

    # --- Budget guard ---
    estimated = _estimate_retry_cost(first_msgs, first_round.response)
    if tracker.would_exceed(estimated):
        suggestion.retry_skipped_reason = (
            f"retry would exceed budget (need ~${estimated:.4f}, "
            f"remaining ${tracker.remaining:.4f})"
        )
        _finalize(
            suggestion,
            generated=generated_first,
            vrs=[vr_first],
            accepted=accepted,
            discarded=failed,
        )
        return suggestion

    # --- 2nd round (retry only the failures) ---
    prev_failures = _build_previous_failures(generated_first, failed)
    retry_msgs = build_messages(
        PromptContext(**base_ctx_kwargs, previous_failures=prev_failures)
    )
    second_round = _call_and_validate(
        messages=retry_msgs,
        func=func,
        tracker=tracker,
        llm_client=llm_client,
        test_dir=test_dir,
        project_root=project_root,
        config=config,
        suggestion=suggestion,
    )

    if isinstance(second_round, _CallFailure):
        if second_round.kind == "budget":
            suggestion.retry_skipped_reason = "budget exceeded during retry"
        else:
            suggestion.retry_skipped_reason = (
                f"retry failed: {second_round.kind}: {second_round.message}"
            )
        # Only promote retry failure to `error` when nothing was accepted in the
        # 1st round — otherwise partial acceptance is still a success.
        if not accepted:
            suggestion.error = suggestion.retry_skipped_reason
        _finalize(
            suggestion,
            generated=generated_first,
            vrs=[vr_first],
            accepted=accepted,
            discarded=failed,
        )
        return suggestion

    generated_second = second_round.generated
    vr_second = second_round.validator_result
    accepted2 = list(vr_second.passed)
    failed2 = list(vr_second.failed)
    accepted_all = accepted + accepted2
    discarded_all = failed + failed2

    merged = _merge_generated(generated_first, generated_second, accepted_all)
    _finalize(
        suggestion,
        generated=merged,
        vrs=[vr_first, vr_second],
        accepted=accepted_all,
        discarded=discarded_all,
    )
    return suggestion


def _call_and_validate(
    *,
    messages: list[dict[str, str]],
    func: UncoveredFunction,
    tracker: CostTracker,
    llm_client: LLMClient,
    test_dir: Path,
    project_root: Path,
    config: TestGapConfig,
    suggestion: FunctionSuggestion,
) -> _CallSuccess | _CallFailure:
    """One LLM round: call → record cost → parse → write tmp → run pytest.

    Responsibilities:
    * Increments ``suggestion.attempts`` right after a successful LLM call so
      the "≤ 2 LLM calls per function" guarantee is directly checkable.
    * Accumulates ``suggestion.cost_usd`` across rounds.
    * Owns its temp-file lifecycle via ``try/finally`` — the 1st-round finally
      runs before the 2nd-round write, so identical stems do not collide and
      exception paths still unlink.
    """
    try:
        response = llm_client.complete(messages)
    except LLMError as e:
        return _CallFailure(kind="llm", message=str(e))

    suggestion.attempts += 1

    try:
        tracker.record(
            label=func.qualname,
            cost_usd=response.cost_usd,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
    except BudgetExceeded as e:
        return _CallFailure(kind="budget", message=str(e))

    suggestion.cost_usd += response.cost_usd

    try:
        generated = parse_response(response.text)
    except ParseError as e:
        return _CallFailure(kind="parse", message=str(e))

    if not generated.tests:
        return _CallFailure(kind="parse", message="parsed test set is empty")

    tmp_path = _write_temp_test(func=func, test_dir=test_dir, generated=generated)
    try:
        result = run_pytest_on_file(
            tmp_path,
            project_root=project_root,
            timeout_seconds=config.generation.test_timeout_seconds,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    return _CallSuccess(generated=generated, validator_result=result, response=response)


def _short_name(nodeid: str) -> str:
    """Reduce a pytest nodeid to the bare test function name.

    Examples:
        ``tests/test_x.py::TestY::test_alpha[case-1]`` → ``test_alpha``
        ``test_alpha`` → ``test_alpha`` (idempotent for already-short inputs)
    """
    last = nodeid.rsplit("::", 1)[-1]
    return last.split("[", 1)[0]


def _build_previous_failures(
    generated: GeneratedTestSet, failed_cases: list[TestCaseResult]
) -> list[PreviousFailure]:
    by_name = {t.name: t for t in generated.tests}
    failures: list[PreviousFailure] = []
    for case in failed_cases:
        short = _short_name(case.name)
        match = by_name.get(short)
        code = match.code if match is not None else ""
        failures.append(
            PreviousFailure(test_name=short, test_code=code, failure_message=case.message)
        )
    return failures


def _estimate_retry_cost(
    retry_msgs: list[dict[str, str]], first_response: LLMResponse
) -> float:
    """Estimate USD cost of a 2nd LLM call from 1st-round per-token economics.

    Returns 0.0 when the 1st-round cost is unknown (so the budget guard becomes
    a no-op and the real ``tracker.record`` call enforces the limit instead).
    """
    response_tokens = 2000
    total_first = first_response.input_tokens + first_response.output_tokens
    if first_response.cost_usd <= 0 or total_first <= 0:
        return 0.0
    unit_cost = first_response.cost_usd / total_first

    text = "\n".join(m.get("content", "") for m in retry_msgs)
    input_tokens = _count_tokens(text)
    return unit_cost * (input_tokens + response_tokens)


def _count_tokens(text: str) -> int:
    try:
        from litellm import token_counter  # type: ignore[import-not-found]

        return int(token_counter(model="gpt-3.5-turbo", text=text))
    except Exception:
        return max(1, len(text) // 4)


def _merge_generated(
    first: GeneratedTestSet,
    second: GeneratedTestSet,
    accepted_cases: list[TestCaseResult],
) -> GeneratedTestSet:
    """Combine two generated sets, keeping only tests whose names were accepted.

    Imports are deduplicated (first occurrence wins). When the same test name
    appears in both rounds, the 2nd-round version takes precedence.
    """
    accepted_names = {_short_name(c.name) for c in accepted_cases}

    merged_imports: list[str] = []
    seen_imports: set[str] = set()
    for imp in list(first.imports) + list(second.imports):
        if imp not in seen_imports:
            seen_imports.add(imp)
            merged_imports.append(imp)

    by_name: dict[str, GeneratedTest] = {}
    for t in first.tests:
        if t.name in accepted_names:
            by_name[t.name] = t
    for t in second.tests:
        if t.name in accepted_names:
            by_name[t.name] = t  # 2nd round wins on name collision

    return GeneratedTestSet(imports=merged_imports, tests=list(by_name.values()))


def _synthesize_validator_result(
    vrs: list[ValidatorResult],
    accepted: list[TestCaseResult],
    discarded: list[TestCaseResult],
) -> ValidatorResult:
    """Build a single ValidatorResult that represents accepted+discarded cases.

    ``raw_stdout`` is concatenated across rounds, ``exit_code`` reflects the last
    round, and ``environment_error`` is the first non-null env error encountered.
    """
    if not vrs:
        return ValidatorResult(cases=list(accepted) + list(discarded))

    env_err: str | None = next((vr.environment_error for vr in vrs if vr.environment_error), None)
    raw_stdout = "\n".join(vr.raw_stdout for vr in vrs if vr.raw_stdout)
    raw_stderr = "\n".join(vr.raw_stderr for vr in vrs if vr.raw_stderr)
    duration = sum(vr.duration_seconds for vr in vrs)

    return ValidatorResult(
        cases=list(accepted) + list(discarded),
        duration_seconds=round(duration, 3),
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=vrs[-1].exit_code,
        environment_error=env_err,
    )


def _finalize(
    suggestion: FunctionSuggestion,
    *,
    generated: GeneratedTestSet,
    vrs: list[ValidatorResult],
    accepted: list[TestCaseResult],
    discarded: list[TestCaseResult],
) -> None:
    """Single sink for finalizing a suggestion. Must be called at most once."""
    suggestion.generated = generated
    suggestion.validator_result = _synthesize_validator_result(vrs, accepted, discarded)
    suggestion.accepted_cases = list(accepted)
    suggestion.discarded_cases = list(discarded)


def _write_temp_test(
    *, func: UncoveredFunction, test_dir: Path, generated: GeneratedTestSet
) -> Path:
    test_dir.mkdir(parents=True, exist_ok=True)
    stem = func.qualname.replace(".", "_")
    # Use a test_*.py prefix so pytest collects it. Cleaned up after validation.
    path = test_dir / f"test_testgap_tmp_{stem}.py"
    path.write_text(generated.to_source(), encoding="utf-8")
    return path


def _module_import_path(file: Path, project_root: Path, source_paths: list[str]) -> str:
    try:
        rel = file.resolve().relative_to(project_root.resolve())
    except ValueError:
        return file.stem

    parts = list(rel.with_suffix("").parts)
    for src in source_paths:
        prefix = src.rstrip("/").split("/")
        if parts[: len(prefix)] == prefix:
            parts = parts[len(prefix) :]
            break

    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or file.stem
