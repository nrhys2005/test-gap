import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitDiffError(Exception):
    pass


@dataclass(frozen=True)
class FileLines:
    path: Path
    lines: frozenset[int]


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def resolve_base_ref(repo_root: Path, explicit: str | None = None) -> str:
    """Pick the base ref for the diff. Order: explicit > origin/HEAD > main > master."""
    if explicit:
        return explicit

    candidates = ("origin/HEAD", "origin/main", "main", "origin/master", "master")
    for ref in candidates:
        if _ref_exists(repo_root, ref):
            return ref
    raise GitDiffError(
        "Could not determine base ref. Specify one with --base or ensure "
        "origin/HEAD / main / master exists."
    )


def changed_lines(
    repo_root: Path,
    base: str,
    head: str = "HEAD",
    only_paths: list[Path] | None = None,
) -> list[FileLines]:
    """Return per-file added/modified line numbers between base and head."""
    cmd = ["git", "diff", "--unified=0", "--no-color", f"{base}...{head}"]
    if only_paths:
        cmd.append("--")
        cmd.extend(str(p) for p in only_paths)

    result = _run_git(repo_root, cmd)
    return _parse_diff(result.stdout, repo_root)


def _parse_diff(diff_text: str, repo_root: Path) -> list[FileLines]:
    results: list[FileLines] = []
    current_file: Path | None = None
    current_lines: set[int] = set()

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_file is not None:
                results.append(FileLines(current_file, frozenset(current_lines)))
            current_file = None
            current_lines = set()
            continue

        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current_file = None
            elif target.startswith("b/"):
                current_file = repo_root / target[2:]
            else:
                current_file = repo_root / target
            continue

        if current_file is None:
            continue

        match = _HUNK_HEADER.match(line)
        if match:
            start = int(match.group(1))
            count_str = match.group(2)
            count = int(count_str) if count_str else 1
            if count == 0:
                continue
            for n in range(start, start + count):
                current_lines.add(n)

    if current_file is not None:
        results.append(FileLines(current_file, frozenset(current_lines)))

    return [fl for fl in results if fl.lines]


def _ref_exists(repo_root: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args, cwd=repo_root, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as e:
        raise GitDiffError("git executable not found on PATH") from e
    if result.returncode != 0:
        raise GitDiffError(
            f"git {' '.join(args[1:])} failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return result
