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

    return GeneratedTestSet(imports=[str(s) for s in imports], tests=tests)


def _extract_json(text: str) -> str:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    raise ParseError("no JSON code block found in response")
