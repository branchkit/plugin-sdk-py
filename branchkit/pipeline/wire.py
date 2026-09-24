"""Pipeline wire protocol: framed events over a byte stream.

Each wire event is a JSON header line terminated by ``\\n``, optionally
followed by exactly ``payload_length`` bytes of binary payload. This is the
Python port of ``pipeline.go`` / ``pipeline.ts``; the framing conformance
suite compares the three byte-for-byte, so changes here are wire changes.
"""

import json
import re
from dataclasses import dataclass, field
from json.decoder import scanstring
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
    # The exact bytes ``data`` arrived as, when this event came off a Reader.
    # Go keeps a frame's data as json.RawMessage and re-emits it verbatim;
    # Python parses it, and re-serialising the parsed value is NOT the same
    # bytes: 1e-7 -> 1e-07, 1e16 -> 1e+16, 1.10 -> 1.1, "\u00e9" -> raw é, and
    # a lone-surrogate escape "\ud800" used to raise in .encode("utf-8") and
    # kill the stage. The Writer re-emits these bytes as long as ``data``
    # still equals what they decode to, so an untouched echo is byte-identical
    # to Go's while a stage that edits ``data`` gets its edit serialised.
    raw_data: Optional[bytes] = field(default=None, repr=False, compare=False)


def _is_empty(data: Any) -> bool:
    """Empty data is omitted from the header, matching the Rust, Go and TS
    writers (the wire contract's omitted-when-empty rule). Without this, an
    event decoded from a lenient peer's ``data:{}`` would re-serialize
    non-canonically and the byte-compat suite would fail on a round trip."""
    return data is None or data == {} 


_WS = re.compile(r"[ \t\n\r]*")
_DECODER = json.JSONDecoder()


def _decode_text(line: bytes) -> str:
    # surrogateescape: Go's reader accepts invalid UTF-8 (json.RawMessage is
    # copied, not validated), so a stray byte must not be a WireError here.
    # The escaped bytes come back out unchanged when the raw slice is
    # re-encoded with the same handler.
    return line.decode("utf-8", "surrogateescape")


def _parse_header(text: str) -> tuple[dict, dict]:
    """Parse the header object with the stdlib scanners, recording each
    top-level value's [start, end) span in ``text`` alongside its value.

    Same grammar and strictness as json.loads (it is json's own scanner);
    the only addition is the spans, which json.loads cannot report. A
    duplicate key keeps the LAST value, as json.loads and Go both do.
    """
    values: dict = {}
    spans: dict = {}
    i = _WS.match(text, 0).end()
    if text[i:i + 1] != "{":
        raise ValueError("expected '{'")
    i = _WS.match(text, i + 1).end()
    if text[i:i + 1] == "}":
        i += 1
    else:
        while True:
            if text[i:i + 1] != '"':
                raise ValueError(f"expected a key at {i}")
            key, i = scanstring(text, i + 1, True)
            i = _WS.match(text, i).end()
            if text[i:i + 1] != ":":
                raise ValueError(f"expected ':' at {i}")
            start = _WS.match(text, i + 1).end()
            try:
                value, end = _DECODER.raw_decode(text, start)
            except StopIteration as exc:  # pragma: no cover - defensive
                raise ValueError(f"expected a value at {start}") from exc
            values[key] = value
            spans[key] = (start, end)
            i = _WS.match(text, end).end()
            ch = text[i:i + 1]
            if ch == ",":
                i = _WS.match(text, i + 1).end()
                continue
            if ch == "}":
                i += 1
                break
            raise ValueError(f"expected ',' or '}}' at {i}")
    if _WS.match(text, i).end() != len(text):
        raise ValueError(f"trailing data at {i}")
    return values, spans


def _compact(raw: bytes) -> bytes:
    """Drop insignificant whitespace outside strings, leaving every other
    byte alone — what Go's encoder does to a json.RawMessage (with
    SetEscapeHTML(false), which the Go writer sets)."""
    if not any(b in raw for b in (b" ", b"\t", b"\n", b"\r")):
        return raw
    out = bytearray()
    in_str = esc = False
    for c in raw:
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == 0x5C:  # backslash
                esc = True
            elif c == 0x22:  # quote
                in_str = False
        elif c in (0x20, 0x09, 0x0A, 0x0D):
            continue
        else:
            out.append(c)
            if c == 0x22:
                in_str = True
    return bytes(out)


def _same(a: Any, b: Any) -> bool:
    """Structural equality that also requires identical types, so True is
    not 1 and 1 is not 1.0 — a stage that swapped one for the other has
    changed the wire bytes, and the raw slice must not mask that."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


# A lone surrogate cannot be encoded as UTF-8. Go's decoder turns an invalid
# "\ud800" escape (and any invalid UTF-8 byte) into U+FFFD, so a Go stage
# that decoded and re-encoded such a value emits U+FFFD; do the same rather
# than raising UnicodeEncodeError and taking the stage down.
_SURROGATE = re.compile("[\ud800-\udfff]")


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

        text = _decode_text(line)
        try:
            header, spans = _parse_header(text)
        except ValueError as exc:
            raise WireError(f"wire: bad header {line!r}: {exc}") from exc

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

        raw_data = None
        if "data" in spans:
            start, end = spans["data"]
            raw_data = _compact(
                text[start:end].encode("utf-8", "surrogateescape"))

        return Event(
            type=header.get("type", ""),
            data=header.get("data"),
            payload=payload,
            raw_data=raw_data,
        )

    def __iter__(self):
        while True:
            ev = self.read_event()
            if ev is None:
                return
            yield ev


def _raw_if_unchanged(ev: Event) -> Optional[bytes]:
    """The event's original data bytes, if ``data`` still decodes-equal to
    them; None (serialise ``data``) otherwise or when there are none."""
    raw = ev.raw_data
    if raw is None:
        return None
    try:
        original = json.loads(raw.decode("utf-8", "surrogateescape"))
    except ValueError:
        return None
    return raw if _same(original, ev.data) else None


class Writer:
    """Writes framed events to a binary stream, flushing every event."""

    def __init__(self, stream: BinaryIO):
        self._s = stream

    def write_event(self, ev: Event) -> None:
        header: dict[str, Any] = {"type": ev.type}
        raw = None
        if not _is_empty(ev.data):
            header["data"] = ev.data
            raw = _raw_if_unchanged(ev)
        if ev.payload:
            header["payload_length"] = len(ev.payload)

        # separators: Go's encoding/json and serde_json both emit compact
        # JSON. ensure_ascii=False: Go emits UTF-8 directly, so escaping
        # non-ASCII here would differ byte-for-byte from the other ports.
        parts = []
        for k in _HEADER_ORDER:
            if k not in header:
                continue
            if k == "data" and raw is not None:
                value = raw
            else:
                value = _SURROGATE.sub("\ufffd", json.dumps(
                    header[k], separators=(",", ":"), ensure_ascii=False,
                )).encode("utf-8")
            parts.append(json.dumps(k).encode("utf-8") + b":" + value)
        line = b"{" + b",".join(parts) + b"}"

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
