import ast
from dataclasses import dataclass, field
from pathlib import Path

from testgap.coverage.diff_coverage import UncoveredLine


@dataclass
class UncoveredFunction:
    file: Path
    qualname: str  # e.g. "ClassName.method" or "function_name"
    start_line: int
    end_line: int
    source: str
    uncovered_lines: list[int] = field(default_factory=list)
    has_branch: bool = False

    @property
    def priority(self) -> tuple[int, int]:
        """Sort key: branches first, then larger functions first."""
        return (0 if self.has_branch else 1, -(self.end_line - self.start_line))


def group_by_function(uncovered: list[UncoveredLine]) -> list[UncoveredFunction]:
    """Group uncovered lines into the enclosing function/method."""
    by_file: dict[Path, list[int]] = {}
    for u in uncovered:
        by_file.setdefault(u.file, []).append(u.line)

    results: list[UncoveredFunction] = []
    for file, lines in by_file.items():
        results.extend(_group_in_file(file, sorted(set(lines))))

    results.sort(key=lambda f: f.priority)
    return results


def _group_in_file(file: Path, lines: list[int]) -> list[UncoveredFunction]:
    try:
        source = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    try:
        tree = ast.parse(source, filename=str(file))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    functions = _collect_functions(tree)
    if not functions:
        return []

    by_function: dict[tuple[str, int, int], list[int]] = {}
    for line in lines:
        owner = _find_owning_function(functions, line)
        if owner is None:
            continue
        key = (owner.qualname, owner.start, owner.end)
        by_function.setdefault(key, []).append(line)

    out: list[UncoveredFunction] = []
    for (qualname, start, end), uncov in by_function.items():
        body_text = "\n".join(source_lines[start - 1 : end])
        has_branch = _function_has_branch(functions_index=functions, start=start)
        out.append(
            UncoveredFunction(
                file=file,
                qualname=qualname,
                start_line=start,
                end_line=end,
                source=body_text,
                uncovered_lines=sorted(uncov),
                has_branch=has_branch,
            )
        )
    return out


@dataclass
class _FunctionRange:
    qualname: str
    start: int
    end: int
    node: ast.AST


def _collect_functions(tree: ast.AST) -> list[_FunctionRange]:
    ranges: list[_FunctionRange] = []

    def visit(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, [*prefix, child.name])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = ".".join([*prefix, child.name]) if prefix else child.name
                start = child.lineno
                end = _last_line(child)
                ranges.append(_FunctionRange(qualname=name, start=start, end=end, node=child))
                visit(child, [*prefix, child.name])
            else:
                visit(child, prefix)

    visit(tree, [])
    return ranges


def _find_owning_function(functions: list[_FunctionRange], line: int) -> _FunctionRange | None:
    candidates = [f for f in functions if f.start <= line <= f.end]
    if not candidates:
        return None
    return min(candidates, key=lambda f: f.end - f.start)


def _last_line(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None)
    if end is not None:
        return end
    max_line = getattr(node, "lineno", 0)
    for child in ast.walk(node):
        line = getattr(child, "end_lineno", None) or getattr(child, "lineno", 0)
        if line > max_line:
            max_line = line
    return max_line


def _function_has_branch(*, functions_index: list[_FunctionRange], start: int) -> bool:
    for func in functions_index:
        if func.start != start:
            continue
        for node in ast.walk(func.node):
            if isinstance(node, ast.If | ast.For | ast.While | ast.Try | ast.Match):
                return True
        return False
    return False
