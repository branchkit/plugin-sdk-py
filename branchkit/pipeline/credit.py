"""Receiver-side flow-credit granting.

The pipeline contract mandates window-based backpressure: the receiver
advertises credit, the sender decrements per audio_chunk and blocks at zero.
HOW MUCH to advertise — the initial window, the grant cadence, the grant
size — is receiver-chosen buffering policy and deliberately NOT part of the
contract (a gate buffering freely wants a wide window; an STT engine wants
shallow queues). This owns only the mechanism: the chunks-since-last-grant
counter and the flow_credit emission.

There is deliberately no sender-side counterpart. The platform sits between
every pair of stages and holds that window itself; a stage that PRODUCES
audio implements no credit at all and simply blocks on the pipe.

Port of ``credit.go`` / the credit half of ``stage.ts``.
"""

from .events_gen import EVENT_FLOW_CREDIT
from .wire import Writer


class CreditGranter:
    """Counts processed chunks and grants credit on a cadence."""

    def __init__(self, every: int, grant: int):
        """Grant ``grant`` frames after every ``every`` chunks."""
        self._every = every
        self._grant = grant
        self._since = 0

    def grant_now(self, w: Writer, session_id: str, frames: int) -> None:
        """Emit ``frames`` of credit unconditionally and reset the cadence
        counter. Use it for the initial window, or a re-grant at an utterance
        boundary."""
        self._since = 0
        _emit_credit(w, session_id, frames)

    def on_chunk(self, w: Writer, session_id: str) -> None:
        """Count one processed chunk and grant after every ``every`` of them.

        The counter deliberately survives session boundaries: a stage wanting
        a per-session reset gets it from grant_now's initial grant, and one
        that does not simply never resets.
        """
        if self._every == 0:
            return
        self._since += 1
        if self._since >= self._every:
            self._since = 0
            _emit_credit(w, session_id, self._grant)


def _emit_credit(w: Writer, session_id: str, frames: int) -> None:
    w.write_typed(EVENT_FLOW_CREDIT, {"session_id": session_id,
                                      "frames": frames})
