"""Memory arbiter for DMA-stub address allocation.

Helps test authors avoid hardcoding scratch / trampoline / data-blob
addresses inside RAM ranges that a consumer project's build is already
using.  The C64 has no MMU and no segfault, so a collision between a
DMA-injected stub and (say) an X25519 lookup table fails silently:
crypto produces wrong-but-deterministic results, the test eventually
fails on a downstream symptom, and the bisection ends up nowhere near
the actual root cause.  See harness issue #93 for the motivating case.

Typical use::

    from c64_test_harness import Labels, MemoryArbiter

    labels = Labels.from_file("build/labels.txt")
    arbiter = MemoryArbiter(
        labels=labels,
        reserved=[
            (0x0801, 0x1FFF, "BASIC stub + LOADER"),
            (0x2000, 0x5FFF, "NET_CODE + NET_BSS"),
            (0x6000, 0x9FFF, "CRYPTO"),
            (0xA000, 0xBFFF, "SHADOW_BSS"),
            (0xC000, 0xCFFF, "TCP_BUF"),
        ],
    )

    routine_addr = arbiter.alloc(256, name="trampoline")
    sentinel_addr = arbiter.alloc(16, name="sentinel")

The arbiter walks the 16-bit address space from low to high, returning
the first chunk of the requested size that does not overlap any
reserved range, any prior allocation, or any address claimed by a
label in the labels file.  Failures raise ``MemoryArbiterError`` with
a human-readable trace of what was considered and why each candidate
was rejected — collisions should fail loudly at test-launch time, not
silently corrupt RAM at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .labels import Labels


# 6502 address space spans 16 bits.
_ADDR_MAX = 0xFFFF

# Default scan window — skip the zero page, stack, and KERNAL vectors
# at $0000-$00FF / $0100-$01FF.  Test authors who need those ranges
# should pass an explicit ``window=`` to ``alloc()``.
_DEFAULT_WINDOW = (0x0200, _ADDR_MAX)


class MemoryArbiterError(Exception):
    """Raised when :meth:`MemoryArbiter.alloc` cannot satisfy a request.

    Attributes:
        size: bytes requested.
        alignment: alignment requested.
        name: caller-supplied name (for the diagnostic trace).
        trace: human-readable line-per-candidate explanation of every
            address window the arbiter considered and why it was
            rejected.  Print this for debugging.
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
        joined = "\n  ".join(trace) if trace else "(no candidates considered)"
        super().__init__(
            f"MemoryArbiter.alloc(size={size}, alignment={alignment}, "
            f"name={name!r}) found no free range.\nTrace:\n  {joined}"
        )


@dataclass(frozen=True)
class _Range:
    """Inclusive [start, end] address range with a human-readable label."""

    start: int
    end: int
    label: str

    def overlaps(self, lo: int, hi: int) -> bool:
        return not (hi < self.start or lo > self.end)

    def __str__(self) -> str:
        return f"${self.start:04X}-${self.end:04X} ({self.label})"


@dataclass
class MemoryArbiter:
    """First-fit allocator over the 16-bit C64 address space.

    Parameters
    ----------
    labels:
        Optional ``Labels`` instance.  Each label whose address falls
        inside the scan window is treated as a 1-byte reserved range
        named after the label, unless the address is also covered by
        an explicit *reserved* entry (which takes precedence and
        carries a more useful label).
    reserved:
        List of ``(start, end, label)`` tuples marking known-bad
        ranges.  ``end`` is **inclusive** — ``(0x6000, 0x9FFF,
        "CRYPTO")`` reserves 16 KiB, not 16 KiB+1.
    label_address_is_data:
        Predicate ``Callable[[str], bool]`` invoked on each label name
        to decide whether the label denotes a data byte the arbiter
        should treat as reserved.  Default: every label.  Override
        to ignore CODE labels — e.g. ``lambda n: not
        n.startswith("code_")`` — if your labels file is noisy.

    Notes
    -----
    The arbiter holds no transport reference and does not write to the
    C64.  It produces addresses; the caller does the DMA inject.

    All ranges are inclusive on both ends, which matches the C64
    linker-cfg convention (``%S = $A000`` ``%E = $BFFF``).  Internally
    the allocator works with inclusive ranges throughout to avoid
    off-by-one bugs.
    """

    labels: "Labels | None" = None
    reserved: list[tuple[int, int, str]] = field(default_factory=list)
    label_address_is_data: Callable[[str], bool] = field(
        default=lambda _name: True
    )

    _allocated: list[_Range] = field(default_factory=list, init=False)
    _reserved_ranges: list[_Range] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # Normalise reserved entries into _Range objects.
        for entry in self.reserved:
            if len(entry) != 3:
                raise ValueError(
                    f"reserved entry must be (start, end, label); got {entry!r}"
                )
            start, end, label = entry
            if not (0 <= start <= _ADDR_MAX and 0 <= end <= _ADDR_MAX):
                raise ValueError(
                    f"reserved range ${start:04X}-${end:04X} ({label}) is "
                    f"outside the 16-bit address space"
                )
            if end < start:
                raise ValueError(
                    f"reserved range ${start:04X}-${end:04X} ({label}) has "
                    f"end < start"
                )
            self._reserved_ranges.append(_Range(start, end, label))

        # Merge in labels-derived single-byte reserves, but skip any
        # whose address is already covered by an explicit reserved
        # entry (the explicit label is more useful in error messages).
        if self.labels is not None:
            covered: set[int] = set()
            for r in self._reserved_ranges:
                covered.update(range(r.start, r.end + 1))
            for name, addr in self.labels.items():
                if not (0 <= addr <= _ADDR_MAX):
                    continue
                if addr in covered:
                    continue
                if not self.label_address_is_data(name):
                    continue
                self._reserved_ranges.append(
                    _Range(addr, addr, f"label:{name}")
                )

        # Sort so we can walk left-to-right.
        self._reserved_ranges.sort(key=lambda r: (r.start, r.end))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def alloc(
        self,
        size: int,
        *,
        alignment: int = 1,
        name: str = "<anon>",
        window: tuple[int, int] = _DEFAULT_WINDOW,
    ) -> int:
        """Reserve *size* bytes and return the base address.

        Walks the address space inside *window* (default
        ``$0200-$FFFF``) and returns the lowest address that:

        - is aligned to *alignment* (default 1)
        - does not overlap any reserved range
        - does not overlap any prior :meth:`alloc` result
        - keeps the entire ``[base, base+size-1]`` chunk inside *window*

        Raises :class:`MemoryArbiterError` if no such address exists.
        The exception's ``trace`` attribute carries a line-per-candidate
        explanation that callers can print for debugging.

        Parameters
        ----------
        size:
            Number of bytes to reserve.  Must be ``>= 1``.
        alignment:
            Power-of-two alignment.  Default 1 (no alignment).
        name:
            Human-readable name for diagnostic traces and the
            allocation list.
        window:
            ``(lo, hi)`` inclusive scan window.  Defaults to
            ``$0200-$FFFF``.
        """
        if size < 1:
            raise ValueError(f"size must be >= 1, got {size}")
        if alignment < 1 or (alignment & (alignment - 1)) != 0:
            raise ValueError(
                f"alignment must be a power of two >= 1, got {alignment}"
            )
        lo, hi = window
        if not (0 <= lo <= _ADDR_MAX and 0 <= hi <= _ADDR_MAX) or lo > hi:
            raise ValueError(
                f"window ${lo:04X}-${hi:04X} is invalid"
            )

        trace: list[str] = []
        # Combine reserved + already-allocated into a single sorted list
        # for the scan.
        blocks = sorted(
            self._reserved_ranges + self._allocated,
            key=lambda r: (r.start, r.end),
        )

        candidate = (lo + alignment - 1) & ~(alignment - 1)
        for blk in blocks:
            if blk.end < candidate:
                continue  # already past this block
            if blk.start > hi:
                break  # blocks beyond window — irrelevant
            chunk_end = candidate + size - 1
            if chunk_end < blk.start:
                # Free chunk found before this block.
                if chunk_end <= hi:
                    return self._commit(candidate, size, name)
                trace.append(
                    f"${candidate:04X}-${chunk_end:04X} would exceed "
                    f"window end ${hi:04X}"
                )
                raise MemoryArbiterError(size, alignment, name, trace)
            # Overlap — advance past this block.
            trace.append(
                f"${candidate:04X}-${chunk_end:04X} overlaps {blk}"
            )
            candidate = blk.end + 1
            candidate = (candidate + alignment - 1) & ~(alignment - 1)

        # No more blocks — try the tail of the window.
        chunk_end = candidate + size - 1
        if candidate <= hi and chunk_end <= hi:
            return self._commit(candidate, size, name)
        trace.append(
            f"${candidate:04X}-${chunk_end:04X} would exceed "
            f"window end ${hi:04X}"
        )
        raise MemoryArbiterError(size, alignment, name, trace)

    def reserve(self, start: int, end: int, *, name: str = "<manual>") -> None:
        """Manually mark ``[start, end]`` (inclusive) as reserved.

        Useful for ad-hoc one-off reserves after the arbiter is
        constructed, e.g. when a test discovers a clash dynamically.
        """
        if not (0 <= start <= _ADDR_MAX and 0 <= end <= _ADDR_MAX):
            raise ValueError(
                f"reserve range ${start:04X}-${end:04X} outside 16-bit space"
            )
        if end < start:
            raise ValueError(
                f"reserve range ${start:04X}-${end:04X} has end < start"
            )
        self._reserved_ranges.append(_Range(start, end, name))
        self._reserved_ranges.sort(key=lambda r: (r.start, r.end))

    @property
    def allocations(self) -> list[tuple[int, int, str]]:
        """List of ``(start, end, name)`` for every successful alloc.

        Returned in allocation order (not address order).  Useful for
        printing a summary at the end of a test.
        """
        return [(r.start, r.end, r.label) for r in self._allocated]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _commit(self, base: int, size: int, name: str) -> int:
        rng = _Range(base, base + size - 1, name)
        self._allocated.append(rng)
        return base
