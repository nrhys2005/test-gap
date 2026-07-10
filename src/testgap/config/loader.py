from pathlib import Path

import yaml
from pydantic import ValidationError

from testgap.config.schema import TestGapConfig

CONFIG_FILENAME = ".testgap.yml"


class ConfigError(Exception):
    pass


class ConfigNotFoundError(ConfigError):
    pass


class ConfigInvalidError(ConfigError):
    pass


def find_config(start: Path | None = None) -> Path:
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    raise ConfigNotFoundError(
        f"{CONFIG_FILENAME} not found in {cwd} or any parent directory. "
        "Run `testgap init` to create one."
    )


def load_config(path: Path | None = None) -> TestGapConfig:
    config_path = path or find_config()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigInvalidError(f"Failed to parse {config_path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigInvalidError(f"{config_path}: root must be a mapping, got {type(raw).__name__}")

    try:
        return TestGapConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigInvalidError(f"Invalid config at {config_path}:\n{e}") from e


def dump_config(config: TestGapConfig, path: Path) -> None:
    data = config.model_dump(mode="json")
    # Auto-detection is the default — an unset pytest section is YAML noise,
    # so prune it instead of writing ``pytest: {python: null}`` (TG-417).
    if data.get("pytest", {}).get("python") is None:
        data.pop("pytest", None)
    yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(yaml_text, encoding="utf-8")
