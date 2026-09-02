"""The ethernet TX/RX scenarios are wired to the capture abstraction.

``tests/test_ethernet.py`` needs a live VICE and a bridge, and importing
it launches a VICE probe.  Its two capture-dependent bodies therefore
live in ``tests/ethernet_scenarios.py`` and take the capture as a
parameter, so this file can prove -- with a fake transport and a fake
capture, no emulator, no interface -- three things issue #158 requires:

* the TX scenario's *expectation* is the frame the capture delivers, and
  it **fails** (AssertionError, never ``pytest.skip``) when the capture
  delivers nothing or delivers the wrong bytes;
* the RX scenario's *send path* is ``capture.send`` with the 64-byte
  marker frame, and a send error or a C64-side poll timeout is a
  failure, not the silent ``except OSError: pass`` + skip it used to be;
* every expected value is something the code has to *produce*: the
  frames compared against are non-default byte patterns, the fake RAM
  starts zeroed, and the fake capture starts empty.
"""

from __future__ import annotations

from typing import Callable

import pytest

from c64_test_harness.capture import CaptureTimeout
from ethernet_scenarios import (
    CODE_BASE,
    ETHERTYPE,
    FRAME_BUF,
    FRAME_DATA,
    FRAME_LEN,
    RX_FRAME,
    RX_MARKER,
    run_rx_scenario,
    run_tx_scenario,
)


class FakeTransport:
    """64 KiB of zeroed RAM plus the binary-monitor calls the scenarios make.

    ``on_resume`` stands in for the 6502 executing the routine: it is
    called once per ``resume()`` with the RAM so a test can decide what
    the "C64" leaves behind.
    """

    def __init__(self, on_resume: Callable[[bytearray], None] | None = None) -> None:
        self.ram = bytearray(0x10000)
        self.on_resume = on_resume
        self.checkpoints: list[int] = []
        self.deleted: list[int] = []
        self.pc: int | None = None
        self.resumes = 0

    def write_memory(self, addr: int, data) -> None:
        data = bytes(data)
        self.ram[addr:addr + len(data)] = data

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(self.ram[addr:addr + length])

    def set_checkpoint(self, addr: int) -> int:
        self.checkpoints.append(addr)
        return len(self.checkpoints)

    def delete_checkpoint(self, num: int) -> None:
        self.deleted.append(num)

    def set_registers(self, regs: dict[str, int]) -> None:
        self.pc = regs.get("PC")

    def resume(self) -> None:
        self.resumes += 1
        if self.on_resume is not None:
            self.on_resume(self.ram)

    def wait_for_stopped(self, timeout: float = 0.0) -> None:
        pass

    def read_registers(self) -> dict[str, int]:
        return {"PC": self.pc or 0}


class FakeCapture:
    """A PacketCapture that hands out queued frames and records sends."""

    iface = "fake0"

    def __init__(self, frames: list[bytes] = (), *, send_error: Exception | None = None) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.send_error = send_error
        self.recv_calls: list[float] = []
        self.closed = False

    def recv(self, timeout: float, *, match=None) -> bytes:
        self.recv_calls.append(timeout)
        while self.frames:
            frame = self.frames.pop(0)
            if match is None or match(frame):
                return frame
        raise CaptureTimeout(f"fake: nothing on {self.iface}")

    def send(self, frame: bytes) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(bytes(frame))

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _c64_sets_tx_flag(ram: bytearray) -> None:
    ram[0xC000] = 0x01


STRAY_IPV4 = b"\x01\x00\x5e\x00\x00\xfb" + b"\x02\xc6\x40\x00\x00\x09" + b"\x08\x00" + b"\x45" * 50


# ---------------------------------------------------------------------------
# TX
# ---------------------------------------------------------------------------


def test_tx_scenario_returns_the_frame_the_capture_delivered():
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    cap = FakeCapture([STRAY_IPV4, FRAME_DATA])

    captured = run_tx_scenario(transport, cap, timeout=1.25)

    assert captured == FRAME_DATA
    assert captured[12:14] == ETHERTYPE and len(captured) == FRAME_LEN
    # The stray multicast frame ahead of ours was filtered, not returned.
    assert cap.frames == []
    assert cap.recv_calls == [1.25]
    # The scenario staged the frame in C64 RAM and loaded a routine.
    assert transport.read_memory(FRAME_BUF, FRAME_LEN) == FRAME_DATA
    assert transport.read_memory(CODE_BASE, 1) != b"\x00"


def test_tx_scenario_fails_when_no_frame_is_captured():
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    cap = FakeCapture([])  # the wire stayed silent

    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, cap, timeout=0.01)
    assert "reached the wire" in str(ei.value)
    assert "fake0" in str(ei.value)


def test_tx_scenario_fails_when_the_c64_routine_did_not_complete():
    transport = FakeTransport(on_resume=None)  # flag stays 0x00
    cap = FakeCapture([FRAME_DATA])

    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, cap, timeout=0.01)
    assert "success flag" in str(ei.value)
    # It must not have consulted the wire and declared victory on a
    # frame that some other sender put there.
    assert cap.recv_calls == []


def test_tx_scenario_rejects_a_matching_ethertype_with_wrong_payload():
    wrong = FRAME_DATA[:14] + bytes(len(FRAME_DATA) - 14)
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    cap = FakeCapture([wrong])

    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, cap, timeout=0.01)
    assert "Payload" in str(ei.value)


# ---------------------------------------------------------------------------
# RX
# ---------------------------------------------------------------------------


def _c64_receives_marker(ram: bytearray) -> None:
    ram[0xC000:0xC004] = RX_MARKER
    ram[0xC004] = 0x01


def _c64_poll_times_out(ram: bytearray) -> None:
    ram[0xC000] = 0xFF


def test_rx_scenario_sends_the_marker_frame_through_the_capture():
    transport = FakeTransport(on_resume=_c64_receives_marker)
    cap = FakeCapture()

    result = run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)

    assert cap.sent == [RX_FRAME]
    frame = cap.sent[0]
    assert len(frame) == FRAME_LEN
    assert frame[:6] == b"\xff" * 6
    assert frame[12:14] == ETHERTYPE
    assert frame[14:18] == RX_MARKER == b"\xDE\xAD\xBE\xEF"
    assert result == RX_MARKER + b"\x01"
    assert transport.resumes == 1 and transport.deleted == [1]


def test_rx_scenario_fails_when_the_host_send_fails():
    transport = FakeTransport(on_resume=_c64_poll_times_out)
    cap = FakeCapture(send_error=OSError(1, "Operation not permitted"))

    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    msg = str(ei.value)
    assert "Operation not permitted" in msg
    assert cap.sent == []


def test_rx_scenario_fails_not_skips_when_the_c64_never_sees_the_frame():
    transport = FakeTransport(on_resume=_c64_poll_times_out)
    cap = FakeCapture()

    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    assert "never reached the CS8900a" in str(ei.value)
    # The frame *was* put on the wire; the failure is on the C64 side.
    assert cap.sent == [RX_FRAME]


def test_rx_scenario_rejects_a_wrong_marker():
    def _wrong_marker(ram: bytearray) -> None:
        ram[0xC000:0xC004] = b"\x00\x00\x00\x00"
        ram[0xC004] = 0x01

    transport = FakeTransport(on_resume=_wrong_marker)
    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, FakeCapture(), send_delay=0.0, timeout=1.0)
    assert "marker" in str(ei.value).lower()
