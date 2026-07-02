"""Unit tests for ``testgap.detect.cache.DetectCache``."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from testgap.detect.cache import (
    CACHE_FILENAME,
    CACHE_VERSION,
    DEFAULT_TTL_SECONDS,
    DetectCache,
    RunnableCacheEntry,
)


@pytest.fixture
def isolated_detect_cache(monkeypatch, tmp_path):
    """Point XDG_CACHE_HOME at a per-test tmp dir so ``default_path`` is isolated."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


def _entry(model="ollama/qwen2.5-coder:7b", endpoint="http://localhost:11434", *, runnable=True):
    return RunnableCacheEntry(
        model=model,
        endpoint=endpoint,
        runnable=runnable,
        checked_at=time.time(),
        error=None,
    )


# ---------------------------------------------------------------------------
# load / store / clear
# ---------------------------------------------------------------------------


def test_load_returns_none_when_file_missing(tmp_path: Path):
    cache = DetectCache(tmp_path / CACHE_FILENAME)
    assert cache.load_runnable("m", "http://x") is None


def test_store_and_load_roundtrip(tmp_path: Path):
    cache = DetectCache(tmp_path / CACHE_FILENAME)
    entry = _entry()
    cache.store_runnable(entry)
    loaded = cache.load_runnable(entry.model, entry.endpoint)
    assert loaded is not None
    assert loaded.model == entry.model
    assert loaded.runnable is True
    assert abs(loaded.checked_at - entry.checked_at) < 0.5


def test_load_returns_none_when_expired(tmp_path: Path):
    cache = DetectCache(tmp_path / CACHE_FILENAME, ttl_seconds=5)
    entry = RunnableCacheEntry(
        model="m", endpoint="ep", runnable=True, checked_at=time.time() - 3600
    )
    cache.store_runnable(entry)
    assert cache.load_runnable("m", "ep") is None


def test_load_ignores_corrupt_json(tmp_path: Path):
    path = tmp_path / CACHE_FILENAME
    path.write_text("{{ not json", encoding="utf-8")
    cache = DetectCache(path)
    assert cache.load_runnable("m", "ep") is None
    # subsequent store must still succeed
    cache.store_runnable(_entry("m", "ep"))
    assert cache.load_runnable("m", "ep") is not None


def test_load_ignores_version_mismatch(tmp_path: Path):
    path = tmp_path / CACHE_FILENAME
    path.write_text(
        json.dumps({"version": 999, "entries": [{"model": "m", "endpoint": "ep",
                                                 "runnable": True, "checked_at": time.time()}]}),
        encoding="utf-8",
    )
    cache = DetectCache(path)
    assert cache.load_runnable("m", "ep") is None


def test_clear_removes_file(tmp_path: Path):
    path = tmp_path / CACHE_FILENAME
    cache = DetectCache(path)
    cache.store_runnable(_entry())
    assert path.exists()
    cache.clear()
    assert not path.exists()


def test_clear_when_missing_is_noop(tmp_path: Path):
    cache = DetectCache(tmp_path / CACHE_FILENAME)
    # must not raise
    cache.clear()


def test_store_creates_parent_dir(tmp_path: Path):
    # target: tmp/nested/dir/detect_cache.json
    target = tmp_path / "nested" / "dir" / CACHE_FILENAME
    cache = DetectCache(target)
    cache.store_runnable(_entry())
    assert target.exists()


# ---------------------------------------------------------------------------
# (model, endpoint) key semantics (P1-1)
# ---------------------------------------------------------------------------


def test_load_returns_none_for_different_endpoint(tmp_path: Path):
    cache = DetectCache(tmp_path / CACHE_FILENAME)
    cache.store_runnable(_entry(endpoint="http://a:11434"))
    assert cache.load_runnable("ollama/qwen2.5-coder:7b", "http://b:11434") is None


def test_store_upserts_by_model_endpoint_key(tmp_path: Path):
    cache = DetectCache(tmp_path / CACHE_FILENAME)
    cache.store_runnable(_entry(endpoint="http://a", runnable=False))
    cache.store_runnable(_entry(endpoint="http://a", runnable=True))
    cache.store_runnable(_entry(endpoint="http://b", runnable=True))
    # Same (model, endpoint) → last write wins.
    a = cache.load_runnable("ollama/qwen2.5-coder:7b", "http://a")
    b = cache.load_runnable("ollama/qwen2.5-coder:7b", "http://b")
    assert a is not None and a.runnable is True
    assert b is not None and b.runnable is True


# ---------------------------------------------------------------------------
# default_path XDG behaviour (P1-1)
# ---------------------------------------------------------------------------


def test_default_path_uses_xdg_cache_home(isolated_detect_cache: Path):
    expected = isolated_detect_cache / "testgap" / CACHE_FILENAME
    assert DetectCache.default_path() == expected


def test_default_path_falls_back_to_home_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert DetectCache.default_path() == tmp_path / ".cache" / "testgap" / CACHE_FILENAME


def test_default_path_ignores_empty_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # empty string treated as unset
    assert DetectCache.default_path() == tmp_path / ".cache" / "testgap" / CACHE_FILENAME


# ---------------------------------------------------------------------------
# constants sanity
# ---------------------------------------------------------------------------


def test_constants_are_stable():
    assert DEFAULT_TTL_SECONDS == 24 * 60 * 60
    assert CACHE_VERSION == 1
    assert CACHE_FILENAME.endswith(".json")
