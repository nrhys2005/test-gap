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
