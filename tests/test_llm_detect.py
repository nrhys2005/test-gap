"""Unit tests for ``testgap.detect.llm_provider``.

All I/O is injected — no live Ollama server, no ``shutil.which`` on the host.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest

from testgap.detect import llm_provider as lp
from testgap.detect.llm_provider import (
    OllamaScan,
    ProviderKind,
    ProviderStatus,
    detect_llm_providers,
    probe_model_runnable,
    scan_ollama,
)

# ---------------------------------------------------------------------------
# scan_ollama
# ---------------------------------------------------------------------------


def _http_returning(body: bytes):
    def fake(url, timeout=None):
        return body

    return fake


def _http_raising(exc: BaseException):
    def fake(url, timeout=None):
        raise exc

    return fake


def test_scan_ollama_binary_and_endpoint_ok():
    body = json.dumps(
        {"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.1:8b"}]}
    ).encode("utf-8")
    scan = scan_ollama(which_fn=lambda _: "/usr/local/bin/ollama", http_fn=_http_returning(body))
    assert scan.binary_present is True
    assert scan.server_reachable is True
    assert scan.pulled_models == ("qwen2.5-coder:7b", "llama3.1:8b")
    assert scan.error is None


def test_scan_ollama_binary_missing():
    body = json.dumps({"models": []}).encode("utf-8")
    scan = scan_ollama(which_fn=lambda _: None, http_fn=_http_returning(body))
    assert scan.binary_present is False
    # server reachable even without binary — user might connect to a remote one
    assert scan.server_reachable is True
    assert scan.pulled_models == ()


def test_scan_ollama_server_unreachable():
    scan = scan_ollama(
        which_fn=lambda _: "/x/ollama",
        http_fn=_http_raising(URLError("connection refused")),
    )
    assert scan.binary_present is True
    assert scan.server_reachable is False
    assert "connection refused" in (scan.error or "")


def test_scan_ollama_server_500():
    exc = HTTPError(url="http://x/api/tags", code=500, msg="oops", hdrs=None, fp=None)
    scan = scan_ollama(which_fn=lambda _: "/x/ollama", http_fn=_http_raising(exc))
    assert scan.server_reachable is False
    assert scan.pulled_models == ()
    assert scan.error


def test_scan_ollama_bad_json():
    scan = scan_ollama(
        which_fn=lambda _: "/x/ollama", http_fn=_http_returning(b"not-json")
    )
    assert scan.server_reachable is False
    assert scan.error


def test_scan_ollama_uses_provided_endpoint():
    captured = {}

    def fake(url, timeout=None):
        captured["url"] = url
        return b'{"models": []}'

    scan_ollama(
        endpoint="http://custom.example:11434",
        which_fn=lambda _: "/x/ollama",
        http_fn=fake,
    )
    assert captured["url"].startswith("http://custom.example:11434")


# ---------------------------------------------------------------------------
# probe_model_runnable
# ---------------------------------------------------------------------------


def test_probe_runnable_ok():
    def fake(url, timeout=None):
        return b'{"license": "mit"}'

    status, err = probe_model_runnable(
        "http://localhost:11434", "ollama/qwen2.5-coder:7b", http_fn=fake
    )
    assert status == "runnable"
    assert err is None


def test_probe_runnable_http_500():
    exc = HTTPError(url="http://x/api/show", code=500, msg="oops", hdrs=None, fp=None)
    status, err = probe_model_runnable(
        "http://localhost:11434", "ollama/foo", http_fn=_http_raising(exc)
    )
    assert status == "broken"
    assert "500" in (err or "")


def test_probe_runnable_network_error():
    status, err = probe_model_runnable(
        "http://localhost:11434",
        "ollama/foo",
        http_fn=_http_raising(URLError("no route to host")),
    )
    assert status == "broken"
    assert "no route" in (err or "")


# ---------------------------------------------------------------------------
# detect_llm_providers
# ---------------------------------------------------------------------------


def _scan(*, pulled=(), reachable=True, binary=True):
    def fake(**kwargs):
        endpoint = kwargs.get("endpoint", "http://localhost:11434")
        return OllamaScan(
            binary_present=binary,
            endpoint=endpoint,
            server_reachable=reachable,
            pulled_models=tuple(pulled),
        )

    return fake


def test_detect_llm_providers_prioritizes_pulled_runnable():
    providers = detect_llm_providers(
        env={"ANTHROPIC_API_KEY": "sk"},
        scan_fn=_scan(pulled=("qwen2.5-coder:7b",)),
    )
    assert providers[0].status == ProviderStatus.PULLED_RUNNABLE
    assert providers[0].model == "ollama/qwen2.5-coder:7b"
    # KEY_FOUND ranks below PULLED_RUNNABLE
    assert providers[1].status == ProviderStatus.KEY_FOUND


def test_detect_llm_providers_falls_back_when_nothing_available():
    providers = detect_llm_providers(env={}, scan_fn=_scan(reachable=False, binary=False))
    # last entry should be NOT_INSTALLED
    assert providers[-1].status == ProviderStatus.NOT_INSTALLED


def test_detect_llm_providers_pulled_broken_sinks_priority():
    providers = detect_llm_providers(
        env={"ANTHROPIC_API_KEY": "sk"},
        scan_fn=_scan(pulled=("qwen2.5-coder:7b",)),
        runnable_check_fn=lambda endpoint, model: False,
    )
    # KEY_FOUND (priority 1) should be first because PULLED_BROKEN is priority 2.
    assert providers[0].status == ProviderStatus.KEY_FOUND
    broken = next(p for p in providers if p.status == ProviderStatus.PULLED_BROKEN)
    assert broken.model == "ollama/qwen2.5-coder:7b"


def test_detect_uses_ollama_endpoint_override():
    captured = {}

    def fake_scan(**kwargs):
        captured["endpoint"] = kwargs.get("endpoint")
        return OllamaScan(
            binary_present=False,
            endpoint=kwargs.get("endpoint", ""),
            server_reachable=False,
            pulled_models=(),
        )

    detect_llm_providers(
        env={}, ollama_endpoint="http://remote:11434", scan_fn=fake_scan
    )
    assert captured["endpoint"] == "http://remote:11434"


def test_detect_surfaces_non_recommended_pulled_models():
    providers = detect_llm_providers(
        env={},
        scan_fn=_scan(pulled=("qwen2.5-coder:7b", "llama3.1:8b")),
    )
    # First is the recommended pulled model. There must also be an ``ollama/llama3.1:8b``
    # entry with RUNNABLE status.
    llama = next(p for p in providers if p.model == "ollama/llama3.1:8b")
    assert llama.status == ProviderStatus.PULLED_RUNNABLE
    assert llama.kind == ProviderKind.OLLAMA


def test_detect_not_pulled_when_ollama_up_but_no_recommended():
    providers = detect_llm_providers(env={}, scan_fn=_scan(pulled=("mistral:7b",)))
    top = providers[0]
    # First is either NOT_PULLED (recommended missing) or the non-recommended
    # pulled model (both may sort to top with equal priority — accept either but
    # ensure a NOT_PULLED row exists).
    assert any(p.status == ProviderStatus.NOT_PULLED for p in providers)
    _ = top


def test_detect_call_time_default_scan_reads_module_symbol(monkeypatch):
    """Monkeypatching ``llm_provider.scan_ollama`` must flow into the default."""
    marker = {"called": 0}

    def fake_scan(**kwargs):
        marker["called"] += 1
        return OllamaScan(
            binary_present=False,
            endpoint=kwargs.get("endpoint", ""),
            server_reachable=False,
            pulled_models=(),
        )

    monkeypatch.setattr(lp, "scan_ollama", fake_scan)
    detect_llm_providers(env={})
    assert marker["called"] == 1


# ---------------------------------------------------------------------------
# priority ordering
# ---------------------------------------------------------------------------


def test_priority_ordering_is_stable_within_status():
    """API providers preserve registration order when they share KEY_MISSING."""
    providers = detect_llm_providers(env={}, scan_fn=_scan(reachable=False, binary=False))
    key_missing = [p for p in providers if p.status == ProviderStatus.KEY_MISSING]
    assert [p.env_var for p in key_missing] == [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    ]


def test_provider_priority_int_matches_enum_order():
    providers = detect_llm_providers(
        env={"ANTHROPIC_API_KEY": "sk"},
        scan_fn=_scan(pulled=("qwen2.5-coder:7b",)),
    )
    p1 = next(p for p in providers if p.status == ProviderStatus.PULLED_RUNNABLE)
    p2 = next(p for p in providers if p.status == ProviderStatus.KEY_FOUND)
    assert p1.priority < p2.priority


# ---------------------------------------------------------------------------
# RECOMMENDED_OLLAMA_MODELS shape
# ---------------------------------------------------------------------------


def test_recommended_models_tuple_shape():
    from testgap.detect import RECOMMENDED_OLLAMA_MODELS

    assert isinstance(RECOMMENDED_OLLAMA_MODELS, tuple)
    assert len(RECOMMENDED_OLLAMA_MODELS) >= 1
    # 1순위는 코드 특화 모델
    assert "qwen2.5-coder:7b" in RECOMMENDED_OLLAMA_MODELS[0]


# ---------------------------------------------------------------------------
# scan_ollama defensive parsing
# ---------------------------------------------------------------------------


def test_scan_ollama_ignores_nameless_models():
    body = json.dumps(
        {"models": [{"digest": "abc"}, {"name": ""}, {"name": "ok:1"}]}
    ).encode("utf-8")
    scan = scan_ollama(which_fn=lambda _: "/x/ollama", http_fn=_http_returning(body))
    assert scan.pulled_models == ("ok:1",)


def test_scan_ollama_handles_non_dict_root():
    scan = scan_ollama(which_fn=lambda _: "/x/ollama", http_fn=_http_returning(b"[]"))
    # non-dict root → pulled_models empty, still reachable
    assert scan.pulled_models == ()
    assert scan.server_reachable is True


@pytest.mark.parametrize("exc", [TimeoutError("slow"), OSError("nope")])
def test_scan_ollama_wraps_other_errors(exc):
    scan = scan_ollama(which_fn=lambda _: "/x/ollama", http_fn=_http_raising(exc))
    assert scan.server_reachable is False
    assert scan.error
