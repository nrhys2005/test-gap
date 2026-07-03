"""Event type constants and timestamp helpers for session logging.

These live in a dedicated module (a) so callers can import event names as
constants instead of stringly-typed literals, and (b) so we can unit-test
the Windows-safe filename stamp in isolation from the file-writing layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

# Event type strings. Keep short — they appear on every JSONL line.
EVENT_SESSION_START = "session_start"
EVENT_LLM_CALL = "llm_call"
EVENT_PYTEST_RUN = "pytest_run"
EVENT_USER_ACTION = "user_action"
EVENT_SESSION_END = "session_end"


def utc_iso_now(now: datetime | None = None) -> str:
    """ISO8601 UTC timestamp with microseconds — used inside JSONL payloads.

    Example: ``"2026-07-03T09:42:15.123456+00:00"``. Colons are preserved
    here because this string lives *inside* a JSON string value, not in a
    filesystem path.
    """
    now = now or datetime.now(timezone.utc)
    return now.isoformat()


def safe_utc_stamp(now: datetime | None = None) -> str:
    """Windows-safe UTC stamp for filenames — no colons.

    Example: ``"2026-07-03T09-42-15Z"``. Colons (``:``) are forbidden in
    Windows filenames, so we substitute dashes and drop the microsecond /
    offset suffix in favour of a trailing ``Z``.
    """
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def log_filename(now: datetime | None = None) -> str:
    """Build a unique log filename ``<safe_utc_stamp>-<8char_uuid>.jsonl``."""
    return f"{safe_utc_stamp(now)}-{uuid.uuid4().hex[:8]}.jsonl"
