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
