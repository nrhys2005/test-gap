from dataclasses import dataclass, field

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

_TRUNCATION_MARKER = "\n# ... truncated"


@dataclass
class PreviousFailure:
    """A previously generated test that failed validation in a prior LLM round.

    Fed back into the next generation as regression context so the model can
    avoid the same failure modes.
    """

    test_name: str
    test_code: str
    failure_message: str


@dataclass
class PromptContext:
    function: UncoveredFunction
    module_import_path: str
    few_shot_examples: list[str]
    max_tests: int = 3
    previous_failures: list[PreviousFailure] = field(default_factory=list)


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

    # When no previous failures are provided, use the legacy `# Task` block so
    # existing golden tests pass byte-for-byte. The retry context section is
    # appended *before* this block when failures are present.
    if ctx.previous_failures:
        truncated = _truncate_failure_payload(ctx.previous_failures)
        parts.append(_render_previous_failures_section(truncated))
        parts.append(
            f"# Task\n\nRe-generate replacements for the failing tests above. "
            f"Keep at most {ctx.max_tests} tests, each addressing one previously-failed "
            "scenario. Do not repeat the same failure. Return only the JSON code block — "
            "no prose."
        )
    else:
        parts.append(
            f"# Task\n\nGenerate up to {ctx.max_tests} pytest tests that cover the "
            "uncovered lines listed above. Return only the JSON code block — no prose."
        )
    return "\n\n".join(parts)


def _render_previous_failures_section(failures: list[PreviousFailure]) -> str:
    lines: list[str] = [
        "# Previously failed attempts (regenerate ONLY these and avoid the listed failure modes)",
    ]
    for failure in failures:
        lines.append("")
        lines.append(f"## {failure.test_name}")
        lines.append("```python")
        lines.append(failure.test_code)
        lines.append("```")
        lines.append("pytest output:")
        lines.append("```")
        lines.append(failure.failure_message)
        lines.append("```")
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Estimate token count using litellm when available, char/4 otherwise.

    The litellm tokenizer may not match the actual provider exactly — this is a
    rough budget guard, not a precise count. Wrapped in a broad except because
    litellm can raise on unknown models, missing tokenizer data, etc.
    """
    try:
        from litellm import token_counter  # type: ignore[import-not-found]

        return int(token_counter(model="gpt-3.5-turbo", text=text))
    except Exception:
        return max(1, len(text) // 4)


def _truncate_failure_payload(
    failures: list[PreviousFailure], max_tokens: int = 500
) -> list[PreviousFailure]:
    """Cap the total token footprint of a failure list at ``max_tokens``.

    Returns new ``PreviousFailure`` instances; inputs are not mutated. Tokens are
    distributed evenly across entries (code and message split 50/50 within each).
    When an entry exceeds its share, the failure message is trimmed first and
    then the test code; trimmed strings get a ``# ... truncated`` marker so the
    model knows it received partial context.
    """
    if not failures:
        return []

    per_entry_budget = max(1, max_tokens // len(failures))
    per_field_budget = max(1, per_entry_budget // 2)

    truncated: list[PreviousFailure] = []
    for failure in failures:
        code = _truncate_text_to_tokens(failure.test_code, per_field_budget)
        message_budget = per_entry_budget - _estimate_tokens(code)
        if message_budget < 1:
            message_budget = 1
        message = _truncate_text_to_tokens(failure.failure_message, message_budget)
        truncated.append(
            PreviousFailure(
                test_name=failure.test_name,
                test_code=code,
                failure_message=message,
            )
        )
    return truncated


def _truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    if not text:
        return text
    current = _estimate_tokens(text)
    if current <= max_tokens:
        return text

    # char/4 heuristic gives us a safe cut point when token-per-char is ~average.
    # When the text packs more tokens per char (litellm-reported, multibyte, etc.),
    # scale the budget proportionally so we never exceed max_tokens after cutting.
    char_budget = max(1, max_tokens * 4)
    if char_budget >= len(text):
        char_budget = max(1, int(len(text) * max_tokens / current))
    return text[:char_budget].rstrip() + _TRUNCATION_MARKER
