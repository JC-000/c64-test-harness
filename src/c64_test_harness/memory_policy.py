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


# ---------------------------------------------------------------------------
# The harness's own scratch addresses (issue #169)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScratchRegion:
    """One fixed address range the harness itself writes during normal use.

    ``HARNESS_SCRATCH`` is the machine-readable form of the table in
    ``docs/memory_safety.md`` (which is *generated* from it by
    ``scripts/gen_memory_table.py``).  :class:`MemoryArbiter` consults
    it by default so it never hands a consumer an address the harness
    is about to overwrite, and :meth:`MemoryPolicy.from_prg` warns when
    a consumer's load image overlaps one.

    ``end`` is exclusive, like :class:`MemoryRegion`.  ``owner`` is the
    dotted path of the writer; ``configurable`` names the kwarg or
    constant that moves the address (``"hardcoded"`` when nothing
    does).  ``transient`` means the prior contents are written back
    afterwards — nothing more.  It does NOT mean the span is safe to
    execute from while the operation runs (the REU staging window is
    filled by REC DMA with the CPU live, and the liveness probe only
    restores on success), and it does not undo side effects of code
    that ran there meanwhile.  Transient spans are declared like every
    other write but not withheld from allocation by default.
    """

    start: int
    end: int
    owner: str
    purpose: str
    configurable: str = "hardcoded"
    transient: bool = False

    def __post_init__(self) -> None:
        # Reuse MemoryRegion's bounds validation.
        MemoryRegion(self.start, self.end)

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def span(self) -> str:
        """Inclusive ``$XXXX-$YYYY`` form; a single byte prints as ``$XXXX``."""
        if self.length == 1:
            return f"${self.start:04X}"
        return f"${self.start:04X}-${self.end - 1:04X}"

    @property
    def region(self) -> MemoryRegion:
        return MemoryRegion(
            self.start, self.end, note=f"harness scratch: {self.owner}"
        )


#: Every fixed C64 RAM address the harness writes as part of normal
#: operation, verified against the code that performs the write (the
#: owner column).  Inclusion criterion: library writes under ``src/``
#: at fixed default addresses, plus the one live-suite stub at ``$CF00``
#: (``tests/test_vice_core.py::_restore_basic``) because every screen
#: and keyboard test runs it.  Caller-supplied addresses (required
#: kwargs with no default, e.g. bridge_ping's ``rx_buf``/``result_addr``)
#: are the caller's to declare and are not listed.  Keep sorted by
#: ``(start, end)``.  Overlaps between entries are expected — several
#: features share the cassette buffer and the ``$C000`` page.
#: I/O-register writes (``$D000-$DFFF``: CIA/REC/UCI/CS8900a) are not
#: RAM and are not listed.
HARNESS_SCRATCH: tuple[ScratchRegion, ...] = (
    ScratchRegion(
        0x0000, 0x0002,
        owner="snapshot.restore_snapshot",
        purpose="6510 CPU port direction/data re-asserted after the RAM "
                "restore (override=\"snapshot-restore\")",
        configurable="restore-time only; carries override=",
    ),
    ScratchRegion(
        0x00C6, 0x00C7,
        owner="uci_network._execute_uci_routine, "
              "backends.ultimate64.Ultimate64Transport.inject_keys, "
              "backends.ultimate64_client.Ultimate64Client.send_text",
        purpose="KERNAL NDX — keyboard-buffer fill count set after a "
                "SYS/text injection",
        configurable="KERNAL-mandated (keybuf_count_addr= on U64 transport)",
    ),
    ScratchRegion(
        0x0277, 0x0281,
        owner="uci_network._execute_uci_routine, "
              "backends.ultimate64.Ultimate64Transport.inject_keys, "
              "backends.ultimate64_client.Ultimate64Client.send_text",
        purpose="KERNAL KEYD — 10-byte keyboard buffer receiving "
                "\"SYS<addr>\\r\" or injected text",
        configurable="KERNAL-mandated (keybuf_addr= on U64 transport)",
    ),
    ScratchRegion(
        0x0314, 0x0316,
        owner="sid_player.stop_sid_vice",
        purpose="RAM IRQ vector (CINV) restored to $EA31; the installer "
                "stub also patches it from 6502 code",
        configurable="hardcoded",
    ),
    ScratchRegion(
        0x0334, 0x0339,
        owner="execute.jsr",
        purpose="JSR addr / NOP / NOP trampoline; checkpoint at +3",
        configurable="scratch_addr=",
    ),
    ScratchRegion(
        0x0334, 0x03B4,
        owner="backends.ultimate64_probe.liveness_probe",
        purpose="128-byte writemem POST round-trip payload via the raw "
                "REST client (bypasses the transport MemoryPolicy); "
                "original bytes written back on success only — the "
                "readback-failure branches leave the pattern in place",
        configurable="hardcoded",
        transient=True,
    ),
    ScratchRegion(
        0x0339, 0x033C,
        owner="sid_player.play_sid_vice",
        purpose="park JMP ($A002) executed after the installer so "
                "resume() lands in BASIC warm start",
        configurable="_PARK_ADDR constant",
    ),
    ScratchRegion(
        0x033C, 0x0342,
        owner="sid_player.play_sid_vice",
        purpose="song trampoline: LDA #song / JSR init / RTS",
        configurable="_SONG_TRAMPOLINE_ADDR constant",
    ),
    ScratchRegion(
        0x0360, 0x036E,
        owner="execute.run_subroutine (U64 path)",
        purpose="14-byte sentinel trampoline; on VICE the 5-byte jsr() "
                "trampoline is written here instead",
        configurable="trampoline_addr=",
    ),
    ScratchRegion(
        0x03F0, 0x03F2,
        owner="execute.run_subroutine (U64 path)",
        purpose="running / done flag bytes polled by the host",
        configurable="hardcoded",
    ),
    ScratchRegion(
        0x0800, 0x8800,
        owner="snapshot.extract_reu_contents",
        purpose="32 KiB REU→C64 DMA staging window (opt-in "
                "include_reu=True, override=\"reu-snapshot-staging\"). "
                "Filled by REC DMA with the CPU running (unpaused is "
                "mandatory on hardware) — MemoryPolicy cannot see the "
                "fill; prior contents written back afterwards, but code "
                "executing there meanwhile runs REU data",
        configurable="hardcoded",
        transient=True,
    ),
    ScratchRegion(
        0xC000, 0xC012,
        owner="sid_player.play_sid_vice",
        purpose="18-byte IRQ installer + wrapper stub",
        configurable="stub_addr= (DEFAULT_STUB_ADDR)",
    ),
    ScratchRegion(
        0xC000, 0xC040,
        owner="bridge_ping.run_ping_and_wait / run_icmp_responder",
        purpose="64-byte CS8900a RX peek routine",
        configurable="peek_addr=",
    ),
    ScratchRegion(
        0xC000, 0xC400,
        owner="uci_network._execute_uci_routine / build_uci_command",
        purpose="UCI stub block: code $C000, data $C100, response $C200, "
                "status $C300, lengths $C3F0-$C3F3, sentinel $C3FE, "
                "error $C3FF",
        configurable="code_addr= for the routine; buffers hardcoded",
    ),
    ScratchRegion(
        0xC100, 0xC25D,
        owner="bridge_ping.run_ping_and_wait / run_icmp_responder",
        purpose="TX / echo-match / echo-respond routines (largest "
                "349 bytes)",
        configurable="consume_addr=",
    ),
    ScratchRegion(
        0xC400, 0xC403,
        owner="uci_network.build_socket_write",
        purpose="16-bit inner-loop countdown (lo, hi) + Y save slot "
                "across the turbo fence",
        configurable="hardcoded",
    ),
    ScratchRegion(
        0xC403, 0xC404,
        owner="uci_network.uci_socket_write",
        purpose="socket-id slot for the lifted-cap write routine",
        configurable="hardcoded",
    ),
    ScratchRegion(
        0xC500, 0xC87E,
        owner="uci_network.uci_socket_write",
        purpose="data buffer (up to 892 bytes) followed by the 2-byte LE "
                "length",
        configurable="hardcoded",
    ),
    ScratchRegion(
        0xCF00, 0xCF04,
        owner="tests/test_vice_core.py::_restore_basic (also "
              "scripts/vice_keyecho_probe.py + scripts/vice_stall_probe.py)",
        purpose="CLI; JMP $E5CD stub returning the CPU to BASIC MAINLOOP "
                "before every screen/keyboard test — test-suite scratch, "
                "not library",
        configurable="hardcoded",
    ),
)


def harness_scratch_regions(
    *, include_transient: bool = False
) -> tuple[MemoryRegion, ...]:
    """``HARNESS_SCRATCH`` as :class:`MemoryRegion` objects.

    Transient spans (prior contents written back afterwards, e.g. the
    32 KiB REU staging window) are omitted unless ``include_transient``
    is set — reserving them by default would withhold half the address
    space for a window whose bytes are put back afterwards.  "Written
    back" is all transient promises; see :class:`ScratchRegion`.
    """
    return tuple(
        r.region for r in HARNESS_SCRATCH
        if include_transient or not r.transient
    )


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
        policy = cls(
            safe_regions=tuple(safe_regions),
            reserved_regions=(prg_region,) + tuple(extra_reserved),
            unknown=unknown,
        )
        # Issue #169: say so *now* if the load image sits on an address
        # the harness writes for itself.  The policy still blocks the
        # eventual collision (the PRG span is reserved, so jsr()'s
        # trampoline write raises MemoryPolicyError) — but that surfaces
        # only when the first harness call happens, deep in a test.  We
        # do NOT add HARNESS_SCRATCH to either region list: as reserved
        # it would break every harness write into its own scratch; as
        # safe it would change the unknown-address semantics.
        overlaps = policy.harness_scratch_overlaps()
        if overlaps:
            detail = "; ".join(
                f"{r} vs {s.span} ({s.owner}; {s.configurable})"
                for r, s in overlaps
            )
            warnings.warn(
                f"MemoryPolicy.from_prg: the load image overlaps harness "
                f"scratch — harness writes there will raise "
                f"MemoryPolicyError. Move the harness (see the "
                f"'configurable' hint) or the program: {detail}",
                stacklevel=2,
            )
        return policy

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

    def harness_scratch_overlaps(
        self, *, include_transient: bool = False
    ) -> tuple[tuple[MemoryRegion, ScratchRegion], ...]:
        """Reserved regions that overlap the harness's own scratch (#169).

        Each pair is ``(reserved_region, scratch_entry)``.  A non-empty
        result means a harness write into that scratch will raise
        :class:`MemoryPolicyError` under this policy — the consumer
        should relocate the program or pass the harness the kwarg named
        in ``scratch_entry.configurable``.  Transient entries (prior
        contents written back afterwards) are omitted unless requested,
        since the 32 KiB REU staging window overlaps nearly every PRG.
        """
        pairs: list[tuple[MemoryRegion, ScratchRegion]] = []
        for reserved in self.reserved_regions:
            for scratch in HARNESS_SCRATCH:
                if scratch.transient and not include_transient:
                    continue
                if reserved.overlaps_range(scratch.start, scratch.length):
                    pairs.append((reserved, scratch))
        return tuple(pairs)

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

        Raises
        ------
        ValueError
            If ``addr + length`` exceeds the 16-bit address space.  The
            transport would wrap such a write back to ``$0000``, and the
            wrapped span cannot be evaluated against regions declared in
            ``$0000-$FFFF`` — so it is rejected as invalid input rather
            than silently approved.  ``override`` does not bypass this.
        """
        if length <= 0:
            return
        if addr + length > _ADDR_SPACE:
            raise ValueError(
                f"write_memory(${addr:04X}, {length} B) extends past the top "
                f"of the 16-bit address space (end ${addr + length:05X} > "
                f"$10000); the wrapped span cannot be policy-checked — split "
                f"the write at $FFFF"
            )
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
    "HARNESS_SCRATCH",
    "MemoryPolicy",
    "MemoryPolicyError",
    "MemoryRegion",
    "ScratchRegion",
    "UnknownPolicy",
    "harness_scratch_regions",
]
