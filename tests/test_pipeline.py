"""Integration-ish test of the pipeline with a fake LLM client."""

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
