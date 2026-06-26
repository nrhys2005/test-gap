from dataclasses import dataclass

from testgap.coverage.ast_grouping import UncoveredFunction

SYSTEM_PROMPT = """You generate pytest tests for Python functions.

Rules:
1. Output MUST be a single JSON code block matching the schema below.
2. Use pytest fixtures and conventions, not unittest.
3. Match the style of provided examples.
4. Focus on the uncovered lines specified.
5. Do not test private helpers separately — test through public API.
6. Each test name must start with `test_` and describe the scenario.
7. Mock external calls using the same library as the examples.

Output schema:
```json
{
  "tests": [
    {"name": "test_...", "purpose": "...", "code": "def test_...():\\n    ..."}
  ],
  "imports": ["import x", "from y import z"]
}
```
"""


@dataclass
class PromptContext:
    function: UncoveredFunction
    module_import_path: str
    few_shot_examples: list[str]
    max_tests: int = 3


def build_messages(ctx: PromptContext) -> list[dict[str, str]]:
    user_message = _build_user_message(ctx)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def _build_user_message(ctx: PromptContext) -> str:
    parts: list[str] = []
    parts.append(f"# Target function\n\nModule: `{ctx.module_import_path}`")
    parts.append(f"Qualified name: `{ctx.function.qualname}`")
    parts.append(f"Uncovered lines: {ctx.function.uncovered_lines}")
    parts.append("```python\n" + ctx.function.source + "\n```")

    if ctx.few_shot_examples:
        parts.append("# Existing tests for style reference")
        for example in ctx.few_shot_examples:
            parts.append("```python\n" + example + "\n```")

    parts.append(
        f"# Task\n\nGenerate up to {ctx.max_tests} pytest tests that cover the "
        "uncovered lines listed above. Return only the JSON code block — no prose."
    )
    return "\n\n".join(parts)
