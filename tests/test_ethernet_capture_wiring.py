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
    RESULT,
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
    ram[RESULT] = 0x01


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


def _c64_poll_times_out(ram: bytearray) -> None:
    ram[RESULT] = 0xFF


def _marker_step(marker: bytes):
    def step(ram: bytearray) -> None:
        ram[RESULT:RESULT + 4] = marker
        ram[RESULT + 4] = 0x01
    return step


STALE_LOOPBACK = bytes([0xC6, 0x40, 0xC6, 0x40])  # payload of the TX test's own frame


def scripted(*steps):
    """One behaviour per resume(); the last step repeats.

    run_rx_scenario first *drains* stale frames (runs the routine until its
    poll times out) and only then sends, so the "C64 receives our marker"
    story is: nothing pending (poll times out), then our frame.
    """
    remaining = list(steps)

    def on_resume(ram: bytearray) -> None:
        step = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        step(ram)
    return on_resume


#: Drain finds nothing, then the host frame arrives with our marker.
_c64_receives_marker = scripted(_c64_poll_times_out, _marker_step(RX_MARKER))


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
    # One drain run (poll timed out: nothing pending) + the real attempt.
    assert transport.resumes == 2 and transport.deleted == [1, 2]


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
    _wrong_marker = scripted(_c64_poll_times_out, _marker_step(b"\x00\x00\x00\x00"))

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


# ---------------------------------------------------------------------------
# NITs: mutants the reviewer found alive in the scenarios
# ---------------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402


def test_tx_scenario_rejects_a_wrong_destination_mac():
    wrong = b"\x02\xc6\x40\x00\x00\x07" + FRAME_DATA[6:]
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, FakeCapture([wrong]), timeout=0.01)
    assert "Dest MAC" in str(ei.value)


def test_tx_scenario_rejects_a_wrong_source_mac():
    wrong = FRAME_DATA[:6] + b"\x02\xc6\x40\x00\x00\x07" + FRAME_DATA[12:]
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, FakeCapture([wrong]), timeout=0.01)
    assert "Source MAC" in str(ei.value)


def test_tx_scenario_rejects_a_truncated_frame():
    short = FRAME_DATA[:50]  # header intact, payload cut: the length check must fire first
    transport = FakeTransport(on_resume=_c64_sets_tx_flag)
    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, FakeCapture([short]), timeout=0.01)
    assert "too short: 50 < 64" in str(ei.value)


class TimestampingCapture(FakeCapture):
    def send(self, frame: bytes) -> None:
        self.sent_at = time.monotonic()
        super().send(frame)


def test_rx_scenario_really_waits_send_delay_before_writing():
    """A `pass` in place of time.sleep(send_delay) sends before the C64 is
    polling; the fake cannot tell, so the clock has to."""
    transport = FakeTransport(on_resume=_c64_receives_marker)
    cap = TimestampingCapture()
    started = time.monotonic()
    run_rx_scenario(transport, cap, send_delay=0.08, timeout=1.0)
    assert cap.sent_at - started >= 0.08


class HangingCapture(FakeCapture):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def send(self, frame: bytes) -> None:
        self.release.wait(5.0)
        super().send(frame)


def test_rx_scenario_fails_when_the_host_send_never_returns():
    transport = FakeTransport(on_resume=_c64_poll_times_out)
    cap = HangingCapture()
    try:
        with pytest.raises(AssertionError) as ei:
            run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0, join_timeout=0.05)
        assert "did not return" in str(ei.value) and "fake0" in str(ei.value)
    finally:
        cap.release.set()


# ---------------------------------------------------------------------------
# Live failure 2026-09-01: the 6502 never returned from the TX routine
# ---------------------------------------------------------------------------
#
# run_tx_scenario did load_code(CODE_BASE, ...) and then write_bytes(0xC000,
# [0]) to "clear the success flag" -- but CODE_BASE *is* 0xC000, so that
# zeroed the routine's first opcode into BRK.  The KERNAL BRK handler warm-
# starts BASIC and the trampoline's breakpoint is never reached:
# wait_for_stopped timed out after 10 s and nothing downstream (capture,
# netstat -B counters) was ever consulted.  The RX scenario had the same
# collision over five bytes.  Both were inherited from the original test.
#
# Two rules, pinned here: results live in DATA_BASE and no host write may
# land inside the loaded routine; and when a JSR does not return, the
# failure text must say where the CPU is (PC, the instruction window
# there, the CS8900a I/O window, and the first routine bytes), so this
# class of hang is diagnosed from the message rather than by re-running.

from ethernet_scenarios import DATA_BASE, rx_routine as _rx_routine, tx_routine as _tx_routine  # noqa: E402


class RecordingTransport(FakeTransport):
    """FakeTransport that logs every write_memory(addr, data) in order."""

    def __init__(self, on_resume=None) -> None:
        super().__init__(on_resume)
        self.writes: list[tuple[int, bytes]] = []

    def write_memory(self, addr: int, data) -> None:
        self.writes.append((addr, bytes(data)))
        super().write_memory(addr, data)


def _writes_inside_code_after_load(transport: RecordingTransport, code: bytes) -> list[tuple[int, bytes]]:
    loaded_at = next(i for i, (a, d) in enumerate(transport.writes) if a == CODE_BASE and d == code)
    lo, hi = CODE_BASE, CODE_BASE + len(code)
    return [(a, d) for a, d in transport.writes[loaded_at + 1:] if a < hi and a + len(d) > lo]


def test_tx_scenario_never_writes_over_the_loaded_routine():
    seen_first_bytes: list[bytes] = []

    def c64(ram: bytearray) -> None:
        seen_first_bytes.append(bytes(ram[CODE_BASE:CODE_BASE + 3]))
        ram[DATA_BASE] = 0x01

    transport = RecordingTransport(on_resume=c64)
    run_tx_scenario(transport, FakeCapture([FRAME_DATA]), timeout=0.01)
    assert _writes_inside_code_after_load(transport, _tx_routine()) == []
    assert seen_first_bytes == [_tx_routine()[:3]], "the first opcode must be intact at JSR time"


def test_rx_scenario_never_writes_over_the_loaded_routine():
    seen_first_bytes: list[bytes] = []
    story = scripted(_c64_poll_times_out, _marker_step(RX_MARKER))

    def c64(ram: bytearray) -> None:
        seen_first_bytes.append(bytes(ram[CODE_BASE:CODE_BASE + 3]))
        story(ram)

    transport = RecordingTransport(on_resume=c64)
    run_rx_scenario(transport, FakeCapture(), send_delay=0.0, timeout=1.0)
    assert _writes_inside_code_after_load(transport, _rx_routine()) == []
    assert seen_first_bytes == [_rx_routine()[:3]] * 2, "intact at every JSR (drain + attempt)"


def test_routines_store_results_in_data_base_not_in_their_own_page():
    for code in (_tx_routine(), _rx_routine()):
        # STA $C0xx (8D xx C0) anywhere in the routine would be a self-overwrite.
        for i in range(len(code) - 2):
            assert not (code[i] == 0x8D and code[i + 2] == (CODE_BASE >> 8)), (
                f"STA into the code page at offset {i}: {code[i:i+3].hex()}"
            )
    assert DATA_BASE >> 8 != CODE_BASE >> 8


class HangingTransport(FakeTransport):
    """The CPU never reaches the breakpoint; registers say where it is.

    ``on_resume`` models what RAM looks like while the CPU is stuck --
    here, a routine whose first opcode has been zeroed *after* it was
    loaded, which is what the live bench showed.  ``hang_on_resume`` says
    which resume() hangs (1 = the first; for RX that is the drain run).
    """

    def __init__(self, pc: int, on_resume=None, *, hang_on_resume: int = 1) -> None:
        super().__init__(on_resume)
        self._pc = pc
        self.hang_on_resume = hang_on_resume

    def wait_for_stopped(self, timeout: float = 0.0) -> None:
        if self.resumes >= self.hang_on_resume:
            raise TimeoutError(f"No stopped event within {timeout}s")

    def read_registers(self) -> dict[str, int]:
        return {"PC": self._pc, "A": 0x12, "X": 0x00, "Y": 0x40, "SP": 0xF6}


def test_jsr_timeout_reports_pc_disassembly_io_window_and_routine_bytes():
    def clobbered(ram: bytearray) -> None:
        ram[0xC000:0xC00B] = bytes([0x00, 0x01, 0xDE, 0x09, 0x01, 0x8D, 0x01, 0xDE, 0xF0, 0xF9, 0x60])
        ram[0xDE00:0xDE10] = bytes(range(0x10, 0x20))

    transport = HangingTransport(pc=0xC003, on_resume=clobbered)

    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, FakeCapture([FRAME_DATA]), timeout=0.01)
    msg = str(ei.value)
    assert "6502 did not return from $C000 within 10.0s" in msg
    assert "PC=$C003" in msg and "A=$12" in msg and "Y=$40" in msg
    # Disassembly window around PC, with the PC line marked.
    assert "C000  00        BRK" in msg
    assert "> C003  09 01     ORA #$01" in msg
    # CS8900a I/O window.
    assert "$DE00: 10 11 12 13 14 15 16 17 18 19 1a 1b 1c 1d 1e 1f" in msg
    # First bytes of the routine as loaded, so a clobbered opcode is visible.
    assert "code@$C000: 00 01 de 09" in msg


def test_rx_scenario_timeout_carries_the_same_cpu_report():
    """run_rx_scenario drives its own trampoline (the send has to overlap the
    poll), so it must not lose the report binary_jsr gives.  Live 2026-09-02:
    RX timed out and the message said only "No stopped event within 15.0s"."""
    def spinning(ram: bytearray) -> None:
        ram[0xDE00:0xDE10] = bytes(range(0x20, 0x30))
        ram[RESULT] = 0xFF  # the drain run: nothing pending

    # Drain run completes; the real attempt (second resume) hangs.
    transport = HangingTransport(pc=0xC02A, on_resume=spinning, hang_on_resume=2)
    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, FakeCapture(), send_delay=0.0, timeout=1.0)
    msg = str(ei.value)
    assert "6502 did not return from $C000 within 1.0s" in msg
    assert "PC=$C02A" in msg
    assert "$DE00: 20 21 22 23 24 25 26 27 28 29 2a 2b 2c 2d 2e 2f" in msg
    assert "> C02A" in msg
    # The host send happened (or its failure is reported) before the CPU verdict.
    assert "host wrote 64 bytes to fake0" in msg


def test_rx_scenario_hang_during_the_drain_says_so():
    transport = HangingTransport(pc=0xC02A)  # hangs on the first resume = drain
    cap = FakeCapture()
    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    assert "6502 did not return from $C000" in str(ei.value)
    assert "stale-frame drain" in str(ei.value) and "before any host send" in str(ei.value)
    assert cap.sent == []


from c64_test_harness.transport import TimeoutError as TransportTimeoutError  # noqa: E402


class HangingViceTransport(HangingTransport):
    """What the real BinaryViceTransport raises: the *transport's* TimeoutError,
    a TransportError -- not a subclass of the builtin.  Live 2026-09-02 the
    report never appeared because only the builtin was caught."""

    def wait_for_stopped(self, timeout: float = 0.0) -> None:
        raise TransportTimeoutError(f"No stopped event within {timeout}s")


def test_tx_jsr_timeout_report_fires_for_the_transports_own_timeout_error():
    transport = HangingViceTransport(pc=0xC010)
    with pytest.raises(AssertionError) as ei:
        run_tx_scenario(transport, FakeCapture([FRAME_DATA]), timeout=0.01)
    assert "6502 did not return from $C000 within 10.0s" in str(ei.value)
    assert "> C010" in str(ei.value)


def test_rx_timeout_report_fires_for_the_transports_own_timeout_error():
    transport = HangingViceTransport(pc=0xC02A)
    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, FakeCapture(), send_delay=0.0, timeout=1.0)
    assert "6502 did not return from $C000 within 1.0s" in str(ei.value)
    assert "> C02A" in str(ei.value)


# ---------------------------------------------------------------------------
# Live 2026-09-02, bisected: the RX routine's header-skip loop branched -8
# ---------------------------------------------------------------------------
#
# `.skip: LDA $DE08 / LDA $DE09 / DEX / BNE .skip` is 9 bytes, so the branch
# back is -9.  The routine (inherited from the original test) had -8, which
# lands on the `$08` operand byte = PHP: six iterations push six bytes, RTS
# pops garbage, and the CPU ends up in BASIC's READY loop instead of at the
# trampoline's checkpoint.  The wire was fine (VICE's descriptor Recv went
# 1 -> 2), the reads were fine (bisect: 34 straight RTDATA word reads return
# to the checkpoint), the loop was not.  Pinned generally: every branch in
# both routines lands on an instruction boundary inside the routine.

from c64_test_harness.disasm import disassemble  # noqa: E402

_BRANCHES = {"BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"}


def _branch_targets_off_boundary(code: bytes, base: int) -> list[str]:
    lines = disassemble(code, base)
    boundaries = {int(l[:4], 16) for l in lines}
    bad = []
    for line in lines:
        mn = line[16:19]
        if mn in _BRANCHES:
            target = int(line.split("$")[-1], 16)
            if target not in boundaries or not (base <= target < base + len(code)):
                bad.append(f"{line}  -> ${target:04X} is not an instruction boundary")
    return bad


@pytest.mark.parametrize("routine", [_tx_routine, _rx_routine], ids=["tx", "rx"])
def test_every_branch_lands_on_an_instruction_boundary(routine):
    assert _branch_targets_off_boundary(routine(), CODE_BASE) == []


def test_rx_header_skip_loop_branches_back_to_its_first_read():
    code = _rx_routine()
    i = code.find(bytes([0xA2, 0x07]))  # LDX #7
    assert i >= 0
    loop = i + 2
    assert code[loop:loop + 9] == bytes([0xAD, 0x08, 0xDE, 0xAD, 0x09, 0xDE, 0xCA, 0xD0, 0xF7]), (
        f"skip loop bytes {code[loop:loop + 9].hex()}: BNE must be -9 (F7), not -8 (F8)"
    )


# ---------------------------------------------------------------------------
# Stale frames: VICE's CS8900a hands the RX routine whatever it received
# first -- after the TX test that is VICE's *own* transmitted frame, seen
# back through pcap (Recv=1 on its descriptor).  cs8900_receive() replaces a
# pending frame with the next one on every RxEvent read, so draining is a
# matter of running the routine until its poll times out, then sending.
# ---------------------------------------------------------------------------


def test_rx_scenario_drains_stale_frames_before_sending():
    transport = FakeTransport(on_resume=scripted(
        _marker_step(STALE_LOOPBACK),          # drain run 1: the TX loopback frame
        _marker_step(b"\x01\x02\x03\x04"),     # drain run 2: another stale frame
        _c64_poll_times_out,                   # drain run 3: nothing pending
        _marker_step(RX_MARKER),               # the real attempt sees our frame
    ))

    class OrderCapture(FakeCapture):
        def send(self, frame: bytes) -> None:
            self.sent_after_resumes = transport.resumes
            super().send(frame)

    cap = OrderCapture()
    result = run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    assert result == RX_MARKER + b"\x01"
    assert cap.sent == [RX_FRAME]
    assert cap.sent_after_resumes == 3, "the frame goes out only after the drain timed out"
    assert transport.resumes == 4


def test_rx_scenario_gives_up_when_stale_frames_never_stop():
    transport = FakeTransport(on_resume=_marker_step(STALE_LOOPBACK))
    cap = FakeCapture()
    with pytest.raises(AssertionError) as ei:
        run_rx_scenario(transport, cap, send_delay=0.0, timeout=1.0)
    assert "stale" in str(ei.value) and "c640c640" in str(ei.value)
    assert cap.sent == [], "never send into a chip that is still draining"
