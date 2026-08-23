# The actor label — who this plugin is acting *on behalf of*.
#
# A host-shaped plugin runs things the platform does not model: a scripting
# host runs script files, the browser plugin fronts an extension, an
# ambassador fronts an external app. Every platform call it makes is its own,
# over its own session, so audit rows, collection records, and events can name
# only the plugin. `acting_for` stamps the finer-grained label on the outbound
# envelope, and the platform carries it into what it writes.
#
# Observability only, by construction. The platform never consults the label
# for any decision — it cannot, because the host supplies it from inside its
# own process, so a lying host would only lie about a label it already had the
# grant to act under. Setting it widens nothing and narrows nothing; it makes
# the trail readable. Per-hosted-thing ENFORCEMENT would need real delegated
# identities, which is a different (unbuilt) feature.
#
# Scoped with a `contextvars.ContextVar`, exactly like the ambient inbound
# correlation: hosted things interleave across tasks, and a module global
# would let one hosted thing's label ride another's calls.

from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[str] = ContextVar("branchkit_actor", default="")


def set_actor(actor: str | None):
    """Make `actor` ambient for the current context. Returns a token for
    `reset_actor`. A falsy actor sets the empty ambient — "no label"."""
    return _current.set(actor or "")


def reset_actor(token) -> None:
    _current.reset(token)


def get_current_actor() -> str:
    """The actor label outbound calls currently carry, or "" if none."""
    return _current.get()


@contextmanager
def acting_for(actor: str | None):
    """Stamp `actor` on every RPC made inside the block.

        with acting_for("headphones.py"):
            await plugin.dispatch(action)

    Nested blocks restore the outer label on exit, so one hosted thing
    invoking another leaves the trail intact.
    """
    token = set_actor(actor)
    try:
        yield
    finally:
        reset_actor(token)
