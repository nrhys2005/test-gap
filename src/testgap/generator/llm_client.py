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
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature
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
                raw = fn(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=max_output_tokens,
                )
                return self._normalize(raw)
            except Exception as e:  # network / provider errors bubble out of LiteLLM
                last_error = e
                if attempt >= self.max_retries:
                    break
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")

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
            raise LLMError(f"unexpected response shape: {raw!r}") from e

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
