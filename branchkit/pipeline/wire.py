"""Pipeline wire protocol: framed events over a byte stream.

Each wire event is a JSON header line terminated by ``\\n``, optionally
followed by exactly ``payload_length`` bytes of binary payload. This is the
Python port of ``pipeline.go`` / ``pipeline.ts``; the framing conformance
suite compares the three byte-for-byte, so changes here are wire changes.
"""

import json
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Optional

# The largest binary payload the reader will accept (16 MB).
MAX_PAYLOAD = 16 * 1024 * 1024

# Header keys are written in THIS order. Go marshals struct fields in
# declaration order and TS builds the object literal in the same order, so
# the byte-compat suite sees one canonical header from all three ports.
_HEADER_ORDER = ("type", "data", "payload_length")


class WireError(Exception):
    """A malformed frame. Distinct from OSError so a caller can tell a
    protocol violation from a dead pipe."""


@dataclass
class Event:
    """One wire message: a type tag, an optional decoded JSON body, and an
    optional binary payload."""

    type: str
    data: Optional[Any] = None
    payload: bytes = field(default=b"")


def _is_empty(data: Any) -> bool:
    """Empty data is omitted from the header, matching the Rust, Go and TS
    writers (the wire contract's omitted-when-empty rule). Without this, an
    event decoded from a lenient peer's ``data:{}`` would re-serialize
    non-canonically and the byte-compat suite would fail on a round trip."""
    return data is None or data == {} 


class Reader:
    """Reads framed events from a binary stream."""

    def __init__(self, stream: BinaryIO):
        self._s = stream

    def read_event(self) -> Optional[Event]:
        """Next event, or None at a clean end of stream.

        A clean EOF is None. A partial header with no trailing newline is a
        WireError, not an EOF — truncation must not read as an orderly close.
        """
        line = self._s.readline()
        if not line:
            return None
        if not line.endswith(b"\n"):
            raise WireError("wire: incomplete header (no trailing newline)")

        try:
            header = json.loads(line)
        except ValueError as exc:
            raise WireError(f"wire: bad header {line!r}: {exc}") from exc
        if not isinstance(header, dict):
            raise WireError(f"wire: header is not an object: {line!r}")

        length = header.get("payload_length") or 0
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise WireError(f"wire: bad payload_length {length!r}")
        if length > MAX_PAYLOAD:
            raise WireError(f"wire: payload_length {length} exceeds 16 MB cap")

        payload = b""
        if length:
            # readline() may have buffered ahead; read exactly, and refuse a
            # short read rather than handing back a truncated payload.
            payload = self._s.read(length)
            if payload is None or len(payload) != length:
                got = 0 if payload is None else len(payload)
                raise WireError(
                    f"wire: short payload: wanted {length} bytes, got {got}")

        return Event(
            type=header.get("type", ""),
            data=header.get("data"),
            payload=payload,
        )

    def __iter__(self):
        while True:
            ev = self.read_event()
            if ev is None:
                return
            yield ev


class Writer:
    """Writes framed events to a binary stream, flushing every event."""

    def __init__(self, stream: BinaryIO):
        self._s = stream

    def write_event(self, ev: Event) -> None:
        header: dict[str, Any] = {"type": ev.type}
        if not _is_empty(ev.data):
            header["data"] = ev.data
        if ev.payload:
            header["payload_length"] = len(ev.payload)

        # separators: Go's encoding/json and serde_json both emit compact
        # JSON. ensure_ascii=False: Go emits UTF-8 directly, so escaping
        # non-ASCII here would differ byte-for-byte from the other ports.
        line = json.dumps(
            {k: header[k] for k in _HEADER_ORDER if k in header},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        self._s.write(line + b"\n")
        if ev.payload:
            self._s.write(ev.payload)
        self.flush()

    def write_typed(self, event_type: str, data: Any,
                    payload: bytes = b"") -> None:
        """Emit a typed event — the generated TypedDicts are plain dicts at
        runtime, so this is just a named spelling of write_event."""
        self.write_event(Event(type=event_type, data=data, payload=payload))

    def flush(self) -> None:
        try:
            self._s.flush()
        except (BrokenPipeError, ValueError):
            # The platform closed the pipe: the stage is being torn down, and
            # a flush failure here is the teardown, not an error to report.
            pass
