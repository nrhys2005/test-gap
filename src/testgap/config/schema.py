from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ProjectConfig(BaseModel):
    language: Literal["python"] = "python"
    test_framework: Literal["pytest"] = "pytest"
    source_paths: list[str] = Field(default_factory=lambda: ["src/"])
    test_paths: list[str] = Field(default_factory=lambda: ["tests/"])


class CoverageConfig(BaseModel):
    threshold: int = Field(default=80, ge=0, le=100)
    diff_threshold: int = Field(default=90, ge=0, le=100)
    exclude: list[str] = Field(
        default_factory=lambda: ["**/migrations/**", "**/__init__.py"]
    )


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-6"
    # ``0`` means "no cap" — useful for local models (Ollama) where per-call cost
    # is effectively zero. See ``CostTracker`` for the unlimited-budget branch.
    max_cost_per_run: float = Field(default=2.0, ge=0)
    max_retries: int = Field(default=2, ge=0, le=5)


class GenerationConfig(BaseModel):
    style: Literal["match_existing", "minimal"] = "match_existing"
    include_docstrings: bool = True
    max_tests_per_function: int = Field(default=3, ge=1, le=10)
    test_timeout_seconds: int = Field(default=30, ge=1, le=600)


class PytestConfig(BaseModel):
    # Python interpreter used to run the target project's pytest (TG-417/D11).
    # None (default) → auto-detect via $VIRTUAL_ENV / $CONDA_PREFIX, then fall
    # back to sys.executable. See ``testgap.detect.python_env``.
    python: str | None = None


class TestGapConfig(BaseModel):
    __test__ = False  # not a pytest test class despite the name

    version: int = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    pytest: PytestConfig = Field(default_factory=PytestConfig)

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"Unsupported config version: {v}. Only version 1 is supported.")
        return v
