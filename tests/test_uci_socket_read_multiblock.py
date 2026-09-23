"""``READ_SOCKET`` replies that span Data More blocks (issue #420).

Firmware 3.15 (upstream #802/#806) accepts a ``READ_SOCKET`` length up to
``NET_MAX_SOCKET_READ`` = 1472 and splits a long reply: the first block is
the 2-byte LE header (the *total* payload length) plus up to
``NET_FIRST_BLOCK_PAYLOAD`` = 893 bytes, each continuation block is payload
only, up to ``NET_MAX_REPLY_BLOCK`` = 895 bytes, and the result status goes
out once, on the last block (``software/io/network/network_target.cc``
``start_read_reply`` / ``get_more_data`` at bce4535e, v3.15-132).  The
interface sits in STATE ``11`` (Data More) after a block that is not the
last; a DATA_ACC write moves it to ``01`` and the firmware answers with the
next block, ``10`` or ``11`` (``command_protocol.vhd`` and
``command_intf.cc``).  Pre-3.15 firmware refuses a length above 894 with
``82,PARAMETER(S) OUT OF RANGE`` and an empty reply.

These run **whole routines** in the interpreter from
``test_uci_turbo_fence_register.py`` against a fake UCI that models those
blocks, and drive :func:`uci_socket_read` end to end through a fake
transport that runs the routine when the ``SYS`` is typed.

Evidence grade: interpreter and fake-UCI model, from firmware source.
Nothing here has run on a device.
"""
from __future__ import annotations

import hashlib
import logging

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.uci_network import (
    BIT_DATA_AV,
    BIT_STAT_AV,
    CMD_NEXT_DATA,
    CMD_PUSH,
    NET_CMD_SOCKET_READ,
    NET_MAX_SOCKET_READ,
    SOCKET_READ_MAX_BYTES,
    TARGET_NETWORK,
    UCI_CMD_DATA_REG,
    UCI_CONTROL_STATUS_REG,
    UCI_RESP_DATA_REG,
    UCI_STATUS_DATA_REG,
    _CODE_ADDR,
    _RESP_LEN_ADDR,
    _SENTINEL_ADDR,
    _SENTINEL_DONE,
    _STAT_LEN_ADDR,
    _STATUS_ADDR,
    build_socket_read,
    uci_socket_read,
)
from test_uci_turbo_fence_register import UNWRITTEN, _UciMachine

#: Firmware constants at bce4535e (``network_target.h``).
FIRST_BLOCK_PAYLOAD = 893
MAX_REPLY_BLOCK = 895
OLD_FIRMWARE_MAX = 894          # CMD_MAX_REPLY_LEN - 2 before #802

SOCKET_ID_ADDR = 0xC403
LONG_BUF = u._LONG_READ_BUF_ADDR
STATUS_OK = b"00,OK"
OUT_OF_RANGE = b"82,PARAMETER(S) OUT OF RANGE"


def _datagram(n: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


class _DataMoreUci(_UciMachine):
    """The fake UCI with Data More: STATE bits and one block at a time.

    ``split=False`` models pre-3.15 firmware: one block, and a request
    above :data:`OLD_FIRMWARE_MAX` refused.  ``short_by`` drops that many
    bytes off the end of the last block while the header still announces
    the full length -- the "fewer arrived than announced" case.
    ``busy_polls`` holds STATE at ``01`` for that many status reads after
    each continuation accept, so a drain that does not wait is caught.
    """

    def __init__(self, code: bytes, datagram: bytes, *, split: bool = True,
                 short_by: int = 0, busy_polls: int = 3) -> None:
        super().__init__(code)
        self.mem[LONG_BUF:LONG_BUF + 0x600] = bytes([UNWRITTEN]) * 0x600
        self.datagram = datagram
        self.split = split
        self.short_by = short_by
        self.busy_polls = busy_polls
        self.state = 0x00
        self.blocks: list[bytes] = []
        self.status_text = b""
        self._busy_left = 0

    def _next_block(self) -> None:
        block = self.blocks.pop(0)
        last = not self.blocks
        if last and self.short_by:
            block = block[:len(block) - self.short_by]
        self.data_q = list(block)
        self.status_q = list(self.status_text) if last else []
        self.state = 0x20 if last else 0x30

    def read(self, addr: int) -> int:
        if addr == UCI_CONTROL_STATUS_REG:
            if self._busy_left:
                self._busy_left -= 1
                if not self._busy_left:
                    self._next_block()
                return 0x10
            v = self.state
            if self.data_q:
                v |= BIT_DATA_AV
            if self.status_q:
                v |= BIT_STAT_AV
            return v
        if addr == UCI_RESP_DATA_REG:
            return self.data_q.pop(0) if self.data_q else 0
        if addr == UCI_STATUS_DATA_REG:
            return self.status_q.pop(0) if self.status_q else 0
        return self.mem[addr]

    def write(self, addr: int, val: int) -> None:
        val &= 0xFF
        if addr == UCI_CONTROL_STATUS_REG and val == CMD_PUSH:
            self.pushes += 1
            length = self.cmd_bytes[3] | (self.cmd_bytes[4] << 8)
            limit = NET_MAX_SOCKET_READ if self.split else OLD_FIRMWARE_MAX
            if length > limit:
                self.blocks = [b""]
                self.status_text = OUT_OF_RANGE
            else:
                payload = self.datagram[:length]
                header = bytes([len(payload) & 0xFF, len(payload) >> 8])
                first = FIRST_BLOCK_PAYLOAD if self.split else len(payload)
                self.blocks = [header + payload[:first]]
                rest = payload[first:]
                while rest:
                    self.blocks.append(rest[:MAX_REPLY_BLOCK])
                    rest = rest[MAX_REPLY_BLOCK:]
                self.status_text = STATUS_OK
            self._next_block()
            return
        if addr == UCI_CONTROL_STATUS_REG and val == CMD_NEXT_DATA:
            self.accepts += 1
            self.data_q = []
            self.status_q = []
            if self.state == 0x30:
                self.state = 0x10
                self._busy_left = self.busy_polls
                if not self._busy_left:
                    self._next_block()
            else:
                self.state = 0x00
            return
        super().write(addr, val)

    def resp_len16(self) -> int:
        return self.mem[_RESP_LEN_ADDR] | (self.mem[_RESP_LEN_ADDR + 1] << 8)


def _run_long(n: int, *, turbo: bool, max_len: int = NET_MAX_SOCKET_READ,
              **kw) -> _DataMoreUci:
    code = build_socket_read(SOCKET_ID_ADDR, max_len=max_len,
                             turbo_safe=turbo, multi_block=True)
    cpu = _DataMoreUci(code, _datagram(n), **kw)
    cpu.mem[SOCKET_ID_ADDR] = 0x05
    cpu.run(max_steps=20_000_000)
    assert cpu.mem[_SENTINEL_ADDR] == _SENTINEL_DONE
    return cpu


PATHS = [False, True]
#: 893 fills the first block exactly; 894 puts one byte in a continuation;
#: 1472 is the most the command can return (893 + 579).
LONG_LENGTHS = [1, 253, 254, 892, 893, 894, 1471, 1472]


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("n", LONG_LENGTHS)
class TestTheMultiBlockDrain:
    def test_the_blocks_are_concatenated_after_the_header(
        self, turbo: bool, n: int
    ) -> None:
        cpu = _run_long(n, turbo=turbo)
        want = bytes([n & 0xFF, n >> 8]) + _datagram(n)
        assert bytes(cpu.mem[LONG_BUF:LONG_BUF + len(want)]) == want
        assert cpu.mem[LONG_BUF + len(want)] == UNWRITTEN, "stored past the reply"

    def test_the_recorded_length_is_16_bit_and_counts_the_header(
        self, turbo: bool, n: int
    ) -> None:
        assert _run_long(n, turbo=turbo).resp_len16() == n + 2

    def test_one_accept_per_block_and_the_status_lands(
        self, turbo: bool, n: int
    ) -> None:
        cpu = _run_long(n, turbo=turbo)
        blocks = 1 if n <= FIRST_BLOCK_PAYLOAD else 2
        assert (cpu.pushes, cpu.accepts) == (1, blocks)
        assert cpu.cmd_bytes == [TARGET_NETWORK, NET_CMD_SOCKET_READ, 0x05,
                                 NET_MAX_SOCKET_READ & 0xFF,
                                 NET_MAX_SOCKET_READ >> 8]
        assert bytes(cpu.mem[_STATUS_ADDR:_STATUS_ADDR + 5]) == STATUS_OK
        assert cpu.mem[_STAT_LEN_ADDR] == len(STATUS_OK)
        assert cpu.state == 0x00, "left the interface out of idle"


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_storage_is_bounded_by_the_requested_length(turbo: bool) -> None:
    """A reply longer than asked for is drained but not stored past
    ``max_len + 2`` -- the buffer's end is a promise to the memory table."""
    code = build_socket_read(SOCKET_ID_ADDR, max_len=300, turbo_safe=turbo,
                             multi_block=True)
    cpu = _DataMoreUci(code, _datagram(1000))
    cpu.mem[SOCKET_ID_ADDR] = 0x05
    # The fake honours the request, so make it lie: answer 1000 bytes.
    orig_write = cpu.write

    def lying_write(addr: int, val: int) -> None:
        if addr == UCI_CONTROL_STATUS_REG and val == CMD_PUSH:
            cpu.cmd_bytes[3:5] = [1000 & 0xFF, 1000 >> 8]
        orig_write(addr, val)

    cpu.write = lying_write  # type: ignore[method-assign]
    cpu.run(max_steps=20_000_000)
    assert cpu.resp_len16() == 302
    assert cpu.mem[LONG_BUF + 302] == UNWRITTEN
    assert not cpu.data_q, "the rest of the reply was not drained"


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_the_drain_waits_out_the_busy_state_between_blocks(turbo: bool) -> None:
    cpu = _run_long(1472, turbo=turbo, busy_polls=40)
    assert cpu.resp_len16() == 1474


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_pre_315_single_block_reply_drains_the_same_way(turbo: bool) -> None:
    cpu = _run_long(700, turbo=turbo, max_len=700, split=False)
    want = bytes([700 & 0xFF, 700 >> 8]) + _datagram(700)
    assert bytes(cpu.mem[LONG_BUF:LONG_BUF + len(want)]) == want
    assert cpu.accepts == 1


# ------------------------------------------------------ the single-block path
#: sha256 over ``build_socket_read(sid, max_len=n, turbo_safe=t)`` for n in
#: 0..255, captured on master a7a022e before #420.  Every length a caller
#: could already build must emit the bytes it emitted before.
_SINGLE_BLOCK_PINS = {
    (False, None): "0e573c73f5388268eebac3a72c071177b9d07ec5afda004c1c3639a76ab85ac3",
    (False, SOCKET_ID_ADDR): "71e24091e33f99c6b0a28594d0acbbee62fa53422e016c5f836d6efa3740f0d3",
    (True, None): "0cabf20aa56312c0bdec12743c49ffbb6b808e9ea412de223c1a4cc018950a15",
    (True, SOCKET_ID_ADDR): "0cabf20aa56312c0bdec12743c49ffbb6b808e9ea412de223c1a4cc018950a15",
}


@pytest.mark.parametrize("turbo,sid", list(_SINGLE_BLOCK_PINS),
                         ids=["plain", "plain-c403", "turbo", "turbo-c403"])
def test_the_single_block_routine_is_byte_identical_for_every_length(
    turbo: bool, sid: int | None
) -> None:
    h = hashlib.sha256()
    for n in range(256):
        h.update(build_socket_read(sid, max_len=n, turbo_safe=turbo))
    assert h.hexdigest() == _SINGLE_BLOCK_PINS[(turbo, sid)]


def test_a_length_past_one_single_block_drain_picks_the_multi_block_routine() -> None:
    """Above 255 the single-block drain cannot hold the reply, so the
    builder's default is the multi-block routine."""
    assert build_socket_read(max_len=256) == build_socket_read(
        max_len=256, multi_block=True)
    assert build_socket_read(max_len=255) != build_socket_read(
        max_len=255, multi_block=True)


# ------------------------------------------------- uci_socket_read end to end
class _SimTransport:
    """``write_memory``/``read_memory`` on the machine's RAM; typing the
    ``SYS`` (the keyboard-count write) runs the routine to its RTS."""

    def __init__(self, datagram: bytes, **kw) -> None:
        self._datagram = datagram
        self._kw = kw
        self.mem = bytearray(0x10000)
        self.cpu: _DataMoreUci | None = None
        self.writes: list[tuple[int, int]] = []

    def write_memory(self, addr: int, data: bytes, **_: object) -> None:
        self.writes.append((addr, len(data)))
        if 0xDF00 <= addr <= 0xDFFF:
            return
        self.mem[addr:addr + len(data)] = data
        if addr == 0x00C6:
            code = bytes(self.mem[_CODE_ADDR:_CODE_ADDR + 0x300])
            cpu = _DataMoreUci(code, self._datagram, **self._kw)
            cpu.mem[:] = self.mem
            cpu.mem[LONG_BUF:LONG_BUF + 0x600] = bytes([UNWRITTEN]) * 0x600
            cpu.run(max_steps=20_000_000)
            self.mem[:] = cpu.mem
            self.cpu = cpu

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(self.mem[addr:addr + length])


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("n", [894, 1472, 893, 254])
def test_uci_socket_read_returns_the_whole_datagram(turbo: bool, n: int) -> None:
    t = _SimTransport(_datagram(n))
    got = uci_socket_read(t, 5, max_len=NET_MAX_SOCKET_READ, turbo_safe=turbo)
    assert got == _datagram(n)


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("n", [254, 255])
def test_a_request_just_past_the_single_block_cap_is_served_whole(
    turbo: bool, n: int
) -> None:
    """254 and 255 are the lengths the 8-bit drain cannot hold with the
    header (it wraps at 256), so they must take the multi-block routine."""
    t = _SimTransport(_datagram(n))
    assert uci_socket_read(t, 5, max_len=n, turbo_safe=turbo) == _datagram(n)


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_a_short_reply_returns_what_arrived_with_a_warning(
    turbo: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Item 3 of #420: today's contract is kept -- return what arrived and
    say so -- not a raise."""
    t = _SimTransport(_datagram(1472), short_by=100)
    with caplog.at_level(logging.WARNING, logger=u.__name__):
        got = uci_socket_read(t, 5, max_len=NET_MAX_SOCKET_READ, turbo_safe=turbo)
    assert got == _datagram(1372)
    assert "1472" in caplog.text and "1372" in caplog.text


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_a_pre_315_refusal_is_reported_not_silent(
    turbo: bool, caplog: pytest.LogCaptureFixture
) -> None:
    t = _SimTransport(_datagram(1000), split=False)
    with caplog.at_level(logging.WARNING, logger=u.__name__):
        got = uci_socket_read(t, 5, max_len=1000, turbo_safe=turbo)
    assert got == b""
    assert "82,PARAMETER(S) OUT OF RANGE" in caplog.text


def test_uci_socket_read_refuses_past_the_firmware_ceiling() -> None:
    t = _SimTransport(b"")
    with pytest.raises(ValueError, match="1472"):
        uci_socket_read(t, 5, max_len=NET_MAX_SOCKET_READ + 1)
    assert t.writes == [], "refused after touching the device"


def test_a_short_read_keeps_the_single_block_routine() -> None:
    """``max_len`` <= 253 goes out exactly as before #420."""
    t = _SimTransport(_datagram(100))
    assert uci_socket_read(t, 5, max_len=SOCKET_READ_MAX_BYTES) == _datagram(100)
    code_writes = [n for a, n in t.writes if a == _CODE_ADDR]
    assert code_writes == [len(build_socket_read(0xC100, max_len=253))]


def test_the_reply_buffer_is_declared_harness_scratch() -> None:
    from c64_test_harness.memory_policy import HARNESS_SCRATCH

    end = LONG_BUF + NET_MAX_SOCKET_READ + 2
    assert any(r.start <= LONG_BUF and end <= r.end for r in HARNESS_SCRATCH)


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
def test_the_routine_fits_below_its_own_inputs_and_outputs(turbo: bool) -> None:
    """The routine at ``$C000`` must end before the status buffer it fills
    (``$C300``), and a plain one before its ``$C100`` socket-id slot."""
    code = build_socket_read(max_len=NET_MAX_SOCKET_READ, turbo_safe=turbo)
    limit = _STATUS_ADDR if turbo else u._SOCKET_ID_ADDR
    assert _CODE_ADDR + len(code) <= limit


def test_every_uci_register_access_in_the_turbo_routine_is_fenced() -> None:
    """The fake UCI has no timing, so it cannot see a missing fence; this
    reads the emitted code instead.  Every ``LDA``/``STA`` of
    ``$DF1C-$DF1F`` must be followed at once by the fence."""
    code = list(build_socket_read(max_len=NET_MAX_SOCKET_READ, turbo_safe=True))
    fence = u._build_fence()
    sites = [
        i for i in range(len(code) - 2)
        if code[i] in (0xAD, 0x8D) and code[i + 2] == 0xDF
        and 0x1C <= code[i + 1] <= 0x1F
    ]
    assert len(sites) >= 12
    for i in sites:
        assert code[i + 3:i + 3 + len(fence)] == fence, f"unfenced access at +{i}"
