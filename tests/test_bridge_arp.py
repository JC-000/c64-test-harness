"""ARP on the two-VICE bridge (issue #218).

The ping and responder routines in ``bridge_ping`` never sent or answered
ARP, so a macOS host queued every echo reply behind a stale neighbour
entry (0/8 without an ARP request first, 6/6 with -- issue #212).  #218
gives the ping routines an ARP-first transmit and the responders an ARP
answer.  ``tests/test_cs8900a_arp.py`` proves both on a simulated chip;
these tests prove them on VICE's CS8900a emulation across a real bridge.

Two of the four use the host-side capture (``c64_test_harness.capture``)
to inject an ARP request from a *host* identity and to watch the wire
order of what VICE A transmits; the other two need only the two VICE
instances.  On this bench a root VICE takes the two lowest free
``/dev/bpf*`` nodes and one stray holder leaves exactly one for the
harness (CLAUDE.md, macOS trap 4), which is why each capture is opened
inside its test and closed before the test ends, never at module scope.

.. note::

    Written without being run: authoring happened in a worktree that
    must not spawn VICE (root VICE, sudo'd bridge scripts at canonical
    paths, and a bench shared with another project's x64sc).  They are
    modelled line by line on ``tests/test_bridge_ping.py`` (fixture,
    markers, thread shape, memory layout) and ``tests/ethernet_scenarios.py``
    (capture open/skip/fail disposition, send/recv).  Run them from the
    canonical checkout with the bridge up.

Memory layout -- the ARP-capable routines are larger than the legacy
ones (consume routine 585 bytes, full responder 630, TOD responder 754,
ping-and-wait 319 -- measured 2026-09-05), so the code windows below
are sized for them; the 480-byte ``$C000-$C1DF`` window the legacy
``build_icmp_responder_code`` used in ``test_bridge_ping.py`` does *not*
fit an ARP-enabled responder.
"""

from __future__ import annotations

import threading
import time

import pytest

from bridge_platform import (
    BRIDGE_IP_A,
    BRIDGE_IP_B,
    IFACE_A,
    IFACE_B,
    bridge_ip,
    probe_vice_pcap_ok,
)
from ethernet_scenarios import capture_failure_disposition

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.bridge_ping import (
    Asm,
    _emit_clockport_enable,
    _emit_read_frame,
    build_arp_request_frame,
    build_echo_request_frame,
    build_ping_and_wait_code,
    build_rx_peek_code,
    build_tx_code,
    parse_arp,
    run_icmp_responder,
    run_ping_and_wait,
)
from c64_test_harness.capture import (
    CaptureTimeout,
    CaptureUnavailable,
    PacketCapture,
    open_capture,
)
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.poll_until import poll_until_ready

# ---------------------------------------------------------------------------
# Skip conditions (identical to test_bridge_ping.py)
# ---------------------------------------------------------------------------

_PCAP_OK, _PCAP_REASON = probe_vice_pcap_ok(iface=IFACE_A)

pytestmark = [
    pytest.mark.vice_live,
    # Reaching the binary monitor is not proof of capture: a VICE whose
    # rawnet driver never attached still emulates the CS8900 registers, so
    # these tests would assert on register readbacks with zero host traffic
    # and pass vacuously.  probe_vice_pcap_ok() demands a real /dev/bpf*
    # attach.  See issue #144.  Module-specific (an active launch probe,
    # not a static prerequisite), so it stays outside the marker.
    pytest.mark.skipif(not _PCAP_OK, reason=_PCAP_REASON),
    # IFACE_A / IFACE_B / BRIDGE_NAME all present and up.
    pytest.mark.elevation("bridge_iface"),
]

# ---------------------------------------------------------------------------
# Memory layout (see the module docstring for the sizes that drove it)
# ---------------------------------------------------------------------------
PEEK_ADDR = 0xC000          # build_rx_peek_code, ~64 bytes
PING_CODE = 0xC000          # build_ping_and_wait_code + ARP, 319 bytes -> < $C140
RESULT = 0xC1F0
CONSUME_ADDR = 0xC200       # ARP-capable consume routine, 585 bytes -> < $C44A
TX_FRAME_BUF = 0xC500
ARP_FRAME_BUF = 0xC580
RX_FRAME_BUF = 0xC700

MAC_A = bytes.fromhex("02C640000001")
MAC_B = bytes.fromhex("02C640000002")
IP_A = BRIDGE_IP_A
IP_B = BRIDGE_IP_B

# A host identity for frames the capture injects.  Locally administered
# MAC, an address in the harness's reserved range that no VICE uses.
HOST_MAC = bytes.fromhex("02C6400000FE")
HOST_IP = bridge_ip(254)

PING_ID = 0xA21D
PING_SEQ = 0x0001
PING_PAYLOAD = b"ARP_THEN_PING_A"

_RX_BYTES = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_capture_or_dispose(iface: str) -> PacketCapture:
    """Open the host capture on *iface*; skip only for genuine absence.

    Same disposition as the ethernet TX/RX scenarios (issue #158): a
    capability the host lacks skips, a path that exists and is broken
    fails, and with a root VICE up ``denied`` is a bench misconfiguration.
    """
    try:
        return open_capture(iface)
    except CaptureUnavailable as exc:
        verdict, reason = capture_failure_disposition(exc, iface=iface, vice_live=True)
        if verdict == "skip":
            pytest.skip(reason)
        pytest.fail(reason)


def _is_arp_reply_from(ip: bytes):
    def match(frame: bytes) -> bool:
        pkt = parse_arp(frame)
        return pkt is not None and pkt.is_reply and pkt.sender_ip == ip
    return match


def _is_echo_reply_from(ip: bytes, ident: int, seq: int):
    def match(frame: bytes) -> bool:
        return (
            len(frame) >= 42
            and frame[12:14] == b"\x08\x00"
            and frame[23] == 0x01
            and frame[34] == 0x00
            and frame[26:30] == ip
            and frame[38:42] == bytes([ident >> 8, ident & 0xFF, seq >> 8, seq & 0xFF])
        )
    return match


def _build_drain_one_frame_code(load_addr: int, rx_buf: int, result_addr: int) -> bytes:
    """Consume routine: read the waiting frame into ``rx_buf``, result 0x01.

    The host decides what the frame is (``parse_arp`` on the buffer); this
    is the peek/consume pattern with the match moved to Python.
    """
    a = Asm(org=load_addr)
    a.emit(0x78)
    _emit_clockport_enable(a)
    _emit_read_frame(a, rx_buf)
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)
    return a.build()


def _run_threads(*targets, join_timeout: float = 45.0) -> None:
    threads = [threading.Thread(target=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=join_timeout)


class _Outcome:
    """One thread's result or exception, for a readable failure message."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.value: int | None = None
        self.error: Exception | None = None

    def run(self, fn) -> None:
        try:
            self.value = fn()
        except Exception as e:  # noqa: BLE001 - re-raised by check()
            self.error = e

    def check(self, extra: str = "") -> int:
        if self.error is not None:
            raise AssertionError(f"{self.name} raised: {self.error!r}{extra}") from self.error
        assert self.value is not None, f"{self.name} did not finish{extra}"
        return self.value


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBridgeArpResponder:
    """B answers ARP requests for its IP, then still answers the echo."""

    def test_responder_answers_host_arp_request(
        self,
        bridge_vice_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        """Host injects "who has IP_B" on B's interface; B's responder replies.

        Deliverable 4(a) of issue #218.  The reply must carry B's MAC/IP
        as sender and the host's as target, unicast to the host's MAC.
        The same responder then answers a host-injected echo request, so
        the ARP path provably returns to waiting for the ping.
        """
        _, transport_b = bridge_vice_pair
        write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

        b = _Outcome("B responder")
        b_thread = threading.Thread(
            target=b.run,
            args=(lambda: run_icmp_responder(
                transport_b,
                rx_buf=RX_FRAME_BUF,
                my_ip=IP_B,
                result_addr=RESULT,
                timeout_s=20.0,
                peek_addr=PEEK_ADDR,
                consume_addr=CONSUME_ADDR,
                my_mac=MAC_B,
            ),),
            daemon=True,
        )

        with _open_capture_or_dispose(IFACE_B) as cap:
            b_thread.start()
            time.sleep(1.0)  # let B start polling before the request goes out

            cap.send(build_arp_request_frame(HOST_MAC, HOST_IP, IP_B))
            try:
                reply = cap.recv(10.0, match=_is_arp_reply_from(IP_B))
            except CaptureTimeout as exc:
                b_rx = bytes(read_bytes(transport_b, RX_FRAME_BUF, _RX_BYTES))
                pytest.fail(
                    f"no ARP reply from B on {IFACE_B}: {exc}; b_rx={b_rx.hex()}"
                )
            pkt = parse_arp(reply)
            assert pkt is not None and pkt.is_reply
            assert pkt.sender_mac == MAC_B, f"sender MAC {pkt.sender_mac.hex()} != B"
            assert pkt.sender_ip == IP_B, f"sender IP {pkt.sender_ip.hex()} != B"
            assert pkt.target_mac == HOST_MAC and pkt.target_ip == HOST_IP, (
                "target must be the asker"
            )
            assert pkt.dst_mac == HOST_MAC and pkt.src_mac == MAC_B, (
                "reply must be unicast from B to the asker"
            )

            # And the responder is still there for the echo request.
            echo = build_echo_request_frame(
                src_mac=HOST_MAC, dst_mac=MAC_B, src_ip=HOST_IP, dst_ip=IP_B,
                identifier=PING_ID, sequence=PING_SEQ, payload=PING_PAYLOAD,
            )
            cap.send(echo.frame)
            try:
                cap.recv(10.0, match=_is_echo_reply_from(IP_B, PING_ID, PING_SEQ))
            except CaptureTimeout as exc:
                pytest.fail(f"B answered ARP but not the echo request that followed: {exc}")

        b_thread.join(timeout=30.0)
        assert b.check() == 0x01, "B's responder must report the echo reply sent"

    def test_responder_answers_peer_vice_arp_request(
        self,
        bridge_vice_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        """Two-VICE only (no host capture): A ARPs for B, reads B's reply.

        A transmits an ARP request with :func:`build_tx_code`, then drains
        frames through the peek/consume pattern until one parses as an
        ARP reply from B; the host checks its fields.  A then pings B with
        ``arp=False`` so B's responder returns and the test proves the
        responder went back to waiting after answering ARP.
        """
        transport_a, transport_b = bridge_vice_pair
        write_bytes(transport_a, RX_FRAME_BUF, [0x00] * 256)
        write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

        b = _Outcome("B responder")
        a = _Outcome("A arp+ping")

        def a_side() -> int:
            time.sleep(1.0)  # B first
            arp = build_arp_request_frame(MAC_A, IP_A, IP_B)
            load_code(transport_a, CONSUME_ADDR,
                      build_tx_code(CONSUME_ADDR, TX_FRAME_BUF, len(arp), RESULT))
            write_bytes(transport_a, TX_FRAME_BUF, arp)
            write_bytes(transport_a, RESULT, [0x00])
            jsr(transport_a, CONSUME_ADDR, timeout=5.0)
            assert read_bytes(transport_a, RESULT, 1)[0] == 0x01, "A's ARP TX did not complete"

            load_code(transport_a, PEEK_ADDR, build_rx_peek_code(PEEK_ADDR, RESULT))
            load_code(transport_a, CONSUME_ADDR,
                      _build_drain_one_frame_code(CONSUME_ADDR, RX_FRAME_BUF, RESULT))
            deadline = time.monotonic() + 10.0
            seen: list[bytes] = []
            got_reply = False
            while time.monotonic() < deadline and not got_reply:
                if poll_until_ready(transport_a, code_addr=PEEK_ADDR, result_addr=RESULT,
                                    timeout_s=deadline - time.monotonic()) != 0x01:
                    break
                write_bytes(transport_a, RESULT, [0x00])
                jsr(transport_a, CONSUME_ADDR, timeout=5.0)
                frame = bytes(read_bytes(transport_a, RX_FRAME_BUF, _RX_BYTES))
                seen.append(frame)
                pkt = parse_arp(frame)
                if pkt is not None and pkt.is_reply and pkt.sender_ip == IP_B:
                    assert pkt.sender_mac == MAC_B, f"sender MAC {pkt.sender_mac.hex()} != B"
                    assert pkt.target_mac == MAC_A and pkt.target_ip == IP_A, "target must be A"
                    assert pkt.dst_mac == MAC_A, "reply must be unicast to A"
                    got_reply = True
            if not got_reply:
                raise AssertionError(
                    f"no ARP reply from B reached A; frames seen: {[f[:14].hex() for f in seen]}"
                )

            echo = build_echo_request_frame(
                src_mac=MAC_A, dst_mac=MAC_B, src_ip=IP_A, dst_ip=IP_B,
                identifier=PING_ID, sequence=PING_SEQ, payload=PING_PAYLOAD,
            )
            return run_ping_and_wait(
                transport_a,
                tx_frame=echo.frame,
                rx_buf=RX_FRAME_BUF,
                result_addr=RESULT,
                identifier=PING_ID,
                sequence=PING_SEQ,
                tx_frame_buf=TX_FRAME_BUF,
                timeout_s=15.0,
                peek_addr=PEEK_ADDR,
                consume_addr=CONSUME_ADDR,
                arp=False,
            )

        _run_threads(
            lambda: b.run(lambda: run_icmp_responder(
                transport_b, rx_buf=RX_FRAME_BUF, my_ip=IP_B, result_addr=RESULT,
                timeout_s=25.0, peek_addr=PEEK_ADDR, consume_addr=CONSUME_ADDR,
                my_mac=MAC_B,
            )),
            lambda: a.run(a_side),
        )

        b_rx = bytes(read_bytes(transport_b, RX_FRAME_BUF, _RX_BYTES))
        extra = f"\n b_rx={b_rx.hex()}"
        assert a.check(extra) == 0x01, f"A did not get its echo reply{extra}"
        assert b.check(extra) == 0x01, f"B's responder did not finish{extra}"


class TestBridgeArpPinger:
    """A resolves B before pinging, in one 6502 run and via the orchestrator."""

    def _collect_from_a(self, cap: PacketCapture, count: int, timeout: float) -> list[bytes]:
        frames: list[bytes] = []
        deadline = time.monotonic() + timeout
        while len(frames) < count and time.monotonic() < deadline:
            try:
                frames.append(cap.recv(
                    max(0.1, deadline - time.monotonic()),
                    match=lambda f: f[6:12] == MAC_A,
                ))
            except CaptureTimeout:
                break
        return frames

    def test_ping_routine_emits_arp_then_icmp_in_one_run(
        self,
        bridge_vice_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        """Deliverable 4(b): ``build_ping_and_wait_code(arp_frame_buf=...)``
        puts the ARP request and then the echo request on the wire, in that
        order, from a single ``jsr``; B answers both; A matches the reply."""
        transport_a, transport_b = bridge_vice_pair
        write_bytes(transport_a, RX_FRAME_BUF, [0x00] * 256)
        write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

        echo = build_echo_request_frame(
            src_mac=MAC_A, dst_mac=MAC_B, src_ip=IP_A, dst_ip=IP_B,
            identifier=PING_ID, sequence=PING_SEQ, payload=PING_PAYLOAD,
        )
        arp = build_arp_request_frame(MAC_A, IP_A, IP_B)
        load_code(transport_a, PING_CODE, build_ping_and_wait_code(
            load_addr=PING_CODE,
            tx_frame_buf=TX_FRAME_BUF,
            tx_frame_len=len(echo.frame),
            rx_buf=RX_FRAME_BUF,
            result_addr=RESULT,
            identifier=PING_ID,
            sequence=PING_SEQ,
            arp_frame_buf=ARP_FRAME_BUF,
            arp_frame_len=len(arp),
        ))
        write_bytes(transport_a, TX_FRAME_BUF, echo.frame)
        write_bytes(transport_a, ARP_FRAME_BUF, arp)
        write_bytes(transport_a, RESULT, [0x00])

        b = _Outcome("B responder")
        a = _Outcome("A ping")

        def a_side() -> int:
            time.sleep(1.0)
            jsr(transport_a, PING_CODE, timeout=30.0)
            return read_bytes(transport_a, RESULT, 1)[0]

        with _open_capture_or_dispose(IFACE_A) as cap:
            _run_threads(
                lambda: b.run(lambda: run_icmp_responder(
                    transport_b, rx_buf=RX_FRAME_BUF, my_ip=IP_B, result_addr=RESULT,
                    timeout_s=20.0, peek_addr=PEEK_ADDR, consume_addr=CONSUME_ADDR,
                    my_mac=MAC_B,
                )),
                lambda: a.run(a_side),
            )
            frames = self._collect_from_a(cap, 2, timeout=5.0)

        kinds = [f[12:14].hex() for f in frames]
        assert len(frames) >= 2, f"expected ARP then ICMP from A on {IFACE_A}, saw {kinds}"
        first = parse_arp(frames[0])
        assert first is not None and first.is_request and first.target_ip == IP_B, (
            f"first frame from A is not an ARP request for B: {frames[0][:42].hex()}"
        )
        assert frames[1][12:14] == b"\x08\x00" and frames[1][34] == 0x08, (
            f"second frame from A is not an ICMP echo request: {frames[1][:42].hex()}"
        )

        a_rx = bytes(read_bytes(transport_a, RX_FRAME_BUF, _RX_BYTES))
        extra = f"\n a_rx={a_rx.hex()} wire={kinds}"
        assert b.check(extra) == 0x01, f"B did not answer the echo{extra}"
        assert a.check(extra) == 0x01, f"A did not match the echo reply{extra}"
        assert a_rx[34] == 0x00 and a_rx[26:30] == IP_B, "A's buffer must hold B's echo reply"

    def test_run_ping_and_wait_arps_by_default(
        self,
        bridge_vice_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        """The orchestrator's default now resolves first: wire order ARP, ICMP."""
        transport_a, transport_b = bridge_vice_pair
        write_bytes(transport_a, RX_FRAME_BUF, [0x00] * 256)
        write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

        echo = build_echo_request_frame(
            src_mac=MAC_A, dst_mac=MAC_B, src_ip=IP_A, dst_ip=IP_B,
            identifier=PING_ID, sequence=PING_SEQ + 1, payload=PING_PAYLOAD,
        )
        b = _Outcome("B responder")
        a = _Outcome("A ping")

        def a_side() -> int:
            time.sleep(1.0)
            return run_ping_and_wait(
                transport_a, tx_frame=echo.frame, rx_buf=RX_FRAME_BUF, result_addr=RESULT,
                identifier=PING_ID, sequence=PING_SEQ + 1, tx_frame_buf=TX_FRAME_BUF,
                timeout_s=15.0, peek_addr=PEEK_ADDR, consume_addr=CONSUME_ADDR,
            )

        with _open_capture_or_dispose(IFACE_A) as cap:
            _run_threads(
                lambda: b.run(lambda: run_icmp_responder(
                    transport_b, rx_buf=RX_FRAME_BUF, my_ip=IP_B, result_addr=RESULT,
                    timeout_s=20.0, peek_addr=PEEK_ADDR, consume_addr=CONSUME_ADDR,
                    my_mac=MAC_B,
                )),
                lambda: a.run(a_side),
            )
            frames = self._collect_from_a(cap, 2, timeout=5.0)

        kinds = [f[12:14].hex() for f in frames]
        assert kinds[:2] == ["0806", "0800"], f"wire order from A was {kinds}"
        pkt = parse_arp(frames[0])
        assert pkt is not None and pkt.is_request and pkt.target_ip == IP_B
        assert pkt.sender_mac == MAC_A and pkt.sender_ip == IP_A, (
            "the orchestrator must derive the ARP sender from the echo frame"
        )
        extra = f"\n wire={kinds}"
        assert b.check(extra) == 0x01
        assert a.check(extra) == 0x01
