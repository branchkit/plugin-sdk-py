"""Structured stage diagnostics on stderr.

Port of ``stagelog.go`` / the logging half of ``stage.ts``.
"""

import sys
import threading

# The sentinel every structured stage diagnostic begins with. The platform's
# stage stderr reader splits on it to parse
# `BKLOG1<TAB><level><TAB><session_id><TAB><message>` into correlated
# per-stage, per-session log records. Lines without it still reach the bus
# via a generic fallback — just uncorrelated and at info.
LOG_LINE_PREFIX = "BKLOG1\t"

_lock = threading.Lock()
_current_session = ""


def set_log_session(session_id: str) -> None:
    """Set the ambient session id (call it on audio_start).

    Subsequent log lines carry it, so a diagnostic correlates to the command
    that caused it without threading the id through every call site.
    """
    global _current_session
    with _lock:
        _current_session = session_id


def clear_log_session() -> None:
    """Clear the ambient session id (call it at session end). Lines after
    this carry no session, which is correct for between-command output."""
    set_log_session("")


def stage_log(level: str, message: str) -> None:
    """Emit one structured diagnostic on stderr at the given level.

    Prefer the level helpers. Embedded newlines are flattened so one logical
    diagnostic stays one line.
    """
    with _lock:
        session = _current_session
    if "\n" in message or "\r" in message:
        message = message.replace("\n", " ").replace("\r", " ")
    sys.stderr.write(f"{LOG_LINE_PREFIX}{level}\t{session}\t{message}\n")
    sys.stderr.flush()


# The platform's five-level model.
def log_trace(message: str) -> None: stage_log("trace", message)
def log_debug(message: str) -> None: stage_log("debug", message)
def log_info(message: str) -> None: stage_log("info", message)
def log_warn(message: str) -> None: stage_log("warn", message)
def log_error(message: str) -> None: stage_log("error", message)
