from pathlib import Path

import pytest


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """An empty temporary directory that callers can populate to simulate a project."""
    return tmp_path
