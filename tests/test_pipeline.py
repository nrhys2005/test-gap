"""Integration-ish test of the pipeline with a fake LLM client."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from testgap.config.schema import GenerationConfig, LLMConfig, ProjectConfig, TestGapConfig
from testgap.generator import LLMClient
from testgap.pipeline import run_diff


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def demo_project(tmp_project: Path) -> Path:
    (tmp_project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths=["tests"]\n', encoding="utf-8"
    )
    src = tmp_project / "src" / "demo"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    tests = tmp_project / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    conftest_src = (
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
    )
    (tests / "conftest.py").write_text(conftest_src, encoding="utf-8")

    _git(tmp_project, "init", "-q")
    _git(tmp_project, "add", ".")
    _git(tmp_project, "commit", "-q", "-m", "init")
    _git(tmp_project, "branch", "-M", "main")
    _git(tmp_project, "checkout", "-q", "-b", "feature")

    (src / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    _git(tmp_project, "add", ".")
    _git(tmp_project, "commit", "-q", "-m", "add sub")

    return tmp_project


def _fake_completion_factory(generated_code: str):
    def fake(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f"```json\n{generated_code}\n```"
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80),
            _hidden_params={"response_cost": 0.001},
        )

    return fake


def _queued_completion(
    payloads: list[str],
    *,
    hidden_params: list[dict] | None = None,
    usages: list[tuple[int, int]] | None = None,
):
    """Return a fake completion fn that yields different payloads per call.

    Useful for testing retry flow where 1st and 2nd responses differ.
    """
    calls = {"n": 0}

    def fake(**kwargs):
        idx = calls["n"]
        calls["n"] += 1
        if idx >= len(payloads):
            raise AssertionError(
                f"unexpected LLM call #{idx + 1}; only {len(payloads)} payloads queued"
            )
        usage = usages[idx] if usages else (100, 80)
        hidden = hidden_params[idx] if hidden_params else {"response_cost": 0.001}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"```json\n{payloads[idx]}\n```")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
            _hidden_params=hidden,
        )

    return fake, calls


def _payload(tests: list[dict], imports: list[str] | None = None) -> str:
    return json.dumps(
        {"imports": imports or ["from demo.calc import sub"], "tests": tests}
    )


def _test_entry(name: str, body: str) -> dict:
    code = f"def {name}():\n    {body}"
    return {"name": name, "purpose": "auto", "code": code}


def test_pipeline_generates_and_validates(demo_project: Path):
    test_code = (
        "def test_sub_returns_difference():\\n"
        "    from demo.calc import sub\\n"
        "    assert sub(5, 3) == 2"
    )
    payload = (
        '{"imports": ["from demo.calc import sub"], '
        '"tests": [{"name": "test_sub_returns_difference", "purpose": "happy path", '
        f'"code": "{test_code}"}}]}}'
    )
    client = LLMClient(model="fake/model", completion_fn=_fake_completion_factory(payload))

    config = TestGapConfig(
        project=ProjectConfig(source_paths=["src/"], test_paths=["tests/"]),
        llm=LLMConfig(model="fake/model", max_cost_per_run=1.0),
        generation=GenerationConfig(test_timeout_seconds=30),
    )

    report = run_diff(
        project_root=demo_project,
        config=config,
        llm_client=client,
        base_ref="main",
    )

    assert report.suggestions, "expected at least one suggestion"
    succeeded = [s for s in report.suggestions if s.succeeded]
    errors = [s.error for s in report.suggestions]
    assert succeeded, f"expected successful suggestion. errors: {errors}"
    assert report.cost_total > 0
    # Existing happy-path case must also satisfy the stricter `fully_passed` semantic.
    assert succeeded[0].fully_passed
    assert succeeded[0].attempts == 1


def test_pipeline_skips_when_no_diff(tmp_project: Path):
    _git(tmp_project, "init", "-q")
    (tmp_project / "x.py").write_text("X = 1\n", encoding="utf-8")
    _git(tmp_project, "add", ".")
    _git(tmp_project, "commit", "-q", "-m", "init")
    _git(tmp_project, "branch", "-M", "main")

    client = LLMClient(model="fake", completion_fn=_fake_completion_factory("{}"))
    config = TestGapConfig()

    report = run_diff(
        project_root=tmp_project,
        config=config,
        llm_client=client,
        base_ref="main",
        head_ref="main",
    )
    assert report.skipped_reason is not None
    assert report.suggestions == []


def _config(max_cost: float = 1.0) -> TestGapConfig:
    return TestGapConfig(
        project=ProjectConfig(source_paths=["src/"], test_paths=["tests/"]),
        llm=LLMConfig(model="fake/model", max_cost_per_run=max_cost),
        generation=GenerationConfig(test_timeout_seconds=30),
    )


def test_partial_pass_keeps_passed_and_retries_failed(demo_project: Path):
    first = _payload([
        _test_entry("test_sub_pass", "from demo.calc import sub\n    assert sub(5, 3) == 2"),
        _test_entry("test_sub_fail", "assert False, 'intentional'"),
    ])
    second = _payload([
        _test_entry(
            "test_sub_retry", "from demo.calc import sub\n    assert sub(10, 4) == 6"
        ),
    ])
    fn, calls = _queued_completion([first, second])
    client = LLMClient(model="fake/model", completion_fn=fn)

    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )

    assert calls["n"] == 2
    assert len(report.suggestions) == 1
    s = report.suggestions[0]
    assert s.error is None
    assert s.attempts == 2
    assert len(s.accepted_cases) == 2
    assert len(s.discarded_cases) == 1
    assert s.succeeded
    assert not s.fully_passed
    # Retry was actually exercised → cost spans both calls.
    assert s.cost_usd == pytest.approx(0.002)


def test_retry_failure_keeps_first_round_pass(demo_project: Path):
    first = _payload([
        _test_entry("test_sub_pass", "from demo.calc import sub\n    assert sub(5, 3) == 2"),
        _test_entry("test_sub_fail_a", "assert False"),
        _test_entry("test_sub_fail_b", "assert False"),
    ])
    second = _payload([
        _test_entry("test_sub_fail_a", "assert False"),
        _test_entry("test_sub_fail_b", "assert False"),
    ])
    fn, calls = _queued_completion([first, second])
    client = LLMClient(model="fake/model", completion_fn=fn)

    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )

    assert calls["n"] == 2
    s = report.suggestions[0]
    assert s.attempts == 2
    assert len(s.accepted_cases) == 1
    assert len(s.discarded_cases) >= 2
    assert s.succeeded is True
    assert s.fully_passed is False
    assert s.error is None


def test_no_retry_when_first_round_all_pass(demo_project: Path):
    first = _payload([
        _test_entry("test_sub_only_pass", "from demo.calc import sub\n    assert sub(5, 3) == 2"),
    ])
    fn, calls = _queued_completion([first])
    client = LLMClient(model="fake/model", completion_fn=fn)

    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )

    assert calls["n"] == 1
    s = report.suggestions[0]
    assert s.attempts == 1
    assert s.fully_passed is True
    assert s.discarded_cases == []
    assert s.retry_skipped_reason is None


def test_no_retry_when_environment_error(demo_project: Path, monkeypatch):
    from testgap import pipeline as pipeline_mod
    from testgap.validator.result import ValidatorResult

    def fake_runner(*args, **kwargs):
        return ValidatorResult(
            cases=[],
            duration_seconds=0.0,
            raw_stdout="",
            raw_stderr="",
            exit_code=2,
            environment_error="pytest collection blew up",
        )

    monkeypatch.setattr(pipeline_mod, "run_pytest_on_file", fake_runner)

    first = _payload([
        _test_entry("test_anything", "assert True"),
    ])
    fn, calls = _queued_completion([first])
    client = LLMClient(model="fake/model", completion_fn=fn)

    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )

    s = report.suggestions[0]
    assert calls["n"] == 1
    assert s.attempts == 1
    assert s.error is None
    assert s.validator_result is not None
    assert s.validator_result.environment_error == "pytest collection blew up"


def test_pipeline_passes_configured_python_to_runners(demo_project: Path, monkeypatch):
    """TG-417: ``config.pytest.python`` reaches both pytest runners as
    ``python_executable`` after resolution against the project root.

    Both runners are replaced by capturing fakes — the configured interpreter
    is a bare touched file, so a real subprocess exec would fail."""
    from testgap import pipeline as pipeline_mod
    from testgap.coverage.runner import CoverageRunResult
    from testgap.validator.result import ValidatorResult

    venv_python = demo_project / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    calc = (demo_project / "src" / "demo" / "calc.py").resolve()
    received_coverage: dict = {}
    received_validator: dict = {}

    def fake_coverage(project_root, source_paths, **kwargs):
        received_coverage.update(kwargs)
        # ``sub`` (lines 4-5) left unexecuted → it becomes the pipeline target.
        return CoverageRunResult(
            coverage_json_path=project_root / ".testgap" / "coverage.json",
            executed_lines={calc: frozenset({1, 2})},
            raw_pytest_exit_code=0,
        )

    def fake_validator(*args, **kwargs):
        received_validator.update(kwargs)
        # environment_error suppresses the retry round → exactly one LLM call.
        return ValidatorResult(
            cases=[],
            duration_seconds=0.0,
            raw_stdout="",
            raw_stderr="",
            exit_code=2,
            environment_error="fake env error",
        )

    monkeypatch.setattr(pipeline_mod, "run_pytest_with_coverage", fake_coverage)
    monkeypatch.setattr(pipeline_mod, "run_pytest_on_file", fake_validator)

    first = _payload([_test_entry("test_anything", "assert True")])
    fn, _calls = _queued_completion([first])
    client = LLMClient(model="fake/model", completion_fn=fn)

    config = _config()
    config.pytest.python = ".venv/bin/python"
    run_diff(
        project_root=demo_project,
        config=config,
        llm_client=client,
        base_ref="main",
    )

    expected = str(venv_python.resolve())
    assert received_coverage["python_executable"] == expected
    assert received_validator["python_executable"] == expected


def test_retry_skipped_when_budget_would_exceed(demo_project: Path):
    """When 1st-round cost leaves no headroom for retry, guard fires."""
    first = _payload([
        _test_entry("test_sub_pass", "from demo.calc import sub\n    assert sub(5, 3) == 2"),
        _test_entry("test_sub_fail", "assert False"),
    ])
    fn, calls = _queued_completion(
        [first],
        hidden_params=[{"response_cost": 0.5}],
        usages=[(200, 100)],
    )
    client = LLMClient(model="fake/model", completion_fn=fn)

    report = run_diff(
        project_root=demo_project,
        config=_config(max_cost=0.6),
        llm_client=client,
        base_ref="main",
    )

    assert calls["n"] == 1, "retry must NOT execute when budget guard fires"
    s = report.suggestions[0]
    assert s.attempts == 1
    assert s.retry_skipped_reason is not None
    assert s.retry_skipped_reason.startswith("retry would exceed budget")
    assert "remaining" in s.retry_skipped_reason
    # 1st-round pass case remains accepted.
    assert len(s.accepted_cases) == 1
    assert len(s.discarded_cases) == 1
    assert s.succeeded is True


def _multi_function_project(demo_project: Path, monkeypatch, n: int) -> None:
    """Monkeypatch ``discover_targets`` to return ``n`` copies of the single real
    uncovered function so tests can exercise multi-function flows deterministically.
    """
    from testgap import pipeline as pipeline_mod
    from testgap.coverage import UncoveredFunction

    real_funcs, meta = pipeline_mod.discover_targets(
        project_root=demo_project,
        config=_config(),
        base_ref="main",
        head_ref="HEAD",
        max_functions=None,
    )
    assert real_funcs, "demo_project should have exactly one uncovered function"
    base = real_funcs[0]
    duplicates = [
        UncoveredFunction(
            file=base.file,
            qualname="sub",  # matches the real function; validation passes on success
            start_line=base.start_line,
            end_line=base.end_line,
            source=base.source,
            uncovered_lines=base.uncovered_lines,
            has_branch=base.has_branch,
        )
        for _ in range(n)
    ]
    monkeypatch.setattr(
        pipeline_mod, "discover_targets", lambda **kw: (duplicates, meta)
    )


def _raising_completion(exc_factory):
    def fn(**kwargs):
        raise exc_factory()

    return fn


def test_provider_unhealthy_skips_remaining_functions(demo_project: Path, monkeypatch):
    """Two consecutive full-LLM-failures trigger provider-unhealthy early-exit."""
    from testgap.generator import LLMError

    _multi_function_project(demo_project, monkeypatch, n=5)
    client = LLMClient(
        model="fake/model",
        completion_fn=_raising_completion(lambda: LLMError("500 server error")),
        max_retries=0,
    )

    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )

    assert report.provider_unhealthy is True
    assert report.unhealthy_reason is not None
    assert "consecutive" in report.unhealthy_reason
    # Break after 2nd failure — third function must NOT be processed.
    assert len(report.suggestions) == 2
    for s in report.suggestions:
        assert s.llm_failure_observed is True
        assert s.accepted_cases == []


def test_consecutive_llm_failures_reset_after_success(demo_project: Path, monkeypatch):
    """fail → success → fail → success — counter never reaches 2 → complete run."""
    from testgap.generator import LLMError

    _multi_function_project(demo_project, monkeypatch, n=4)
    good_payload = _payload(
        [_test_entry("test_sub_ok", "from demo.calc import sub\n    assert sub(5,3)==2")]
    )
    # Sequence per function: LLMError, success, LLMError, success. Each fn issues
    # a single LLM call (max_retries=0, no partial-pass retry because success is
    # full-pass or full-LLM-fail).
    seq: list[LLMError | str] = [LLMError("boom"), good_payload, LLMError("boom"), good_payload]
    call_state = {"i": 0}

    def scripted(**kwargs):
        idx = call_state["i"]
        call_state["i"] += 1
        step = seq[idx]
        if isinstance(step, LLMError):
            raise step
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"```json\n{step}\n```")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80),
            _hidden_params={"response_cost": 0.001},
        )

    client = LLMClient(model="fake/model", completion_fn=scripted, max_retries=0)
    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )
    # Counter timeline: +1, reset, +1, reset — never reaches 2, so no early exit.
    assert report.provider_unhealthy is False
    assert len(report.suggestions) == 4


def test_partial_pass_resets_consecutive_counter(demo_project: Path, monkeypatch):
    """fn1 partial-pass → fn2 full-LLM-fail → fn3 partial-pass. No early exit."""
    from testgap.generator import LLMError

    _multi_function_project(demo_project, monkeypatch, n=3)

    partial = _payload(
        [
            _test_entry(
                "test_sub_pass",
                "from demo.calc import sub\n    assert sub(5, 3) == 2",
            ),
            _test_entry("test_sub_fail", "assert False, 'x'"),
        ]
    )
    retry_fail = _payload(
        [_test_entry("test_sub_retry_fail", "assert False, 'still fails'")]
    )

    # For each function process_function issues up to 2 LLM calls: partial pass
    # triggers a retry. So per function we need [partial, retry_fail] payloads
    # OR raise LLMError. Scripted plan:
    #   fn1: partial → retry_fail (accepted_cases non-empty → counter reset)
    #   fn2: LLMError (first call raises → llm_failure_observed=True, no accepted → +1)
    #   fn3: partial → retry_fail (counter reset)
    step_state = {"i": 0}

    def scripted(**kwargs):
        i = step_state["i"]
        step_state["i"] += 1
        # Sequence of responses. LLMError instances raised; strings JSON'd.
        seq = [partial, retry_fail, LLMError("fn2 boom"), partial, retry_fail]
        item = seq[i]
        if isinstance(item, LLMError):
            raise item
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"```json\n{item}\n```")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80),
            _hidden_params={"response_cost": 0.001},
        )

    client = LLMClient(model="fake/model", completion_fn=scripted, max_retries=0)
    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )

    assert report.provider_unhealthy is False
    assert len(report.suggestions) == 3
    # fn1 & fn3 accepted at least one case
    assert report.suggestions[0].accepted_cases
    assert report.suggestions[2].accepted_cases
    # fn2 completely failed on LLM
    fn2 = report.suggestions[1]
    assert fn2.llm_failure_observed is True
    assert fn2.accepted_cases == []


def test_two_round_llm_failure_counts_once(demo_project: Path, monkeypatch):
    """A single function whose 1st AND 2nd round both raise LLMError counts +1."""
    from testgap.generator import LLMError

    _multi_function_project(demo_project, monkeypatch, n=1)
    client = LLMClient(
        model="fake/model",
        completion_fn=_raising_completion(lambda: LLMError("still 500")),
        max_retries=1,  # 2 calls total per invocation
    )
    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )
    assert len(report.suggestions) == 1
    # Counter should have gone from 0 to 1 (still under threshold).
    assert report.provider_unhealthy is False
    assert report.suggestions[0].llm_failure_observed is True


def test_llm_failure_observed_flag_set_on_first_round_llm_error(
    demo_project: Path, monkeypatch
):
    from testgap.generator import LLMError

    _multi_function_project(demo_project, monkeypatch, n=1)
    client = LLMClient(
        model="fake/model",
        completion_fn=_raising_completion(lambda: LLMError("boom")),
        max_retries=0,
    )
    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )
    assert report.suggestions[0].llm_failure_observed is True


def test_llm_failure_observed_flag_set_on_second_round_llm_error(
    demo_project: Path, monkeypatch
):
    """1st round succeeds partially → 2nd round LLMError → flag True + acceptance kept."""
    from testgap.generator import LLMError

    _multi_function_project(demo_project, monkeypatch, n=1)

    partial = _payload(
        [
            _test_entry(
                "test_sub_pass",
                "from demo.calc import sub\n    assert sub(5, 3) == 2",
            ),
            _test_entry("test_sub_fail", "assert False, 'x'"),
        ]
    )
    steps = {"i": 0}

    def scripted(**kwargs):
        i = steps["i"]
        steps["i"] += 1
        if i == 0:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"```json\n{partial}\n```")
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80),
                _hidden_params={"response_cost": 0.001},
            )
        raise LLMError("retry boom")

    client = LLMClient(model="fake/model", completion_fn=scripted, max_retries=0)
    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )
    s = report.suggestions[0]
    assert s.llm_failure_observed is True
    # 1st-round pass survived the retry failure.
    assert s.accepted_cases


def test_llm_failure_observed_flag_false_for_parse_only_failures(
    demo_project: Path, monkeypatch
):
    """LLM answers but the response cannot be parsed → flag stays False."""
    _multi_function_project(demo_project, monkeypatch, n=1)

    def unparseable(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="not JSON, not code — nothing usable")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80),
            _hidden_params={"response_cost": 0.001},
        )

    client = LLMClient(model="fake/model", completion_fn=unparseable, max_retries=0)
    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )
    s = report.suggestions[0]
    assert s.llm_failure_observed is False
    assert s.error is not None and s.error.startswith("parse:")


def test_pipeline_succeeded_means_any_accepted(demo_project: Path):
    """Documents the BREAKING semantic change: succeeded ↔ at least one accepted case."""
    first = _payload([
        _test_entry("test_sub_pass", "from demo.calc import sub\n    assert sub(5, 3) == 2"),
        _test_entry("test_sub_fail", "assert False"),
    ])
    # Second round also fails → discarded grows, but 1st-round pass keeps `succeeded` True.
    second = _payload([_test_entry("test_retry_fail", "assert False")])
    fn, _calls = _queued_completion([first, second])
    client = LLMClient(model="fake/model", completion_fn=fn)

    report = run_diff(
        project_root=demo_project,
        config=_config(),
        llm_client=client,
        base_ref="main",
    )
    s = report.suggestions[0]
    assert s.accepted_cases, "at least one accepted case expected"
    assert s.discarded_cases, "at least one discarded case expected"
    assert s.succeeded is True
    assert s.fully_passed is False
