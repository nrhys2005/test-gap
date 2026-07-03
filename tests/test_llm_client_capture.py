"""Verify LiteLLM print()/stderr noise suppression in LLMClient.complete.

Scope (per TG-413 plan):
1. verbose=False swallows stdout/stderr writes from the completion callable.
2. verbose=True lets them through so operators can debug live.
3. On exception with verbose=False, tail of captured stderr is appended to
   the raised exception (last ~400 chars, marked ``[captured stderr]``).
4. On exception with verbose=True, no attachment; stderr stays visible.
5. Original exception type is preserved (via ``type(e)(msg)`` reconstruction
   and ``raise ... from e``).
6. verbose=False + empty stderr → no ``[captured stderr]`` marker.

LLMClient is exercised with a fake ``completion_fn`` — no LiteLLM import.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from testgap.generator.llm_client import LLMClient, LLMError


def _ok_response() -> SimpleNamespace:
    """Minimal successful LiteLLM-shape response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        _hidden_params={"response_cost": 0.0},
    )


def test_verbose_false_swallows_litellm_print(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_completion(**kwargs):
        print("Give Feedback / Get Help: https://github.com/BerriAI/litellm/issues/new")
        print("LiteLLM.Info: If you need to debug this error, use debug", file=sys.stderr)
        return _ok_response()

    client = LLMClient(model="fake/model", completion_fn=fake_completion, verbose=False)
    resp = client.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "hello"

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_verbose_true_lets_litellm_print_through(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_completion(**kwargs):
        print("Give Feedback / Get Help: https://github.com/BerriAI/litellm/issues/new")
        print("LiteLLM.Info: debug hint", file=sys.stderr)
        return _ok_response()

    client = LLMClient(model="fake/model", completion_fn=fake_completion, verbose=True)
    resp = client.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "hello"

    captured = capsys.readouterr()
    assert "Give Feedback" in captured.out
    assert "LiteLLM.Info" in captured.err


def test_verbose_false_attaches_stderr_snippet_on_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_completion(**kwargs):
        sys.stderr.write("provider error stack trace: connection refused at 127.0.0.1:11434\n")
        raise RuntimeError("connection refused")

    client = LLMClient(
        model="fake/model",
        completion_fn=fake_completion,
        max_retries=0,
        verbose=False,
    )
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    text = str(excinfo.value)
    assert "connection refused" in text
    assert "[captured stderr]" in text
    assert "provider error stack trace" in text

    # Captured stderr must have been swallowed by the redirect (pytest sees empty).
    captured = capsys.readouterr()
    assert captured.err == ""


def test_verbose_true_no_stderr_attachment_on_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_completion(**kwargs):
        sys.stderr.write("provider stack trace visible\n")
        raise RuntimeError("boom")

    client = LLMClient(
        model="fake/model",
        completion_fn=fake_completion,
        max_retries=0,
        verbose=True,
    )
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    text = str(excinfo.value)
    assert "[captured stderr]" not in text

    # With verbose=True the redirect is skipped → pytest's capsys sees the write.
    captured = capsys.readouterr()
    assert "provider stack trace visible" in captured.err


def test_verbose_false_preserves_original_exception_type() -> None:
    """``type(e)(msg)`` reconstruction preserves the provider exception class.

    ``_call_with_optional_capture`` re-raises a fresh instance of the *same*
    class as the original exception with the stderr snippet appended, chained
    via ``raise ... from e`` so the original traceback is preserved. This is
    what ``complete()``'s retry loop catches as ``last_error``. Testing the
    helper directly avoids the outer ``LLMError`` wrap in ``complete()``.
    """

    def fake_completion(**kwargs):
        sys.stderr.write("net: could not resolve host\n")
        raise ConnectionError("host unreachable")

    client = LLMClient(model="fake/model", verbose=False)
    with pytest.raises(ConnectionError) as excinfo:
        client._call_with_optional_capture(fake_completion, {})

    # Same class (not silently swapped to LLMError).
    assert type(excinfo.value) is ConnectionError
    assert "host unreachable" in str(excinfo.value)
    assert "[captured stderr]" in str(excinfo.value)
    # ``raise new_exc from e`` chain preserves the original raise.
    assert isinstance(excinfo.value.__cause__, ConnectionError)
    assert str(excinfo.value.__cause__) == "host unreachable"


def test_verbose_false_fallback_when_type_reconstruction_fails() -> None:
    """Exceptions whose ``__init__`` rejects a single positional arg fall back
    to ``LLMError(...)`` while still preserving the original via ``__cause__``.
    """

    class _WeirdError(Exception):
        def __init__(self, code: int, detail: str) -> None:
            if not isinstance(code, int):
                raise TypeError("code must be int")
            super().__init__(f"{code}: {detail}")
            self.code = code
            self.detail = detail

    def fake_completion(**kwargs):
        sys.stderr.write("weird provider stack\n")
        raise _WeirdError(500, "provider blew up")

    client = LLMClient(model="fake/model", verbose=False)
    with pytest.raises(LLMError) as excinfo:
        client._call_with_optional_capture(fake_completion, {})

    assert "[captured stderr]" in str(excinfo.value)
    assert "weird provider stack" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, _WeirdError)


def test_verbose_false_no_stderr_no_attachment() -> None:
    """No stderr writes → no ``[captured stderr]`` marker in the LLMError message."""

    def fake_completion(**kwargs):
        raise RuntimeError("plain failure")

    client = LLMClient(
        model="fake/model",
        completion_fn=fake_completion,
        max_retries=0,
        verbose=False,
    )
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    text = str(excinfo.value)
    assert "plain failure" in text
    assert "[captured stderr]" not in text
