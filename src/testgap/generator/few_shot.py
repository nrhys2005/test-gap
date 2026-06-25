from pathlib import Path


def find_few_shot_examples(
    *,
    test_dirs: list[Path],
    target_module_path: Path,
    project_root: Path,
    max_examples: int = 3,
    max_chars_per_example: int = 1500,
) -> list[str]:
    """Pick existing test source snippets to use as style examples."""
    candidates = _ranked_candidates(test_dirs, target_module_path, project_root)

    out: list[str] = []
    for path in candidates:
        snippet = _read_test_snippet(path, max_chars=max_chars_per_example)
        if snippet:
            out.append(snippet)
        if len(out) >= max_examples:
            break
    return out


def _ranked_candidates(
    test_dirs: list[Path], target_module_path: Path, project_root: Path
) -> list[Path]:
    target_stem = target_module_path.stem
    try:
        target_parts = target_module_path.resolve().relative_to(project_root.resolve()).parts
    except ValueError:
        target_parts = (target_stem,)

    ranked: list[tuple[int, Path]] = []
    for tdir in test_dirs:
        if not tdir.is_dir():
            continue
        for path in tdir.rglob("test_*.py"):
            score = _score(path, target_stem, target_parts)
            ranked.append((score, path))

    ranked.sort(key=lambda x: (-x[0], str(x[1])))
    return [p for _, p in ranked]


def _score(test_path: Path, target_stem: str, target_parts: tuple[str, ...]) -> int:
    score = 0
    if test_path.stem == f"test_{target_stem}":
        score += 100
    if target_stem in test_path.stem:
        score += 20
    parts = test_path.parts
    for token in target_parts[:-1]:
        if token in parts:
            score += 10
    return score


def _read_test_snippet(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.strip():
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0]
