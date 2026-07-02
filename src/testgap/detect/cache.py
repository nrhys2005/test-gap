"""User-home shared cache for LLM provider runnability probes.

Rationale (TG-401 P1-1): Ollama server state (endpoint + pulled models +
runnability) is a **user-machine** fact, not a project fact. Sharing the cache
across projects avoids repeating the D2 (pulled-but-broken) diagnosis on every
new project's first run.

Cache location follows XDG:
    ``$XDG_CACHE_HOME/testgap/detect_cache.json``
    → fallback ``~/.cache/testgap/detect_cache.json``.

Format (v1):
    {
      "version": 1,
      "entries": [
        {"model": "ollama/qwen2.5-coder:7b",
         "endpoint": "http://localhost:11434",
         "runnable": true,
         "checked_at": 1720000000.0,
         "error": null}
      ]
    }

Corrupt JSON / version mismatch → treated as empty; the next ``store_runnable``
overwrites atomically. This module never raises on corruption — silent recovery
is the right UX (we do not want to spam the console).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CACHE_FILENAME = "detect_cache.json"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
CACHE_VERSION = 1


@dataclass
class RunnableCacheEntry:
    model: str
    endpoint: str
    runnable: bool
    checked_at: float
    error: str | None = None


class DetectCache:
    """Load/store ``RunnableCacheEntry`` records keyed by ``(model, endpoint)``.

    Instantiate without arguments to use :meth:`default_path`. Tests inject a
    custom ``path`` for isolation (typically via ``monkeypatch.setenv`` on
    ``XDG_CACHE_HOME`` + ``default_path()`` — see ``tests/test_detect_cache.py``).
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.path = path if path is not None else self.default_path()
        self.ttl_seconds = ttl_seconds

    # ------------------------------------------------------------------
    # location
    # ------------------------------------------------------------------

    @staticmethod
    def default_path() -> Path:
        """Resolve the XDG-compliant default cache location.

        Order:
          1. ``$XDG_CACHE_HOME/testgap/detect_cache.json`` when the env var is set
             to a non-empty value.
          2. ``~/.cache/testgap/detect_cache.json`` otherwise.
        """
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".cache"
        return base / "testgap" / CACHE_FILENAME

    # ------------------------------------------------------------------
    # read/write
    # ------------------------------------------------------------------

    def _load_raw(self) -> list[dict] | None:
        """Load raw entries or return None on any error / version mismatch."""
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("version") != CACHE_VERSION:
            return None
        entries = data.get("entries")
        if not isinstance(entries, list):
            return None
        return entries

    def load_runnable(self, model: str, endpoint: str) -> RunnableCacheEntry | None:
        """Return the cached entry for ``(model, endpoint)`` if still fresh."""
        entries = self._load_raw()
        if entries is None:
            return None
        now = time.time()
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            if raw.get("model") != model or raw.get("endpoint") != endpoint:
                continue
            checked = raw.get("checked_at")
            if not isinstance(checked, int | float):
                continue
            if now - float(checked) > self.ttl_seconds:
                return None
            try:
                return RunnableCacheEntry(
                    model=str(raw["model"]),
                    endpoint=str(raw["endpoint"]),
                    runnable=bool(raw["runnable"]),
                    checked_at=float(checked),
                    error=raw.get("error"),
                )
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def store_runnable(self, entry: RunnableCacheEntry) -> None:
        """Upsert ``entry`` keyed by ``(model, endpoint)`` and write atomically."""
        entries = self._load_raw() or []
        merged: list[dict] = [
            e
            for e in entries
            if isinstance(e, dict)
            and (e.get("model") != entry.model or e.get("endpoint") != entry.endpoint)
        ]
        merged.append(asdict(entry))
        payload = {"version": CACHE_VERSION, "entries": merged}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp file in same directory + rename.
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.path.parent),
            prefix=".detect_cache.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(payload, tf, indent=2)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, self.path)

    def clear(self) -> None:
        """Delete the cache file, ignoring "already missing" errors."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "CACHE_FILENAME",
    "CACHE_VERSION",
    "DEFAULT_TTL_SECONDS",
    "DetectCache",
    "RunnableCacheEntry",
]
