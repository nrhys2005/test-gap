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
    _classify_binary_source,
    _ollama_provider_entries,
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
# probe_model_runnable (deprecated shim — see review round 1 F1)
# ---------------------------------------------------------------------------


def test_probe_model_runnable_is_optimistic_shim():
    """The shim never performs I/O and always reports "runnable".

    The historical implementation issued an HTTP call to Ollama's ``/api/show``.
    Round-1 review of TG-401 concluded that the probe was unreliable in
    practice (wrong HTTP verb + version drift) and that the pipeline's
    consecutive-failure guard is the right safety net. We keep the symbol for
    import back-compat, but it is a no-op today.
    """

    def _boom(url, timeout=None):  # pragma: no cover — must never fire
        raise AssertionError("probe_model_runnable must not perform I/O")

    status, err = probe_model_runnable(
        "http://localhost:11434", "ollama/qwen2.5-coder:7b", http_fn=_boom
    )
    assert status == "runnable"
    assert err is None


def test_probe_model_runnable_ignores_endpoint_and_model():
    """Signature stability: the shim tolerates any input and never raises."""
    status, err = probe_model_runnable("", "")
    assert (status, err) == ("runnable", None)


def test_detect_pulled_marked_runnable_without_probe():
    """By default a pulled recommended model is PULLED_RUNNABLE — no probe fires."""
    providers = detect_llm_providers(
        env={}, scan_fn=_scan(pulled=("qwen2.5-coder:7b",))
    )
    top = providers[0]
    assert top.status == ProviderStatus.PULLED_RUNNABLE
    assert top.model == "ollama/qwen2.5-coder:7b"


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


def test_detect_surfaces_multiple_pulled_recommended_models():
    """Regression: previously all recommended models were filtered from the extra
    loop, so only the top-priority match appeared. Now every pulled model shows,
    with only the single ``matched`` entry deduplicated.
    """
    # User has pulled two recommended models (7b + 14b) and one non-recommended
    # (llama3.1:8b). All three should appear as PULLED_RUNNABLE.
    providers = detect_llm_providers(
        env={},
        scan_fn=_scan(
            pulled=("qwen2.5-coder:7b", "qwen2.5-coder:14b", "llama3.1:8b"),
        ),
    )
    pulled_runnable_models = [
        p.model
        for p in providers
        if p.status == ProviderStatus.PULLED_RUNNABLE
    ]
    # 7b matches first in RECOMMENDED_OLLAMA_MODELS priority so it's the top.
    # 14b and llama3.1:8b must both surface as additional entries.
    assert "ollama/qwen2.5-coder:7b" in pulled_runnable_models
    assert "ollama/qwen2.5-coder:14b" in pulled_runnable_models
    assert "ollama/llama3.1:8b" in pulled_runnable_models
    # And no duplicate of the matched model.
    assert pulled_runnable_models.count("ollama/qwen2.5-coder:7b") == 1


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


# ---------------------------------------------------------------------------
# TG-414: _classify_binary_source + Ollama.app vs CLI hint matrix
# ---------------------------------------------------------------------------


def test_classify_binary_source_app_path():
    assert (
        _classify_binary_source(
            "/Applications/Ollama.app/Contents/Resources/ollama"
        )
        == "app"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/opt/homebrew/bin/ollama",
        "/usr/local/bin/ollama",
        "~/bin/ollama",
    ],
)
def test_classify_binary_source_cli_paths(path):
    assert _classify_binary_source(path) == "cli"


def test_classify_binary_source_cli_home_prefix(monkeypatch, tmp_path):
    """A path under ``Path.home()`` (e.g. ``~/.local/bin/ollama``) is CLI."""
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = str(tmp_path / ".local" / "bin" / "ollama")
    assert _classify_binary_source(resolved) == "cli"


def test_classify_binary_source_unknown_path():
    assert _classify_binary_source("/opt/custom/ollama") == "unknown"


@pytest.mark.parametrize("value", [None, ""])
def test_classify_binary_source_missing(value):
    assert _classify_binary_source(value) == "missing"


def test_scan_ollama_records_binary_source_and_path_for_app():
    app_path = "/Applications/Ollama.app/Contents/Resources/ollama"
    body = json.dumps({"models": []}).encode("utf-8")
    scan = scan_ollama(
        which_fn=lambda _: app_path, http_fn=_http_returning(body)
    )
    assert scan.binary_source == "app"
    assert scan.binary_path == app_path


def test_scan_ollama_records_binary_source_when_unreachable():
    """Even when the server is unreachable the binary source is still recorded."""
    scan = scan_ollama(
        which_fn=lambda _: "/opt/homebrew/bin/ollama",
        http_fn=_http_raising(URLError("nope")),
    )
    assert scan.binary_source == "cli"
    assert scan.binary_path == "/opt/homebrew/bin/ollama"
    assert scan.server_reachable is False


def test_scan_ollama_records_missing_when_which_returns_none():
    body = json.dumps({"models": []}).encode("utf-8")
    scan = scan_ollama(which_fn=lambda _: None, http_fn=_http_returning(body))
    assert scan.binary_source == "missing"
    assert scan.binary_path is None


def test_ollama_provider_entries_app_not_installed_hint():
    """binary_source=app + server unreachable → app-specific hint."""
    scan = OllamaScan(
        binary_present=True,
        endpoint="http://localhost:11434",
        server_reachable=False,
        pulled_models=(),
        binary_source="app",
        binary_path="/Applications/Ollama.app/Contents/Resources/ollama",
    )
    entries = _ollama_provider_entries(scan, runnable_check_fn=None)
    assert entries[0].status == ProviderStatus.NOT_INSTALLED
    assert "Ollama.app not running" in entries[0].hint
    assert "Applications" in entries[0].hint
    assert entries[0].extra.get("binary_source") == "app"


def test_ollama_provider_entries_app_broken_uses_menubar_hint():
    """binary_source=app + PULLED_BROKEN → menu bar upgrade guidance."""
    scan = OllamaScan(
        binary_present=True,
        endpoint="http://localhost:11434",
        server_reachable=True,
        pulled_models=("qwen2.5-coder:7b",),
        binary_source="app",
        binary_path="/Applications/Ollama.app/Contents/Resources/ollama",
    )
    entries = _ollama_provider_entries(
        scan, runnable_check_fn=lambda ep, m: False
    )
    broken = next(p for p in entries if p.status == ProviderStatus.PULLED_BROKEN)
    assert "Upgrade via menu bar" in broken.hint


def test_ollama_provider_entries_app_not_pulled_hint():
    """binary_source=app + NOT_PULLED → app-aware pull instruction."""
    scan = OllamaScan(
        binary_present=True,
        endpoint="http://localhost:11434",
        server_reachable=True,
        pulled_models=("mistral:7b",),
        binary_source="app",
        binary_path="/Applications/Ollama.app/Contents/Resources/ollama",
    )
    entries = _ollama_provider_entries(scan, runnable_check_fn=None)
    top = next(p for p in entries if p.status == ProviderStatus.NOT_PULLED)
    assert "Ollama.app detected" in top.hint
    assert "ollama pull qwen2.5-coder:7b" in top.hint


def test_ollama_provider_entries_cli_regression_pulled_runnable():
    """binary_source=cli + PULLED_RUNNABLE keeps the historical hint verbatim."""
    scan = OllamaScan(
        binary_present=True,
        endpoint="http://localhost:11434",
        server_reachable=True,
        pulled_models=("qwen2.5-coder:7b",),
        binary_source="cli",
        binary_path="/opt/homebrew/bin/ollama",
    )
    entries = _ollama_provider_entries(scan, runnable_check_fn=None)
    top = entries[0]
    assert top.status == ProviderStatus.PULLED_RUNNABLE
    assert top.hint == "ready — using ollama/qwen2.5-coder:7b"


def test_ollama_provider_entries_unknown_surfaces_path():
    """binary_source=unknown → hint surfaces the raw path (NOT_PULLED case)."""
    scan = OllamaScan(
        binary_present=True,
        endpoint="http://localhost:11434",
        server_reachable=True,
        pulled_models=(),
        binary_source="unknown",
        binary_path="/opt/exotic/bin/ollama",
    )
    entries = _ollama_provider_entries(scan, runnable_check_fn=None)
    top = entries[0]
    assert top.status == ProviderStatus.NOT_PULLED
    assert "/opt/exotic/bin/ollama" in top.hint


def test_ollama_provider_entries_missing_matches_cli_default():
    """binary_source=missing (no binary, no server) → historical install hint."""
    scan = OllamaScan(
        binary_present=False,
        endpoint="http://localhost:11434",
        server_reachable=False,
        pulled_models=(),
        binary_source="missing",
        binary_path=None,
    )
    entries = _ollama_provider_entries(scan, runnable_check_fn=None)
    assert entries[0].status == ProviderStatus.NOT_INSTALLED
    assert "install ollama" in entries[0].hint
