"""JSONL session logs under ``.testgap/logs/`` for post-run inspection.

Public surface:

* :class:`SessionLog` — real implementation, writes JSONL lines.
* :class:`NoopSessionLog` — drop-in when logging is disabled or degraded.
* :class:`SessionLogProtocol` — the interface pipeline / interactive depend on.
* :func:`open_session_log` — CLI factory; ``enabled=False`` returns Noop.
* :func:`log_file_rel` — project-relative path helper for payloads.

The module name deliberately avoids ``logging`` to prevent shadowing the
Python standard library.
"""

from testgap.session_logging.session_log import (
    NoopSessionLog,
    SessionLog,
    SessionLogProtocol,
    log_file_rel,
    open_session_log,
)

__all__ = [
    "NoopSessionLog",
    "SessionLog",
    "SessionLogProtocol",
    "log_file_rel",
    "open_session_log",
]
