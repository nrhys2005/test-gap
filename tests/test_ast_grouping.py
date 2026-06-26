from pathlib import Path

from testgap.coverage.ast_grouping import group_by_function
from testgap.coverage.diff_coverage import UncoveredLine


def _write_module(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_groups_lines_into_top_level_function(tmp_project: Path):
    mod = _write_module(
        tmp_project / "mod.py",
        "def add(a, b):\n    if a < 0:\n        return 0\n    return a + b\n",
    )
    uncov = [UncoveredLine(file=mod, line=2), UncoveredLine(file=mod, line=3)]

    groups = group_by_function(uncov)
    assert len(groups) == 1
    assert groups[0].qualname == "add"
    assert groups[0].uncovered_lines == [2, 3]
    assert groups[0].has_branch is True


def test_groups_method_with_class_prefix(tmp_project: Path):
    mod = _write_module(
        tmp_project / "mod.py",
        "class Calc:\n    def add(self, a, b):\n        return a + b\n",
    )
    uncov = [UncoveredLine(file=mod, line=3)]

    groups = group_by_function(uncov)
    assert groups[0].qualname == "Calc.add"
    assert groups[0].has_branch is False


def test_branch_priority_first(tmp_project: Path):
    mod = _write_module(
        tmp_project / "mod.py",
        "\n".join(
            [
                "def linear():",
                "    return 1",
                "",
                "def branchy(x):",
                "    if x > 0:",
                "        return 1",
                "    return 0",
                "",
            ]
        ),
    )
    uncov = [UncoveredLine(file=mod, line=2), UncoveredLine(file=mod, line=5)]
    groups = group_by_function(uncov)

    assert groups[0].qualname == "branchy"
    assert groups[1].qualname == "linear"


def test_skips_lines_outside_any_function(tmp_project: Path):
    mod = _write_module(
        tmp_project / "mod.py",
        "X = 1\ndef f():\n    return X\n",
    )
    uncov = [UncoveredLine(file=mod, line=1), UncoveredLine(file=mod, line=3)]
    groups = group_by_function(uncov)
    assert len(groups) == 1
    assert groups[0].qualname == "f"
