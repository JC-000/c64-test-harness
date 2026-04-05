"""SID file playback on VICE (via 6502 IRQ stub) or Ultimate 64 (REST API).

Provides a single ``play_sid()`` dispatcher that routes to the correct
backend based on the transport type.

On VICE, a minimal 6502 installer + IRQ wrapper stub is written into RAM.
The installer redirects the RAM IRQ vector at ``$0314/$0315`` to a wrapper
that calls the SID's ``play`` routine once per jiffy IRQ, then chains to
the KERNAL IRQ handler at ``$EA31``.

On Ultimate 64, the REST API's native ``sid_play`` endpoint is used
directly — the firmware handles everything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.vice_binary import BinaryViceTransport
    from .backends.ultimate64 import Ultimate64Transport

from .sid import SidFile

# Default address for the VICE player stub — well clear of BASIC RAM,
# unused by KERNAL, and below the I/O area at $D000.
DEFAULT_STUB_ADDR = 0xC000

# KERNAL default IRQ handler — wrapper chains here after calling play.
_KERNAL_IRQ = 0xEA31

# C64 RAM IRQ vector (lo/hi).
_IRQ_VEC_LO = 0x0314
_IRQ_VEC_HI = 0x0315

# Scratch area for the song-number trampoline (free cassette buffer slot
# just past the one ``execute.jsr()`` uses at $0334).
_SONG_TRAMPOLINE_ADDR = 0x033C

_STUB_LEN = 18
_INSTALLER_LEN = 12  # wrapper starts at stub_addr + 12


class SidPlaybackError(Exception):
    """Raised when SID playback dispatch or execution fails."""


def build_vice_stub(play_addr: int, stub_addr: int = DEFAULT_STUB_ADDR) -> bytes:
    """Build the 6502 IRQ installer + wrapper stub.

    The installer lives at ``stub_addr`` and, when executed via ``JSR``,
    points the RAM IRQ vector at the wrapper (at ``stub_addr + 12``) and
    returns.  The wrapper is invoked by the KERNAL jiffy IRQ, calls
    ``play_addr`` via ``JSR``, then falls through to ``$EA31`` so the
    normal KERNAL IRQ chain (cursor flash, keyboard scan) still runs.

    Layout (18 bytes total)::

        stub_addr + 0x00  installer (12 bytes)
            A9 <hi        LDA #>wrapper
            8D 15 03      STA $0315
            A9 <lo        LDA #<wrapper
            8D 14 03      STA $0314
            EA            NOP (padding)
            60            RTS
        stub_addr + 0x0C  wrapper (6 bytes)
            20 LO HI      JSR play_addr
            4C 31 EA      JMP $EA31

    Note: the vector is written high-byte-first so that if a jiffy IRQ
    lands in the short window between the two stores, it still invokes
    the old (KERNAL) handler via the still-valid low byte paired with
    a plausible high byte.

    Args:
        play_addr: 16-bit address of the SID's play routine.
        stub_addr: Base address the stub will be written to — needed so
            that the installer's ``LDA #<wrapper`` / ``LDA #>wrapper``
            immediates reference the wrapper's true location.

    Returns:
        Exactly 18 bytes of 6502 machine code.
    """
    wrapper_addr = (stub_addr + _INSTALLER_LEN) & 0xFFFF
    wrap_lo = wrapper_addr & 0xFF
    wrap_hi = (wrapper_addr >> 8) & 0xFF

    play_lo = play_addr & 0xFF
    play_hi = (play_addr >> 8) & 0xFF

    kernal_lo = _KERNAL_IRQ & 0xFF
    kernal_hi = (_KERNAL_IRQ >> 8) & 0xFF

    # Installer: 12 bytes. No SEI/CLI — to stay atomic we update the
    # high byte first (leaving the old vector addressable via still-valid
    # low byte if an IRQ lands mid-update), then the low byte. Even in the
    # worst case the KERNAL IRQ fires at 60 Hz (~16,666 cycles apart)
    # while this window is only ~8 cycles, so the race is statistically
    # insignificant for a one-shot boot routine.
    installer = bytes([
        0xA9, wrap_hi,             # LDA #>wrapper        (2)
        0x8D, 0x15, 0x03,          # STA $0315            (3)
        0xA9, wrap_lo,             # LDA #<wrapper        (2)
        0x8D, 0x14, 0x03,          # STA $0314            (3)
        0xEA,                      # NOP (padding)        (1)
        0x60,                      # RTS                  (1)
    ])
    assert len(installer) == _INSTALLER_LEN

    wrapper = bytes([
        0x20, play_lo, play_hi,    # JSR play_addr
        0x4C, kernal_lo, kernal_hi,  # JMP $EA31
    ])

    stub = installer + wrapper
    assert len(stub) == _STUB_LEN
    return stub


def _validate_song(sid: SidFile, song: int) -> None:
    """Raise SidPlaybackError if *song* is not a valid 0-based index."""
    if song < 0 or song >= sid.songs:
        raise SidPlaybackError(
            f"song {song} out of range (0..{sid.songs - 1})"
        )


def play_sid_vice(
    transport: BinaryViceTransport,
    sid: SidFile,
    song: int = 0,
    stub_addr: int = DEFAULT_STUB_ADDR,
) -> None:
    """Load SID data into VICE memory, run init, and install the IRQ wrapper.

    Steps:

    1. Write ``sid.c64_data`` to ``sid.effective_load_addr``.
    2. Write the player stub (see :func:`build_vice_stub`) at ``stub_addr``.
    3. Place a tiny trampoline ``LDA #song ; JSR init ; BRK`` at
       ``$033C`` and ``JSR`` it — this invokes the SID's init routine
       with ``A`` = song number (0-based, per the SID spec).
    4. ``JSR`` the installer at ``stub_addr`` so the RAM IRQ vector is
       patched to point at the wrapper.
    5. ``resume()`` the CPU — the KERNAL jiffy IRQ will now invoke the
       wrapper once per frame, driving the SID's play routine.

    Args:
        transport: A :class:`BinaryViceTransport` connected to a running
            VICE instance.
        sid: Parsed SID file.
        song: 0-based song index (translated to the SID's 1-based
            ``start_song`` convention via the init routine's A-register
            protocol — the SID spec requires A = song_number - 1, i.e.
            0-based, which matches our parameter directly).
        stub_addr: Where the 18-byte stub is written.

    Raises:
        SidPlaybackError: If ``sid.play_addr`` is 0 (IRQ-installing
            inits are not supported by this simple stub) or if ``song``
            is out of range.
    """
    _validate_song(sid, song)

    if sid.play_addr == 0:
        raise SidPlaybackError(
            "IRQ-driven SIDs not supported by simple VICE stub — "
            "play_addr is 0"
        )

    from .execute import jsr, load_code

    # 1. Load the SID's 6502 data into memory.
    load_code(transport, sid.effective_load_addr, sid.c64_data)

    # 2. Write the player stub.
    load_code(transport, stub_addr, build_vice_stub(sid.play_addr, stub_addr))

    # 3. Run init with A = song (0-based per SID init A-register protocol).
    init_lo = sid.init_addr & 0xFF
    init_hi = (sid.init_addr >> 8) & 0xFF
    song_trampoline = bytes([
        0xA9, song & 0xFF,              # LDA #song
        0x20, init_lo, init_hi,         # JSR init_addr
        0x60,                           # RTS (return to outer jsr() trampoline)
    ])
    load_code(transport, _SONG_TRAMPOLINE_ADDR, song_trampoline)
    jsr(transport, _SONG_TRAMPOLINE_ADDR)

    # 4. Run the installer — it patches $0314/$0315 and RTSes.
    jsr(transport, stub_addr)

    # 5. Park PC at a tiny "JMP ($A002)" trampoline so resume() lands in
    #    BASIC's warm-start vector (BASIC idle loop) rather than the
    #    stale NOPs left by jsr() at $0334 + 3. Without this, resume()
    #    executes NOP NOP and falls into whatever garbage follows,
    #    BRKing into the KERNAL BRK handler which calls RESTOR and
    #    resets $0314/$0315 back to $EA31 — wiping out our IRQ hook.
    #
    #    $A002/$A003 holds BASIC warm-start address (typically $A474).
    _PARK_ADDR = 0x0339
    transport.write_memory(_PARK_ADDR, bytes([0x6C, 0x02, 0xA0]))  # JMP ($A002)
    transport.set_registers({"PC": _PARK_ADDR})
    # Round-trip a register read to flush the set before resume().
    transport.read_registers()

    # 6. Resume: KERNAL IRQ will now tick the SID player.
    transport.resume()


def play_sid_ultimate64(
    transport: Ultimate64Transport,
    sid: SidFile,
    song: int = 0,
) -> None:
    """Delegate SID playback to the Ultimate 64's native ``sid_play`` endpoint.

    The U64 REST API takes the raw ``.sid`` file bytes and a 0-based
    ``songnr`` query parameter — no stub code, no memory fiddling.

    Args:
        transport: :class:`Ultimate64Transport` connected to a U64.
        sid: Parsed SID file.
        song: 0-based song index (passed through as ``songnr``).

    Raises:
        SidPlaybackError: If ``song`` is out of range.
    """
    _validate_song(sid, song)
    transport._client.sid_play(sid.raw, songnr=song)


def stop_sid_vice(transport: BinaryViceTransport) -> None:
    """Restore the default KERNAL IRQ vector at ``$0314/$0315``.

    Writes ``$31 $EA`` (lo, hi of ``$EA31``) so the jiffy IRQ no longer
    calls the SID player wrapper.  Safe to call even if no SID is playing.
    """
    transport.write_memory(
        _IRQ_VEC_LO,
        bytes([_KERNAL_IRQ & 0xFF, (_KERNAL_IRQ >> 8) & 0xFF]),
    )


def play_sid(
    transport,
    sid: SidFile,
    song: int = 0,
    stub_addr: int = DEFAULT_STUB_ADDR,
) -> None:
    """Play a SID file on either VICE or Ultimate 64.

    Dispatches based on the transport's concrete class:

    - :class:`Ultimate64Transport` → :func:`play_sid_ultimate64`
    - :class:`BinaryViceTransport` → :func:`play_sid_vice`

    Args:
        transport: Either a ``BinaryViceTransport`` or
            ``Ultimate64Transport``.
        sid: Parsed SID file.
        song: 0-based song index.  Converted to the SID init convention
            (A-register, 0-based) for VICE, or passed as ``songnr``
            (0-based) to the U64 REST API.  Both backends use the same
            0-based semantics on the wire.
        stub_addr: Where to place the VICE player stub.  Ignored on U64.

    Raises:
        SidPlaybackError: If the transport type is unsupported, if
            ``song`` is out of range, or if VICE playback is requested
            for a SID with ``play_addr == 0``.
    """
    from .backends.ultimate64 import Ultimate64Transport
    from .backends.vice_binary import BinaryViceTransport

    if isinstance(transport, Ultimate64Transport):
        return play_sid_ultimate64(transport, sid, song)
    if isinstance(transport, BinaryViceTransport):
        return play_sid_vice(transport, sid, song, stub_addr)
    raise SidPlaybackError(
        f"Unsupported transport type: {type(transport).__name__}"
    )


__all__ = [
    "DEFAULT_STUB_ADDR",
    "SidPlaybackError",
    "build_vice_stub",
    "play_sid",
    "play_sid_vice",
    "play_sid_ultimate64",
    "stop_sid_vice",
]
