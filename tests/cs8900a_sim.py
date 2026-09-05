"""A minimal 6502 interpreter plus a behavioural CS8900a (RR-Net) model.

Test infrastructure for running the ``bridge_ping`` code builders' output
*functionally* without VICE: feed the simulated chip a frame, run the
routine, and look at what it transmitted.  The two-VICE bridge suite
needs root VICE, sudo'd bridge scripts and an idle bench; this needs
nothing, so it can sit in the default suite.

Scope is deliberately narrow:

* **CPU**: the opcodes the builders emit (and nothing else -- an unknown
  opcode raises, so a builder that starts using a new instruction fails
  loudly here rather than mis-executing).  No cycle counting, no decimal
  mode, no interrupts (``SEI``/``CLI`` are no-ops).
* **Chip**: the register interface the routines drive -- clockport bit,
  PPPtr/PPData for RxEvent (PP ``0x0124``), BusST (PP ``0x0138``), RxCFG
  (PP ``0x0102``, SkipNow), the RTDATA FIFO in both directions, TxCMD and
  TxLength.  RX frames are queued by the test; a TX is complete when
  ``TxLength`` bytes have been written to RTDATA.

  The RX FIFO is a **byte stream in the documented read order**:
  ``RxStatus`` high, ``RxStatus`` low, ``RxLength`` high, ``RxLength``
  low, then the body bytes in wire order.  Every RTDATA read -- from
  either half register -- pops the next byte.  So the harness reader
  (header high-half-first, body low-half-first; issue #210, ip65's order)
  observes the right words and the frame in wire order, while a reader
  that takes the header low-half-first observes byte-swapped ``RxStatus``
  / ``RxLength`` (``tests/test_cs8900a_sim.py`` pins both).  That is the
  extent of the claim: the #210 report says real silicon also shifts the
  *body* by a byte after a wrong-order header, and this model does not
  reproduce that -- the order itself stays pinned structurally by
  ``tests/test_cs8900a_frame_reader.py`` and the hash pins.  Reads past
  the end of the frame return ``0`` until SkipNow (measured, #210).

  **Not modelled** -- nothing run here is evidence about these:
  RxEvent is not read-to-clear (it reports "frame pending" until the
  frame is skipped); the TxCMD / TxLength-before-data ordering is not
  enforced (data written before TxLength is discarded when TxLength is
  written, no error); ``Rdy4TxNOW`` is always set, so a routine that
  never polls BusST still transmits; there is no acceptance filter --
  RxCTL, LineCTL and the IA are accepted and ignored, so every queued
  frame is "received" whatever its destination address.
* **CIA1 TOD** (``$DC08-$DC0B``) reads as ``00:00:00.0`` forever, so a
  TOD-deadline routine never times out; ``$DC0F`` is plain RAM.

Everything else is 64 KiB of flat RAM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# CS8900a RR-Net register window
_ISQ_LO, _ISQ_HI = 0xDE00, 0xDE01
_PPTR_LO, _PPTR_HI = 0xDE02, 0xDE03
_PPDATA_LO, _PPDATA_HI = 0xDE04, 0xDE05
_RTDATA_LO, _RTDATA_HI = 0xDE08, 0xDE09
_TXCMD_LO, _TXCMD_HI = 0xDE0C, 0xDE0D
_TXLEN_LO, _TXLEN_HI = 0xDE0E, 0xDE0F

PP_RXCFG = 0x0102
PP_RXEVENT = 0x0124
PP_BUSST = 0x0138

RXEVENT_RXOK = 0x0100
BUSST_RDY4TXNOW = 0x0100


class SimError(RuntimeError):
    """The routine did something the simulator does not model."""


@dataclass
class Cs8900aSim:
    """Behavioural CS8900a: queue RX frames in, collect TX frames out."""

    rx_queue: list[bytes] = field(default_factory=list)
    tx_frames: list[bytes] = field(default_factory=list)
    pptr: int = 0
    rxcfg: int = 0x0003            # reset value: register number 3
    txcmd: int = 0
    txlen: int = 0
    clockport: int = 0
    # Current RX frame being drained: byte stream in documented read order
    # (status hi, status lo, length hi, length lo, body...); see the
    # module docstring.
    _rx_stream: list[int] = field(default_factory=list)
    _rx_pos: int = 0
    _tx_buf: bytearray = field(default_factory=bytearray)
    _pending_hi: dict[int, int] = field(default_factory=dict)
    #: Every RTDATA read, in order, as (register, value) -- lets a test
    #: assert the #210 half ordering directly.
    rtdata_reads: list[tuple[int, int]] = field(default_factory=list)

    # -- RX side -------------------------------------------------------
    def _start_frame(self) -> None:
        if self._rx_stream or not self.rx_queue:
            return
        frame = self.rx_queue.pop(0)
        status = RXEVENT_RXOK
        length = len(frame)
        # Byte stream in the documented read order: each header word high
        # half first, then the body in wire order.  A reader pops one byte
        # per RTDATA read whichever half register it uses, so only a
        # reader that follows this order sees the right values.
        body = list(frame)
        if len(body) % 2:
            body.append(0)
        self._rx_stream = [status >> 8, status & 0xFF, length >> 8, length & 0xFF] + body
        self._rx_pos = 0

    def _rtdata_read(self, reg: int) -> int:
        self._start_frame()
        if self._rx_pos >= len(self._rx_stream):
            value = 0                       # past the end of the frame
        else:
            value = self._rx_stream[self._rx_pos]
            self._rx_pos += 1
        self.rtdata_reads.append((reg, value))
        return value

    def _skip_now(self) -> None:
        # SkipNow discards the frame at the head of the receive buffer
        # whether or not any of it has been read: a "blind" SkipNow after
        # polling RxEvent releases a queued-but-unread frame on silicon
        # (issues #219, #222 -- the drain in _emit_drain_rx relies on it).
        if not self._rx_stream and self.rx_queue:
            self.rx_queue.pop(0)
        self._rx_stream = []
        self._rx_pos = 0

    def _rx_event(self) -> int:
        if self._rx_stream or self.rx_queue:
            return RXEVENT_RXOK
        return 0

    # -- register interface -------------------------------------------
    def read(self, addr: int) -> int:
        if addr == _ISQ_HI:
            return self.clockport
        if addr == _ISQ_LO:
            return 0
        if addr == _PPTR_LO:
            return self.pptr & 0xFF
        if addr == _PPTR_HI:
            return self.pptr >> 8
        if addr in (_PPDATA_LO, _PPDATA_HI):
            word = self._pp_read(self.pptr)
            return (word & 0xFF) if addr == _PPDATA_LO else (word >> 8) & 0xFF
        if addr in (_RTDATA_LO, _RTDATA_HI):
            return self._rtdata_read(addr)
        if addr in (_TXCMD_LO, _TXCMD_HI, _TXLEN_LO, _TXLEN_HI):
            return 0
        raise SimError(f"unmodelled CS8900a read at ${addr:04X}")

    def write(self, addr: int, value: int) -> None:
        if addr == _ISQ_HI:
            self.clockport = value & 0x01
            return
        if addr == _PPTR_LO:
            self.pptr = (self.pptr & 0xFF00) | value
            return
        if addr == _PPTR_HI:
            self.pptr = (self.pptr & 0x00FF) | (value << 8)
            return
        if addr == _PPDATA_LO:
            self._pp_write_half(self.pptr, value, high=False)
            return
        if addr == _PPDATA_HI:
            self._pp_write_half(self.pptr, value, high=True)
            return
        if addr == _TXCMD_LO:
            self.txcmd = (self.txcmd & 0xFF00) | value
            return
        if addr == _TXCMD_HI:
            self.txcmd = (self.txcmd & 0x00FF) | (value << 8)
            return
        if addr == _TXLEN_LO:
            self.txlen = (self.txlen & 0xFF00) | value
            self._tx_buf = bytearray()
            return
        if addr == _TXLEN_HI:
            self.txlen = (self.txlen & 0x00FF) | (value << 8)
            self._tx_buf = bytearray()
            return
        if addr in (_RTDATA_LO, _RTDATA_HI):
            self._tx_write(addr, value)
            return
        raise SimError(f"unmodelled CS8900a write at ${addr:04X}")

    def _pp_read(self, pp: int) -> int:
        if not self.clockport:
            return 0                      # chip is invisible without it
        if pp == PP_RXEVENT:
            return self._rx_event()
        if pp == PP_BUSST:
            return BUSST_RDY4TXNOW
        if pp == PP_RXCFG:
            return self.rxcfg
        return 0

    def _pp_write_half(self, pp: int, value: int, *, high: bool) -> None:
        if pp == PP_RXCFG:
            if high:
                raise SimError("RxCFG high byte written (drops chip state on silicon)")
            self.rxcfg = (self.rxcfg & 0xFF00) | value
            if value & 0x40:
                self._skip_now()
            return
        # Other PacketPage writes (RxCTL, LineCTL, IA...) are accepted and
        # ignored: nothing here models the acceptance filter.

    def _tx_write(self, reg: int, value: int) -> None:
        # The TX body is written low half first: $DE08 then $DE09.
        if reg == _RTDATA_LO:
            self._pending_hi[0] = value
            return
        lo = self._pending_hi.pop(0, None)
        if lo is None:
            raise SimError("RTDATA high half written before low half on TX")
        self._tx_buf += bytes([lo, value])
        if len(self._tx_buf) >= self.txlen:
            self.tx_frames.append(bytes(self._tx_buf[: self.txlen]))
            self._tx_buf = bytearray()


class Cpu6502:
    """Just enough 6502 to run the bridge_ping builders' output."""

    def __init__(self, chip: Cs8900aSim) -> None:
        self.mem = bytearray(0x10000)
        self.chip = chip
        self.a = self.x = self.y = 0
        self.sp = 0xFF
        self.pc = 0
        self.c = self.z = self.n = self.v = False
        self.steps = 0

    # -- memory --------------------------------------------------------
    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if 0xDE00 <= addr <= 0xDE0F:
            return self.chip.read(addr)
        if 0xDC08 <= addr <= 0xDC0B:
            return 0                      # TOD stuck at 00:00:00.0
        return self.mem[addr]

    def write(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        value &= 0xFF
        if 0xDE00 <= addr <= 0xDE0F:
            self.chip.write(addr, value)
            return
        self.mem[addr] = value

    def load(self, addr: int, data: bytes) -> None:
        self.mem[addr:addr + len(data)] = data

    # -- helpers -------------------------------------------------------
    def _nz(self, v: int) -> int:
        v &= 0xFF
        self.z = v == 0
        self.n = bool(v & 0x80)
        return v

    def _fetch(self) -> int:
        v = self.mem[self.pc]
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def _fetch16(self) -> int:
        lo = self._fetch()
        return lo | (self._fetch() << 8)

    def _push(self, v: int) -> None:
        self.mem[0x100 + self.sp] = v & 0xFF
        self.sp = (self.sp - 1) & 0xFF

    def _pop(self) -> int:
        self.sp = (self.sp + 1) & 0xFF
        return self.mem[0x100 + self.sp]

    def _cmp(self, reg: int, v: int) -> None:
        r = (reg - v) & 0x1FF
        self.c = reg >= v
        self._nz(r)

    def _adc(self, v: int) -> None:
        r = self.a + v + (1 if self.c else 0)
        self.v = bool(~(self.a ^ v) & (self.a ^ r) & 0x80)
        self.c = r > 0xFF
        self.a = self._nz(r)

    def _sbc(self, v: int) -> None:
        r = self.a - v - (0 if self.c else 1)
        self.v = bool((self.a ^ v) & (self.a ^ r) & 0x80)
        self.c = r >= 0
        self.a = self._nz(r)

    def _branch(self, cond: bool) -> None:
        disp = self._fetch()
        if cond:
            if disp & 0x80:
                disp -= 0x100
            self.pc = (self.pc + disp) & 0xFFFF

    # -- run -----------------------------------------------------------
    def jsr(self, addr: int, max_steps: int = 2_000_000) -> None:
        """Run from *addr* until the matching RTS, or raise on the budget."""
        self.pc = addr
        self._push(0xFF)
        self._push(0xFE)                  # fake return address $FFFE+1
        base_sp = self.sp
        while self.steps < max_steps:
            self.steps += 1
            self.step()
            if self.sp == (base_sp + 2) & 0xFF and self.pc == 0xFFFF:
                return
        raise SimError(f"step budget {max_steps} exhausted at PC=${self.pc:04X}")

    def step(self) -> None:
        op = self._fetch()
        m = self  # noqa: F841 - readability in the table below
        if op == 0xEA:                      # NOP
            return
        if op in (0x78, 0x58):              # SEI / CLI
            return
        if op == 0x18:                      # CLC
            self.c = False
            return
        if op == 0x38:                      # SEC
            self.c = True
            return
        if op == 0x60:                      # RTS
            lo = self._pop()
            hi = self._pop()
            self.pc = ((hi << 8) | lo) + 1
            return
        if op == 0x4C:                      # JMP abs
            self.pc = self._fetch16()
            return
        if op == 0x20:                      # JSR abs
            target = self._fetch16()
            ret = (self.pc - 1) & 0xFFFF
            self._push(ret >> 8)
            self._push(ret & 0xFF)
            self.pc = target
            return
        # loads
        if op == 0xA9:
            self.a = self._nz(self._fetch()); return
        if op == 0xA5:
            self.a = self._nz(self.read(self._fetch())); return
        if op == 0xAD:
            self.a = self._nz(self.read(self._fetch16())); return
        if op == 0xBD:
            self.a = self._nz(self.read(self._fetch16() + self.x)); return
        if op == 0xB9:
            self.a = self._nz(self.read(self._fetch16() + self.y)); return
        if op == 0xB1:
            zp = self._fetch()
            ptr = self.mem[zp] | (self.mem[(zp + 1) & 0xFF] << 8)
            self.a = self._nz(self.read(ptr + self.y)); return
        if op == 0xA2:
            self.x = self._nz(self._fetch()); return
        if op == 0xA6:
            self.x = self._nz(self.read(self._fetch())); return
        if op == 0xAE:
            self.x = self._nz(self.read(self._fetch16())); return
        if op == 0xA0:
            self.y = self._nz(self._fetch()); return
        if op == 0xAC:
            self.y = self._nz(self.read(self._fetch16())); return
        # stores
        if op == 0x85:
            self.write(self._fetch(), self.a); return
        if op == 0x8D:
            self.write(self._fetch16(), self.a); return
        if op == 0x9D:
            self.write(self._fetch16() + self.x, self.a); return
        if op == 0x91:
            zp = self._fetch()
            ptr = self.mem[zp] | (self.mem[(zp + 1) & 0xFF] << 8)
            self.write(ptr + self.y, self.a); return
        if op == 0x86:
            self.write(self._fetch(), self.x); return
        if op == 0x8E:
            self.write(self._fetch16(), self.x); return
        if op == 0x84:
            self.write(self._fetch(), self.y); return
        if op == 0x8C:
            self.write(self._fetch16(), self.y); return
        # transfers
        if op == 0xAA:
            self.x = self._nz(self.a); return
        if op == 0xA8:
            self.y = self._nz(self.a); return
        if op == 0x8A:
            self.a = self._nz(self.x); return
        if op == 0x98:
            self.a = self._nz(self.y); return
        if op == 0x9A:
            self.sp = self.x; return
        # inc/dec
        if op == 0xC8:
            self.y = self._nz(self.y + 1); return
        if op == 0x88:
            self.y = self._nz(self.y - 1); return
        if op == 0xE8:
            self.x = self._nz(self.x + 1); return
        if op == 0xCA:
            self.x = self._nz(self.x - 1); return
        if op == 0xE6:
            zp = self._fetch(); self.write(zp, self._nz(self.read(zp) + 1)); return
        if op == 0xC6:
            zp = self._fetch(); self.write(zp, self._nz(self.read(zp) - 1)); return
        if op == 0xEE:
            ad = self._fetch16(); self.write(ad, self._nz(self.read(ad) + 1)); return
        if op == 0xCE:
            ad = self._fetch16(); self.write(ad, self._nz(self.read(ad) - 1)); return
        # logic / arithmetic
        if op == 0x29:
            self.a = self._nz(self.a & self._fetch()); return
        if op == 0x2D:
            self.a = self._nz(self.a & self.read(self._fetch16())); return
        if op == 0x09:
            self.a = self._nz(self.a | self._fetch()); return
        if op == 0x0D:
            self.a = self._nz(self.a | self.read(self._fetch16())); return
        if op == 0x49:
            self.a = self._nz(self.a ^ self._fetch()); return
        if op == 0x69:
            self._adc(self._fetch()); return
        if op == 0x65:
            self._adc(self.read(self._fetch())); return
        if op == 0x6D:
            self._adc(self.read(self._fetch16())); return
        if op == 0xE9:
            self._sbc(self._fetch()); return
        if op == 0xE5:
            self._sbc(self.read(self._fetch())); return
        if op == 0xED:
            self._sbc(self.read(self._fetch16())); return
        if op == 0x4A:                      # LSR A
            self.c = bool(self.a & 1); self.a = self._nz(self.a >> 1); return
        if op == 0x0A:                      # ASL A
            self.c = bool(self.a & 0x80); self.a = self._nz(self.a << 1); return
        # compares
        if op == 0xC9:
            self._cmp(self.a, self._fetch()); return
        if op == 0xC5:
            self._cmp(self.a, self.read(self._fetch())); return
        if op == 0xCD:
            self._cmp(self.a, self.read(self._fetch16())); return
        if op == 0xDD:
            self._cmp(self.a, self.read(self._fetch16() + self.x)); return
        if op == 0xE0:
            self._cmp(self.x, self._fetch()); return
        if op == 0xEC:
            self._cmp(self.x, self.read(self._fetch16())); return
        if op == 0xC0:
            self._cmp(self.y, self._fetch()); return
        if op == 0xCC:
            self._cmp(self.y, self.read(self._fetch16())); return
        # branches
        if op == 0xD0:
            self._branch(not self.z); return
        if op == 0xF0:
            self._branch(self.z); return
        if op == 0x90:
            self._branch(not self.c); return
        if op == 0xB0:
            self._branch(self.c); return
        if op == 0x10:
            self._branch(not self.n); return
        if op == 0x30:
            self._branch(self.n); return
        if op == 0x50:
            self._branch(not self.v); return
        if op == 0x70:
            self._branch(self.v); return
        raise SimError(f"unmodelled opcode ${op:02X} at PC=${(self.pc - 1) & 0xFFFF:04X}")


def run_routine(
    code: bytes,
    load_addr: int,
    *,
    rx_frames: list[bytes] = (),
    preload: dict[int, bytes] | None = None,
    max_steps: int = 2_000_000,
) -> tuple[Cpu6502, Cs8900aSim]:
    """Load *code* at *load_addr*, queue *rx_frames*, JSR it, return (cpu, chip).

    *preload* maps addresses to bytes written into RAM before the run --
    the TX frame buffers a ping routine reads from.
    """
    chip = Cs8900aSim(rx_queue=list(rx_frames))
    cpu = Cpu6502(chip)
    for addr, data in (preload or {}).items():
        cpu.load(addr, data)
    cpu.load(load_addr, code)
    cpu.jsr(load_addr, max_steps=max_steps)
    return cpu, chip
