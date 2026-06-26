import subprocess
from pathlib import Path

import pytest

from testgap.coverage.git_diff import GitDiffError, changed_lines, resolve_base_ref


def _git(cwd: Path, *args: str) -> None:
    env_args = [
        "git",
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "-c",
        "commit.gpgsign=false",
        *args,
    ]
    subprocess.run(env_args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_project: Path) -> Path:
    _git(tmp_project, "init", "-q")
    (tmp_project / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(tmp_project, "add", ".")
    _git(tmp_project, "commit", "-q", "-m", "init")
    _git(tmp_project, "branch", "-M", "main")
    return tmp_project


def test_changed_lines_after_edit(git_repo: Path):
    (git_repo / "mod.py").write_text(
        "def f():\n    if True:\n        return 2\n    return 1\n", encoding="utf-8"
    )
    _git(git_repo, "checkout", "-q", "-b", "feature")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "edit")

    result = changed_lines(git_repo, base="main", head="HEAD")
    assert len(result) == 1
    assert (git_repo / "mod.py").samefile(result[0].path)
    assert 2 in result[0].lines or 3 in result[0].lines


def test_resolve_base_ref_prefers_explicit(git_repo: Path):
    assert resolve_base_ref(git_repo, "main") == "main"


def test_resolve_base_ref_falls_back_to_main(git_repo: Path):
    ref = resolve_base_ref(git_repo)
    assert ref in ("origin/HEAD", "origin/main", "main")


def test_resolve_raises_when_no_candidates(tmp_project: Path):
    _git(tmp_project, "init", "-q")
    with pytest.raises(GitDiffError):
        resolve_base_ref(tmp_project)
