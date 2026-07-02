"""Deterministic LLM provider auto-detection (Ollama + API keys).

The module is intentionally free of LLM calls — Ollama detection uses
``shutil.which`` + HTTP ping + ``/api/tags``, API providers are detected by
environment-variable presence. All I/O is injectable so unit tests can drive
this module without a live server or subprocess.

Runnability policy (TG-401 review round 1): a pulled Ollama model is
considered ``PULLED_RUNNABLE`` **without** any additional probe. The old
``/api/show``-based ``probe_model_runnable`` was unreliable (wrong HTTP
verb + version drift) and produced false-negatives on healthy setups.
Callers may still inject a ``runnable_check_fn`` for tests or a future
``testgap doctor --warmup``, but the default is now no-probe optimism —
runtime failures are caught by the pipeline / review-session
consecutive-LLM-failure guards.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"

# Static ranking of recommended Ollama models. Order matters: ``detect_llm_providers``
# picks the first entry present in ``/api/tags``.
#
# Rationale (v0.2):
#   * qwen2.5-coder:7b — ~4.7GB, code-specialised; runs on 8GB RAM machines.
#   * qwen2.5-coder:14b — ~9GB, better quality; 16GB+ recommended.
#   * qwen3.6:27b — ~16GB, top quality; 32GB+ recommended.
#
# In v0.3+ we will filter this tuple by detected RAM/GPU via ``psutil``.
RECOMMENDED_OLLAMA_MODELS: tuple[str, ...] = (
    "qwen2.5-coder:7b",
    "qwen2.5-coder:14b",
    "qwen3.6:27b",
)

# API providers surfaced by ``detect_llm_providers``. Order = default rendering order.
_API_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("anthropic/claude-sonnet-4-6", "ANTHROPIC_API_KEY"),
    ("openai/gpt-4o", "OPENAI_API_KEY"),
    ("gemini/gemini-2.0-flash", "GEMINI_API_KEY"),
)


class ProviderKind(str, Enum):
    OLLAMA = "ollama"
    API = "api"


class ProviderStatus(str, Enum):
    # Priority order (index → priority; lower index = higher priority).
    PULLED_RUNNABLE = "pulled_runnable"   # Ollama detected + model pulled + probe OK
    KEY_FOUND = "key_found"               # API provider env-var present
    PULLED_BROKEN = "pulled_broken"       # model pulled but /api/show failed (D2)
    NOT_PULLED = "not_pulled"             # Ollama detected but recommended model missing
    KEY_MISSING = "key_missing"           # API provider env-var absent
    NOT_INSTALLED = "not_installed"       # Ollama binary/server not detected


_STATUS_ORDER: tuple[ProviderStatus, ...] = (
    ProviderStatus.PULLED_RUNNABLE,
    ProviderStatus.KEY_FOUND,
    ProviderStatus.PULLED_BROKEN,
    ProviderStatus.NOT_PULLED,
    ProviderStatus.KEY_MISSING,
    ProviderStatus.NOT_INSTALLED,
)


@dataclass(frozen=True)
class Provider:
    kind: ProviderKind
    model: str
    status: ProviderStatus
    hint: str
    env_var: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return _STATUS_ORDER.index(self.status)


@dataclass(frozen=True)
class OllamaScan:
    """Raw Ollama probe result. Not cached — recomputed on each call."""

    binary_present: bool
    endpoint: str
    server_reachable: bool
    pulled_models: tuple[str, ...]
    error: str | None = None


HttpGetFn = Callable[..., bytes]
WhichFn = Callable[[str], str | None]


def _default_http_get(url: str, timeout: float = 1.5) -> bytes:
    """Fetch ``url`` and return the body. Raises ``URLError``/``HTTPError``/``TimeoutError``."""
    req = Request(url, headers={"User-Agent": "testgap-doctor/0.1"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — trusted local endpoint
        return resp.read()


def scan_ollama(
    *,
    endpoint: str = DEFAULT_OLLAMA_ENDPOINT,
    which_fn: WhichFn = shutil.which,
    http_fn: HttpGetFn | None = None,
    timeout: float = 1.5,
) -> OllamaScan:
    """Detect Ollama state: binary presence + endpoint reachability + pulled models.

    Purely functional: all outward I/O is injectable. Any HTTP/JSON error is
    caught and folded into ``server_reachable=False`` — this is UX detection,
    not a health check, so we never raise.
    """
    binary = which_fn("ollama") is not None
    fetch = http_fn if http_fn is not None else _default_http_get
    tags_url = f"{endpoint.rstrip('/')}/api/tags"
    try:
        raw = fetch(tags_url, timeout=timeout)
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        return OllamaScan(
            binary_present=binary,
            endpoint=endpoint,
            server_reachable=False,
            pulled_models=(),
            error=str(e),
        )
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return OllamaScan(
            binary_present=binary,
            endpoint=endpoint,
            server_reachable=False,
            pulled_models=(),
            error=str(e),
        )
    models: list[str] = []
    if isinstance(data, dict):
        entries = data.get("models") or []
        if isinstance(entries, list):
            for m in entries:
                if isinstance(m, dict):
                    name = m.get("name")
                    if isinstance(name, str) and name:
                        models.append(name)
    return OllamaScan(
        binary_present=binary,
        endpoint=endpoint,
        server_reachable=True,
        pulled_models=tuple(models),
    )


def probe_model_runnable(
    endpoint: str,
    model: str,
    *,
    http_fn: HttpGetFn | None = None,
    timeout: float = 1.5,
) -> tuple[str, str | None]:
    """Deprecated no-op shim (kept for import back-compat).

    Previous implementations here called Ollama's ``/api/show`` endpoint to
    guess "is this pulled model runnable?". The check was unreliable in
    practice (wrong HTTP verb and Ollama version drift both surface as
    false-negatives), and the review of TG-401 concluded that an optimistic
    "pulled ⇒ runnable" judgment is a better UX: the actual runnability is
    already double-checked by ``pipeline`` (consecutive-LLM-failure guard) and
    the review session's mirror counter.

    We keep the function so that any external importer (or an older release's
    tests) continues to import cleanly, but it now unconditionally reports
    ``("runnable", None)`` and never performs any I/O.

    Callers inside TestGap should not depend on this. Real runnability warmup
    remains the domain of a future ``testgap doctor --warmup``.
    """
    del endpoint, model, http_fn, timeout  # arguments retained for signature stability
    return "runnable", None


def _ollama_recommended_match(pulled: tuple[str, ...]) -> str | None:
    """Return the highest-priority recommended model that is present in ``pulled``."""
    pulled_lower = {m.lower() for m in pulled}
    for m in RECOMMENDED_OLLAMA_MODELS:
        if m.lower() in pulled_lower:
            return m
    return None


def _api_provider_entries(env: dict[str, str]) -> list[Provider]:
    providers: list[Provider] = []
    for model, env_var in _API_PROVIDERS:
        if env.get(env_var):
            providers.append(
                Provider(
                    kind=ProviderKind.API,
                    model=model,
                    status=ProviderStatus.KEY_FOUND,
                    hint=f"env var {env_var} detected",
                    env_var=env_var,
                )
            )
        else:
            providers.append(
                Provider(
                    kind=ProviderKind.API,
                    model=model,
                    status=ProviderStatus.KEY_MISSING,
                    hint=f"set {env_var}",
                    env_var=env_var,
                )
            )
    return providers


def _ollama_provider_entries(
    scan: OllamaScan,
    *,
    runnable_check_fn: Callable[[str, str], bool] | None,
) -> list[Provider]:
    """Build ``Provider`` entries from an ``OllamaScan``.

    Emits:
      * one recommended-model entry (RUNNABLE / BROKEN / NOT_PULLED / NOT_INSTALLED)
      * plus a RUNNABLE entry per pulled model outside the recommended tuple.

    Optimism policy (TG-401 review round 1): when Ollama reports a model as
    pulled we default to :attr:`ProviderStatus.PULLED_RUNNABLE` and let the
    real generation path (pipeline consecutive-failure guard + review session
    mirror counter) catch actual runtime failures. Callers may still pass a
    ``runnable_check_fn`` — historically this was ``probe_model_runnable`` —
    but the default is now **no probe at all**, which matches how the rest of
    the stack treats pulled models (optimistic dispatch).
    """
    if not scan.binary_present and not scan.server_reachable:
        return [
            Provider(
                kind=ProviderKind.OLLAMA,
                model=f"ollama/{RECOMMENDED_OLLAMA_MODELS[0]}",
                status=ProviderStatus.NOT_INSTALLED,
                hint="install ollama (https://ollama.com) or set an API key",
                extra={"reason": scan.error or "no binary and no reachable server"},
            )
        ]

    if not scan.server_reachable:
        # Binary exists but server not up — treat as NOT_INSTALLED (blocker).
        return [
            Provider(
                kind=ProviderKind.OLLAMA,
                model=f"ollama/{RECOMMENDED_OLLAMA_MODELS[0]}",
                status=ProviderStatus.NOT_INSTALLED,
                hint="run: ollama serve",
                extra={"reason": scan.error or "server unreachable"},
            )
        ]

    matched = _ollama_recommended_match(scan.pulled_models)
    entries: list[Provider] = []
    if matched is None:
        top_pick = RECOMMENDED_OLLAMA_MODELS[0]
        entries.append(
            Provider(
                kind=ProviderKind.OLLAMA,
                model=f"ollama/{top_pick}",
                status=ProviderStatus.NOT_PULLED,
                hint=f"run: ollama pull {top_pick}",
            )
        )
    else:
        model_ref = f"ollama/{matched}"
        runnable = True
        broken_reason: str | None = None
        if runnable_check_fn is not None:
            try:
                runnable = bool(runnable_check_fn(scan.endpoint, model_ref))
            except Exception as e:  # noqa: BLE001 — never let probes crash detection
                runnable = False
                broken_reason = str(e)
        if runnable:
            entries.append(
                Provider(
                    kind=ProviderKind.OLLAMA,
                    model=model_ref,
                    status=ProviderStatus.PULLED_RUNNABLE,
                    hint=f"ready — using {model_ref}",
                )
            )
        else:
            entries.append(
                Provider(
                    kind=ProviderKind.OLLAMA,
                    model=model_ref,
                    status=ProviderStatus.PULLED_BROKEN,
                    hint=(
                        "server error; try: ollama serve --upgrade "
                        "(v0.2+ auto-heal)"
                    ),
                    extra={"reason": broken_reason} if broken_reason else {},
                )
            )

    # Extra pulled non-recommended models — surface each as RUNNABLE for visibility.
    recommended_lower = {m.lower() for m in RECOMMENDED_OLLAMA_MODELS}
    for name in scan.pulled_models:
        if name.lower() in recommended_lower:
            continue
        entries.append(
            Provider(
                kind=ProviderKind.OLLAMA,
                model=f"ollama/{name}",
                status=ProviderStatus.PULLED_RUNNABLE,
                hint="detected pulled model",
            )
        )
    return entries


def detect_llm_providers(
    *,
    ollama_endpoint: str | None = None,
    env: dict[str, str] | None = None,
    scan_fn: Callable[..., OllamaScan] | None = None,
    runnable_check_fn: Callable[[str, str], bool] | None = None,
) -> list[Provider]:
    """Return every detected provider sorted by ``ProviderStatus`` priority.

    Stable within a status group: original registration order (Ollama recommended
    → API providers → extra Ollama models) is preserved when priority ties.

    ``scan_fn`` defaults to the current module's ``scan_ollama`` (resolved at
    call time, so monkeypatching ``llm_provider.scan_ollama`` works).
    """
    endpoint = ollama_endpoint or DEFAULT_OLLAMA_ENDPOINT
    env_map = env if env is not None else dict(os.environ)
    # Resolve default at call time so monkeypatched ``scan_ollama`` takes effect.
    scan_callable: Callable[..., OllamaScan] = (
        scan_fn if scan_fn is not None else scan_ollama
    )

    try:
        scan = scan_callable(endpoint=endpoint)
    except TypeError:
        # Legacy scan_fn signature without keyword — try positional fallback.
        scan = scan_callable()  # type: ignore[call-arg]

    ollama_entries = _ollama_provider_entries(scan, runnable_check_fn=runnable_check_fn)
    api_entries = _api_provider_entries(env_map)

    # Registration order for tie-breaking: recommended Ollama entry, then API entries,
    # then any extra Ollama entries. We rely on Python's stable sort.
    ordered = list(ollama_entries[:1]) + api_entries + list(ollama_entries[1:])
    ordered.sort(key=lambda p: p.priority)
    return ordered


__all__ = [
    "DEFAULT_OLLAMA_ENDPOINT",
    "OllamaScan",
    "Provider",
    "ProviderKind",
    "ProviderStatus",
    "RECOMMENDED_OLLAMA_MODELS",
    "detect_llm_providers",
    "probe_model_runnable",
    "scan_ollama",
]
