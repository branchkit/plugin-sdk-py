# Ambient inbound-correlation tracking.
#
# The actuator carries correlation in a thread-local read via
# `correlation::current()` and stamps every event it emits from the live
# scope. The Python SDK mirrors that with a `contextvars.ContextVar`: each
# request/notification is dispatched in its own asyncio task (which owns a
# copy of the context), so a handler and every outbound call it makes see
# the inbound id without any signature change — and `asyncio.to_thread`
# propagates the context, so offloaded sync handlers see it too.

from contextvars import ContextVar

_current: ContextVar[str] = ContextVar("branchkit_correlation", default="")


def set_correlation(correlation_id: str | None):
    """Make `correlation_id` ambient for the current context. Returns a
    token for `reset_correlation`. A falsy id sets the empty ambient."""
    return _current.set(correlation_id or "")


def reset_correlation(token) -> None:
    _current.reset(token)


def get_current_correlation() -> str:
    """The inbound correlation id for the current context, or "" if none."""
    return _current.get()
