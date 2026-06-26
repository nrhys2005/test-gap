# TestGap

AI-powered test generator that closes coverage gaps in your PRs — only suggests tests that actually pass.

> **Status:** alpha. `testgap init` and `testgap diff` work today (non-interactive). `testgap diff --review` is in progress.

TestGap analyzes the uncovered code in your diff, generates pytest tests using your chosen LLM (Claude, GPT, Gemini, or local Ollama), and only suggests tests that have been executed and passed. Provider-agnostic, diff-focused, and verified — no hallucinated tests.

## Install

```bash
pip install -e ".[llm]"   # llm extra pulls in litellm for live calls
```

(Once published: `pip install testgap[llm]`.)

## Quick start

```bash
cd your-project
testgap init                  # detects pytest / src layout / test dir, writes .testgap.yml
testgap diff                  # generate tests for uncovered diff lines, validate them
testgap diff --base origin/main --max-functions 3
```

`testgap diff` runs end-to-end:

1. Resolves the base ref (`origin/HEAD` → `main` → `master`, override with `--base`).
2. Computes the changed-line set from `git diff base..HEAD`.
3. Runs `pytest --cov` to find which changed lines are not executed.
4. Groups uncovered lines into enclosing functions/methods via AST.
5. For each function, calls the configured LLM with a few-shot prompt that
   imitates existing tests in the project.
6. Writes the generated test to a temp file under `tests/`, runs pytest against it,
   and only reports tests that actually passed.

Suggestions are not committed automatically — that's the `--review` interactive mode (coming next).

## Configuration

`testgap init` creates a `.testgap.yml` like:

```yaml
version: 1
project:
  language: python
  test_framework: pytest
  source_paths: ["src/"]
  test_paths: ["tests/"]
coverage:
  threshold: 80
  diff_threshold: 90
  exclude:
    - "**/migrations/**"
    - "**/__init__.py"
llm:
  model: anthropic/claude-sonnet-4-6
  max_cost_per_run: 2.0
  max_retries: 2
generation:
  style: match_existing
  include_docstrings: true
  max_tests_per_function: 3
```

### LLM provider

TestGap uses [LiteLLM](https://docs.litellm.ai/) for provider abstraction. Set the matching env var for the model you choose:

| Model | Env var |
| --- | --- |
| `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `openai/gpt-4o` | `OPENAI_API_KEY` |
| `gemini/gemini-2.0-flash` | `GEMINI_API_KEY` |
| `ollama/qwen2.5-coder` | — (local) |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
