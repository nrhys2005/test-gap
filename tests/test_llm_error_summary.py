"""Unit tests for ``testgap.generator.llm_client.summarize_llm_error``."""

from __future__ import annotations

from testgap.generator import LLMError, summarize_llm_error


def test_summarize_json_error_body_dict():
    err = LLMError(
        'Ollama returned {"error": {"message": "model not found", "code": 404}}'
    )
    assert summarize_llm_error(err) == "model not found"


def test_summarize_json_error_body_string():
    err = LLMError('Provider said: {"error": "quota exceeded"}')
    assert summarize_llm_error(err) == "quota exceeded"


def test_summarize_multiline_first_line_only():
    exc = ValueError(
        "Connection failed to http://x:11434\nTraceback (most recent call last):"
    )
    assert summarize_llm_error(exc) == "Connection failed to http://x:11434"


def test_summarize_truncates_long_message():
    err = LLMError("x" * 500)
    result = summarize_llm_error(err)
    assert len(result) == 200


def test_summarize_empty_message_fallback():
    assert summarize_llm_error(LLMError("")) == "unknown LLM error"


def test_summarize_whitespace_only_fallback():
    assert summarize_llm_error(LLMError("   \n \t")) == "unknown LLM error"


def test_summarize_json_with_error_field_takes_priority_over_first_line():
    err = LLMError(
        "LiteLLM request failed\n"
        '{"error": {"message": "actual cause", "type": "server_error"}}'
    )
    assert summarize_llm_error(err) == "actual cause"


def test_summarize_ignores_json_without_error_field():
    err = LLMError('Non-error JSON: {"status": "ok"}')
    # Falls back to first line
    assert summarize_llm_error(err).startswith("Non-error JSON")


def test_summarize_handles_none_exception():
    assert summarize_llm_error(None) == "unknown LLM error"  # type: ignore[arg-type]


def test_summarize_prefers_error_field_over_nested_message_fallback():
    err = LLMError('{"error": {"error": "fallback name"}}')
    assert summarize_llm_error(err) == "fallback name"
