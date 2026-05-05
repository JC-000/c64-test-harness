"""CIA1 Time-of-Day based 6502 timeout helpers for shippable C64 applications.

This module provides **pure 6502 code builders** that drive networking
poll loops (or any other "wait-for-event-with-timeout" pattern) off the
CIA1 TOD clock rather than off host-driven polling from the Python test
harness.

Why this exists
---------------

The Python-orchestrated ``poll_until_ready`` / ``run_ping_and_wait``
pattern (test-harness side) owns the wall clock in Python: it pauses
the 6502 between iterations and checks host-side monotonic time.  That
works well for test automation under VICE (both normal and warp mode)
and for U64-backed tests, but it is **not shippable** -- a real C64
networking application running on bare iron has no Python on the other
end of a binary monitor socket.  The 6502 must own its own timeouts.

Empirical TOD behaviour on supported platforms
----------------------------------------------

(Taken as given -- do not re-verify.)

* **Real Ultimate 64 Elite hardware**: CIA1 TOD runs at true wall-clock
  rate, flat 1.00x across the full 1-48 MHz turbo range.
* **VICE 3.10 normal mode**: TOD runs at ~1.00x wall (virtual 1 MHz CPU).
* **VICE 3.10 warp mode**: TOD runs at ~31x wall (TOD is virtual-CPU
  clocked).  **Not usable** for shippable-application timeouts.
* **All platforms**: per the 6526 datasheet, TOD powers up *stopped*.
  It must be started explicitly by clearing ``$DC0F`` bit 7 (select TOD
  write mode, not alarm-set mode) and writing hours -> minutes ->
  seconds -> tenths in that order.  Writing tenths unlatches the
  register bank and starts the counter.

TOD-based 6502 timeouts are therefore correct for real C64, real U64E
at any turbo speed, and VICE normal mode.  They are **wrong** for VICE
warp.  Real applications do not run under warp; only automated tests
do, and those use the Python-orchestrated pattern instead.

Zero-page footprint
-------------------

These routines use ``$F0``-``$F5`` as scratch:

* ``$F0/$F1`` -- current elapsed TOD reading (LE16)
* ``$F2/$F3`` -- deadline tenths (LE16, set once at routine entry)
* ``$F4``     -- BCD seconds ones-digit scratch
* ``$F5``     -- raw BCD seconds scratch

Callers that also use :mod:`c64_test_harness.bridge_ping` should note
that ``_emit_read_frame`` in that module reuses ``$F1``-``$F4`` as
temporaries while reading a received frame, with a pointer at
``$FB/$FC``.  The TOD poll loop completes (and releases ``$F0``-``$F5``)
before any frame read happens, so the two do not collide inside a
single JSR.

Design choices
--------------

* ``deadline_tenths`` is capped at **599** (1 minute).  This lets us
  skip the ``minutes * 600`` multiply entirely: if CIA1 TOD reports
  ``minutes > 0`` at any poll iteration, the deadline (< 60 s) has
  already been blown and we time out.  For longer waits, the caller
  must loop externally.
* Each poll routine **re-starts TOD at 00:00:00.0** on entry so that
  "current tenths" is a clean elapsed-since-start value with no
  wraparound handling.
* Seconds-to-tenths conversion uses small inline lookup tables emitted
  at the tail of the code blob: a 16-byte ``tens*100`` table (split
  LE16: 8 low bytes followed by 8 high bytes) and a 10-byte
  ``ones*10`` table.  Fast, simple, and no 6502 multiply needed.
* BCD conversion is done with trivial nibble-split + table lookup.

All routines return via ``RTS``.  Callers typically invoke them with
``execute.jsr()`` (in tests) or ``JSR`` from their own 6502 code (in
shippable applications).
"""

from __future__ import annotations

from .bridge_ping import Asm

# CIA1 Time-of-Day registers
CIA1_TOD_TENTHS = 0xDC08
CIA1_TOD_SEC = 0xDC09
CIA1_TOD_MIN = 0xDC0A
CIA1_TOD_HR = 0xDC0B
CIA1_CRB = 0xDC0F  # control register B -- bit 7 = TOD alarm(1) / write(0)

# Zero-page scratch
ZP_CUR_LO = 0xF0
ZP_CUR_HI = 0xF1
ZP_DEADLINE_LO = 0xF2
ZP_DEADLINE_HI = 0xF3
ZP_ONES = 0xF4
ZP_RAW = 0xF5

# Cap on deadline_tenths so we can skip the minutes*600 multiply.
MAX_DEADLINE_TENTHS = 599


# ---------------------------------------------------------------------------
# Internal 6502 emitters
# ---------------------------------------------------------------------------

def _emit_tod_start(a: Asm) -> None:
    """Emit inline code that starts CIA1 TOD at 00:00:00.0.

    Clears ``$DC0F`` bit 7 (select TOD set-mode, not alarm-set-mode),
    then writes hours, minutes, seconds, tenths in that order.  Writing
    tenths unlatches and starts the counter per the 6526 datasheet.
    """
    a.emit(0xAD, CIA1_CRB & 0xFF, CIA1_CRB >> 8)   # LDA $DC0F
    a.emit(0x29, 0x7F)                              # AND #$7F
    a.emit(0x8D, CIA1_CRB & 0xFF, CIA1_CRB >> 8)   # STA $DC0F
    a.emit(0xA9, 0x00)                              # LDA #0
    a.emit(0x8D, CIA1_TOD_HR & 0xFF, CIA1_TOD_HR >> 8)
    a.emit(0x8D, CIA1_TOD_MIN & 0xFF, CIA1_TOD_MIN >> 8)
    a.emit(0x8D, CIA1_TOD_SEC & 0xFF, CIA1_TOD_SEC >> 8)
    a.emit(0x8D, CIA1_TOD_TENTHS & 0xFF, CIA1_TOD_TENTHS >> 8)


def _emit_sec_table(a: Asm, label: str) -> None:
    """Emit a split 8-entry ``tens*100`` LE16 lookup table.

    Layout:
        label:      <8 low bytes>   (i=0..7  -> (i*100) & 0xFF)
        label + 8:  <8 high bytes>  (i=0..7  -> ((i*100) >> 8) & 0xFF)

    Six entries would suffice (BCD seconds tens are 0..5) but we round
    up to 8 so the opcode sequence doesn't need a range check.
    """
    a.label(label)
    for i in range(8):
        v = i * 100
        a.emit(v & 0xFF)
    for i in range(8):
        v = i * 100
        a.emit((v >> 8) & 0xFF)


def _emit_ones_table(a: Asm, label: str) -> None:
    """Emit a 10-byte ones*10 lookup table: 0, 10, 20, ... 90."""
    a.label(label)
    for i in range(10):
        a.emit(i * 10)


def _emit_read_current_tenths(a: Asm) -> list[int]:
    """Emit code that reads CIA1 TOD and stores elapsed tenths in
    ``$F0/$F1`` (LE16).

    On entry: TOD was started at 00:00:00.0 via ``_emit_tod_start``.
    On exit:  ``$F0/$F1`` = ``seconds*10 + tenths`` (0..599), or
              ``$FFFF`` if minutes > 0 (which means the deadline of
              at most 59.9s has already been exceeded).

    The emitted code contains three ``LDA abs,X`` instructions whose
    absolute operands are **placeholders** -- they must be patched
    after :meth:`Asm.build` to point at the ``sec_tab`` and
    ``ones_tab`` tables that the caller emits elsewhere in the same
    buffer.  This function returns a list of byte offsets
    ``[sec_lo_pos, sec_hi_pos, ones_pos]`` where the three operand
    low-bytes live.
    """
    # LDA $DC0B -- latch
    a.emit(0xAD, CIA1_TOD_HR & 0xFF, CIA1_TOD_HR >> 8)
    # LDA $DC0A -- minutes BCD; BEQ if zero
    a.emit(0xAD, CIA1_TOD_MIN & 0xFF, CIA1_TOD_MIN >> 8)
    a.branch(0xF0, "_tod_min_ok")
    # minutes > 0: drain latch (seconds, tenths), force result = $FFFF
    a.emit(0xAD, CIA1_TOD_SEC & 0xFF, CIA1_TOD_SEC >> 8)
    a.emit(0xAD, CIA1_TOD_TENTHS & 0xFF, CIA1_TOD_TENTHS >> 8)
    a.emit(0xA9, 0xFF, 0x85, ZP_CUR_LO)
    a.emit(0xA9, 0xFF, 0x85, ZP_CUR_HI)
    a.jmp("_tod_read_done")

    a.label("_tod_min_ok")
    # Read seconds (BCD, $SB: S=tens, B=ones)
    a.emit(0xAD, CIA1_TOD_SEC & 0xFF, CIA1_TOD_SEC >> 8)
    a.emit(0x85, ZP_RAW)           # STA $F5
    a.emit(0x29, 0x0F)             # AND #$0F -- ones nibble
    a.emit(0x85, ZP_ONES)          # STA $F4 (ones, 0..9)
    a.emit(0xA5, ZP_RAW)           # LDA $F5
    a.emit(0x4A)                    # LSR
    a.emit(0x4A)
    a.emit(0x4A)
    a.emit(0x4A)                    # A = tens (0..5)
    a.emit(0xAA)                    # TAX

    # LDA sec_tab_lo,X / STA $F0
    sec_lo_pos = a.pos + 1
    a.emit(0xBD, 0x00, 0x00)
    a.emit(0x85, ZP_CUR_LO)
    # LDA sec_tab_hi,X / STA $F1
    sec_hi_pos = a.pos + 1
    a.emit(0xBD, 0x00, 0x00)
    a.emit(0x85, ZP_CUR_HI)

    # Add ones*10 via LDX $F4 / LDA ones_tab,X
    a.emit(0xA6, ZP_ONES)           # LDX $F4
    ones_pos = a.pos + 1
    a.emit(0xBD, 0x00, 0x00)        # LDA ones_tab,X
    a.emit(0x18)                     # CLC
    a.emit(0x65, ZP_CUR_LO)          # ADC $F0
    a.emit(0x85, ZP_CUR_LO)
    a.emit(0x90, 0x02)               # BCC +2
    a.emit(0xE6, ZP_CUR_HI)          # INC $F1

    # Add tenths (single nibble, 0..9) by reading $DC08
    a.emit(0xAD, CIA1_TOD_TENTHS & 0xFF, CIA1_TOD_TENTHS >> 8)
    a.emit(0x29, 0x0F)
    a.emit(0x18)
    a.emit(0x65, ZP_CUR_LO)
    a.emit(0x85, ZP_CUR_LO)
    a.emit(0x90, 0x02)
    a.emit(0xE6, ZP_CUR_HI)

    a.label("_tod_read_done")
    return [sec_lo_pos, sec_hi_pos, ones_pos]


def _patch_abs_operands(
    buf: bytearray,
    positions: list[int],
    targets: list[int],
) -> None:
    """Patch ``abs,X`` operand slots in ``buf`` with absolute addresses.

    Each position is the byte index of the operand low-byte (the byte
    immediately after the opcode).  The corresponding target is the
    absolute address the instruction should reference.
    """
    assert len(positions) == len(targets)
    for pos, tgt in zip(positions, targets):
        buf[pos] = tgt & 0xFF
        buf[pos + 1] = (tgt >> 8) & 0xFF


def _finalize_with_tables(
    a: Asm,
    load_addr: int,
    patch_positions: list[int],
) -> bytes:
    """Emit the lookup tables, build the Asm buffer, then patch the
    three ``LDA abs,X`` operands to point at the tables.

    Must be called after ``_emit_read_current_tenths`` but before the
    caller returns.  The caller must have already emitted the terminal
    RTS / success / timeout sequences before invoking this helper.
    """
    _emit_sec_table(a, "sec_tab")
    _emit_ones_table(a, "ones_tab")
    raw = a.build()
    sec_tab_addr = load_addr + a.labels["sec_tab"]
    ones_tab_addr = load_addr + a.labels["ones_tab"]
    buf = bytearray(raw)
    _patch_abs_operands(
        buf,
        patch_positions,
        [sec_tab_addr, sec_tab_addr + 8, ones_tab_addr],
    )
    return bytes(buf)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_tod_start_code(load_addr: int) -> bytes:
    """Build a standalone 6502 routine that starts CIA1 TOD at 00:00:00.0.

    Clears ``$DC0F`` bit 7 (select TOD write mode, not alarm mode),
    then writes hours -> minutes -> seconds -> tenths as zeros.  Writing
    tenths unlatches and starts the counter.  Safe to call multiple
    times; each call resets TOD to zero.  Returns via RTS.

    Args:
        load_addr: Address where the routine will live in C64 memory.

    Returns:
        Raw 6502 bytes ready to ``load_code()`` / ``jsr()``.
    """
    a = Asm(org=load_addr)
    a.emit(0x78)   # SEI -- TOD writes must be atomic w.r.t. IRQ
    _emit_tod_start(a)
    a.emit(0x58)   # CLI
    a.emit(0x60)   # RTS
    return a.build()


def build_tod_read_tenths_code(load_addr: int, result_addr: int) -> bytes:
    """Build a 6502 routine that reads CIA1 TOD and stores "elapsed
    tenths since TOD start" at ``result_addr`` as a little-endian
    16-bit value.

    Expects TOD to have been started at 00:00:00.0 beforehand (call
    :func:`build_tod_start_code` first).  The stored value is
    ``seconds*10 + tenths`` (range 0..599) or ``$FFFF`` if CIA1 TOD
    reports ``minutes > 0`` (meaning more than 59.9s have elapsed
    since start).

    Uses ZP ``$F0``-``$F5`` as scratch.  Returns via RTS.

    Read protocol (per 6526 datasheet): reading ``$DC0B`` latches the
    register bank; subsequent reads return latched values until
    ``$DC08`` (tenths) is read, which unlatches.  This routine reads
    in the required order: HR (latch & discard), MIN, SEC, TENTHS.
    """
    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    patch_positions = _emit_read_current_tenths(a)
    a.emit(0xA5, ZP_CUR_LO)
    a.emit(0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0xA5, ZP_CUR_HI)
    a.emit(0x8D, (result_addr + 1) & 0xFF, ((result_addr + 1) >> 8) & 0xFF)
    a.emit(0x58)  # CLI
    a.emit(0x60)  # RTS
    return _finalize_with_tables(a, load_addr, patch_positions)


def build_poll_with_tod_deadline_code(
    load_addr: int,
    peek_check_snippet: bytes,
    result_addr: int,
    deadline_tenths: int,
) -> bytes:
    """Build a poll loop that calls a device-specific "ready?" check and
    also watches CIA1 TOD for a deadline.

    Pseudo-code:

    .. code-block:: text

        SEI
        start TOD at 00:00:00.0
        $F2/$F3 = deadline_tenths (stored for observability)
    poll_top:
        <peek_check_snippet>       ; user-provided; Z=0 means "ready"
        BNE ready
        read TOD -> $F0/$F1        ; elapsed tenths
        16-bit SBC ($F0/$F1 - $F2/$F3)
        BCC poll_top               ; elapsed < deadline -> keep polling
        store $FF at result_addr   ; timeout
        CLI / RTS
    ready:
        store $01 at result_addr
        CLI / RTS

    The ``peek_check_snippet`` design lets the same poll core drive a
    CS8900a RxEvent register, a UCI response-ready bit, or any other
    device probe: the snippet is raw 6502 bytes that must leave the
    Z flag clear when the device is ready to proceed.

    Args:
        load_addr: Where the routine will live in C64 memory.
        peek_check_snippet: Raw, position-independent 6502 bytes that
            leave ``Z=0`` when ready.  Typical shape:
            ``LDA <device_reg> / AND #<mask>``.  Must not modify
            ``$F0``-``$F5`` and must not branch outside itself.
        result_addr: 1-byte output slot.  ``$01`` on ready, ``$FF`` on
            timeout.
        deadline_tenths: Timeout in tenths-of-a-second (1..599).

    Returns:
        Raw 6502 bytes.

    Raises:
        ValueError: If ``deadline_tenths`` is out of range.
    """
    if not (1 <= deadline_tenths <= MAX_DEADLINE_TENTHS):
        raise ValueError(
            f"deadline_tenths must be in 1..{MAX_DEADLINE_TENTHS} "
            f"(got {deadline_tenths}); longer waits require a caller loop"
        )

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_tod_start(a)

    # Store deadline at $F2/$F3 (for debugger observability + compare)
    a.emit(0xA9, deadline_tenths & 0xFF, 0x85, ZP_DEADLINE_LO)
    a.emit(0xA9, (deadline_tenths >> 8) & 0xFF, 0x85, ZP_DEADLINE_HI)

    a.label("poll_top")
    # Inline caller-provided ready check; Z=0 -> ready.
    for b in peek_check_snippet:
        a.emit(b)
    a.branch(0xD0, "ready")

    patch_positions = _emit_read_current_tenths(a)

    # 16-bit compare: elapsed ($F0/$F1) - deadline ($F2/$F3).
    # If elapsed >= deadline, C=1 after final SBC -> fall through to timeout.
    a.emit(0xA5, ZP_CUR_LO)
    a.emit(0x38)                         # SEC
    a.emit(0xE5, ZP_DEADLINE_LO)         # SBC $F2
    a.emit(0xA5, ZP_CUR_HI)
    a.emit(0xE5, ZP_DEADLINE_HI)         # SBC $F3
    a.branch(0x90, "poll_top")           # BCC -> elapsed < deadline

    # Timeout
    a.emit(0xA9, 0xFF)
    a.emit(0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("ready")
    a.emit(0xA9, 0x01)
    a.emit(0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    return _finalize_with_tables(a, load_addr, patch_positions)


__all__ = [
    "MAX_DEADLINE_TENTHS",
    "ZP_CUR_LO",
    "ZP_CUR_HI",
    "ZP_DEADLINE_LO",
    "ZP_DEADLINE_HI",
    "build_tod_start_code",
    "build_tod_read_tenths_code",
    "build_poll_with_tod_deadline_code",
]
