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
        seen = 0
        while self.frames:
            frame = self.frames.pop(0)
            seen += 1
            if match is None or match(frame):
                return frame
        raise CaptureTimeout(f"fake: nothing on {self.iface}", seen=seen)

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
    assert "captured on fake0" in str(ei.value)


def test_tx_failure_states_what_was_measured_and_the_vice_descriptor_counters(monkeypatch):
    """The message says what was seen, not what was concluded, and carries
    VICE's descriptor counters so a direction fault (Written=1) can be
    told from a chip fault (Written=0) without re-running anything."""
    import ethernet_scenarios
    monkeypatch.setattr(
        ethernet_scenarios, "bpf_descriptor_summary",
        lambda iface=None: f"netstat -B {iface}: bpf2 Recv=12 Written=1 x64sc.4326",
    )
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    cap = FakeCapture([STRAY_IPV4, STRAY_IPV4])

    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, cap, timeout=0.5)
    msg = str(ei.value)
    assert "no frame with ethertype 88b5 captured on fake0 within 0.5s (2 non-matching seen)" in msg
    assert "netstat -B fake0: bpf2 Recv=12 Written=1 x64sc.4326" in msg


def test_rx_failure_states_the_poll_timeout_and_the_vice_descriptor_counters(monkeypatch):
    import ethernet_scenarios
    monkeypatch.setattr(
        ethernet_scenarios, "bpf_descriptor_summary",
        lambda iface=None: f"netstat -B {iface}: bpf2 Recv=13 Written=0 x64sc.4326",
    )
    transport = FakeTransport(on_resume=_c64_poll_times_out)
    cap = FakeCapture()

    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    msg = str(ei.value)
    assert "C64 poll for RxOK timed out" in msg
    assert "after the host wrote 64 bytes to fake0" in msg
    assert "netstat -B fake0: bpf2 Recv=13 Written=0 x64sc.4326" in msg


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


# ---------------------------------------------------------------------------
# B1: the routines must enable the chip before touching TX/RX
# ---------------------------------------------------------------------------
#
# VICE 3.10 src/core/cs8900.c: reset clears tx_enabled/rx_enabled (:420-421)
# and only a LineCTL (PP 0x0112) write sets them (:923-931, SerTxON 0x0080 /
# SerRxON 0x0040; reset LineCTL is 0x0013, both clear).  TxLength acceptance
# raises Rdy4TxNOW regardless (:969-1002), so a routine that skips the enable
# polls through, writes all 64 bytes, and :780 `if (!tx_enabled)` drops the
# frame before rawnet_arch_transmit.  RX: :1060 `if (!rx_enabled)` means RxOK
# never appears, and reset RxCTL 0x0005 lacks BroadcastA; :594 accepts a
# broadcast only when promiscuous.  bridge_ping.py and test_ethernet_bridge.py
# already write RxCTL=0x00D8 and OR 0x00C0 into LineCTL; these routines must
# too, and before the first TX command / RxEvent poll.

from ethernet_scenarios import rx_routine, tx_routine  # noqa: E402

_PPTR_LO, _PPTR_HI, _PPDATA_LO, _PPDATA_HI = 0x02, 0x03, 0x04, 0x05
# PPPtr = 0x0104 (RxCTL); PPData = 0x00D8 (PromiscuousA + the value the live
# bridge path uses).
RXCTL_WRITE = bytes([
    0xA9, 0x04, 0x8D, _PPTR_LO, 0xDE, 0xA9, 0x01, 0x8D, _PPTR_HI, 0xDE,
    0xA9, 0xD8, 0x8D, _PPDATA_LO, 0xDE, 0xA9, 0x00, 0x8D, _PPDATA_HI, 0xDE,
])
# PPPtr = 0x0112 (LineCTL); PPData lo |= 0xC0 (SerRxON | SerTxON), read-OR-write
# so the other LineCTL bits survive.
LINECTL_OR_C0 = bytes([
    0xA9, 0x12, 0x8D, _PPTR_LO, 0xDE, 0xA9, 0x01, 0x8D, _PPTR_HI, 0xDE,
    0xAD, _PPDATA_LO, 0xDE, 0x09, 0xC0, 0x8D, _PPDATA_LO, 0xDE,
])
STA_TXCMD_LO = bytes([0x8D, 0x0C, 0xDE])          # first TX command register write
LDA_RXEVENT_PTR = bytes([0xA9, 0x24, 0x8D, _PPTR_LO, 0xDE])  # PPPtr = 0x0124 (RxEvent)


def _index(hay: bytes, needle: bytes, what: str) -> int:
    i = hay.find(needle)
    assert i >= 0, f"{what} ({needle.hex()}) not present in routine {hay.hex()}"
    return i


def test_tx_routine_enables_the_chip_before_the_first_tx_command():
    code = tx_routine()
    rxctl = _index(code, RXCTL_WRITE, "RxCTL=0x00D8 write")
    linectl = _index(code, LINECTL_OR_C0, "LineCTL |= 0x00C0 write-back")
    txcmd = _index(code, STA_TXCMD_LO, "STA TXCMD")
    assert rxctl < txcmd and linectl < txcmd, (
        f"chip enable at {rxctl}/{linectl} must precede the TX command at {txcmd}"
    )


def test_rx_routine_enables_the_chip_before_polling_rxevent():
    code = rx_routine()
    rxctl = _index(code, RXCTL_WRITE, "RxCTL=0x00D8 write")
    linectl = _index(code, LINECTL_OR_C0, "LineCTL |= 0x00C0 write-back")
    poll = _index(code, LDA_RXEVENT_PTR, "PPPtr = RxEvent")
    assert rxctl < poll and linectl < poll, (
        f"chip enable at {rxctl}/{linectl} must precede the RxEvent poll at {poll}"
    )


def test_chip_enable_bytes_are_the_bridge_ping_ones_not_a_second_copy():
    """The inline enable comes from bridge_ping's builders, whose RTS-terminated
    forms the live bridge tests already exercise; no parallel byte copy."""
    from c64_test_harness.bridge_ping import (
        cs8900a_enable_inline_code,
        cs8900a_rxctl_code,
        cs8900a_rxctl_inline_code,
    )
    inline = cs8900a_enable_inline_code()
    assert inline.find(RXCTL_WRITE) >= 0 and inline.find(LINECTL_OR_C0) >= 0
    assert not inline.endswith(b"\x60"), "inline form must not RTS mid-routine"
    assert cs8900a_rxctl_code() == cs8900a_rxctl_inline_code() + b"\x60"
    assert tx_routine().startswith(inline) and rx_routine().startswith(inline)


# ---------------------------------------------------------------------------
# S3: the fixture's skip-vs-fail decision, unit-tested per cause
# ---------------------------------------------------------------------------

from c64_test_harness.capture import CaptureUnavailable  # noqa: E402
from ethernet_scenarios import capture_failure_disposition  # noqa: E402


@pytest.mark.parametrize("cause", ["denied", "no-nodes", "cap-net-raw", "platform"])
def test_genuine_absence_skips_with_the_remedy_in_the_reason(cause):
    exc = CaptureUnavailable("nothing to open.", remedy="sudo chmod o+rw /dev/bpf*", cause=cause)
    verdict, reason = capture_failure_disposition(exc, iface="feth0")
    assert verdict == "skip"
    assert "feth0" in reason and "sudo chmod o+rw /dev/bpf*" in reason


@pytest.mark.parametrize("cause", ["busy", "bind", "dlt", "linux-bind", "unknown"])
def test_present_but_broken_path_fails_with_the_remedy(cause):
    exc = CaptureUnavailable("pool eaten.", remedy="sudo chmod o+rw /dev/bpf*", cause=cause)
    verdict, reason = capture_failure_disposition(exc, iface="feth0")
    assert verdict == "fail"
    assert "feth0" in reason and "sudo chmod o+rw /dev/bpf*" in reason
    assert "not absence" in reason


# ---------------------------------------------------------------------------
# S5: the peer-interface knob, so a live direction fault can be pivoted on
# ---------------------------------------------------------------------------
#
# feth0 and feth1 are a peer pair (the interface listing shows feth0 with
# "peer: feth1").  A frame VICE injects on feth0 is *outgoing* there and
# *incoming* on feth1; a frame the host writes to feth0's BPF emerges from
# feth1.  The default binds everything to the VICE interface and relies on
# BIOCSSEESENT for TX and the driver's tap on the write path for RX.  If
# the live TX run fails with VICE's descriptor showing Written=1, that
# assumption is wrong and the capture must bind the peer instead --
# without a code change.

from ethernet_scenarios import resolve_capture_ifaces  # noqa: E402


class PeerCapture(FakeCapture):
    iface = "peer0"


def test_rx_scenario_sends_through_send_capture_when_given():
    transport = FakeTransport(on_resume=_c64_receives_marker)
    cap, peer = FakeCapture(), PeerCapture()

    run_rx_scenario(transport, cap, send_capture=peer, send_delay=0.0, timeout=1.0)

    assert peer.sent == [RX_FRAME]
    assert cap.sent == [], "the frame must go out exactly once, on the send capture"


def test_rx_failure_names_the_interface_the_frame_was_written_to(monkeypatch):
    import ethernet_scenarios
    monkeypatch.setattr(ethernet_scenarios, "bpf_descriptor_summary", lambda iface=None: f"ns[{iface}]")
    transport = FakeTransport(on_resume=_c64_poll_times_out)
    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, FakeCapture(), send_capture=PeerCapture(), send_delay=0.0, timeout=1.0)
    msg = str(ei.value)
    assert "after the host wrote 64 bytes to peer0" in msg
    # Counters for both sides: where we wrote, and where VICE listens.
    assert "ns[peer0]" in msg and "ns[fake0]" in msg


def test_rx_scenario_defaults_to_sending_on_the_capture_itself():
    transport = FakeTransport(on_resume=_c64_receives_marker)
    cap = FakeCapture()
    run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    assert cap.sent == [RX_FRAME]


def test_resolve_capture_ifaces_defaults_to_the_vice_interface():
    assert resolve_capture_ifaces("feth0", {}) == ("feth0", "feth0")


def test_resolve_capture_ifaces_reads_the_two_env_knobs():
    env = {"C64_ETH_CAPTURE_IFACE": "feth1"}
    assert resolve_capture_ifaces("feth0", env) == ("feth1", "feth1"), (
        "the send side follows the capture side unless overridden separately"
    )
    env = {"C64_ETH_CAPTURE_IFACE": "feth1", "C64_ETH_SEND_IFACE": "feth0"}
    assert resolve_capture_ifaces("feth0", env) == ("feth1", "feth0")
    env = {"C64_ETH_SEND_IFACE": "feth1"}
    assert resolve_capture_ifaces("feth0", env) == ("feth0", "feth1")


def test_resolve_capture_ifaces_ignores_blank_values():
    assert resolve_capture_ifaces("feth0", {"C64_ETH_CAPTURE_IFACE": "  "}) == ("feth0", "feth0")
