# Who sent the event notification a listener is handling.
#
# The platform delivers an event to every plugin whose manifest subscription
# matches it, and the event type alone does not say who emitted it: a
# subscription to `*.focused` hears every plugin's `focused`, and a host that
# relays its hosted things' events wants to know the event really came from
# itself. The actuator puts the sender on the notification's envelope, and the
# SDK makes it readable from inside the listener — the same ambient shape as
# the correlation id, so no listener signature changes.
#
# Scoped with a `contextvars.ContextVar`, like the correlation id: the
# notification pump sets it around one delivery, `asyncio.to_thread` carries
# it into an offloaded plain-`def` listener, and a module global would let one
# delivery read another's sender.

from contextvars import ContextVar
from typing import NamedTuple


class EventOrigin(NamedTuple):
    """The sender of an event notification, as the platform delivered it.

    `source` is the emitter the platform authenticated: a plugin id,
    `"_platform"` for platform events, or a stage's name for `ext.*` events.
    The platform force-sets it from the emitting connection, so a listener can
    trust it. `""` outside an event listener, or from an actuator that
    predates it.

    `on_behalf_of` is the emitter's actor label — which hosted thing it said
    it was acting for (see `acting_for`), or `""` if none. A CLAIM by
    `source`, never checked by the platform: trust it exactly as far as you
    trust `source`.
    """

    source: str = ""
    on_behalf_of: str = ""


_NONE = EventOrigin()

_current: ContextVar[EventOrigin] = ContextVar("branchkit_event_origin", default=_NONE)


def set_event_origin(origin: EventOrigin):
    """Make `origin` ambient for the current context. Returns a token for
    `reset_event_origin`."""
    return _current.set(origin)


def reset_event_origin(token) -> None:
    _current.reset(token)


def get_current_event_origin() -> EventOrigin:
    """The sender of the event notification being handled in the current
    context — inside `on` and `on_pattern` listeners — or an empty
    `EventOrigin` when none is in flight (request handlers, and work outside
    a delivery)."""
    return _current.get()
