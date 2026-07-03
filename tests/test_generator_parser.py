import pytest

from testgap.generator import ParseError, parse_response


def _wrap(payload: str) -> str:
    return f"Here you go:\n\n```json\n{payload}\n```\n"


def test_parses_well_formed_response():
    payload = """{
      "imports": ["from myapp.calc import add"],
      "tests": [
        {
          "name": "test_add_positive",
          "purpose": "happy path",
          "code": "def test_add_positive():\\n    assert add(1, 2) == 3"
        }
      ]
    }"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["from myapp.calc import add"]
    assert len(result.tests) == 1
    assert result.tests[0].name == "test_add_positive"


def test_to_source_joins_imports_and_tests():
    payload = """{
      "imports": ["import pytest"],
      "tests": [
        {"name": "test_one", "purpose": "", "code": "def test_one():\\n    assert True"},
        {"name": "test_two", "purpose": "", "code": "def test_two():\\n    assert 1 == 1"}
      ]
    }"""
    result = parse_response(_wrap(payload))
    source = result.to_source()
    assert "import pytest" in source
    assert "def test_one" in source
    assert "def test_two" in source


def test_rejects_missing_tests():
    payload = '{"imports": [], "tests": []}'
    with pytest.raises(ParseError):
        parse_response(_wrap(payload))


def test_rejects_bad_test_name():
    payload = """{
      "imports": [],
      "tests": [{"name": "bad_name", "purpose": "", "code": "def bad_name(): pass"}]
    }"""
    with pytest.raises(ParseError):
        parse_response(_wrap(payload))


def test_rejects_non_json():
    with pytest.raises(ParseError):
        parse_response("not even close to JSON")


def test_accepts_bare_json_without_fence():
    payload = (
        '{"imports": ["import x"], '
        '"tests": [{"name": "test_x", "purpose": "", "code": "def test_x():\\n    pass"}]}'
    )
    result = parse_response(payload)
    assert result.tests[0].name == "test_x"


# --- TG-412: auto-add `import pytest` when generated tests use `pytest.*` ---


def test_auto_adds_pytest_import_when_missing():
    """R4-a: pytest.raises used but `import pytest` missing → auto-appended."""
    payload = """{
"imports": ["from myapp.calc import divide"],
"tests": [
{
  "name": "test_zero_div",
  "purpose": "guard",
  "code": "def test_zero_div():\\n    with pytest.raises(ZeroDivisionError):\\n        divide(1, 0)"
}
]
}"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["from myapp.calc import divide", "import pytest"]


def test_does_not_duplicate_when_import_pytest_present():
    """R4-b: existing `import pytest` prevents duplicate insertion (with whitespace variant)."""
    payload = """{
"imports": ["  import pytest  ", "from myapp.calc import divide"],
"tests": [
{
  "name": "test_zero_div",
  "purpose": "guard",
  "code": "def test_zero_div():\\n    with pytest.raises(ZeroDivisionError):\\n        divide(1, 0)"
}
]
}"""
    result = parse_response(_wrap(payload))
    # imports preserved as-is (whitespace-padded entry still present exactly once)
    assert result.imports == ["  import pytest  ", "from myapp.calc import divide"]
    assert sum(1 for line in result.imports if "import pytest" in line) == 1


def test_no_change_when_no_pytest_reference():
    """R4-c: tests that never reference pytest → imports untouched."""
    payload = """{
      "imports": ["from myapp.calc import add"],
      "tests": [
        {
          "name": "test_add_positive",
          "purpose": "happy path",
          "code": "def test_add_positive():\\n    assert add(1, 2) == 3"
        }
      ]
    }"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["from myapp.calc import add"]


def test_does_not_duplicate_when_from_pytest_present():
    """R4-d: `from pytest import raises` also counts as pytest imported."""
    payload = """{
"imports": ["from pytest import raises"],
"tests": [
{
  "name": "test_raises_ve",
  "purpose": "",
  "code": "def test_raises_ve():\\n    with pytest.raises(ValueError):\\n        raise ValueError()"
}
]
}"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["from pytest import raises"]


def test_known_limitation_string_literal_triggers_import():
    """R4-e: `pytest.raises` in a string literal is a documented false positive.

    This test locks the current (over-add) behavior. If we ever move to an
    AST-based detector, this regression guard signals the intentional break.
    """
    payload = """{
      "imports": [],
      "tests": [
        {
          "name": "test_msg",
          "purpose": "false positive guard",
          "code": "def test_msg():\\n    msg = \\"pytest.raises was expected\\"\\n    assert msg"
        }
      ]
    }"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["import pytest"]


def test_dogfood_2026_07_02_replay():
    """R5: 2026-07-02 dogfood replay — the exact failure mode that produced
    Apply Rate 0/13 (NameError: name 'pytest' is not defined).
    """
    payload = """{
"imports": ["from myapp.money import divide"],
"tests": [
{
"name": "test_div_zero",
"purpose": "guard against zero divisor",
"code": "def test_div_zero():\\n    with pytest.raises(ZeroDivisionError):\\n        divide(10, 0)"
}
]
}"""
    result = parse_response(_wrap(payload))
    # append position: pytest import goes to the tail so existing user imports keep their order
    assert result.imports[-1] == "import pytest"
    # original imports preserved (order + content)
    assert result.imports[0] == "from myapp.money import divide"
    # rendering `to_source()` includes the pytest import so downstream pytest run won't NameError
    source = result.to_source()
    assert "import pytest" in source
    assert "from myapp.money import divide" in source
    assert "pytest.raises(ZeroDivisionError)" in source


def test_does_not_duplicate_when_import_pytest_has_inline_comment():
    """PR #6 review (gemini-code-assist): ``import pytest  # type: ignore`` must
    be recognized so we don't append a duplicate.
    """
    payload = """{
"imports": ["import pytest  # type: ignore"],
"tests": [
{
"name": "test_raises",
"purpose": "raises",
"code": "def test_raises():\\n    with pytest.raises(ValueError):\\n        raise ValueError()"
}
]
}"""
    result = parse_response(_wrap(payload))
    # Only the original comment-bearing import — no duplicate ``import pytest``.
    assert result.imports == ["import pytest  # type: ignore"]


def test_does_not_duplicate_when_pytest_in_multi_import():
    """PR #6 review (gemini-code-assist): ``import sys, pytest`` must be
    recognized so we don't append a duplicate.
    """
    payload = """{
"imports": ["import sys, pytest"],
"tests": [
{
"name": "test_raises",
"purpose": "raises",
"code": "def test_raises():\\n    with pytest.raises(ValueError):\\n        raise ValueError()"
}
]
}"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["import sys, pytest"]


def test_does_not_duplicate_when_from_pytest_with_comment():
    """PR #6 review edge case: ``from pytest import raises  # noqa: F401``."""
    payload = """{
"imports": ["from pytest import raises  # noqa: F401"],
"tests": [
{
"name": "test_raises",
"purpose": "raises",
"code": "def test_raises():\\n    with pytest.raises(ValueError):\\n        raise ValueError()"
}
]
}"""
    result = parse_response(_wrap(payload))
    assert result.imports == ["from pytest import raises  # noqa: F401"]
