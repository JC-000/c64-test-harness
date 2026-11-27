"""Cassette-port helpers that need no device: TAP files and a pulse stimulus (issue #584).

Three things live here, all pure Python:

* **TAP files** (``C64-TAPE-RAW``): :func:`build_tap`, :func:`parse_tap`,
  :func:`build_tap_from_prg` (a PRG encoded the way the KERNAL's ``SAVE`` to
  device 1 writes it) and :func:`decode_tap_blocks` (the reverse, for
  checking what a capture recorded).
* **A known program to load from tape**: :func:`tape_test_program`.
* **A C64-side stimulus for a capture**: :func:`build_tape_pulse_stimulus`,
  a routine that puts a known number of pulses of one known length on the
  cassette write line, and :func:`run_tape_stimulus`, which starts it and
  waits it out.

Driving an Ultimate's tape recorder and TAP player is in
:mod:`c64_test_harness.backends.ultimate64_tape`.

**The cassette buffer is the KERNAL's during tape I/O.** ``LOAD`` and
``SAVE`` on device 1 keep the tape header in ``$033C-$03FB``, where the
harness has scratch of its own (the liveness probe at ``$0334-$03B3``, the
U64 ``jsr()`` trampoline at ``$0360``, the VICE REU park at ``$03C0``). So
nothing here uses those helpers: programs are started with a typed ``SYS``
(:func:`~c64_test_harness.execute.run_prg_via_sys`), never with ``jsr()``.
:func:`cassette_buffer_reserved` makes the transport's ``MemoryPolicy``
refuse host writes to the buffer for as long as a tape operation runs. It
adds a reserved region and removes nothing, and the helpers here never pass
``override=``.

**No memory or screen reads while the 6510 does cassette I/O on an
Ultimate** (#573): a REST read stops the CPU while the tape hardware and
the CIA timers run on. A read during a ``LOAD`` stretches the pulse the
KERNAL is timing; a read during the stimulus stretches the pulse it is
writing. The helpers wait a computed time and read afterwards.

Evidence grade: the TAP encoding and the stimulus are checked offline only
(a decoder and a cycle-counting 6502 model in ``tests/test_tape.py``).
Nothing here has been loaded by a real KERNAL or recorded by a device yet.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .memory_policy import MemoryRegion
from .verify import PrgFile

__all__ = [
    "CASSETTE_BUFFER",
    "NTSC_CLOCK_HZ",
    "PAL_CLOCK_HZ",
    "STIMULUS_DONE",
    "TAP_HEADER_LEN",
    "TAP_LONG",
    "TAP_MEDIUM",
    "TAP_SHORT",
    "TAP_SIGNATURE",
    "TapFile",
    "TapeBlock",
    "TapeStimulus",
    "build_tap",
    "build_tap_from_prg",
    "build_tape_pulse_stimulus",
    "cassette_buffer_reserved",
    "decode_tap_blocks",
    "parse_tap",
    "run_tape_stimulus",
    "tape_test_program",
]

TAP_SIGNATURE = b"C64-TAPE-RAW"
#: Signature (12), version (1), three reserved bytes, data length (4, LE).
TAP_HEADER_LEN = 20

#: The KERNAL's three pulse lengths as TAP bytes (units of 8 CPU cycles).
TAP_SHORT = 0x30
TAP_MEDIUM = 0x42
TAP_LONG = 0x56

#: PAL and NTSC CPU clocks in Hz, for turning cycles into seconds.
PAL_CLOCK_HZ = 985248
NTSC_CLOCK_HZ = 1022727

#: The KERNAL's tape buffer, ``$033C-$03FB``, as a ``MemoryRegion``.
CASSETTE_BUFFER = MemoryRegion(
    0x033C, 0x03FC, "KERNAL cassette buffer, in use by a tape operation (#584)")

_HEADER_PILOT = 0x6A00   # short pulses before a header block
_DATA_PILOT = 0x1500     # short pulses before a data block
_REPEAT_GAP = 0x4F       # short pulses between the two copies of a block
_TRAILER = 0x4E          # short pulses after the second copy
_HEADER_PAYLOAD_LEN = 192


# ---------------------------------------------------------------------------
# TAP container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TapFile:
    """A parsed TAP file.

    ``declared_length`` is the header's length field; ``data`` is everything
    after the 20-byte header, whatever the field says.
    """

    version: int
    declared_length: int
    data: bytes

    @property
    def length_matches(self) -> bool:
        """The header's length field equals the number of data bytes."""
        return self.declared_length == len(self.data)

    def pulses(self) -> list[int]:
        """Each pulse's length in CPU cycles.

        A non-zero byte is that many times 8 cycles. A zero byte is an
        overflow: in version 1 the next three bytes hold the cycle count
        (little-endian); in version 0 it stands for 256 * 8 cycles. A
        version-1 overflow cut short by the end of the data is dropped.
        """
        out: list[int] = []
        d = self.data
        i = 0
        while i < len(d):
            b = d[i]
            i += 1
            if b:
                out.append(b * 8)
            elif self.version >= 1:
                if i + 3 > len(d):
                    break
                out.append(d[i] | (d[i + 1] << 8) | (d[i + 2] << 16))
                i += 3
            else:
                out.append(256 * 8)
        return out

    def duration_seconds(self, clock_hz: int = PAL_CLOCK_HZ) -> float:
        """Playing time: the sum of all pulses at *clock_hz*."""
        return sum(self.pulses()) / clock_hz


def build_tap(data: bytes, *, version: int = 1) -> bytes:
    """A TAP file holding *data* as its pulse bytes."""
    if version not in (0, 1):
        raise ValueError(f"TAP version must be 0 or 1, not {version!r}")
    return (TAP_SIGNATURE + bytes([version, 0, 0, 0])
            + len(data).to_bytes(4, "little") + bytes(data))


def parse_tap(raw: bytes) -> TapFile:
    """Parse a TAP file.

    :raises ValueError: shorter than the 20-byte header, or the signature is
        not ``C64-TAPE-RAW``.
    """
    if len(raw) < TAP_HEADER_LEN:
        raise ValueError(f"a TAP file has a {TAP_HEADER_LEN}-byte header; got {len(raw)} bytes")
    if raw[:12] != TAP_SIGNATURE:
        raise ValueError(f"not a TAP file: signature {bytes(raw[:12])!r}")
    return TapFile(raw[12], int.from_bytes(raw[16:20], "little"), bytes(raw[TAP_HEADER_LEN:]))


# ---------------------------------------------------------------------------
# KERNAL tape encoding
# ---------------------------------------------------------------------------

def _encode_byte(value: int) -> bytes:
    """Byte marker (long, medium), eight bits LSB first, an odd-parity bit.

    A 0 bit is (short, medium), a 1 bit is (medium, short).
    """
    out = bytearray((TAP_LONG, TAP_MEDIUM))
    parity = 1
    for bit in range(8):
        if (value >> bit) & 1:
            out += bytes((TAP_MEDIUM, TAP_SHORT))
            parity ^= 1
        else:
            out += bytes((TAP_SHORT, TAP_MEDIUM))
    out += bytes((TAP_MEDIUM, TAP_SHORT)) if parity else bytes((TAP_SHORT, TAP_MEDIUM))
    return bytes(out)


def _encode_block(payload: bytes, pilot: int) -> bytes:
    """One block as the KERNAL writes it: pilot, the block, a gap, the block
    again, a trailer. Each copy is a countdown (``$89..$81`` first,
    ``$09..$01`` for the repeat), the payload, its XOR checksum, and an
    end-of-data marker (long, short)."""
    checksum = 0
    for b in payload:
        checksum ^= b
    out = bytearray([TAP_SHORT] * pilot)
    for first in (True, False):
        for n in range(9, 0, -1):
            out += _encode_byte(n | (0x80 if first else 0))
        for b in payload:
            out += _encode_byte(b)
        out += _encode_byte(checksum)
        out += bytes((TAP_LONG, TAP_SHORT))
        out += bytes([TAP_SHORT] * (_REPEAT_GAP if first else _TRAILER))
    return bytes(out)


def build_tap_from_prg(prg: PrgFile, *, name: str = "HARNESS",
                       file_type: int | None = None) -> bytes:
    """A TAP file from which the KERNAL loads *prg* (``LOAD`` on device 1).

    A header block and a data block, each written twice, as ``SAVE`` to
    device 1 does. *file_type* is the header's first byte: 1 (a relocatable
    BASIC program, loaded at the start of BASIC by a plain ``LOAD``) or 3
    (loaded at the address in the header). The default is 1 for a program
    at ``$0801`` and 3 otherwise.

    :raises ValueError: an empty program, a name over 16 characters or
        outside printable ASCII without lower case, or a *file_type* other
        than 1 or 3.
    """
    data = prg.data
    if not data:
        raise ValueError("cannot put an empty program on tape")
    if file_type is None:
        file_type = 1 if prg.load_address == 0x0801 else 3
    if file_type not in (1, 3):
        raise ValueError(f"file_type must be 1 or 3, not {file_type!r}")
    if len(name) > 16 or any(not (0x20 <= ord(c) <= 0x5F) for c in name):
        raise ValueError(
            f"tape name {name!r} must be at most 16 characters of upper-case ASCII")
    start, end = prg.load_address, prg.load_address + len(data)
    header = bytes([file_type, start & 0xFF, start >> 8, end & 0xFF, (end >> 8) & 0xFF])
    header += name.encode("ascii").ljust(16, b" ")
    header = header.ljust(_HEADER_PAYLOAD_LEN, b" ")
    return build_tap(_encode_block(header, _HEADER_PILOT) + _encode_block(data, _DATA_PILOT))


@dataclass(frozen=True)
class TapeBlock:
    """One copy of a block decoded from pulses.

    ``countdown`` is the nine sync bytes as read; ``payload`` excludes them
    and the checksum byte. ``checksum_ok`` and ``parity_errors`` are the
    decoder's own checks.
    """

    countdown: bytes
    payload: bytes
    checksum_ok: bool
    parity_errors: int

    @property
    def first_copy(self) -> bool:
        """This is the first of the block's two copies (countdown ``$89..$81``)."""
        return bool(self.countdown) and bool(self.countdown[0] & 0x80)


def _classify(tap_byte: int) -> str:
    """S, M or L by the midpoints between the KERNAL's three lengths."""
    if tap_byte < (TAP_SHORT + TAP_MEDIUM) // 2:
        return "S"
    if tap_byte < (TAP_MEDIUM + TAP_LONG) // 2:
        return "M"
    return "L"


def decode_tap_blocks(tap: TapFile) -> list[TapeBlock]:
    """Decode KERNAL-format blocks from a TAP's pulses.

    Reads byte markers (long, medium) and bit pairs until an end-of-data
    marker (long, short). Pulses outside a block (pilot, gaps, overflows)
    are skipped. A block shorter than countdown plus checksum is dropped.
    """
    kinds = "".join(_classify(c // 8) for c in tap.pulses())
    blocks: list[TapeBlock] = []
    current: list[int] = []
    parity_errors = 0
    i = 0
    n = len(kinds)
    while i + 1 < n:
        pair = kinds[i:i + 2]
        if pair == "LM" and i + 20 <= n:
            value = 0
            ones = 0
            ok = True
            for bit in range(9):
                p = kinds[i + 2 + 2 * bit:i + 4 + 2 * bit]
                if p == "MS":
                    ones += 1
                    if bit < 8:
                        value |= 1 << bit
                elif p != "SM":
                    ok = False
                    break
            if ok:
                current.append(value)
                if ones % 2 == 0:
                    parity_errors += 1
                i += 20
                continue
            i += 1
            continue
        if pair == "LS" and current:
            if len(current) > 10:
                payload = bytes(current[9:-1])
                checksum = 0
                for b in payload:
                    checksum ^= b
                blocks.append(TapeBlock(bytes(current[:9]), payload,
                                        checksum == current[-1], parity_errors))
            current = []
            parity_errors = 0
            i += 2
            continue
        i += 1
    return blocks


# ---------------------------------------------------------------------------
# A known program
# ---------------------------------------------------------------------------

def tape_test_program(lines: int = 6, *, text: str = "C64-TEST-HARNESS TAPE") -> PrgFile:
    """A valid BASIC program of ``REM`` lines at ``$0801``, to load from tape.

    A plain ``LOAD`` relinks the program's line pointers afterwards, which
    rewrites them in RAM. On a valid program the relink writes the values
    already there, so the bytes still compare equal; arbitrary bytes at
    ``$0801`` would not survive it.
    """
    if not 1 <= lines <= 100:
        raise ValueError("lines must be 1..100")
    if not text or any(not (0x20 <= ord(c) <= 0x5F) for c in text) or len(text) > 60:
        raise ValueError("text must be 1..60 characters of upper-case ASCII")
    out = bytearray()
    addr = 0x0801
    for i in range(lines):
        body = bytes([0x8F, 0x20]) + f"{text} {i:02d}".encode("ascii") + b"\x00"
        nxt = addr + 4 + len(body)
        number = 10 * (i + 1)
        out += bytes([nxt & 0xFF, nxt >> 8, number & 0xFF, number >> 8]) + body
        addr = nxt
    out += b"\x00\x00"
    return PrgFile(0x0801, bytes(out))


# ---------------------------------------------------------------------------
# The cassette buffer guard
# ---------------------------------------------------------------------------

@contextmanager
def cassette_buffer_reserved(transport: Any) -> Iterator[None]:
    """Reserve ``$033C-$03FB`` in *transport*'s ``MemoryPolicy`` for the block.

    While a ``LOAD`` or ``SAVE`` on device 1 runs, a host write into the
    cassette buffer corrupts the header the KERNAL keeps there. Inside the
    block, a ``transport.write_memory`` that touches the buffer raises
    ``MemoryPolicyError`` before anything is sent: the U64 ``jsr()``
    trampoline (``$0360``) and the VICE REU park (``$03C0``) go through it.
    The previous policy is put back on exit.

    It cannot stop a write that bypasses the transport: ``liveness_probe``
    and ``probe_u64(check_write=True)`` use the raw REST client. Do not call
    them during a tape operation. A transport without a ``memory_policy``
    attribute is left alone.
    """
    previous = getattr(transport, "memory_policy", None)
    if previous is None:
        yield
        return
    transport.memory_policy = previous.with_reserved(CASSETTE_BUFFER)
    try:
        yield
    finally:
        transport.memory_policy = previous


# ---------------------------------------------------------------------------
# The pulse stimulus
# ---------------------------------------------------------------------------

#: What the routine writes to :attr:`TapeStimulus.done_address` when it has finished.
STIMULUS_DONE = 0xA5

# Cycles one period costs besides the two delay loops and the pad: the two
# edge writes (8 each), two LDX # (2 each), the loops' final not-taken
# branches (-1 each), and the constant-time 16-bit count (32).
_STIMULUS_FIXED_CYCLES = 50
# Pad cycles by (period - fixed) mod 5. One cycle cannot be bought, so a
# remainder of 1 takes six and one loop iteration (five) less.
_PAD_FOR_REMAINDER = {0: 0, 1: 6, 2: 2, 3: 3, 4: 4}

_BASIC_STUB_LEN = 12  # 10 SYS2061


@dataclass(frozen=True)
class TapeStimulus:
    """A pulse routine from :func:`build_tape_pulse_stimulus`.

    ``prg`` is the whole PRG (load address first). ``pulses`` rising edges
    are written, ``period_cycles`` apart, so a recorder that times rising
    edges sees ``pulses - 1`` intervals of ``tap_value``. ``done_address``
    reads :data:`STIMULUS_DONE` once the routine has returned.
    """

    prg: bytes
    load_address: int
    sys_address: int
    done_address: int
    pulses: int
    tap_value: int
    period_cycles: int

    def duration_seconds(self, clock_hz: int = PAL_CLOCK_HZ) -> float:
        """How long the pulse train lasts at *clock_hz*, without the up to
        two video frames the routine waits before its first pulse."""
        return self.pulses * self.period_cycles / clock_hz


def build_tape_pulse_stimulus(pulses: int = 2000, *, tap_value: int = TAP_SHORT,
                              load_address: int = 0x0801) -> TapeStimulus:
    """A routine that writes *pulses* pulses on the cassette write line.

    Each period is exactly ``tap_value * 8`` CPU cycles, so a recorder that
    times one edge of the write line (``$01`` bit 3) stores *tap_value* for
    every interval. The routine sets ``SEI``, turns sprites off and blanks
    the screen (then waits for the next frame) so the VIC steals no cycles,
    drives the line low, toggles bit 3 of ``$01`` and leaves every other bit
    as it found it, restores ``$D011``, ``$D015`` and ``$01`` (if bit 3 was
    set on entry, which is not the KERNAL's default, putting it back is one
    more rising edge after the last pulse), stores :data:`STIMULUS_DONE`
    at ``done_address``, and returns with ``CLI`` / ``RTS``. It keeps its
    counters inside itself: it touches no zero page besides ``$01`` and
    nothing in the cassette buffer. It does not drive the motor line or read
    the sense line; add that in your own program if a recorder needs it.

    With the default *load_address* ``$0801`` the PRG starts with a BASIC
    stub (``10 SYS2061``) so :func:`run_tape_stimulus` and ``RUN`` both
    start it. At any other address the PRG is the bare routine and
    ``sys_address`` is its first byte.

    :raises ValueError: *pulses* outside 1..65535, *tap_value* outside
        8..255, or a *load_address* that puts the routine across a page
        boundary (a branch that crosses a page costs an extra cycle).
    """
    for label, value in (("pulses", pulses), ("tap_value", tap_value),
                         ("load_address", load_address)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be an int, not {value!r}")
    if not 1 <= pulses <= 0xFFFF:
        raise ValueError(f"pulses must be 1..65535, not {pulses}")
    if not 8 <= tap_value <= 0xFF:
        raise ValueError(f"tap_value must be 8..255, not {tap_value}")
    period = tap_value * 8
    spare = period - _STIMULUS_FIXED_CYCLES
    pad = _PAD_FOR_REMAINDER[spare % 5]
    loops = (spare - pad) // 5
    h1, h2 = loops // 2, loops - loops // 2

    stub = b""
    if load_address == 0x0801:
        stub = bytes([0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061" + b"\x00\x00\x00"
        assert len(stub) == _BASIC_STUB_LEN
    base = load_address + len(stub)

    def assemble(data_at: int) -> bytes:
        cnt_lo, cnt_hi, save01, save11, save15, done = range(data_at, data_at + 6)

        def a(addr: int) -> bytes:
            return bytes((addr & 0xFF, addr >> 8))

        c = bytearray()
        c += b"\x78"                                              # SEI
        c += bytes((0xA9, pulses & 0xFF, 0x8D)) + a(cnt_lo)       # LDA #<n / STA cnt_lo
        c += bytes((0xA9, pulses >> 8, 0x8D)) + a(cnt_hi)         # LDA #>n / STA cnt_hi
        c += bytes((0xA9, 0x00, 0x8D)) + a(done)                  # LDA #0  / STA done
        c += b"\xAD\x15\xD0\x8D" + a(save15)                      # LDA $D015 / STA save15
        c += b"\xA9\x00\x8D\x15\xD0"                              # LDA #0 / STA $D015
        c += b"\xAD\x11\xD0\x8D" + a(save11)                      # LDA $D011 / STA save11
        c += b"\x29\xEF\x8D\x11\xD0"                              # AND #$EF / STA $D011
        c += b"\x2C\x11\xD0\x10\xFB"                              # BIT $D011 / BPL *-3
        c += b"\x2C\x11\xD0\x30\xFB"                              # BIT $D011 / BMI *-3
        c += b"\xA5\x01\x8D" + a(save01)                          # LDA $01 / STA save01
        c += b"\x29\xF7\x85\x01"                                  # AND #$F7 / STA $01 (line low)
        loop = len(c)
        c += b"\xA5\x01\x09\x08\x85\x01"                          # LDA $01 / ORA #$08 / STA $01
        c += bytes((0xA2, h1, 0xCA, 0xD0, 0xFD))                  # LDX #h1 / DEX / BNE *-1
        c += b"\xA5\x01\x29\xF7\x85\x01"                          # LDA $01 / AND #$F7 / STA $01
        c += bytes((0xA2, h2, 0xCA, 0xD0, 0xFD))                  # LDX #h2 / DEX / BNE *-1
        if pad == 3:
            c += b"\x4C" + a(base + len(c) + 3)                   # JMP next (3 cycles)
        else:
            c += b"\xEA" * (pad // 2)                             # NOP (2 cycles each)
        c += b"\xAD" + a(cnt_lo)                                  # LDA cnt_lo
        c += b"\xD0\x06"                                          # BNE no_borrow
        c += b"\xCE" + a(cnt_hi)                                  # DEC cnt_hi
        c += b"\x4C" + a(base + len(c) + 3 + 4)                   # JMP count
        c += b"\xEA\xEA\xEA\xEA"                                  # no_borrow: 4 x NOP
        c += b"\xCE" + a(cnt_lo)                                  # count: DEC cnt_lo
        c += b"\xAD" + a(cnt_lo)                                  # LDA cnt_lo
        c += b"\x0D" + a(cnt_hi)                                  # ORA cnt_hi
        c += bytes((0xD0, (loop - (len(c) + 2)) & 0xFF))          # BNE loop
        c += b"\xAD" + a(save01) + b"\x85\x01"                    # LDA save01 / STA $01
        c += b"\xAD" + a(save11) + b"\x8D\x11\xD0"                # LDA save11 / STA $D011
        c += b"\xAD" + a(save15) + b"\x8D\x15\xD0"                # LDA save15 / STA $D015
        c += bytes((0xA9, STIMULUS_DONE, 0x8D)) + a(done)         # LDA #done / STA done
        c += b"\x58\x60"                                          # CLI / RTS
        return bytes(c)

    code = assemble(0)
    data_at = base + len(code)
    code = assemble(data_at)
    assert base + len(code) == data_at
    if base >> 8 != (data_at - 1) >> 8:
        raise ValueError(
            f"the routine would span ${base:04X}-${data_at - 1:04X}, across a page "
            "boundary; a branch across it costs a cycle and the pulses would no longer "
            "be exact. Choose another load_address.")
    if data_at + 6 > 0x10000:
        raise ValueError(f"the routine does not fit below $FFFF at ${load_address:04X}")
    body = stub + code + bytes(6)
    prg = bytes((load_address & 0xFF, load_address >> 8)) + body
    return TapeStimulus(prg, load_address, base, data_at + 5, pulses, tap_value, period)


def run_tape_stimulus(target: Any, stimulus: TapeStimulus, *, reset: bool = False,
                      clock_hz: int = PAL_CLOCK_HZ, margin: float = 1.0,
                      sleep=time.sleep) -> bool:
    """Start *stimulus* with a typed ``SYS``, wait it out, and say whether it finished.

    The program is written with ``run_prg_via_sys`` (through the
    transport's ``MemoryPolicy``, no override) and started from the
    keyboard, so no harness scratch in the cassette buffer is used. Then
    this sleeps for the pulse train's length plus *margin* seconds and only
    afterwards reads ``done_address``.

    #573: nothing is read while the pulses are written. On an Ultimate a
    REST read stops the 6510, and a recorder that times the write line in
    CPU clock ticks would store that pulse too long.

    Pass ``reset=True`` unless the machine is at a BASIC prompt. On VICE
    the machine runs after this call (``run_prg_via_sys`` resumes it) and is
    left halted by the final read, like any monitor read.

    :returns: ``True`` when ``done_address`` reads :data:`STIMULUS_DONE`.
    """
    from .execute import run_prg_via_sys
    from .memory import read_bytes

    run_prg_via_sys(target, stimulus.prg, sys_addr=stimulus.sys_address, reset=reset)
    # #573: wait the pulse train out; no read until it has finished.
    sleep(stimulus.duration_seconds(clock_hz) + 0.05 + margin)
    transport = getattr(target, "transport", target)
    return read_bytes(transport, stimulus.done_address, 1)[0] == STIMULUS_DONE
