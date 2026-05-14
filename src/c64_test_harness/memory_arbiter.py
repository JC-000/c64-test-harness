"""DMA-stub address allocator — a helper on top of :class:`MemoryPolicy`.

When a test author needs a scratch address for a trampoline, sentinel,
or data blob, hand-picking ``0xC000`` (or whatever) risks colliding with
the consumer's program.  :class:`MemoryArbiter` walks a single source of
truth (the active :class:`MemoryPolicy`) and returns the first free
address that won't trip the policy's ``check_write`` when written to.

The arbiter is *not* the safety mechanism — the policy is.  Even if a
caller skips the arbiter and hands a hardcoded address to
``write_memory``, the transport-level policy still catches collisions.
The arbiter is the ergonomic complement: a way to ask "where is it
safe to put this?" without re-encoding the consumer's layout.

Typical usage::

    from c64_test_harness import (
        MemoryArbiter, MemoryPolicy, MemoryRegion, UnknownPolicy,
    )

    policy = MemoryPolicy(
        reserved_regions=(
            MemoryRegion.parse("$0801-$1FFF", note="LOADER"),
            MemoryRegion.parse("$4200-$50FF", note="X25519 RODATA + BSS"),
        ),
        unknown=UnknownPolicy.WARN,
    )
    target.transport.memory_policy = policy

    arbiter = MemoryArbiter(policy=policy)
    trampoline_addr = arbiter.alloc(117, name="trampoline")
    sentinel_addr = arbiter.alloc(16, name="sentinel")
    # Both addresses now pass policy.check_write — guaranteed.

Allocations are tracked internally so a second :meth:`alloc` call won't
hand out an overlapping address.  Calling :meth:`alloc` does NOT mutate
the policy (which is frozen); if you need the arbiter's claims to
participate in policy checks at the transport, use
:meth:`policy_with_allocations` to derive an updated policy and assign
it to the transport.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .memory_policy import MemoryPolicy, MemoryRegion

if TYPE_CHECKING:
    from .labels import Labels


_ADDR_SPACE = 0x10000

# Default scan window — skip the zero page, stack, and KERNAL vector
# at $0000-$01FF.  Test authors who need those ranges should pass an
# explicit ``window=`` to :class:`MemoryArbiter`.
_DEFAULT_WINDOW = (0x0200, 0xFFFF)


class MemoryArbiterError(Exception):
    """Raised when :meth:`MemoryArbiter.alloc` cannot satisfy a request.

    Attributes
    ----------
    size:
        Number of bytes requested.
    alignment:
        Alignment requested.
    name:
        Caller-supplied name (echoed in the diagnostic trace).
    trace:
        Human-readable line-per-candidate explanation of every free
        interval considered and why each was rejected.  Print this for
        debugging.
    """

    def __init__(
        self,
        size: int,
        alignment: int,
        name: str,
        trace: list[str],
    ) -> None:
        self.size = size
        self.alignment = alignment
        self.name = name
        self.trace = trace
        joined = "\n  ".join(trace) if trace else "(no free intervals)"
        super().__init__(
            f"MemoryArbiter.alloc(size={size}, alignment={alignment}, "
            f"name={name!r}) found no free range.\nTrace:\n  {joined}"
        )


@dataclass
class MemoryArbiter:
    """First-fit allocator over the free addresses of a :class:`MemoryPolicy`.

    Parameters
    ----------
    policy:
        The active memory policy.  ``reserved_regions`` and any prior
        allocations are skipped; if ``safe_regions`` is non-empty,
        allocation is restricted to its union (mirroring the policy's
        allow-list semantics).  Defaults to a permissive policy.
    window:
        ``(lo, hi)`` inclusive scan window.  Defaults to
        ``($0200, $FFFF)`` — i.e. excludes the zero page and stack.

    Notes
    -----
    The arbiter holds no transport reference and does not write to the
    C64.  It produces addresses; the caller does the DMA via
    ``transport.write_memory``, which the transport-level policy checks
    independently.
    """

    policy: MemoryPolicy = field(default_factory=MemoryPolicy.permissive)
    window: tuple[int, int] = _DEFAULT_WINDOW
    _allocated: list[MemoryRegion] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    @classmethod
    def from_labels(
        cls,
        labels: Labels,
        *,
        label_address_is_data: Callable[[str], bool] = lambda _name: True,
        extra_reserved: tuple[MemoryRegion, ...] = (),
        window: tuple[int, int] = _DEFAULT_WINDOW,
    ) -> MemoryArbiter:
        """Build an arbiter whose policy reserves each data label.

        Each label in ``labels`` whose name passes
        ``label_address_is_data`` becomes a single-byte reserved region
        named ``"label:<name>"``.  Tune the predicate to filter out code
        labels — e.g. ``lambda n: not n.startswith("code_")``.

        ``extra_reserved`` regions are added on top (useful for segment
        boundaries that aren't expressible as single-byte labels).
        """
        reserved: list[MemoryRegion] = list(extra_reserved)
        for name, addr in labels.items():
            if not (0 <= addr < _ADDR_SPACE):
                continue
            if not label_address_is_data(name):
                continue
            reserved.append(MemoryRegion(addr, addr + 1, f"label:{name}"))
        policy = MemoryPolicy(reserved_regions=tuple(reserved))
        return cls(policy=policy, window=window)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def alloc(
        self,
        size: int,
        *,
        alignment: int = 1,
        name: str = "<anon>",
    ) -> int:
        """Reserve *size* bytes and return the base address.

        Returns the lowest address in :attr:`window` such that

        * the address is aligned to *alignment* (default 1),
        * ``[base, base + size)`` lies entirely inside the policy's
          ``safe_regions`` (when any are declared) or inside the window
          (when none are declared),
        * the span overlaps no reserved region and no prior allocation.

        Raises :class:`MemoryArbiterError` if no such address exists.
        The exception's ``trace`` attribute carries a line-per-candidate
        explanation.

        Parameters
        ----------
        size:
            Number of bytes to reserve.  Must be ``>= 1``.
        alignment:
            Power-of-two alignment.  Default 1 (no alignment).
        name:
            Human-readable name for the diagnostic trace and the
            allocations list.
        """
        if size < 1:
            raise ValueError(f"size must be >= 1, got {size}")
        if alignment < 1 or (alignment & (alignment - 1)) != 0:
            raise ValueError(
                f"alignment must be a power of two >= 1, got {alignment}"
            )

        free_intervals = self._compute_free_intervals()
        trace: list[str] = []
        for lo, hi in free_intervals:
            base = (lo + alignment - 1) & ~(alignment - 1)
            if base + size <= hi:
                region = MemoryRegion(base, base + size, name)
                self._allocated.append(region)
                return base
            trace.append(
                f"free ${lo:04X}-${hi - 1:04X} ({hi - lo} B) too small for "
                f"{size} B @ alignment {alignment}"
            )
        raise MemoryArbiterError(size, alignment, name, trace)

    def reserve(self, region: MemoryRegion) -> None:
        """Manually mark a range as taken.

        Useful for ad-hoc one-off reserves after the arbiter is
        constructed, e.g. when a test discovers a clash dynamically.
        The reservation is local to the arbiter — it does not modify
        :attr:`policy`.  To make the reservation enforced at the
        transport, call :meth:`policy_with_allocations` and assign the
        result to ``transport.memory_policy``.
        """
        self._allocated.append(region)

    @property
    def allocations(self) -> list[tuple[int, int, str]]:
        """List of ``(start, end_inclusive, name)`` for every successful alloc.

        Returned in allocation order, with inclusive end addresses so
        the values can be printed directly with the ``$XXXX-$YYYY``
        convention.
        """
        return [(r.start, r.end - 1, r.note) for r in self._allocated]

    def policy_with_allocations(self) -> MemoryPolicy:
        """Derive a new :class:`MemoryPolicy` that adds the arbiter's
        allocations as reserved regions.

        Assign the returned policy to ``transport.memory_policy`` to
        make the arbiter's claims visible to subsequent
        ``write_memory`` calls from anywhere in the test suite — e.g.
        to catch a second piece of code that didn't go through the
        arbiter trying to write to an arbiter-owned address.
        """
        merged = self.policy
        for r in self._allocated:
            merged = merged.with_reserved(r)
        return merged

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_free_intervals(self) -> list[tuple[int, int]]:
        """Return the list of half-open ``[lo, hi)`` intervals where an
        allocation is permitted, in ascending order.

        The free set is ``allowed_intervals − blocked_intervals``, where:

        * ``allowed_intervals`` is the window intersected with
          ``policy.safe_regions`` (or just the window if no safe
          regions are declared).
        * ``blocked_intervals`` is the union of ``policy.reserved_regions``
          and the arbiter's own ``_allocated`` regions.
        """
        win_lo, win_hi_incl = self.window
        if not (0 <= win_lo < _ADDR_SPACE and 0 <= win_hi_incl < _ADDR_SPACE):
            raise ValueError(
                f"window ${win_lo:04X}-${win_hi_incl:04X} is invalid"
            )
        if win_lo > win_hi_incl:
            raise ValueError(
                f"window ${win_lo:04X}-${win_hi_incl:04X} has lo > hi"
            )
        win_hi = win_hi_incl + 1

        if self.policy.safe_regions:
            allowed: list[tuple[int, int]] = []
            for r in self.policy.safe_regions:
                s = max(r.start, win_lo)
                e = min(r.end, win_hi)
                if e > s:
                    allowed.append((s, e))
            allowed = _merge_intervals(allowed)
        else:
            allowed = [(win_lo, win_hi)]

        blocked: list[tuple[int, int]] = []
        for r in self.policy.reserved_regions:
            s = max(r.start, win_lo)
            e = min(r.end, win_hi)
            if e > s:
                blocked.append((s, e))
        for r in self._allocated:
            s = max(r.start, win_lo)
            e = min(r.end, win_hi)
            if e > s:
                blocked.append((s, e))
        blocked = _merge_intervals(blocked)

        free: list[tuple[int, int]] = []
        for a_lo, a_hi in allowed:
            cursor = a_lo
            for b_lo, b_hi in blocked:
                if b_hi <= cursor:
                    continue
                if b_lo >= a_hi:
                    break
                if b_lo > cursor:
                    free.append((cursor, min(b_lo, a_hi)))
                cursor = max(cursor, b_hi)
                if cursor >= a_hi:
                    break
            if cursor < a_hi:
                free.append((cursor, a_hi))
        return free


def _merge_intervals(
    intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Sort and merge overlapping or abutting half-open intervals."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged: list[tuple[int, int]] = [intervals[0]]
    for s, e in intervals[1:]:
        cur_s, cur_e = merged[-1]
        if s <= cur_e:
            merged[-1] = (cur_s, max(cur_e, e))
        else:
            merged.append((s, e))
    return merged


__all__ = [
    "MemoryArbiter",
    "MemoryArbiterError",
]
