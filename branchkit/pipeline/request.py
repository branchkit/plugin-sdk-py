"""The request stage runtime — the fourth loop shape, one answer per request.

Beside the audio consumer, the source and the speech engine: text or data
in, one answer out, for work a plugin wants done in a confined process of
its own (a language model, a translator, a classifier). The capability
declares ``stage_type: "request"``.

A request stage consumes no audio, so it implements no flow credit.

Port of the Rust ``stage::serve_requests``.
"""

import asyncio
import inspect
import sys
from typing import Any, Awaitable, BinaryIO, Callable, Union

from .events_gen import (
    EVENT_CAPABILITY,
    EVENT_ERROR,
    EVENT_REPLY,
    EVENT_REQUEST,
    Capability,
    Reply,
)
from .wire import Reader, Writer

#: A request stage's work: answer one request's ``body``. Usually a
#: coroutine function; a plain function returning the answer works too.
#: Returning is the answer; raising fails this request only — the exception's
#: text becomes the reply's ``error`` and the stage goes on to the next
#: request. A failure that makes the stage unusable (a model that does not
#: load) belongs before ``serve_requests``, where ``run`` turns it into exit 1.
#:
#: Work that blocks a thread (inference) runs under ``asyncio.to_thread``.
RequestHandler = Callable[[Any], Union[Awaitable[Any], Any]]


def serve_requests(cap: Capability, handler: RequestHandler) -> None:
    """Serve a request stage on stdin/stdout: send the capability, then
    answer every ``request`` with exactly one ``reply`` carrying its
    ``request_id``, in arrival order, until stdin closes. A ``request`` that
    cannot be read (no ``request_id``) gets an ``error`` event, since there is
    no id to reply to; other event types are ignored, as wire leniency is
    contract."""
    serve_requests_on(sys.stdin.buffer, sys.stdout.buffer, cap, handler)


def serve_requests_on(r: BinaryIO, w: BinaryIO, cap: Capability,
                      handler: RequestHandler) -> None:
    """``serve_requests`` over explicit transports, for tests."""
    writer = Writer(w)
    writer.write_typed(EVENT_CAPABILITY, cap)
    reader = Reader(r)

    # One loop for the stage's life, so whatever a handler binds to it (a
    # client session, a lock) stays valid from one request to the next.
    loop = asyncio.new_event_loop()
    try:
        while True:
            ev = reader.read_event()
            if ev is None:
                return
            if ev.type != EVENT_REQUEST:
                continue
            data = ev.data if isinstance(ev.data, dict) else None
            request_id = data.get("request_id") if data is not None else None
            if not isinstance(request_id, str):
                writer.write_typed(EVENT_ERROR, {
                    "code": "bad_request",
                    "message": "unreadable request: no request_id string",
                    "fatal": False,
                })
                continue

            reply: Reply = {"request_id": request_id}
            try:
                answer = handler(data.get("body"))
                if inspect.isawaitable(answer):
                    answer = loop.run_until_complete(answer)
                reply["body"] = answer
            except Exception as exc:  # noqa: BLE001 — fails this request only
                reply["error"] = str(exc) or type(exc).__name__
            writer.write_typed(EVENT_REPLY, reply)
    finally:
        loop.close()
