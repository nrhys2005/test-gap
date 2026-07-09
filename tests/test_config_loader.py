from pathlib import Path

import pytest

from testgap.config.loader import (
    CONFIG_FILENAME,
    ConfigInvalidError,
    ConfigNotFoundError,
    dump_config,
    find_config,
    load_config,
)
from testgap.config.schema import LLMConfig, TestGapConfig


def test_find_config_walks_upward(tmp_project: Path):
    (tmp_project / CONFIG_FILENAME).write_text("version: 1\n", encoding="utf-8")
    nested = tmp_project / "a" / "b" / "c"
    nested.mkdir(parents=True)

    found = find_config(nested)
    assert found == tmp_project / CONFIG_FILENAME


def test_find_config_raises_when_missing(tmp_project: Path):
    with pytest.raises(ConfigNotFoundError):
        find_config(tmp_project)


def test_load_default_config(tmp_project: Path):
    (tmp_project / CONFIG_FILENAME).write_text("version: 1\n", encoding="utf-8")
    config = load_config(tmp_project / CONFIG_FILENAME)
    assert config.version == 1
    assert config.llm.model.startswith("anthropic/")


def test_load_invalid_yaml(tmp_project: Path):
    bad = tmp_project / CONFIG_FILENAME
    bad.write_text("version: 1\n  bad: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        load_config(bad)


def test_load_non_mapping(tmp_project: Path):
    bad = tmp_project / CONFIG_FILENAME
    bad.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        load_config(bad)


def test_dump_and_reload_roundtrip(tmp_project: Path):
    original = TestGapConfig(llm=LLMConfig(model="openai/gpt-4o", max_cost_per_run=1.5))
    path = tmp_project / CONFIG_FILENAME
    dump_config(original, path)

    reloaded = load_config(path)
    assert reloaded.llm.model == "openai/gpt-4o"
    assert reloaded.llm.max_cost_per_run == 1.5


def test_dump_prunes_unset_pytest_section(tmp_project: Path):
    """Auto-detection is the default — no ``pytest: {python: null}`` noise (TG-417)."""
    import yaml

    path = tmp_project / CONFIG_FILENAME
    dump_config(TestGapConfig(), path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "pytest" not in data


def test_dump_and_reload_preserves_pytest_python(tmp_project: Path):
    import yaml

    original = TestGapConfig.model_validate({"pytest": {"python": "/v/bin/python"}})
    path = tmp_project / CONFIG_FILENAME
    dump_config(original, path)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["pytest"] == {"python": "/v/bin/python"}
    reloaded = load_config(path)
    assert reloaded.pytest.python == "/v/bin/python"
