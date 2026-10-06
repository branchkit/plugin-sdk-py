"""Point Apple's frameworks at the temporary directory the sandbox grants.

BranchKit names each confined process's own temporary directory in
``$TMPDIR``, a directory under the user's temporary directory, and refuses
the rest of it. Apple's frameworks ignore ``$TMPDIR`` on macOS: they ask
``confstr(_CS_DARWIN_USER_TEMP_DIR)``, which answers the shared directory,
and writing there is refused. Some stop the process when that happens:
Metal's graph compiler, which Core ML and PyTorch's MPS backend use, fails
an assertion.

libSystem's ``_set_user_dir_suffix`` moves that answer to a subdirectory of
the user's temporary directory. Taking the suffix from ``$TMPDIR`` lands
every framework in the granted directory. It is per-process state, so it
happens here, when the SDK is imported, before any framework is used.
Unconfined, ``$TMPDIR`` is the user's temporary directory itself (or unset),
and nothing changes."""

from __future__ import annotations

import os
import sys

# <unistd.h>'s _CS_DARWIN_USER_TEMP_DIR.
_CS_DARWIN_USER_TEMP_DIR = 65537


def suffix_under(base: str, own: str) -> str | None:
    """The suffix that moves ``base`` (the user's temporary directory) onto
    ``own`` (``$TMPDIR``), or None when ``own`` is not strictly under it.

    Compared as text: the system answers ``/var/folders/...`` and BranchKit
    names ``/private/var/folders/...`` (``/var`` is a link to ``/private/var``),
    and a confined process is refused the reads that resolving the link
    would take."""

    def norm(p: str) -> str:
        p = p.rstrip("/")
        if p.startswith("/private/var/"):
            return p[len("/private"):]
        return p

    base, own = norm(base), norm(own)
    if not own.startswith(base + "/"):
        return None
    rest = own[len(base) + 1:]
    return rest or None


def _user_temp_dir(libc) -> str | None:
    import ctypes

    buf = ctypes.create_string_buffer(1024)
    n = libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, len(buf))
    if n == 0 or n > len(buf):
        return None
    return buf.value.decode()


def adopt(own: str | None = None) -> None:
    """Apply ``$TMPDIR`` (or ``own``) to the frameworks. A no-op off macOS,
    unconfined, or if libSystem lacks the call."""
    if sys.platform != "darwin":
        return
    own = own if own is not None else os.environ.get("TMPDIR")
    if not own:
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        base = _user_temp_dir(libc)
        suffix = suffix_under(base, own) if base else None
        if suffix is None:
            return
        set_suffix = libc._set_user_dir_suffix
        set_suffix.argtypes = [ctypes.c_char_p]
        set_suffix.restype = ctypes.c_bool
        if not set_suffix(suffix.encode()):
            print("branchkit: could not move the temporary directory to $TMPDIR; "
                  "Apple frameworks may be refused theirs", file=sys.stderr)
    except (OSError, AttributeError):
        return
