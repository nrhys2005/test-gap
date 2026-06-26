"""High-level orchestration that ties coverage → generator → validator together."""

from dataclasses import dataclass, field
from pathlib import Path

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
    GeneratedTestSet,
    LLMClient,
    LLMError,
    ParseError,
    build_messages,
    find_few_shot_examples,
    parse_response,
)
from testgap.generator.prompt import PromptContext
from testgap.validator import ValidatorResult, run_pytest_on_file


@dataclass
class FunctionSuggestion:
    function: UncoveredFunction
    generated: GeneratedTestSet | None = None
    validator_result: ValidatorResult | None = None
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.validator_result is not None and self.validator_result.all_passed


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

    messages = build_messages(
        PromptContext(
            function=func,
            module_import_path=module_import,
            few_shot_examples=few_shot,
            max_tests=config.generation.max_tests_per_function,
        )
    )

    try:
        response = llm_client.complete(messages)
    except LLMError as e:
        suggestion.error = f"LLM call failed: {e}"
        return suggestion

    try:
        tracker.record(
            label=func.qualname,
            cost_usd=response.cost_usd,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        suggestion.cost_usd = response.cost_usd
    except BudgetExceeded as e:
        suggestion.error = str(e)
        return suggestion

    try:
        generated = parse_response(response.text)
    except ParseError as e:
        suggestion.error = f"failed to parse LLM response: {e}"
        return suggestion

    suggestion.generated = generated

    test_dir = test_dirs[0] if test_dirs else (project_root / "tests")
    tmp_path = _write_temp_test(func=func, test_dir=test_dir, generated=generated)
    try:
        result = run_pytest_on_file(
            tmp_path,
            project_root=project_root,
            timeout_seconds=config.generation.test_timeout_seconds,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    suggestion.validator_result = result
    return suggestion


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
