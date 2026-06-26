from pathlib import Path

from testgap.coverage.ast_grouping import UncoveredFunction
from testgap.generator import build_messages
from testgap.generator.prompt import PromptContext


def _make_function() -> UncoveredFunction:
    source = (
        "def apply_discount(price, pct):\n"
        "    if pct < 0:\n"
        "        raise ValueError\n"
        "    return price * (1 - pct)"
    )
    return UncoveredFunction(
        file=Path("/tmp/src/myapp/calc.py"),
        qualname="apply_discount",
        start_line=10,
        end_line=20,
        source=source,
        uncovered_lines=[12, 13],
        has_branch=True,
    )


def test_messages_have_system_and_user():
    msgs = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
            max_tests=3,
        )
    )
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


def test_user_message_includes_function_body_and_lines():
    msgs = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
        )
    )
    user = msgs[1]["content"]
    assert "myapp.calc" in user
    assert "apply_discount" in user
    assert "12" in user and "13" in user


def test_few_shot_appears_in_message():
    msgs = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=["def test_existing():\n    assert True"],
        )
    )
    assert "test_existing" in msgs[1]["content"]
