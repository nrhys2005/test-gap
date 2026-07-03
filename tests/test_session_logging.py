"""Unit tests for ``testgap.session_logging``.

Scope:
* :class:`SessionLog` file lifecycle (start / record / close).
* Event payload shapes and counter aggregation.
* Best-effort degrade paths (open failure, write failure, exception in with).
* Windows-safe filename stamping.

Filesystem isolation uses ``tmp_path``. No LLM or subprocess required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testgap.config.schema import (
    GenerationConfig,
    LLMConfig,
    ProjectConfig,
    TestGapConfig,
)
from testgap.session_logging import (
    NoopSessionLog,
    SessionLog,
    SessionLogProtocol,
    open_session_log,
)
from testgap.session_logging.events import log_filename, safe_utc_stamp


def _config() -> TestGapConfig:
    return TestGapConfig(
        project=ProjectConfig(source_paths=["src/"], test_paths=["tests/"]),
        llm=LLMConfig(model="fake/model", max_cost_per_run=1.5),
        generation=GenerationConfig(max_tests_per_function=4),
    )


def _read_events(path: Path) -> list[dict]:
    """Parse a JSONL file into a list of dicts. Fails on any malformed line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# 1. session_start + session_end recorded on open + close
# ---------------------------------------------------------------------------


def test_default_session_writes_start_and_end(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    assert isinstance(log, SessionLogProtocol)
    assert log.path is not None
    path = log.path
    with log:
        pass  # no additional events

    events = _read_events(path)
    assert len(events) >= 2
    assert events[0]["event"] == "session_start"
    assert events[-1]["event"] == "session_end"
    # Every line must be valid JSON with common fields.
    for e in events:
        assert "event" in e
        assert "ts" in e


# ---------------------------------------------------------------------------
# 2. llm_call payload shape
# ---------------------------------------------------------------------------


def test_record_llm_call_payload_shape(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    with log:
        log.record(
            "llm_call",
            {
                "function_qualname": "mod.add",
                "function_file": "src/demo/mod.py",
                "attempt": 1,
                "model": "fake/model",
                "prompt_tokens": 100,
                "completion_tokens": 80,
                "cost_usd": 0.01,
                "duration_s": 0.42,
            },
        )
    events = _read_events(log.path)
    llm_calls = [e for e in events if e["event"] == "llm_call"]
    assert len(llm_calls) == 1
    payload = llm_calls[0]
    assert payload["function_qualname"] == "mod.add"
    assert payload["attempt"] == 1
    assert payload["model"] == "fake/model"
    assert payload["prompt_tokens"] == 100
    assert payload["completion_tokens"] == 80
    assert payload["cost_usd"] == 0.01
    assert payload["duration_s"] == 0.42


# ---------------------------------------------------------------------------
# 3. pytest_run payload shape
# ---------------------------------------------------------------------------


def test_record_pytest_run_payload_shape(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    with log:
        log.record(
            "pytest_run",
            {
                "function_qualname": "mod.add",
                "tmp_file": "test_testgap_tmp_add.py",
                "exit_code": 0,
                "pass_count": 3,
                "fail_count": 1,
                "duration_s": 0.5,
                "environment_error": None,
            },
        )
    events = _read_events(log.path)
    pytest_runs = [e for e in events if e["event"] == "pytest_run"]
    assert len(pytest_runs) == 1
    payload = pytest_runs[0]
    assert payload["tmp_file"] == "test_testgap_tmp_add.py"
    assert payload["exit_code"] == 0
    assert payload["pass_count"] == 3
    assert payload["fail_count"] == 1
    assert payload["environment_error"] is None


# ---------------------------------------------------------------------------
# 4. user_action payload — all 5 choices representable
# ---------------------------------------------------------------------------


def test_record_user_action_payload_shape(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    with log:
        for action, applied_path in [
            ("apply", "tests/test_mod.py"),
            ("skip", None),
            ("regenerate", None),
            ("edit", None),
            ("quit", None),
        ]:
            log.record(
                "user_action",
                {
                    "function_qualname": "mod.add",
                    "action": action,
                    "applied_path": applied_path,
                },
            )
    events = _read_events(log.path)
    actions = [e for e in events if e["event"] == "user_action"]
    assert [a["action"] for a in actions] == [
        "apply",
        "skip",
        "regenerate",
        "edit",
        "quit",
    ]
    assert actions[0]["applied_path"] == "tests/test_mod.py"
    assert actions[1]["applied_path"] is None


# ---------------------------------------------------------------------------
# 5. session_end aggregates counters
# ---------------------------------------------------------------------------


def test_session_end_aggregates_counters(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    with log:
        log.record(
            "llm_call",
            {"function_qualname": "f1", "attempt": 1, "cost_usd": 0.01},
        )
        log.record(
            "llm_call",
            {"function_qualname": "f2", "attempt": 1, "cost_usd": 0.02},
        )
        log.record(
            "pytest_run",
            {
                "function_qualname": "f1",
                "exit_code": 0,
                "pass_count": 3,
                "fail_count": 1,
            },
        )
        log.record(
            "pytest_run",
            {
                "function_qualname": "f2",
                "exit_code": 0,
                "pass_count": 2,
                "fail_count": 0,
            },
        )
        log.increment_functions()
        log.increment_functions()

    events = _read_events(log.path)
    ends = [e for e in events if e["event"] == "session_end"]
    assert len(ends) == 1
    end = ends[0]
    assert end["total_cost"] == pytest.approx(0.03)
    assert end["functions_processed"] == 2
    assert end["tests_accepted"] == 5
    assert end["tests_discarded"] == 1
    # duration_s must be a real, non-negative float.
    assert isinstance(end["duration_s"], (int, float))
    assert end["duration_s"] >= 0.0
    assert end["quit_reason"] is None


# ---------------------------------------------------------------------------
# 5b. llm_call without cost_usd must not KeyError inside counter update
# (validation note #6).
# ---------------------------------------------------------------------------


def test_llm_call_failure_event_without_cost_does_not_break_counter(
    tmp_path: Path,
) -> None:
    log = SessionLog.start(tmp_path, _config())
    with log:
        # failure event: no cost_usd, no tokens — mimics LLMError path.
        log.record(
            "llm_call",
            {
                "function_qualname": "f",
                "attempt": 1,
                "model": "fake/model",
                "duration_s": 0.1,
                "error": "network refused",
            },
        )
    events = _read_events(log.path)
    end = next(e for e in events if e["event"] == "session_end")
    assert end["total_cost"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. permission denied on mkdir → Noop
# ---------------------------------------------------------------------------


def test_open_permission_denied_returns_noop(tmp_path: Path) -> None:
    # Force mkdir to fail by creating a *file* where the ``.testgap`` dir
    # would normally live. mkdir(parents=True) then raises FileExistsError.
    (tmp_path / ".testgap").write_text("blocker", encoding="utf-8")

    log = open_session_log(tmp_path, _config(), enabled=True)
    assert isinstance(log, NoopSessionLog)
    assert log.path is None
    # Full lifecycle must remain no-op — no exceptions leaking out.
    with log:
        log.record("llm_call", {"cost_usd": 0.5})
        log.increment_functions()


# ---------------------------------------------------------------------------
# 7. write failure after start becomes silent no-op
# ---------------------------------------------------------------------------


def test_write_failure_becomes_noop_silently(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    assert isinstance(log, SessionLog)
    # Simulate a corrupted file handle by closing it out from under us.
    assert log._file is not None
    log._file.close()

    # Neither record nor close should raise; both silently degrade.
    log.record("llm_call", {"cost_usd": 0.01})
    log.close()  # emits nothing because degrade happens during write


# ---------------------------------------------------------------------------
# 8. exception inside `with` → session_end recorded with quit_reason
# ---------------------------------------------------------------------------


def test_exception_inside_with_records_session_end_with_quit_reason(
    tmp_path: Path,
) -> None:
    log = SessionLog.start(tmp_path, _config())
    path = log.path
    with pytest.raises(KeyboardInterrupt):
        with log:
            raise KeyboardInterrupt()

    events = _read_events(path)
    ends = [e for e in events if e["event"] == "session_end"]
    assert len(ends) == 1
    assert ends[0]["quit_reason"] == "keyboard_interrupt"


def test_generic_exception_inside_with_reports_exception_quit_reason(
    tmp_path: Path,
) -> None:
    log = SessionLog.start(tmp_path, _config())
    path = log.path
    with pytest.raises(RuntimeError):
        with log:
            raise RuntimeError("boom")
    events = _read_events(path)
    ends = [e for e in events if e["event"] == "session_end"]
    assert ends[0]["quit_reason"] == "exception"


# ---------------------------------------------------------------------------
# 9. Windows-safe filename (no colons, ends with Z + uuid8)
# ---------------------------------------------------------------------------


def test_safe_utc_stamp_no_colon() -> None:
    stamp = safe_utc_stamp()
    assert ":" not in stamp
    assert stamp.endswith("Z")

    name = log_filename()
    assert ":" not in name
    assert name.endswith(".jsonl")
    # <stamp>-<8char hex>.jsonl → dash-count includes stamp dashes.
    assert len(name.rsplit("-", 1)[1].split(".")[0]) == 8


# ---------------------------------------------------------------------------
# 10. existing logs dir reused; two sessions coexist
# ---------------------------------------------------------------------------


def test_existing_logs_dir_reused(tmp_path: Path) -> None:
    log_a = SessionLog.start(tmp_path, _config())
    path_a = log_a.path
    with log_a:
        log_a.record("user_action", {"action": "skip"})

    log_b = SessionLog.start(tmp_path, _config())
    path_b = log_b.path
    with log_b:
        log_b.record("user_action", {"action": "quit"})

    # Two distinct files under the same directory.
    assert path_a != path_b
    assert path_a.parent == path_b.parent == tmp_path / ".testgap" / "logs"
    assert path_a.exists()
    assert path_b.exists()


# ---------------------------------------------------------------------------
# extras
# ---------------------------------------------------------------------------


def test_noop_session_log_is_full_protocol() -> None:
    log = NoopSessionLog()
    assert isinstance(log, SessionLogProtocol)
    assert log.path is None
    # All operations silently succeed.
    with log:
        log.record("anything", {"k": 1})
        log.increment_functions(3)
        log.close(quit_reason="whatever")


def test_open_session_log_disabled_returns_noop(tmp_path: Path) -> None:
    log = open_session_log(tmp_path, _config(), enabled=False)
    assert isinstance(log, NoopSessionLog)
    assert log.path is None
    # No ``.testgap`` directory created.
    assert not (tmp_path / ".testgap").exists()


def test_open_session_log_enabled_writes_file(tmp_path: Path) -> None:
    log = open_session_log(tmp_path, _config(), enabled=True)
    assert log.path is not None
    with log:
        pass
    assert log.path.exists()
    assert log.path.parent == tmp_path / ".testgap" / "logs"


def test_session_start_records_config_fields(tmp_path: Path) -> None:
    cfg = _config()
    log = SessionLog.start(tmp_path, cfg)
    path = log.path
    with log:
        pass
    events = _read_events(path)
    start = next(e for e in events if e["event"] == "session_start")
    assert start["config"]["model"] == "fake/model"
    assert start["config"]["max_cost"] == 1.5
    assert start["config"]["max_tests_per_function"] == 4
    assert start["config"]["source_paths"] == ["src/"]
    assert start["config"]["test_paths"] == ["tests/"]
    assert "testgap_version" in start


def test_serialize_falls_back_via_default_str(tmp_path: Path) -> None:
    """Non-JSON-native values (Path) must not crash record()."""
    log = SessionLog.start(tmp_path, _config())
    path = log.path
    with log:
        log.record("user_action", {"applied_path": Path("/tmp/x.py")})
    events = _read_events(path)
    ua = next(e for e in events if e["event"] == "user_action")
    assert ua["applied_path"] == "/tmp/x.py"


def test_close_is_idempotent(tmp_path: Path) -> None:
    log = SessionLog.start(tmp_path, _config())
    path = log.path
    log.close()
    log.close()  # second call must not raise
    events = _read_events(path)
    # Only one session_end recorded.
    assert sum(1 for e in events if e["event"] == "session_end") == 1
