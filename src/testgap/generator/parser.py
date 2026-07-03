import json
import re
from dataclasses import dataclass, field


class ParseError(Exception):
    pass


@dataclass
class GeneratedTest:
    name: str
    purpose: str
    code: str


@dataclass
class GeneratedTestSet:
    imports: list[str] = field(default_factory=list)
    tests: list[GeneratedTest] = field(default_factory=list)

    def to_source(self) -> str:
        parts: list[str] = []
        if self.imports:
            parts.append("\n".join(self.imports))
        for t in self.tests:
            parts.append(t.code.rstrip())
        return "\n\n\n".join(parts) + "\n"


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_PYTEST_REF_RE = re.compile(r"\bpytest\.[a-zA-Z_]")


def parse_response(text: str) -> GeneratedTestSet:
    payload = _extract_json(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ParseError(f"response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ParseError("top-level JSON must be an object")

    raw_tests = data.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise ParseError("`tests` must be a non-empty list")

    imports = data.get("imports", []) or []
    if not isinstance(imports, list) or not all(isinstance(s, str) for s in imports):
        raise ParseError("`imports` must be a list of strings")

    tests: list[GeneratedTest] = []
    for i, item in enumerate(raw_tests):
        if not isinstance(item, dict):
            raise ParseError(f"tests[{i}] is not an object")
        name = item.get("name")
        code = item.get("code")
        purpose = item.get("purpose", "")
        if not isinstance(name, str) or not name.startswith("test_"):
            raise ParseError(f"tests[{i}].name must start with 'test_'")
        if not isinstance(code, str) or "def " not in code:
            raise ParseError(f"tests[{i}].code must contain a function definition")
        tests.append(GeneratedTest(name=name, purpose=str(purpose), code=code))

    return _ensure_pytest_import(
        GeneratedTestSet(imports=[str(s) for s in imports], tests=tests)
    )


def _extract_json(text: str) -> str:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    raise ParseError("no JSON code block found in response")


def _is_pytest_import_line(line: str) -> bool:
    """True when *line* imports the ``pytest`` module.

    Handles ``import pytest`` (with optional ``as`` alias), multi-module
    forms like ``import sys, pytest``, ``from pytest import ...``, and
    lines that carry inline comments (``import pytest  # type: ignore``).
    """
    # Strip inline comment first so trailing text can't shadow the module name.
    stripped = line.split("#", 1)[0].strip()
    if stripped.startswith("import "):
        # Check every comma-separated module (``import sys, pytest``).
        parts = [
            p.strip().split(" as ", 1)[0].strip()
            for p in stripped[len("import ") :].split(",")
        ]
        return "pytest" in parts
    if stripped.startswith("from "):
        # ``from pytest import raises``, ``from pytest.x import y``.
        head, _, rest = stripped[len("from ") :].partition(" import ")
        if not rest:
            return False
        module = head.strip()
        return module == "pytest" or module.startswith("pytest.")
    return False


def _ensure_pytest_import(generated: GeneratedTestSet) -> GeneratedTestSet:
    """Post-generation fix: LLM often uses ``pytest.raises`` without importing pytest.

    Scans every test's code with :data:`_PYTEST_REF_RE`. If any test references
    ``pytest.<attr>`` and the imports list lacks an ``import pytest`` (or
    ``from pytest import ...``) line, we append ``"import pytest"`` to the
    imports list.

    Behavior:

    - append-only: never removes, reorders, or rewrites the user-supplied
      imports. Returns a **new** :class:`GeneratedTestSet` instance so the
      caller cannot mutate the original by accident.
    - no-op detection: an existing ``import pytest`` (word-boundary match on
      the first token) or ``from pytest import`` line prevents duplicate
      insertion.
    - Known false positives: the regex will also match ``"pytest.foo"`` inside
      string literals, docstrings, f-strings, or comments. This is documented
      and considered acceptable — the worst case is a redundant ``import
      pytest`` (no runtime harm).
    - Known false negatives (documented, out of scope): ``import pytest as pt``
      + ``pt.raises(...)`` would not trigger detection here, but in practice
      the LLM already includes the aliased import in that case.

    :param generated: parsed response from the LLM (already validated).
    :returns: new :class:`GeneratedTestSet` instance; ``tests`` list is shared
        (shallow) with the input.
    """
    needs_pytest = any(_PYTEST_REF_RE.search(t.code) for t in generated.tests)
    if not needs_pytest:
        return GeneratedTestSet(
            imports=list(generated.imports), tests=generated.tests
        )

    already_imported = any(
        _is_pytest_import_line(line) for line in generated.imports
    )
    if already_imported:
        return GeneratedTestSet(
            imports=list(generated.imports), tests=generated.tests
        )

    new_imports = list(generated.imports) + ["import pytest"]
    return GeneratedTestSet(imports=new_imports, tests=generated.tests)
