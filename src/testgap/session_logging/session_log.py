"""JSONL session log for ``testgap diff`` runs.

Design notes (see ``.plans/TG-406.md`` for the full plan):

* **Best-effort**: every filesystem interaction is wrapped in ``try/except``.
  A failure at ``start`` returns a :class:`NoopSessionLog`; a failure during
  ``record`` flips ``_degraded=True`` so subsequent writes silently no-op;
  a failure during ``close`` emits a single stderr warning and is swallowed.
  The pipeline never dies because of a log-write problem.
* **Ownership**: the CLI opens the log with a ``with`` block. ``run_diff`` /
  ``run_review_session`` accept an already-open instance and only ``record``.
  This avoids double-close and lets the CLI print the session-log path once.
* **Counters**: ``record`` inspects the event name and folds ``cost_usd`` /
  ``pass_count`` / ``fail_count`` into ``_Counters`` so ``session_end`` can
  emit the aggregates without callers tracking them.
* **Windows-safe**: filenames use ``2026-07-03T09-42-15Z-<8>.jsonl`` (dash
  instead of colon). Event payloads keep standard ISO8601 (with colons) in
  the ``ts`` field because they are JSON strings.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Protocol, runtime_checkable

from testgap import __version__ as _testgap_version
from testgap.config.schema import TestGapConfig
from testgap.session_logging.events import (
    EVENT_LLM_CALL,
    EVENT_PYTEST_RUN,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    log_filename,
    utc_iso_now,
)


@runtime_checkable
class SessionLogProtocol(Protocol):
    """Interface pipeline / interactive depend on.

    Kept minimal so :class:`NoopSessionLog` can be a drop-in when logging
    is disabled or degraded.
    """

    def record(self, event: str, payload: dict[str, Any]) -> None: ...
    def close(self, *, quit_reason: str | None = None) -> None: ...
    def increment_functions(self, n: int = 1) -> None: ...
    def __enter__(self) -> SessionLogProtocol: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    @property
    def path(self) -> Path | None: ...


@dataclass
class _Counters:
    """Aggregates surfaced in ``session_end``.

    Updated inside :meth:`SessionLog.record` based on event branching:

    * ``llm_call`` → ``total_cost += payload.get("cost_usd", 0.0)``
      (``.get`` because failure events omit ``cost_usd``).
    * ``pytest_run`` → tests_accepted / tests_discarded pull from
      ``pass_count`` / ``fail_count``.
    * ``functions_processed`` is bumped explicitly by callers via
      :meth:`SessionLog.increment_functions` — pipeline calls it once per
      finalized function, so double-round retries do not double-count.
    """

    functions_processed: int = 0
    tests_accepted: int = 0
    tests_discarded: int = 0
    total_cost: float = 0.0


class SessionLog:
    """Real (writing) session log. Backed by a line-buffered JSONL file."""

    def __init__(
        self,
        path: Path,
        *,
        project_root: Path,
        config: TestGapConfig,
    ) -> None:
        self._path = path
        self._project_root = project_root
        self._config = config
        self._counters = _Counters()
        self._degraded = False
        self._closed = False
        self._file: IO[str] | None = None
        # ``time.monotonic()`` gives us a wall-clock-independent duration.
        # See validation note #2: session_end.duration_s must not attempt
        # ISO8601 string subtraction.
        self._t_start = time.monotonic()
        # Best-effort open. If it fails we flip ``_degraded`` so all further
        # ``record`` / ``close`` calls no-op. The factory below still returns
        # a real ``SessionLog`` in that case (path is set), but users prefer
        # the factory anyway which swaps in a Noop.
        try:
            self._file = open(path, "a", encoding="utf-8", buffering=1)
        except OSError as e:  # pragma: no cover - covered via factory path
            self._degraded = True
            _warn(f"session log open failed: {e}")

        self._emit_session_start()

    # ------------------------------------------------------------------
    # factory
    # ------------------------------------------------------------------

    @classmethod
    def start(cls, project_root: Path, config: TestGapConfig) -> SessionLogProtocol:
        """Best-effort factory. Falls back to :class:`NoopSessionLog` on failure.

        The caller (typically the CLI) does not need to distinguish between
        "logging enabled" and "logging degraded" — both satisfy
        :class:`SessionLogProtocol`.
        """
        logs_dir = project_root / ".testgap" / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _warn(f"session log disabled: {e}")
            return NoopSessionLog()

        path = logs_dir / log_filename()
        try:
            instance = cls(path, project_root=project_root, config=config)
        except OSError as e:  # pragma: no cover - constructor swallows OSError
            _warn(f"session log disabled: {e}")
            return NoopSessionLog()
        if instance._degraded:
            # Open failed inside ``__init__`` — fall back to Noop.
            return NoopSessionLog()
        return instance

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self._path

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def record(self, event: str, payload: dict[str, Any]) -> None:
        """Append one JSONL line. Silently no-ops after degrade / close."""
        if self._degraded or self._closed:
            return
        self._update_counters(event, payload)
        self._write_event(event, payload)

    def increment_functions(self, n: int = 1) -> None:
        """Bump ``functions_processed`` — pipeline calls this per finalization."""
        if self._degraded or self._closed:
            return
        self._counters.functions_processed += n

    def close(self, *, quit_reason: str | None = None) -> None:
        """Emit ``session_end`` and close the file. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._degraded:
            self._safe_close_file()
            return

        payload = {
            "total_cost": round(self._counters.total_cost, 6),
            "functions_processed": self._counters.functions_processed,
            "tests_accepted": self._counters.tests_accepted,
            "tests_discarded": self._counters.tests_discarded,
            "duration_s": round(time.monotonic() - self._t_start, 3),
            "quit_reason": quit_reason,
        }
        try:
            self._write_event(EVENT_SESSION_END, payload)
        except Exception as e:  # pragma: no cover - defensive
            _warn(f"session log close write failed: {e}")

        self._safe_close_file()

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> SessionLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Infer quit_reason from the exception context so callers never
        # need to remember to set it themselves.
        if exc_type is None:
            quit_reason = None
        elif issubclass(exc_type, KeyboardInterrupt):
            quit_reason = "keyboard_interrupt"
        else:
            quit_reason = "exception"
        self.close(quit_reason=quit_reason)
        # Return None (falsy) → propagate the exception.

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _emit_session_start(self) -> None:
        payload = {
            "project_root": str(self._project_root.resolve()),
            "config": {
                "model": self._config.llm.model,
                "max_cost": self._config.llm.max_cost_per_run,
                "max_tests_per_function": (
                    self._config.generation.max_tests_per_function
                ),
                "source_paths": list(self._config.project.source_paths),
                "test_paths": list(self._config.project.test_paths),
            },
            "testgap_version": _testgap_version,
        }
        try:
            self._write_event(EVENT_SESSION_START, payload)
        except Exception as e:  # pragma: no cover - defensive
            _warn(f"session log start write failed: {e}")
            self._degraded = True

    def _update_counters(self, event: str, payload: dict[str, Any]) -> None:
        # ``.get`` guards against missing keys: failure ``llm_call`` events
        # omit ``cost_usd`` (see validation note #6); a pytest_run with an
        # environment_error omits pass/fail counts entirely.
        if event == EVENT_LLM_CALL:
            self._counters.total_cost += float(payload.get("cost_usd", 0.0))
        elif event == EVENT_PYTEST_RUN:
            self._counters.tests_accepted += int(payload.get("pass_count", 0))
            self._counters.tests_discarded += int(payload.get("fail_count", 0))

    def _write_event(self, event: str, payload: dict[str, Any]) -> None:
        if self._file is None:  # pragma: no cover - degraded path
            return
        line = {"event": event, "ts": utc_iso_now(), **payload}
        try:
            # ``default=str`` catches Path / Enum / dataclass leaking into
            # payloads. ``ensure_ascii=False`` keeps unicode readable.
            serialized = json.dumps(line, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            _warn(f"session log serialize failed: {e}")
            self._degraded = True
            return

        try:
            self._file.write(serialized + "\n")
        except (OSError, ValueError) as e:
            # ``ValueError`` covers "I/O operation on closed file" which
            # ``open()`` raises rather than ``OSError``.
            _warn(f"session log write failed: {e}")
            self._degraded = True

    def _safe_close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError as e:  # pragma: no cover - defensive
                _warn(f"session log close failed: {e}")
            self._file = None


class NoopSessionLog:
    """Drop-in replacement when logging is disabled or degraded at start."""

    @property
    def path(self) -> Path | None:
        return None

    def record(self, event: str, payload: dict[str, Any]) -> None:
        return None

    def increment_functions(self, n: int = 1) -> None:
        return None

    def close(self, *, quit_reason: str | None = None) -> None:
        return None

    def __enter__(self) -> NoopSessionLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def open_session_log(
    project_root: Path,
    config: TestGapConfig,
    *,
    enabled: bool = True,
) -> SessionLogProtocol:
    """CLI-facing factory. ``enabled=False`` returns a :class:`NoopSessionLog`."""
    if not enabled:
        return NoopSessionLog()
    return SessionLog.start(project_root, config)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _warn(message: str) -> None:
    """Emit a single-line warning to stderr — never raises."""
    try:
        print(f"testgap: {message}", file=sys.stderr)
    except Exception:  # pragma: no cover - stderr write cannot fail meaningfully
        pass


def log_file_rel(file: Path, project_root: Path) -> str:
    """Project-relative path string for log payloads, absolute fallback."""
    try:
        return str(file.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(file)
