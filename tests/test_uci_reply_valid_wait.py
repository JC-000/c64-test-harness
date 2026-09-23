"""Every UCI routine waits for the reply to be valid, not just for "not busy" (#486).

``$DF1C`` bit 0, which the harness calls CMD_BUSY, is ``handshake_in(0)``,
the new-command flag (``command_protocol.vhd:85-89`` at bce4535e).  The
firmware clears it with ``HANDSHAKE_ACCEPT_COMMAND`` (``command_intf.cc:169``)
*before* ``copy_result`` copies the reply and status and writes VALIDATE
(``:173``, ``:201-205``).  In between, STATE reads ``01`` and neither
DATA_AV nor STAT_AV is set.  A drain that starts inside that window stores
nothing; if the window closes before the status drain, the routine records
``00,OK`` with zero data bytes -- the #486 signature, measured on the U64E
2026-09-23 (stock 5/16 whole 253-byte reads, with the wait 16/16).

The fix waits for STATE bit 5 (``state(1)``, reply valid) **or** the error
bit (a PUSH while not idle sets ``error_busy`` and leaves the state alone,
so a wait on bit 5 alone would spin there).

The fake UCI below models the window as a number of ``$DF1C`` reads after
bit 0 clears.  Evidence grade: interpreter model of source-read firmware
behaviour; the live pairing is on #486.
"""
from __future__ import annotations

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.uci_network import (
    BIT_DATA_AV,
    BIT_ERROR,
    BIT_STAT_AV,
    CMD_ABORT,
    CMD_NEXT_DATA,
    CMD_PUSH,
    UCI_CONTROL_STATUS_REG,
    UCI_RESP_DATA_REG,
    UCI_STATUS_DATA_REG,
    _RESP_ADDR,
    _RESP_LEN_ADDR,
    _SENTINEL_ADDR,
    _SENTINEL_DONE,
    _STAT_LEN_ADDR,
    _STATUS_ADDR,
    build_get_ip,
    build_socket_read,
    uci_socket_read,
)
from test_uci_turbo_fence_register import UNWRITTEN, _UciMachine

STATUS_OK = b"00,OK"


def _datagram(n: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


class _WindowUci(_UciMachine):
    """A fake UCI with the accept-before-validate window.

    After PUSH, bit 0 reads set for ``busy_reads`` status reads; then it
    clears while STATE stays ``01`` with both queues empty for
    ``window_reads`` further reads; then the reply is validated (STATE
    ``10``, queues loaded).  ``push_error`` models a PUSH while not idle:
    the error bit is set and nothing else changes.
    """

    def __init__(self, code: bytes, *, data: bytes = b"", status: bytes = STATUS_OK,
                 busy_reads: int = 2, window_reads: int = 2,
                 push_error: bool = False) -> None:
        super().__init__(code, data=data, status=status)
        self.busy_reads = busy_reads
        self.window_reads = window_reads
        self.push_error = push_error
        self.state = 0x00
        self.error = False
        self._phase: list[str] = []

    def read(self, addr: int) -> int:
        if addr == UCI_CONTROL_STATUS_REG:
            if self._phase:
                phase = self._phase.pop(0)
                if not self._phase:          # window over: VALIDATE_LAST
                    self.state = 0x20
                    self.data_q = list(self._reply_data)
                    self.status_q = list(self._reply_status)
                return 0x11 if phase == "busy" else 0x10
            v = self.state
            if self.data_q:
                v |= BIT_DATA_AV
            if self.status_q:
                v |= BIT_STAT_AV
            if self.error:
                v |= BIT_ERROR
            return v
        return super().read(addr)

    def write(self, addr: int, val: int) -> None:
        val &= 0xFF
        if addr == UCI_CONTROL_STATUS_REG:
            if val == CMD_PUSH:
                self.pushes += 1
                if self.push_error:
                    self.error = True
                    return
                self.state = 0x10
                self._phase = ["busy"] * self.busy_reads + ["window"] * self.window_reads
                if not self._phase:
                    self._phase = ["window"]
                return
            if val == CMD_NEXT_DATA:
                self.accepts += 1
                if self.state & 0x20:        # accept is gated on state(1)
                    self.state = 0x00
                    self.data_q, self.status_q = [], []
                return
            if val == u.CMD_CLR_ERR:
                self.error = False
                return
            if val == CMD_ABORT:
                return
        super().write(addr, val)


def _run(code: bytes, **kw) -> _WindowUci:
    cpu = _WindowUci(code, **kw)
    cpu.mem[0xC100] = cpu.mem[0xC403] = 0x05
    cpu.run(max_steps=3_000_000)
    assert cpu.mem[_SENTINEL_ADDR] == _SENTINEL_DONE
    return cpu


PATHS = [False, True]


# ------------------------------------------------------------- the model
#: The plain post-PUSH wait, fixed (#486) and as it was before.
_WAIT_FIXED = bytes([0xAD, 0x1C, 0xDF, 0x29, 0x28, 0xF0, 0xF9])  # AND #$28; BEQ
_WAIT_STOCK = bytes([0xAD, 0x1C, 0xDF, 0x29, 0x01, 0xD0, 0xF9])  # AND #$01; BNE


def test_the_model_reproduces_the_486_signature_on_the_stock_wait() -> None:
    """Premise: with the old "bit 0 clear" wait the model gives exactly what
    the U64E gave -- zero data bytes stored, a fresh ``00,OK``."""
    code = build_socket_read(0xC100, max_len=253)
    assert code.count(_WAIT_FIXED) == 1
    stock = code.replace(_WAIT_FIXED, _WAIT_STOCK)
    block = bytes([253, 0]) + _datagram(253)
    # Three reads: the one that sees bit 0 clear, check_error's, and the
    # drain's first DATA_AV test all fall inside the window.
    cpu = _run(stock, data=block, window_reads=3)
    assert cpu.resp_len() == 0
    assert bytes(cpu.mem[_STATUS_ADDR:_STATUS_ADDR + 5]) == STATUS_OK
    assert cpu.mem[_RESP_ADDR] == UNWRITTEN


# ------------------------------------------------------------ the fix
@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("window", [0, 1, 2, 3, 8, 40])
def test_a_socket_read_drains_whatever_the_window(turbo: bool, window: int) -> None:
    block = bytes([253, 0]) + _datagram(253)
    cpu = _run(build_socket_read(0xC403 if turbo else 0xC100, max_len=253,
                                 turbo_safe=turbo),
               data=block, window_reads=window)
    assert cpu.resp_len() == len(block)
    assert bytes(cpu.mem[_RESP_ADDR:_RESP_ADDR + len(block)]) == block
    assert bytes(cpu.mem[_STATUS_ADDR:_STATUS_ADDR + 5]) == STATUS_OK


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("window", [1, 2, 8])
def test_get_ip_drains_whatever_the_window(turbo: bool, window: int) -> None:
    """The race is in the shared push-and-wait, not the read path."""
    reply = bytes([10, 43, 23, 81, 255, 255, 255, 0, 10, 43, 23, 1])
    cpu = _run(build_get_ip(turbo_safe=turbo), data=reply, window_reads=window)
    assert cpu.resp_len() == len(reply)
    assert bytes(cpu.mem[_RESP_ADDR:_RESP_ADDR + len(reply)]) == reply


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_a_push_error_ends_the_wait_instead_of_spinning(turbo: bool) -> None:
    """A PUSH while not idle sets the error bit and never validates a reply;
    the wait must end on the error bit, and the error flag must be raised."""
    cpu = _run(build_get_ip(turbo_safe=turbo), data=b"\x01", push_error=True)
    assert cpu.mem[u._ERROR_ADDR] == 0xFF


# ------------------------------------------------ end to end, the signature
class _SimTransport:
    """RAM for ``write_memory``/``read_memory``; typing the ``SYS`` runs the
    routine in :class:`_WindowUci`."""

    def __init__(self, reply: bytes, window_reads: int) -> None:
        self.mem = bytearray(0x10000)
        self._reply = reply
        self._window = window_reads

    def write_memory(self, addr: int, data: bytes, **_: object) -> None:
        if 0xDF00 <= addr <= 0xDFFF:
            return
        self.mem[addr:addr + len(data)] = data
        if addr == 0x00C6:
            cpu = _WindowUci(b"", data=self._reply, window_reads=self._window)
            cpu.mem[:] = self.mem
            cpu.run(max_steps=3_000_000)
            self.mem[:] = cpu.mem

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(self.mem[addr:addr + length])


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_uci_socket_read_returns_the_datagram_through_the_window(turbo: bool) -> None:
    dg = _datagram(253)
    t = _SimTransport(bytes([253, 0]) + dg, window_reads=3)
    assert uci_socket_read(t, 5, 253, turbo_safe=turbo) == dg
