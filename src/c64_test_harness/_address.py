"""One refusal for a flag passed where a C64 address belongs (#340, #352, #357).

``bool`` subclasses ``int``, so ``True``/``False`` pass every
``isinstance(x, int)`` and ``0 <= x <= 0xFFFF`` check and go out as
``$0001``/``$0000`` -- ``$0001`` being the 6510 processor port.  Arithmetic
launders the flag into a plain int (``True + 0 == 1``), so a helper that
offsets or masks the address before handing it to a guarded transport
defeats the transport's guard.  Every address-taking entry point therefore
calls :func:`refuse_bool_address` **first**, before any range check, early
return, lock, connection or wire use.

``numpy.bool_`` is refused too.  It is not an ``int`` subclass, but it
supports ``&``/``+`` into ``numpy.int64`` (``np.True_ & 0xFFFF == 1``), which
``struct.pack`` accepts -- so it reaches ``$0001`` through the same
laundering.  It is detected by type name without importing numpy.

``int`` subclasses other than ``bool`` (``IntEnum`` members, for example)
are real addresses and stay accepted, as in #350/#354.

Standard-library imports only: every layer (transport, client, execute,
memory, the code builders) can use it without a cycle.

Code builders (#373) take several address parameters each and mask them
straight into operands (``result_addr & 0xFF``), so no downstream check can
see the flag.  :func:`refuses_bool_address_args` refuses a flag in any
parameter whose name ends in ``addr``, ``address`` or ``buf`` before the
function body runs -- before any byte is emitted or any transport is used.
"""
from __future__ import annotations

import functools
import inspect
import re
from typing import Any, Callable, TypeVar

__all__ = [
    "ADDRESS_PARAM_RE",
    "is_bool_like",
    "refuse_bool_address",
    "refuses_bool_address_args",
]

_F = TypeVar("_F", bound=Callable[..., Any])

#: Parameter names treated as C64 addresses by
#: :func:`refuses_bool_address_args`: ``addr``/``address``/``buf`` as the
#: whole name or as its last ``_``-separated word (``load_addr``,
#: ``rx_buf``, ``socket_id_addr``).  ``frame_len``, ``my_ip``, ``mac`` and
#: ``peek_check_snippet`` do not match.
ADDRESS_PARAM_RE = re.compile(r"(?:^|_)(?:addr|address|buf)$")


def is_bool_like(value: object) -> bool:
    """True for ``bool`` and for ``numpy.bool_`` (either numpy spelling)."""
    if isinstance(value, bool):
        return True
    t = type(value)
    return t.__module__ == "numpy" and t.__name__ in ("bool", "bool_")


def refuse_bool_address(value: object, what: str = "address") -> None:
    """Raise ``ValueError("<what> must be an int, not bool: <value>")`` for a flag.

    The message shape is the one #350/#354 shipped, so callers matching on
    ``"not bool"`` keep working.
    """
    if is_bool_like(value):
        raise ValueError(f"{what} must be an int, not bool: {value!r}")


def refuses_bool_address_args(fn: _F) -> _F:
    """Refuse a flag in every address-named parameter before *fn* runs (#373).

    The message is ``"<function> <parameter> must be an int, not bool: <v>"``.
    ``None`` (an optional buffer left out) and real ints, ``IntEnum``
    included, pass through untouched, so the decorated function's output is
    byte-identical for every valid call.  A call that does not bind to the
    signature is passed through so the function raises its own
    ``TypeError``.
    """
    sig = inspect.signature(fn)
    names = tuple(p for p in sig.parameters if ADDRESS_PARAM_RE.search(p))

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError:
            return fn(*args, **kwargs)
        for name in names:
            if name in bound.arguments:
                refuse_bool_address(bound.arguments[name], f"{fn.__name__} {name}")
        return fn(*args, **kwargs)

    wrapper.__refuses_bool_addresses__ = names  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]
