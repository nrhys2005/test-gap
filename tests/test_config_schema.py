import pytest
from pydantic import ValidationError

from testgap.config.schema import TestGapConfig


def test_defaults_are_valid():
    config = TestGapConfig()
    assert config.version == 1
    assert config.project.language == "python"
    assert config.project.test_framework == "pytest"
    assert config.coverage.threshold == 80
    assert config.coverage.diff_threshold == 90
    assert config.llm.max_cost_per_run == 2.0


def test_rejects_unsupported_version():
    with pytest.raises(ValidationError):
        TestGapConfig(version=2)


def test_threshold_bounds():
    with pytest.raises(ValidationError):
        TestGapConfig.model_validate({"coverage": {"threshold": 150}})
    with pytest.raises(ValidationError):
        TestGapConfig.model_validate({"coverage": {"threshold": -1}})


def test_max_cost_zero_allowed():
    """0 means "no cap" — used by Ollama / local models. See TG-401 D1."""
    config = TestGapConfig.model_validate({"llm": {"max_cost_per_run": 0}})
    assert config.llm.max_cost_per_run == 0


def test_max_cost_negative_rejected():
    with pytest.raises(ValidationError):
        TestGapConfig.model_validate({"llm": {"max_cost_per_run": -1}})


def test_max_tests_per_function_bounds():
    with pytest.raises(ValidationError):
        TestGapConfig.model_validate({"generation": {"max_tests_per_function": 0}})
    with pytest.raises(ValidationError):
        TestGapConfig.model_validate({"generation": {"max_tests_per_function": 11}})


def test_pytest_python_defaults_to_none():
    """None → auto-detection at resolve time (TG-417)."""
    config = TestGapConfig()
    assert config.pytest.python is None


def test_pytest_python_parses_from_mapping():
    config = TestGapConfig.model_validate({"pytest": {"python": "/v/bin/python"}})
    assert config.pytest.python == "/v/bin/python"


def test_pytest_unknown_keys_ignored():
    """Forward compatibility: older testgap must ignore newer pytest sub-keys."""
    config = TestGapConfig.model_validate({"pytest": {"python": None, "future_key": True}})
    assert config.pytest.python is None
