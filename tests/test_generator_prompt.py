from pathlib import Path

from testgap.coverage.ast_grouping import UncoveredFunction
from testgap.generator import PreviousFailure, build_messages
from testgap.generator.prompt import PromptContext, _truncate_failure_payload


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


def test_previous_failures_empty_keeps_legacy_prompt():
    """Golden regression: previous_failures=[] must produce the exact legacy message."""
    legacy = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
            max_tests=3,
        )
    )
    with_empty = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
            max_tests=3,
            previous_failures=[],
        )
    )
    assert legacy == with_empty
    assert "Previously failed attempts" not in legacy[1]["content"]
    assert "Generate up to 3 pytest tests" in legacy[1]["content"]


def test_previous_failures_appends_regression_section():
    failure = PreviousFailure(
        test_name="test_negative_pct_raises",
        test_code="def test_negative_pct_raises():\n    apply_discount(10, -0.1)",
        failure_message="AssertionError: did not raise ValueError",
    )

    baseline = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
            max_tests=3,
        )
    )
    msgs = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
            max_tests=3,
            previous_failures=[failure],
        )
    )

    # System message must be byte-identical regardless of failure context.
    assert msgs[0] == baseline[0]

    user = msgs[1]["content"]
    assert "# Previously failed attempts" in user
    assert "test_negative_pct_raises" in user
    assert "apply_discount(10, -0.1)" in user
    assert "did not raise ValueError" in user
    # Replaced Task block must be present.
    assert "Re-generate replacements for the failing tests above." in user
    # Legacy task block must be gone.
    assert "Generate up to 3 pytest tests that cover the" not in user


def test_previous_failures_truncated_to_500_tokens():
    huge_code = "x = 1\n" * 5000  # ~30000 chars → ~7500 tokens heuristic
    huge_msg = "ERROR " * 5000
    failure = PreviousFailure(
        test_name="test_huge",
        test_code=huge_code,
        failure_message=huge_msg,
    )
    msgs = build_messages(
        PromptContext(
            function=_make_function(),
            module_import_path="myapp.calc",
            few_shot_examples=[],
            max_tests=3,
            previous_failures=[failure],
        )
    )
    user = msgs[1]["content"]
    # Either the code or the message (or both) had to be truncated.
    assert "# ... truncated" in user
    # The fully-rendered user message must fit well below the heuristic 500-token
    # cap plus prompt scaffolding (target function + task block + section
    # headers). Allow generous overhead.
    assert len(user) // 4 < 2000


def test_truncate_failure_payload_helper():
    # Empty input → empty output, no exception.
    assert _truncate_failure_payload([]) == []

    short = PreviousFailure(
        test_name="t", test_code="def t(): pass", failure_message="boom"
    )
    out = _truncate_failure_payload([short])
    assert len(out) == 1
    # Short payloads should pass through unchanged (no truncation marker).
    assert out[0].test_code == short.test_code
    assert out[0].failure_message == short.failure_message
    assert "# ... truncated" not in out[0].test_code
    assert "# ... truncated" not in out[0].failure_message

    # Many small failures → still bounded, returns N entries.
    many = [
        PreviousFailure(test_name=f"t{i}", test_code="x" * 200, failure_message="m" * 200)
        for i in range(5)
    ]
    out_many = _truncate_failure_payload(many, max_tokens=200)
    assert len(out_many) == 5

    # Verify immutability — original instances unchanged.
    assert many[0].test_code == "x" * 200
