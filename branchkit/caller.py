"""The one call every generated wrapper makes, and the wrappers over any
caller.

A ``Caller`` is anything with the Plugin core's ``call``. ``Client`` gives
the generated wrappers (``MethodsMixin``) over one: a test drives them over
its own fake with no transport, and ``Plugin.with_timeout`` returns one
whose calls wait longer (or shorter) than the 10 s default.
"""

from __future__ import annotations

from typing import Any, Protocol

from .methods_gen import MethodsMixin


class Caller(Protocol):
    """Makes one platform RPC call. A Plugin is one."""

    async def call(self, method: str, params: Any = None, timeout: float | None = None) -> Any: ...


class Client(MethodsMixin):
    """The typed wrappers over ``caller``, each call waiting ``timeout``
    seconds unless the call names its own."""

    def __init__(self, caller: Caller, timeout: float | None = None) -> None:
        self._caller = caller
        self._timeout = timeout

    async def call(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        return await self._caller.call(method, params, timeout if timeout is not None else self._timeout)
