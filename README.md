# TestGap

AI-powered test generator that closes coverage gaps in your PRs — only suggests tests that actually pass.

> **Status:** alpha. `testgap init` works today. `testgap diff --review` is in progress.

TestGap analyzes the uncovered code in your diff, generates pytest tests using your chosen LLM (Claude, GPT, Gemini, or local Ollama), and only suggests tests that have been executed and passed. Provider-agnostic, diff-focused, and verified — no hallucinated tests.

## Install

```bash
pip install -e .
```

(Once published: `pip install testgap`.)

## Quick start

```bash
cd your-project
testgap init           # detects pytest / src layout / test dir, writes .testgap.yml
testgap diff --review  # (coming soon) interactive test suggestions for your branch diff
```

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
