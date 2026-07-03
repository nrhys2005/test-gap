import contextlib
import io
import json
from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(Exception):
    pass


class CompletionCallable(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


class LLMClient:
    """Thin wrapper around LiteLLM with retry and cost extraction.

    LiteLLM is imported lazily so the package can be installed without it,
    and unit tests can inject a fake `completion_fn`.
    """

    def __init__(
        self,
        model: str,
        *,
        completion_fn: CompletionCallable | None = None,
        max_retries: int = 2,
        temperature: float = 0.2,
        verbose: bool = False,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature
        self.verbose = verbose
        self._completion_fn = completion_fn

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 2000,
    ) -> LLMResponse:
        fn = self._resolve_completion_fn()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = self._call_with_optional_capture(
                    fn,
                    dict(
                        model=self.model,
                        messages=messages,
                        temperature=self.temperature,
                        max_tokens=max_output_tokens,
                    ),
                )
                return self._normalize(raw)
            except Exception as e:  # network / provider errors bubble out of LiteLLM
                last_error = e
                if attempt >= self.max_retries:
                    break
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")

    def _call_with_optional_capture(
        self,
        fn: CompletionCallable,
        kwargs: dict[str, Any],
    ) -> Any:
        """Invoke completion; suppress LiteLLM's branded stdout/stderr unless verbose.

        LiteLLM writes brand-strings (``Give Feedback / Get Help: ...``, ``Provider
        List: ...``) via ``print()`` and ``sys.stderr.write()`` on provider errors —
        these bypass the ``logging`` filters set by ``_setup_litellm_logging``. We
        redirect stdout/stderr around the raw completion call only, so parsing and
        normalization logic still surface their own log output.

        On exception with captured stderr, we attach the last ~400 chars to the
        exception message as ``[captured stderr] ...`` so operators still get a
        diagnostic hint. stdout is discarded (branded noise).
        """
        if self.verbose:
            return fn(**kwargs)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                return fn(**kwargs)
        except Exception as e:
            captured = stderr_buf.getvalue().strip()
            if not captured:
                raise
            snippet = captured[-400:]
            new_msg = f"{e}\n[captured stderr] {snippet}"
            try:
                new_exc: Exception = type(e)(new_msg)
            except Exception:  # noqa: BLE001 — see below
                # PR #8 review (gemini M): some exception classes reject a
                # single positional arg not just with ``TypeError`` (e.g.
                # HTTPError) but also with ``ValueError`` or custom
                # ``__init__`` validation. Catch anything raised during
                # reconstruction and fall back to ``LLMError``; the original
                # remains available via ``__cause__``.
                new_exc = LLMError(new_msg)
            raise new_exc from e

    def _resolve_completion_fn(self) -> CompletionCallable:
        if self._completion_fn is not None:
            return self._completion_fn
        try:
            from litellm import completion  # type: ignore[import-not-found]
        except ImportError as e:
            raise LLMError(
                "litellm is required for live LLM calls. "
                "Install with `pip install testgap[llm]` or pass --dry-run."
            ) from e
        return completion

    def _normalize(self, raw: Any) -> LLMResponse:
        try:
            text = raw.choices[0].message.content
        except (AttributeError, IndexError, KeyError) as e:
            # When the response is a raw error payload (e.g. Ollama returned a
            # 500 body instead of a completion), fold the JSON ``error`` field
            # into the LLMError so ``summarize_llm_error`` can surface a concise
            # human message downstream.
            summary = summarize_llm_error(Exception(repr(raw)))
            raise LLMError(f"unexpected response shape: {summary}") from e

        usage = getattr(raw, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

        cost = _extract_cost(raw)
        return LLMResponse(
            text=text or "",
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cost_usd=cost,
            model=self.model,
        )


def _extract_cost(raw: Any) -> float:
    hidden = getattr(raw, "_hidden_params", None) or {}
    cost = hidden.get("response_cost") if isinstance(hidden, dict) else None
    if isinstance(cost, int | float):
        return float(cost)
    return 0.0


_MAX_SUMMARY_CHARS = 200


def _iter_json_objects(text: str):
    """Yield every balanced ``{...}`` substring in ``text`` (naive brace matching).

    Handles nested objects correctly by counting brace depth. Bails out safely
    on strings that contain unbalanced braces.
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        start = i
        j = i
        while j < n:
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : j + 1]
                    i = j + 1
                    break
            j += 1
        else:
            return  # unbalanced — stop scanning
        if depth != 0:
            i = j + 1


def summarize_llm_error(exc: BaseException) -> str:
    """Extract a single-line human message from an LLMError chain.

    Priority:
      1. Ollama / provider JSON body: ``{"error": ...}`` — pick ``error`` value.
      2. First line of ``str(exc)`` — providers such as LiteLLM often frame the
         useful message as the first line of a stack-trace-heavy repr.
      3. Fallback ``"unknown LLM error"`` when the exception has no message.
    All results are truncated to 200 characters.
    """
    text = str(exc) if exc is not None else ""
    if not text or not text.strip():
        return "unknown LLM error"

    # 1) JSON error body — scan balanced ``{...}`` blocks until one has ``error``.
    for payload in _iter_json_objects(text):
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or "error" not in data:
            continue
        err = data["error"]
        if isinstance(err, dict):
            message = err.get("message") or err.get("error") or payload
            return str(message)[:_MAX_SUMMARY_CHARS]
        return str(err)[:_MAX_SUMMARY_CHARS]

    # 2) First non-empty line only.
    first = next((line for line in text.splitlines() if line.strip()), "")
    if not first:
        return "unknown LLM error"
    return first[:_MAX_SUMMARY_CHARS]
