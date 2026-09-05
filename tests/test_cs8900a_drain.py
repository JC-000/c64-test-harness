"""``drain_first`` on the ping builders (issue #222), on the simulated chip.

What was measured (U64E fw 3.15 fork, external RR-Net point-to-point to a
macOS host, 2026-09-05, scratchpad ``exp222.py`` / ``exp222b.py``): the
first exchange after a fresh reset + chip init + 5 s idle matched 3/6
while the same exchange preceded by a drain of the chip's RX queue
matched 6/6 (arms interleaved).  Every miss had LinkOK set, both the
request and the reply on the wire, RxMISS +1 on the chip, and frames
already queued when the routine started (the host's periodic 342-byte
UDP broadcasts); every no-drain match started with an empty queue.  So
the reply is discarded by the chip for lack of receive buffer when stale
frames sit in front of it, and the fix is to SkipNow them before the
first transmit.

These tests pin the emitted behaviour on ``tests/cs8900a_sim.py``: with
``drain_first`` the queue is empty at the moment of the first TX and the
reply (which only arrives after the echo request goes out) is still
matched; without it the stale frames are still queued at TX time; the
drain is bounded; and the default output is byte-identical to before.
Live counterpart: ``tests/test_first_exchange_live.py`` (``RRNET_LIVE=1``).
"""
from __future__ import annotations

import pytest

from c64_test_harness import bridge_ping as bp
from c64_test_harness.bridge_ping import (
    DRAIN_RX_MAX_FRAMES,
    build_arp_request_frame,
    build_echo_request_frame,
)
from cs8900a_sim import Cpu6502, Cs8900aSim

LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300
MAC_A = bytes.fromhex("02c640000001")
MAC_B = bytes.fromhex("c05627b11638")
IP_A, IP_B = bytes([10, 0, 66, 201]), bytes([10, 0, 66, 1])

BUILDERS = {
    "build_ping_and_wait_code": lambda **kw: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, **kw),
    "build_ping_and_wait_tod_code": lambda **kw: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, **kw),
}


def _stale(n: int) -> bytes:
    """A host broadcast like the ones measured: 342-byte UDP to 255.255.255.255."""
    f = bytearray(b"\xff" * 6 + MAC_B + b"\x08\x00")
    f += bytes([0x45, 0, 0x01, 0x48]) + bytes([0, n, 0, 0, 64, 17, 0, 0]) + IP_B + b"\xff" * 4
    f += bytes([0, 68, 0, 67, 0x01, 0x34, 0, 0])
    return bytes(f) + bytes(342 - len(f))


def _echo_reply_for(echo_frame: bytes) -> bytes:
    f = bytearray(echo_frame)
    f[0:6], f[6:12] = echo_frame[6:12], echo_frame[0:6]
    f[26:30], f[30:34] = echo_frame[30:34], echo_frame[26:30]
    f[34] = 0
    cksum = int.from_bytes(f[36:38], "big") + 0x0800
    cksum = (cksum & 0xFFFF) + (cksum >> 16)
    f[36:38] = cksum.to_bytes(2, "big")
    return bytes(f)


class _ChipThatAnswersLater(Cs8900aSim):
    """The reply is not queued up front: it lands only once the echo request
    has been transmitted -- the timing that makes stale frames matter.
    Records the queue depth at each TX and every SkipNow."""

    def __init__(self, stale: list[bytes], echo: bytes, reply: bytes) -> None:
        super().__init__(rx_queue=list(stale))
        self._echo, self._reply = echo, reply
        self.queue_at_tx: list[int] = []
        self.skips = 0
        self.skips_at_first_tx: int | None = None

    def _tx_write(self, reg: int, value: int) -> None:
        before = len(self.tx_frames)
        super()._tx_write(reg, value)
        if len(self.tx_frames) > before:
            if self.skips_at_first_tx is None:
                self.skips_at_first_tx = self.skips
            self.queue_at_tx.append(len(self.rx_queue) + (1 if self._rx_stream else 0))
            if self.tx_frames[-1] == self._echo:
                self.rx_queue.append(self._reply)

    def _skip_now(self) -> None:
        self.skips += 1
        super()._skip_now()


def _run(code: bytes, chip: Cs8900aSim, preload: dict[int, bytes]) -> Cpu6502:
    cpu = Cpu6502(chip)
    for addr, data in preload.items():
        cpu.load(addr, data)
    cpu.load(LOAD, code)
    cpu.jsr(LOAD)
    return cpu


def _setup(name: str, n_stale: int, **kw):
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B, identifier=0x1234, sequence=1)
    arp = build_arp_request_frame(MAC_A, IP_A, IP_B)
    chip = _ChipThatAnswersLater([_stale(i) for i in range(n_stale)], echo.frame,
                                 _echo_reply_for(echo.frame))
    code = BUILDERS[name](arp_frame_buf=ARP_BUF, **kw)
    cpu = _run(code, chip, {TX_BUF: echo.frame, ARP_BUF: arp})
    return cpu, chip


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_drain_first_empties_the_queue_before_the_first_tx(name: str) -> None:
    cpu, chip = _setup(name, 2, drain_first=True)
    assert chip.skips_at_first_tx == 2
    assert chip.queue_at_tx[0] == 0, f"{name}: {chip.queue_at_tx[0]} frames still queued at ARP TX"
    assert cpu.mem[RESULT] == 0x01, f"{name}: reply not matched after the drain"


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_without_drain_the_stale_frames_are_still_queued_at_tx(name: str) -> None:
    """The control on the simulator (which never discards): the routine
    transmits over a queue that still holds both stale frames."""
    cpu, chip = _setup(name, 2, drain_first=False)
    assert chip.skips_at_first_tx == 0
    assert chip.queue_at_tx[0] == 2
    assert cpu.mem[RESULT] == 0x01      # the sim delivers everything; silicon does not


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_drain_is_bounded(name: str) -> None:
    """More frames than the bound: the drain stops and the routine goes on."""
    cpu, chip = _setup(name, DRAIN_RX_MAX_FRAMES + 3, drain_first=True)
    assert chip.skips_at_first_tx == DRAIN_RX_MAX_FRAMES
    assert chip.queue_at_tx[0] == 3
    assert cpu.mem[RESULT] == 0x01


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_default_output_is_unchanged(name: str) -> None:
    assert BUILDERS[name]() == BUILDERS[name](drain_first=False)
    assert BUILDERS[name](drain_first=True) != BUILDERS[name]()


STATUS = 0x5301


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_drain_status_reports_the_budget_left_when_the_queue_emptied(name: str) -> None:
    cpu, chip = _setup(name, 2, drain_first=True, drain_status_addr=STATUS)
    assert cpu.mem[STATUS] == DRAIN_RX_MAX_FRAMES - 2
    assert chip.queue_at_tx[0] == 0


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_drain_status_is_zero_when_the_bound_was_hit(name: str) -> None:
    """The host can tell an exhausted drain from a clean one: 0 means the
    routine transmitted over frames that were still queued."""
    cpu, chip = _setup(name, DRAIN_RX_MAX_FRAMES + 3, drain_first=True,
                       drain_status_addr=STATUS)
    assert cpu.mem[STATUS] == 0
    assert chip.queue_at_tx[0] == 3


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_drain_status_without_drain_first_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="drain_status_addr"):
        BUILDERS[name](drain_status_addr=STATUS)


def test_drain_emitter_rejects_a_bad_bound() -> None:
    a = bp.Asm(org=LOAD)
    with pytest.raises(ValueError):
        bp._emit_drain_rx(a, "x", max_frames=0)
    with pytest.raises(ValueError):
        bp._emit_drain_rx(a, "x", max_frames=256)
