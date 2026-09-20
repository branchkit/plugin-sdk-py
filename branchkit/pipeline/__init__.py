"""The BranchKit pipeline port — what a Python stage speaks.

A stage is a subprocess, not a plugin: it reads framed events on stdin,
writes them on stdout, and logs structured diagnostics on stderr. This
package is the Python equivalent of ``plugin-sdk-go/pipeline`` and
``plugin-sdk-ts``'s ``pipeline.ts`` / ``stage.ts``.

Tiers are subpackages so the default import stays domain-free — someone
writing a foot pedal should never be handed a command-grammar DAG:

    from branchkit.pipeline import Reader, Writer, Event, Capability
    from branchkit.pipeline.audio import AudioChunk       # opt in
    from branchkit.pipeline.recognition import Transcript # opt in

The event vocabulary (``events_gen.py`` and the tier subpackages) is
GENERATED from ``contracts/pipeline.json``, itself generated from the
stage-sdk Rust types; ``just check-stage-sdk-gen`` fails if it drifts.
Framing is hand-written here, as it is in Go and TS, and the framing
conformance suite compares the ports byte-for-byte.
"""

from .credit import CreditGranter
from .events_gen import *  # noqa: F401,F403
from .stagelog import (
    LOG_LINE_PREFIX,
    clear_log_session,
    log_debug,
    log_error,
    log_info,
    log_trace,
    log_warn,
    set_log_session,
    stage_log,
)
from .stage import (
    NO_CREDIT,
    AudioConsumer,
    AudioCtx,
    Chunk,
    CreditPolicy,
    Flow,
    InitialGrant,
    SourceCtx,
    SourceOptions,
    run,
    serve_audio_consumer,
    serve_audio_consumer_on,
    serve_source,
    serve_source_on,
)
from .wire import MAX_PAYLOAD, Event, Reader, WireError, Writer

__all__ = [  # noqa: F405 — the generated names come in via the star import
    "AudioConsumer",
    "AudioCtx",
    "Chunk",
    "CreditGranter",
    "CreditPolicy",
    "Event",
    "Flow",
    "InitialGrant",
    "NO_CREDIT",
    "SourceCtx",
    "SourceOptions",
    "LOG_LINE_PREFIX",
    "MAX_PAYLOAD",
    "Reader",
    "WireError",
    "Writer",
    "clear_log_session",
    "log_debug",
    "log_error",
    "log_info",
    "log_trace",
    "log_warn",
    "run",
    "serve_audio_consumer",
    "serve_audio_consumer_on",
    "serve_source",
    "serve_source_on",
    "set_log_session",
    "stage_log",
]
