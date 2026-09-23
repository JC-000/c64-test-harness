"""A command routine must not write its command until the firmware has taken the ABORT (#419).

Every routine ``build_uci_command`` emits opens with an ABORT (``$04`` to
``$DF1C``) to clear any pending state.  The FPGA only latches it as
``handshake_in(2)`` -- bit 2 of ``$DF1C`` -- and the firmware services it
later, from its command task (``software/io/command_interface/command_intf.cc``,
``CommandInterface::run_task``), by writing ``HANDSHAKE_RESET`` (``$87``).
That write does more than clear bit 2 (``command_protocol.vhd``,
``c_cif_io_handshake_out``): it resets the command pointer, clears bit 0 and
forces the state to idle.  So a routine that goes on writing target, opcode
and parameters while bit 2 is still set loses whatever it wrote before the
reset lands:

* reset between the target byte and the opcode -- the firmware parses
  ``[05 00]``, which selects the SoftIEC target (id 5), and answers with its
  one-byte unknown-command status ``"\\x04"`` and no data;
* reset after the opcode -- ``[00]`` reaches a target with no such command:
  ``"21,UNKNOWN COMMAND"``;
* reset after the last byte, before or during the PUSH -- the pushed command is
  empty ("Null command."): zero-length data and status.

The live probe on the U64E read back all three, and read bit 2 set before the
target byte in every failure and clear in every success.

These tests run the builders' emitted 6502 on ``tests/cs8900a_sim.py``'s CPU
against a behavioural model of the command interface whose firmware takes a
set number of register accesses to service the ABORT.  ``latency=0`` is the
control: the reset lands before the routine touches the interface again, so
every routine passes and the model is shown to answer a well-formed command.
"""
from __future__ import annotations

import pytest

from cs8900a_sim import Cpu6502, Cs8900aSim

from c64_test_harness import uci_network as un

GET_IP = bytes([un.TARGET_NETWORK, un.NET_CMD_GET_IPADDR, 0x00])
REPLY = bytes([10, 43, 23, 81, 255, 255, 255, 0, 10, 43, 23, 1])
STATUS_OK = b"00,OK"
SOFTIEC_UNKNOWN = b"\x04"      # softiec_target.cc c_status_unknown_cmd_local
UNKNOWN = b"21,UNKNOWN COMMAND"  # command_intf.cc c_status_unknown_command


class UciModel:
    """``$DF1C``-``$DF1F`` as the FPGA presents them, over a scripted firmware.

    The firmware services an ABORT after *latency* further accesses to the
    interface registers, then answers any pushed command at once.  It parses
    what the command buffer actually holds: ``GET_IP`` gets the reply, a
    buffer starting with target 5 gets the SoftIEC status, any other gets
    ``"21,UNKNOWN COMMAND"`` and an empty one gets nothing at all -- the three
    failure statuses the live probe read back.
    """

    def __init__(self, *, latency: int) -> None:
        self.latency = latency
        self.state = 0            # $DF1C bits 5:4
        self.hs0 = False          # bit 0: command pushed, not yet taken
        self.hs2 = False          # bit 2: abort requested, not yet serviced
        self.abort_countdown: int | None = None
        self.pending_push = False
        self.cmd = bytearray()
        self.parsed: list[bytes] = []
        self.resp = self.stat = b""
        self.resp_ptr = self.stat_ptr = 0

    # -- firmware ------------------------------------------------------
    def _firmware(self) -> None:
        if self.abort_countdown is not None:
            if self.abort_countdown > 0:
                self.abort_countdown -= 1
            else:
                self.abort_countdown = None
                # HANDSHAKE_RESET ($87)
                self.hs0 = self.hs2 = False
                self.state = 0
                self.cmd.clear()
                self.resp = self.stat = b""
        if self.pending_push and not self.hs2:
            self.pending_push = False
            msg = bytes(self.cmd)
            self.parsed.append(msg)
            if msg == GET_IP:
                self.resp, self.stat = REPLY, STATUS_OK
            elif msg[:1] == b"\x05":
                self.resp, self.stat = b"", SOFTIEC_UNKNOWN
            elif msg:
                self.resp, self.stat = b"", UNKNOWN
            else:                     # "Null command.": both lengths zero
                self.resp, self.stat = b"", b""
            self.resp_ptr = self.stat_ptr = 0
            self.cmd.clear()
            self.hs0 = False          # HANDSHAKE_ACCEPT_COMMAND
            self.state = 2            # copy_result: VALIDATE_LAST

    def _access(self) -> None:
        self._firmware()

    # -- bus -----------------------------------------------------------
    def status(self) -> int:
        v = (self.state << 4) | (0x04 if self.hs2 else 0) | (0x01 if self.hs0 else 0)
        if self.state & 2 and self.resp_ptr < len(self.resp):
            v |= 0x80
        if self.state & 2 and self.stat_ptr < len(self.stat):
            v |= 0x40
        return v

    def read(self, addr: int) -> int:
        self._access()
        if addr == 0xDF1C:
            return self.status()
        if addr == 0xDF1D:
            return 0xC9
        if addr == 0xDF1E:
            if self.state & 2 and self.resp_ptr < len(self.resp):
                self.resp_ptr += 1
                return self.resp[self.resp_ptr - 1]
            return 0
        if addr == 0xDF1F:
            if self.state & 2 and self.stat_ptr < len(self.stat):
                self.stat_ptr += 1
                return self.stat[self.stat_ptr - 1]
            return 0
        return 0xFF

    def write(self, addr: int, value: int) -> None:
        self._access()
        if addr == 0xDF1D:
            self.cmd.append(value)
            return
        if addr != 0xDF1C:
            return
        if value & 0x04:
            self.hs2 = True
            self.abort_countdown = self.latency
        if value & 0x01 and self.state == 0:
            self.state, self.hs0 = 1, True
            self.pending_push = True
        if value & 0x02 and self.state & 2:
            self.state = 0            # data accepted, last part
        self._firmware()


class UciCpu(Cpu6502):
    def __init__(self, uci: UciModel) -> None:
        super().__init__(Cs8900aSim(rx_queue=[]))
        self.uci = uci

    def read(self, addr: int) -> int:
        if 0xDF1C <= (addr & 0xFFFF) <= 0xDF1F:
            return self.uci.read(addr & 0xFFFF)
        return super().read(addr)

    def write(self, addr: int, value: int) -> None:
        if 0xDF1C <= (addr & 0xFFFF) <= 0xDF1F:
            self.uci.write(addr & 0xFFFF, value & 0xFF)
            return
        super().write(addr, value)

    def step(self) -> None:
        # The UCI builders use three opcodes the bridge_ping builders never
        # emit, so the shared CPU does not model them: the turbo fence saves
        # A and X with PHA/PLA, and the plain readers store with abs,Y.
        op = self.mem[self.pc]
        if op == 0x99:                  # STA abs,Y
            self.pc = (self.pc + 1) & 0xFFFF
            self.write((self._fetch16() + self.y) & 0xFFFF, self.a)
            return
        if op == 0x48:                  # PHA
            self.pc = (self.pc + 1) & 0xFFFF
            self._push(self.a)
            return
        if op == 0x68:                  # PLA
            self.pc = (self.pc + 1) & 0xFFFF
            self.a = self._nz(self._pop())
            return
        super().step()


def _run_get_ip(*, turbo_safe: bool, latency: int) -> tuple[UciCpu, UciModel]:
    uci = UciModel(latency=latency)
    cpu = UciCpu(uci)
    cpu.load(un._CODE_ADDR, un.build_get_ip(turbo_safe=turbo_safe))
    cpu.jsr(un._CODE_ADDR, max_steps=5_000_000)
    return cpu, uci


def _collected(cpu: UciCpu) -> tuple[bytes, bytes]:
    m = cpu.mem
    rlen = m[un._RESP_LEN_ADDR] | m[un._RESP_LEN_ADDR + 1] << 8
    slen = m[un._STAT_LEN_ADDR] | m[un._STAT_LEN_ADDR + 1] << 8
    return (bytes(m[un._RESP_ADDR:un._RESP_ADDR + rlen]),
            bytes(m[un._STATUS_ADDR:un._STATUS_ADDR + slen]))


@pytest.mark.parametrize("turbo_safe", [False, True], ids=["plain", "turbo_safe"])
@pytest.mark.parametrize("latency", [0, 1, 2, 3, 4, 8, 64])
def test_get_ip_command_survives_a_late_abort(turbo_safe: bool, latency: int) -> None:
    cpu, uci = _run_get_ip(turbo_safe=turbo_safe, latency=latency)
    assert uci.parsed == [GET_IP], (
        f"latency={latency}: the firmware parsed {[p.hex() for p in uci.parsed]}; "
        f"command bytes written while $DF1C bit 2 was set were lost to "
        f"HANDSHAKE_RESET"
    )
    assert _collected(cpu) == (REPLY, STATUS_OK)
    assert cpu.mem[un._ERROR_ADDR] == 0x00
    assert cpu.mem[un._SENTINEL_ADDR] == un._SENTINEL_DONE
    assert uci.status() == 0, "the routine must leave the interface idle"
