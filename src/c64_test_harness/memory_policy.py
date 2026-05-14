"""Memory policy enforced at the transport boundary.

The 6502 has no MMU and no segfault.  A host-side ``write_memory()`` into
a RAM region that the loaded PRG is also using cannot fault — both writes
land, the last one wins, and a subsequent read of the clobbered bytes
produces wrong-but-deterministic results.  Tests fail on a downstream
symptom and the bisection ends up nowhere near the actual root cause.

``MemoryPolicy`` is the safety net.  A policy is held by the transport
(VICE or U64); every ``write_memory()`` call routes through
:meth:`MemoryPolicy.check_write` *before* a byte crosses the wire.  The
invariant the policy enforces is **allow-list** semantics — writes are
permitted only if they fall inside a region the consumer has declared
safe, with an explicit per-call ``override="reason"`` escape hatch for
the rare legitimate clobber.

Default behaviour is permissive (an empty policy) so existing tests
keep working with no migration effort; consumers opt in by passing a
PRG, a TOML config, or building a policy programmatically.

Construction patterns
---------------------

From a PRG file (cheapest accurate signal — auto-reserves the load
image)::

    from c64_test_harness import MemoryPolicy, UnknownPolicy
    from c64_test_harness.verify import PrgFile

    prg = PrgFile.from_file("build/program.prg")
    policy = MemoryPolicy.from_prg(prg, unknown=UnknownPolicy.WARN)
    target.transport.memory_policy = policy

From a TOML config::

    [memory]
    prg = "build/program.prg"
    safe_regions = [
        { range = "$0334-$03FB", note = "cassette buffer (harness scratch)" },
        { range = "$C000-$CFFF", note = "harness-claimed scratch page" },
    ]
    reserved_regions = [
        { range = "$4200-$50FF", note = "X25519 RODATA + BSS" },
        { range = "$A000-$BFFF", note = "SHADOW_BSS under BASIC ROM" },
    ]
    unknown_policy = "deny"

Programmatically::

    policy = (
        MemoryPolicy.permissive()
        .with_reserved(MemoryRegion(0x4200, 0x5100, "X25519 RODATA"))
        .with_safe(MemoryRegion(0xC000, 0xD000, "harness scratch"))
        .with_unknown(UnknownPolicy.DENY)
    )
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .verify import PrgFile

_log = logging.getLogger(__name__)

# Exclusive upper bound of the 16-bit C64 address space.
_ADDR_SPACE = 0x10000


class UnknownPolicy(str, Enum):
    """How to treat writes that hit neither safe_regions nor reserved_regions.

    ``ALLOW`` is the migration default — the policy never fails unless a
    write actively hits a reserved region.  Consumers tighten to ``WARN``
    once they have safe_regions declared, then ``DENY`` once the layout
    is stable.
    """

    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


class MemoryPolicyError(Exception):
    """A host→C64 write was blocked by the active memory policy.

    Carries the offending address, length, and (when relevant) the
    violated region.  The exception message names the violation in
    ${addr:04X} form and points at the bypass mechanism so the diagnostic
    is actionable on first read.
    """

    def __init__(
        self,
        addr: int,
        length: int,
        reason: str,
        region: MemoryRegion | None = None,
    ) -> None:
        self.addr = addr
        self.length = length
        self.reason = reason
        self.region = region
        end_incl = addr + length - 1
        super().__init__(
            f"write_memory(${addr:04X}, {length} B) → ${end_incl:04X} blocked: "
            f"{reason}. Pass override=\"<reason>\" to bypass for a single "
            f"call, or update the harness MemoryPolicy."
        )


def _parse_addr(text: str) -> int:
    """Parse a C64 address literal — ``$XXXX``, ``0xXXXX`` or decimal."""
    text = text.strip()
    if text.startswith("$"):
        return int(text[1:], 16)
    return int(text, 0)


@dataclass(frozen=True)
class MemoryRegion:
    """Half-open ``[start, end)`` address range with a human-readable note.

    The end bound is **exclusive** to make ``len = end - start`` and
    range-arithmetic loops obvious.  When parsing from strings or
    serialising to messages, the inclusive form ``$XXXX-$YYYY`` is used,
    matching the C64 linker-cfg convention.
    """

    start: int
    end: int
    note: str = ""

    def __post_init__(self) -> None:
        if not (0 <= self.start < _ADDR_SPACE):
            raise ValueError(
                f"MemoryRegion start ${self.start:04X} is outside the 16-bit "
                f"address space"
            )
        if not (0 < self.end <= _ADDR_SPACE):
            raise ValueError(
                f"MemoryRegion end ${self.end:04X} is outside the 16-bit "
                f"address space"
            )
        if self.end <= self.start:
            raise ValueError(
                f"MemoryRegion ${self.start:04X}-${self.end:04X} has "
                f"end <= start"
            )

    @classmethod
    def parse(cls, spec: str, *, note: str = "") -> MemoryRegion:
        """Parse a region spec — ``"$XXXX-$YYYY"`` (inclusive) or ``"$XXXX+N"``.

        A bare address ``"$XXXX"`` is treated as a single byte.
        """
        spec = spec.strip()
        if "-" in spec:
            lo, hi = spec.split("-", 1)
            start = _parse_addr(lo)
            end_incl = _parse_addr(hi)
            return cls(start, end_incl + 1, note=note)
        if "+" in spec:
            base, length = spec.split("+", 1)
            start = _parse_addr(base)
            n = int(length.strip(), 0)
            return cls(start, start + n, note=note)
        start = _parse_addr(spec)
        return cls(start, start + 1, note=note)

    def contains_addr(self, addr: int) -> bool:
        return self.start <= addr < self.end

    def overlaps_range(self, addr: int, length: int) -> bool:
        return addr < self.end and addr + length > self.start

    @property
    def length(self) -> int:
        return self.end - self.start

    def __str__(self) -> str:
        end_incl = self.end - 1
        suffix = f" ({self.note})" if self.note else ""
        return f"${self.start:04X}-${end_incl:04X}{suffix}"


def _region_from_entry(entry: object) -> MemoryRegion:
    """Coerce a TOML/dict region entry into a :class:`MemoryRegion`.

    Supported forms:

    * ``"$4200-$50FF"`` — bare string, no note
    * ``{"range": "$4200-$50FF", "note": "X25519 RODATA"}`` — dict
    * ``{"start": 0x4200, "end": 0x5100, "note": "..."}`` — dict, ints
    """
    if isinstance(entry, str):
        return MemoryRegion.parse(entry)
    if isinstance(entry, dict):
        note = str(entry.get("note", ""))
        if "range" in entry:
            return MemoryRegion.parse(str(entry["range"]), note=note)
        if "addr" in entry:
            return MemoryRegion.parse(str(entry["addr"]), note=note)
        if "start" in entry and "end" in entry:
            start = int(entry["start"])
            end = int(entry["end"])
            return MemoryRegion(start, end, note)
        raise ValueError(
            f"memory region entry missing 'range', 'addr', or "
            f"'start'+'end': {entry!r}"
        )
    raise TypeError(f"unsupported memory region entry type: {type(entry).__name__}")


@dataclass(frozen=True)
class MemoryPolicy:
    """Allow-list / deny-list policy enforced at the transport boundary.

    The check runs *before* any byte crosses the wire to the C64.  A
    write of ``[addr, addr+length)`` is evaluated against:

    1. ``reserved_regions`` — any overlap → :class:`MemoryPolicyError`.
       Deny-list takes precedence over the allow-list.
    2. ``safe_regions`` — full coverage of the write span → pass.
       (Multiple abutting safe regions can cover a span together.)
    3. ``unknown`` — for spans that hit neither list:

       * ``ALLOW`` — pass (the migration default)
       * ``WARN`` — emit a :class:`UserWarning` and pass
       * ``DENY`` — raise :class:`MemoryPolicyError`

    The empty policy (``MemoryPolicy()`` / :meth:`permissive`) keeps
    legacy behaviour: no safe regions, no reserved regions, unknown =
    ALLOW → every write passes.

    Per-call ``override="<reason>"`` on ``write_memory`` bypasses the
    check for one call; the bypass is logged at WARNING level so the
    use is visible in test output.
    """

    safe_regions: tuple[MemoryRegion, ...] = ()
    reserved_regions: tuple[MemoryRegion, ...] = ()
    unknown: UnknownPolicy = UnknownPolicy.ALLOW

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def permissive(cls) -> MemoryPolicy:
        """Empty policy — every write passes.  The pre-policy default."""
        return cls()

    @classmethod
    def from_prg(
        cls,
        prg: PrgFile | str | Path,
        *,
        note: str = "PRG load image",
        unknown: UnknownPolicy = UnknownPolicy.WARN,
        extra_reserved: tuple[MemoryRegion, ...] = (),
        safe_regions: tuple[MemoryRegion, ...] = (),
    ) -> MemoryPolicy:
        """Build a policy that reserves the PRG's load span.

        ``unknown=WARN`` by default so consumers see writes that escape
        the load image without breaking pre-existing tests.  Tighten to
        ``UnknownPolicy.DENY`` once the consumer's full layout is
        declared via ``safe_regions``/``extra_reserved``.
        """
        from .verify import PrgFile as _PrgFile

        if isinstance(prg, (str, Path)):
            prg = _PrgFile.from_file(prg)
        prg_region = MemoryRegion(prg.load_address, prg.end_address, note)
        return cls(
            safe_regions=tuple(safe_regions),
            reserved_regions=(prg_region,) + tuple(extra_reserved),
            unknown=unknown,
        )

    @classmethod
    def from_config(cls, data: dict) -> MemoryPolicy:
        """Build from a TOML ``[memory]`` section parsed into a dict.

        Recognised keys: ``prg``, ``safe_regions``, ``reserved_regions``,
        ``unknown_policy``.  See module docstring for shape.
        """
        safe: list[MemoryRegion] = [
            _region_from_entry(e) for e in (data.get("safe_regions") or [])
        ]
        reserved: list[MemoryRegion] = [
            _region_from_entry(e) for e in (data.get("reserved_regions") or [])
        ]
        prg_path = data.get("prg")
        if prg_path:
            from .verify import PrgFile

            prg = PrgFile.from_file(prg_path)
            reserved.append(
                MemoryRegion(
                    prg.load_address,
                    prg.end_address,
                    f"PRG load image ({prg_path})",
                )
            )
        unknown_raw = data.get("unknown_policy", "allow")
        try:
            unknown = UnknownPolicy(str(unknown_raw).lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown_policy must be one of allow|warn|deny; got {unknown_raw!r}"
            ) from exc
        return cls(
            safe_regions=tuple(safe),
            reserved_regions=tuple(reserved),
            unknown=unknown,
        )

    # ------------------------------------------------------------------
    # Mutators (return new instances — MemoryPolicy is frozen)
    # ------------------------------------------------------------------

    def with_safe(self, region: MemoryRegion) -> MemoryPolicy:
        return MemoryPolicy(
            safe_regions=self.safe_regions + (region,),
            reserved_regions=self.reserved_regions,
            unknown=self.unknown,
        )

    def with_reserved(self, region: MemoryRegion) -> MemoryPolicy:
        return MemoryPolicy(
            safe_regions=self.safe_regions,
            reserved_regions=self.reserved_regions + (region,),
            unknown=self.unknown,
        )

    def with_unknown(self, unknown: UnknownPolicy) -> MemoryPolicy:
        return MemoryPolicy(
            safe_regions=self.safe_regions,
            reserved_regions=self.reserved_regions,
            unknown=unknown,
        )

    def merged(self, other: MemoryPolicy) -> MemoryPolicy:
        """Combine two policies; ``other.unknown`` wins the unknown setting."""
        return MemoryPolicy(
            safe_regions=self.safe_regions + other.safe_regions,
            reserved_regions=self.reserved_regions + other.reserved_regions,
            unknown=other.unknown,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_permissive(self) -> bool:
        """True when every write would pass without checks.

        Transports short-circuit the check when this is true to keep the
        zero-config path overhead-free.
        """
        return (
            not self.safe_regions
            and not self.reserved_regions
            and self.unknown == UnknownPolicy.ALLOW
        )

    # ------------------------------------------------------------------
    # The check
    # ------------------------------------------------------------------

    def check_write(
        self,
        addr: int,
        length: int,
        *,
        override: str | None = None,
    ) -> None:
        """Raise :class:`MemoryPolicyError` if the write is forbidden.

        Parameters
        ----------
        addr:
            Start address (16-bit).
        length:
            Number of bytes that will be written.
        override:
            Non-empty reason string bypasses the check.  The bypass is
            logged at WARNING so it remains visible in test output.
        """
        if length <= 0:
            return
        if override:
            _log.warning(
                "memory policy override at $%04X+%d (reason: %s)",
                addr,
                length,
                override,
            )
            return

        # Deny-list first: any byte in a reserved region → fail.
        for region in self.reserved_regions:
            if region.overlaps_range(addr, length):
                raise MemoryPolicyError(
                    addr,
                    length,
                    f"overlaps reserved region {region}",
                    region=region,
                )

        # Allow-list: full coverage → pass.
        if self.safe_regions and _fully_covered(addr, length, self.safe_regions):
            return

        # Unknown territory.
        if self.unknown == UnknownPolicy.ALLOW:
            return
        msg = (
            "address not fully inside any declared safe_region"
            if self.safe_regions
            else "no safe_regions declared and unknown_policy is not 'allow'"
        )
        if self.unknown == UnknownPolicy.WARN:
            warnings.warn(
                f"write_memory(${addr:04X}, {length} B): {msg}",
                stacklevel=3,
            )
            return
        # DENY
        raise MemoryPolicyError(addr, length, msg)


def _fully_covered(
    addr: int,
    length: int,
    regions: tuple[MemoryRegion, ...],
) -> bool:
    """True iff every byte of ``[addr, addr+length)`` lies in some region.

    Handles abutting regions (e.g. ``$0200-$02FF`` + ``$0300-$03FF``
    together cover ``$0200-$03FF``).
    """
    end = addr + length
    pos = addr
    for r in sorted(regions, key=lambda r: r.start):
        if r.end <= pos:
            continue
        if r.start > pos:
            return False  # gap
        pos = r.end
        if pos >= end:
            return True
    return pos >= end


__all__ = [
    "MemoryPolicy",
    "MemoryPolicyError",
    "MemoryRegion",
    "UnknownPolicy",
]
