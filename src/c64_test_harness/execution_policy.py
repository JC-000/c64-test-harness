"""Execution guard: refuse to call into memory the caller declared dead.

Sibling of :mod:`.memory_policy`, one operation over.  That one asks
"may the host *write* here?"; this asks "may the 6510 *execute* here?".
They are deliberately separate declarations, because a span can be both
dead and writable at once -- reclaimed init code becomes BSS, so writing
it is the whole point and calling it is the bug.  Folding the two into
one declaration would force every consumer to describe the same span
twice with opposite senses.

Everything else mirrors :mod:`.memory_policy`: permissive by default, a
per-call ``override="reason"`` logged at WARNING, and a violation that
raises a harness-specific exception carrying the offending address.
:class:`.MemoryRegion` is reused rather than reimplemented.

Why the guard has to be here rather than in a static check
----------------------------------------------------------

A static analyser can follow ``jsr(transport, labels["sym"])`` back to a
symbol.  It cannot follow a hand-built trampoline::

    trampoline = bytes([0x20, addr & 0xFF, addr >> 8, 0x8D, 0x60, 0x03, 0x60])
    write_bytes(transport, 0x0340, trampoline)
    jsr(transport, 0x0340)

which is a real and legitimate pattern for capturing the accumulator.
Tracing it means constant-folding byte arithmetic across an intervening
write, and the next variation defeats it again.  At run time the address
is simply a number, and :func:`~.execute.jsr` is the one place that sees
every call however it was spelled.

Why the symptom is otherwise unreadable
---------------------------------------

Calling reclaimed code does not fault cleanly.  The entry point survives
the reclaim, so the CPU runs real instructions before wandering into
zero-filled RAM (``$00`` = ``BRK``) and wedging somewhere unpredictable.
The harness reports only ``TimeoutError: No stopped event within
180.0s`` -- indistinguishable from a hung emulator, host contention, or
a genuinely long routine.

And the obvious defensive guard does not help::

    if "reu_mul_init" in labels:      # True.  Always True.
        jsr(transport, labels["reu_mul_init"], timeout=180.0)

The reclaim removes the *code*, not the *symbol*; the label survives as
a genuine link-time address.  The line reads as "only call this if this
build has it" and is satisfied by every build.  It is not careless -- it
is careful and wrong, for a reason nothing surfaced until now.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .memory_policy import MemoryRegion

_log = logging.getLogger(__name__)


class ExecutionPolicyError(Exception):
    """A call was blocked because its target is in a declared dead span.

    Mirrors :class:`~.memory_policy.MemoryPolicyError`: a harness-specific
    exception (not a ``TimeoutError`` and not derived from one -- reporting
    "it did not finish" is precisely the defect this replaces), carrying
    the offending address and the region it fell in, with a message that
    names the bypass so it is actionable on first read.
    """

    def __init__(
        self,
        addr: int,
        region: MemoryRegion,
        symbol: str | None = None,
    ) -> None:
        self.addr = addr
        self.region = region
        self.symbol = symbol
        where = f" ({symbol})" if symbol else ""
        super().__init__(
            f"call to ${addr:04X}{where} is inside a span declared dead: "
            f"{region}. Pass override=\"<reason>\" to call it anyway for a "
            f"single call, or update the ExecutionPolicy."
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    """Spans the caller has declared must never be called.

    Permissive by default: a transport with no policy, or a policy with
    no regions, changes nothing.  Attach one alongside the memory
    policy::

        target.transport.execution_policy = ExecutionPolicy.from_labels(
            labels,
            dead=[("__LIB_X25519_INIT_CODE_LOAD__",
                   "__LIB_X25519_INIT_CODE_SIZE__")],
            reason="reclaimed as BSS after boot; see c64-wireguard#107",
        )

    Declare spans through :meth:`from_labels` wherever symbols exist.
    Addresses move: two builds a few merges apart put the same entry at
    ``$87BB`` and ``$87D1``, so a policy holding a literal would be wrong
    by the next relink and would then be a comment wearing a guard's
    costume.  :meth:`from_regions` exists for code with no label file and
    is the exception, not the shape to copy.
    """

    dead_regions: tuple[MemoryRegion, ...] = ()
    #: Kept so a violation can name the symbol at the offending address.
    labels: Mapping[str, int] | None = None

    @classmethod
    def permissive(cls) -> "ExecutionPolicy":
        """A policy that forbids nothing (the default)."""
        return cls()

    @classmethod
    def from_labels(
        cls,
        labels: Mapping[str, int],
        dead: Iterable[Sequence[str]],
        *,
        reason: str = "",
    ) -> "ExecutionPolicy":
        """Derive dead spans from ``(load_symbol, size_symbol)`` pairs.

        Start/size rather than start/end because that is the pair
        linkers emit, and deriving the end here keeps the off-byte-one
        out of every caller.  A third element overrides *reason* for that
        span.

        :raises ValueError: if a named symbol is absent, which is a
            declaration-time mistake and is reported where it can be
            fixed rather than on some later call.
        """
        regions: list[MemoryRegion] = []
        for entry in dead:
            entry = tuple(entry)
            if len(entry) not in (2, 3):
                raise ValueError(
                    f"dead span entry must be (load_symbol, size_symbol[, "
                    f"reason]), got {entry!r}"
                )
            load_sym, size_sym = entry[0], entry[1]
            note = entry[2] if len(entry) == 3 else reason
            for sym in (load_sym, size_sym):
                if sym not in labels:
                    raise ValueError(
                        f"symbol {sym!r} is not in the label file; a dead-span "
                        f"declaration cannot be derived from it"
                    )
            start = labels[load_sym]
            size = labels[size_sym]
            if size <= 0:
                raise ValueError(
                    f"dead span {load_sym!r} has size symbol {size_sym!r} = "
                    f"{size}; a span must cover at least one byte"
                )
            regions.append(
                MemoryRegion(start, start + size, f"{load_sym}: {note}".strip(": "))
            )
        return cls(tuple(regions), labels)

    @classmethod
    def from_regions(
        cls,
        regions: Iterable[MemoryRegion],
        *,
        reason: str = "",
        labels: Mapping[str, int] | None = None,
    ) -> "ExecutionPolicy":
        """Declare dead spans by address, for code with no label file.

        Prefer :meth:`from_labels`; a literal address stops being right
        at the next relink.
        """
        out = []
        for region in regions:
            note = region.note or reason
            out.append(MemoryRegion(region.start, region.end, note))
        return cls(tuple(out), labels)

    def _symbol_at(self, addr: int) -> str | None:
        name = getattr(self.labels, "name", None)
        if callable(name):
            return name(addr)
        return None

    def check_call(self, addr: int, *, override: str | None = None) -> None:
        """Raise if calling *addr* is forbidden.

        :param addr: The 6502 address about to be called.
        :param override: Reason string permitting this one call, logged
            at WARNING. A bare ``True`` is rejected: an override has to
            be justified in the diff, not merely switched on.
        :raises ExecutionPolicyError: if *addr* is inside a dead span.
        :raises ValueError: if *override* is neither ``None`` nor a
            non-empty string.
        """
        if override is not None and (
            not isinstance(override, str) or not override
        ):
            raise ValueError(
                "override must be a non-empty reason string, e.g. "
                'override="verifying the wedge is reproducible"'
            )
        if not self.dead_regions:
            return
        if override:
            _log.warning(
                "execution policy override at $%04X (reason: %s)",
                addr,
                override,
            )
            return
        for region in self.dead_regions:
            if region.contains_addr(addr):
                raise ExecutionPolicyError(
                    addr, region, self._symbol_at(addr)
                )


def check_execution_policy(
    holder: object, addr: int, *, override: str | None = None
) -> None:
    """Apply *holder*'s ``execution_policy``, if it has one.

    Read with ``getattr`` rather than a declared transport property so
    the guard works on either backend without each transport class
    growing one. The symmetric thing would be an explicit property
    beside ``memory_policy``; that is worth doing and is deliberately
    not done here, because it would mean editing the VICE transport
    while another lane owns those files.
    """
    policy = getattr(holder, "execution_policy", None)
    if policy is None:
        if override is not None and (
            not isinstance(override, str) or not override
        ):
            raise ValueError(
                "override must be a non-empty reason string, e.g. "
                'override="verifying the wedge is reproducible"'
            )
        return
    policy.check_call(addr, override=override)


__all__ = [
    "ExecutionPolicy",
    "ExecutionPolicyError",
    "check_execution_policy",
]
