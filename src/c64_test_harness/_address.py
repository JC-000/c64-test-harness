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

No imports: every layer (transport, client, execute, memory) can use it
without a cycle.
"""
from __future__ import annotations

__all__ = ["is_bool_like", "refuse_bool_address"]


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
