"""Live tests for the TOD-based bridge ping helpers.

Exercises the ``build_*_tod_code`` shippable-application variants in
``bridge_ping.py`` end-to-end.

Two test classes:

* ``TestBridgeIcmpRoundTripTodViceNormal`` -- full two-VICE round trip
  using the TOD variants at VICE normal speed.  This is the correctness
  proof for the shippable-application path.
* ``TestBridgeIcmpRoundTripTodViceWarp`` -- the same test marked
  ``xfail`` with a clear reason explaining that CIA1 TOD accelerates
  with the CPU under VICE warp (~31x on VICE 3.10), so TOD-based
  timeouts expire ~31x too fast.  This is documentation in the form
  of a test.

Ultimate 64 live tests live in ``TestTodPrimitiveU64Live`` (gated on
``U64_HOST`` env var).  These validate that CIA1 TOD runs at wall-
clock rate on real hardware across several turbo speeds using the
``build_tod_start_code`` / ``build_tod_read_tenths_code`` primitives.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from bridge_platform import BRIDGE_NAME, IFACE_A, IFACE_B, SETUP_HINT, iface_present
from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.bridge_ping import (
    build_echo_request_frame,
    build_icmp_responder_tod_code,
    build_ping_and_wait_tod_code,
)
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.tod_timer import (
    build_tod_read_tenths_code,
    build_tod_start_code,
)


# ---------------------------------------------------------------------------
# VICE bridge skip conditions
# ---------------------------------------------------------------------------
_HAS_X64SC = shutil.which("x64sc") is not None

_VICE_SKIPS = [
    pytest.mark.skipif(not _HAS_X64SC, reason="x64sc not found on PATH"),
    pytest.mark.skipif(
        not iface_present(IFACE_A),
        reason=f"{IFACE_A} not found ({SETUP_HINT})",
    ),
    pytest.mark.skipif(
        not iface_present(IFACE_B),
        reason=f"{IFACE_B} not found ({SETUP_HINT})",
    ),
    pytest.mark.skipif(
        not iface_present(BRIDGE_NAME),
        reason=(
            f"{BRIDGE_NAME} not found -- feth/tap peers alone aren't enough; "
            f"the host bridge must be up ({SETUP_HINT})"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Memory layout (must not overlap with TOD poll loop, which can reach
# ~400 bytes with data tables)
# ---------------------------------------------------------------------------
CODE = 0xC000
RESULT = 0xC7F0          # out of code + frame buffers
TX_FRAME_BUF = 0xC800
RX_FRAME_BUF = 0xCA00
_RX_BYTES = 60

MAC_A = bytes.fromhex("02C640000001")
MAC_B = bytes.fromhex("02C640000002")
IP_A = bytes([10, 0, 65, 2])
IP_B = bytes([10, 0, 65, 3])

PING_ID = 0xBEEF
PING_SEQ = 0x0001
PING_PAYLOAD = b"TOD_PING_FROM_A"


# ---------------------------------------------------------------------------
# VICE normal-mode round-trip test
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("bridge_vice_pair")
class TestBridgeIcmpRoundTripTodViceNormal:
    """TOD-based full ICMP round trip on bridged VICE pair (normal speed).

    This is the correctness proof for the shippable-application path:
    A runs ``build_ping_and_wait_tod_code`` and B runs
    ``build_icmp_responder_tod_code``; both 6502 routines enforce
    their own deadlines via CIA1 TOD without any host-side poll loop.
    """

    pytestmark = _VICE_SKIPS

    def test_icmp_round_trip_tod(
        self,
        bridge_vice_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        transport_a, transport_b = bridge_vice_pair

        echo = build_echo_request_frame(
            src_mac=MAC_A,
            dst_mac=MAC_B,
            src_ip=IP_A,
            dst_ip=IP_B,
            identifier=PING_ID,
            sequence=PING_SEQ,
            payload=PING_PAYLOAD,
        )
        frame_len = len(echo.frame)

        # B: TOD-based responder, 10-second TOD deadline
        responder_code = build_icmp_responder_tod_code(
            load_addr=CODE,
            rx_buf=RX_FRAME_BUF,
            my_ip=IP_B,
            result_addr=RESULT,
            deadline_tenths=100,  # 10.0 s
        )
        load_code(transport_b, CODE, responder_code)
        write_bytes(transport_b, RESULT, [0x00])
        write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

        # A: TOD-based ping-and-wait, 10-second TOD deadline
        ping_code = build_ping_and_wait_tod_code(
            load_addr=CODE,
            tx_frame_buf=TX_FRAME_BUF,
            tx_frame_len=frame_len,
            rx_buf=RX_FRAME_BUF,
            result_addr=RESULT,
            identifier=PING_ID,
            sequence=PING_SEQ,
            deadline_tenths=100,
        )
        load_code(transport_a, CODE, ping_code)
        write_bytes(transport_a, TX_FRAME_BUF, echo.frame)
        write_bytes(transport_a, RESULT, [0x00])
        write_bytes(transport_a, RX_FRAME_BUF, [0x00] * 256)

        rx_error: list[Exception] = []
        tx_error: list[Exception] = []

        def responder_worker() -> None:
            try:
                jsr(transport_b, CODE, timeout=30.0)
            except Exception as e:
                rx_error.append(e)

        def ping_worker() -> None:
            try:
                time.sleep(1.0)  # let B start polling first
                jsr(transport_a, CODE, timeout=30.0)
            except Exception as e:
                tx_error.append(e)

        tr = threading.Thread(target=responder_worker, daemon=True)
        tt = threading.Thread(target=ping_worker, daemon=True)
        tr.start()
        tt.start()
        tr.join(timeout=60.0)
        tt.join(timeout=60.0)

        b_result = read_bytes(transport_b, RESULT, 1)[0]
        a_result = read_bytes(transport_a, RESULT, 1)[0]
        b_rx = bytes(read_bytes(transport_b, RX_FRAME_BUF, _RX_BYTES))
        a_rx = bytes(read_bytes(transport_a, RX_FRAME_BUF, _RX_BYTES))

        if rx_error:
            raise AssertionError(
                f"responder raised: {rx_error[0]}\n"
                f"b_result=0x{b_result:02X} a_result=0x{a_result:02X}"
            ) from rx_error[0]
        if tx_error:
            raise AssertionError(
                f"pinger raised: {tx_error[0]}\n"
                f"b_result=0x{b_result:02X} a_result=0x{a_result:02X}"
            ) from tx_error[0]

        assert b_result == 0x01, (
            f"B TOD responder did not complete (result=0x{b_result:02X}); "
            f"b_rx={b_rx.hex()}"
        )
        assert a_result == 0x01, (
            f"A TOD ping-and-wait did not receive matching reply "
            f"(a_result=0x{a_result:02X}, b_result=0x{b_result:02X}); "
            f"a_rx={a_rx.hex()}"
        )

        # Verify A received an IPv4/ICMP echo reply from B
        assert a_rx[12:14] == b"\x08\x00"
        assert a_rx[23] == 0x01
        assert a_rx[34] == 0x00
        assert a_rx[26:30] == IP_B
        assert a_rx[30:34] == IP_A


# ---------------------------------------------------------------------------
# VICE warp-mode: documented as xfail
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "TOD accelerates with warp in VICE 3.10 (~31x wall). "
        "TOD-based 6502 timeouts expire ~31x too fast under warp "
        "mode and are therefore not usable for shippable-application "
        "tests that need a wall-clock deadline. Use the host-driven "
        "run_ping_and_wait path for warp-mode bridge tests."
    ),
    strict=False,
    run=False,
)
class TestBridgeIcmpRoundTripTodViceWarp:
    """Documents (via xfail) that the TOD path is not usable in warp mode.

    This class exists to record the intentional limitation of the
    shippable-application path: the TOD timeouts are expressed in
    wall-clock tenths, and VICE warp mode makes CIA1 TOD tick ~31x
    faster than wall.  A 5-second TOD deadline then expires in about
    160 ms of wall time, long before the bridge peer can respond.

    This is not a bug in the TOD helpers -- it is a fundamental
    mismatch between wall-clock expectations and VICE's warp mode
    semantics.  Real hardware and VICE normal run TOD at 1.0x wall.
    """

    def test_tod_under_warp_not_supported(self) -> None:
        pytest.fail("See class docstring: intentionally xfailed.")


# ---------------------------------------------------------------------------
# Ultimate 64 live primitive test (gated on U64_HOST)
# ---------------------------------------------------------------------------

_U64_HOST = os.environ.get("U64_HOST")


@pytest.mark.skipif(not _U64_HOST, reason="U64_HOST not set")
class TestTodPrimitiveU64Live:
    """Validate CIA1 TOD ticks at wall-clock rate on real U64 hardware.

    We do NOT run the full bridge ping on U64 -- there is no bridge
    peer here, only a single device.  Instead we exercise the TOD
    primitive alone: start TOD at 00:00:00.0, let the CPU idle for a
    known wall-clock duration, then read elapsed tenths and compare
    against the wall clock.

    The ratio ``reported / wall`` should be approximately 1.0x at
    every turbo speed the U64 supports (1 / 8 / 24 / 48 MHz), since
    CIA1 TOD is driven by an emulated 50/60 Hz mains zero-cross
    rather than the CPU clock.
    """

    # ZP-safe code region, past BASIC ($0801) and below KERNAL
    TOD_START_CODE_ADDR = 0xC000
    TOD_READ_CODE_ADDR = 0xC100
    WRAPPER_ADDR = 0xC200
    GO_FLAG = 0xC1E0          # host writes 0x01 here to trigger read
    DONE_FLAG = 0xC1E1        # 6502 writes 0x42 when done
    RESULT_ADDR = 0xC1E2      # LE16 elapsed tenths
    DIAG_ADDR = 0xC1E4        # diagnostic progress byte
    MAIN_LOOP = 0x0810        # idle PRG main loop

    @pytest.fixture(scope="class")
    def u64_client(self):
        """Acquire DeviceLock + Ultimate64Client; skip if unavailable."""
        from c64_test_harness.backends.device_lock import DeviceLock
        from c64_test_harness.backends.ultimate64 import Ultimate64Transport
        from c64_test_harness.backends.ultimate64_client import Ultimate64Client

        lock = DeviceLock(_U64_HOST)
        if not lock.acquire(timeout=120.0):
            pytest.skip(f"Could not acquire device lock for {_U64_HOST}")
        try:
            pw = os.environ.get("U64_PASSWORD")
            client = Ultimate64Client(host=_U64_HOST, password=pw, timeout=10.0)
            transport = Ultimate64Transport(host=_U64_HOST, password=pw, client=client)
            yield client, transport
        finally:
            lock.release()

    @staticmethod
    def _make_idle_prg() -> bytes:
        """Build a minimal PRG that installs a tight idle loop at $0810.

        Layout:
            $0801  BASIC stub: 10 SYS 2064
            $0810  main_loop: JMP $0810

        When run via ``client.run_prg``, the CPU will spin at $0810
        forever, which we can then hijack by writing ``JMP $C200`` at
        $0810 to redirect execution.
        """
        # PRG files start with a 2-byte load address (little-endian).
        header = bytes([0x01, 0x08])
        # BASIC line: 10 SYS 2064
        basic = bytes([
            0x0B, 0x08,                        # next line ptr ($080B)
            0x0A, 0x00,                        # line number 10
            0x9E,                              # SYS token
            0x20, 0x32, 0x30, 0x36, 0x34,      # " 2064"
            0x00,                              # end of line
            0x00, 0x00,                        # end of program marker
        ])
        # Pad with zeros to $0810
        prg_offset = 0x0801 + len(basic)
        pad = bytes([0x00]) * (0x0810 - prg_offset)
        main_loop = bytes([0x4C, 0x10, 0x08])  # JMP $0810
        return header + basic + pad + main_loop

    def _boot_idle_prg(self, client, transport) -> None:
        """Boot the U64 with the idle PRG and wait for the main loop.

        Verifies via direct memory read that ``JMP $0810`` is installed at
        $0810 before returning, so we know the CPU is actually spinning
        in the idle loop and not stuck at a BASIC READY prompt.
        """
        prg = self._make_idle_prg()
        client.reboot()
        time.sleep(8.0)
        client.run_prg(prg)
        # Wait up to 10 s for the idle main loop to appear at $0810
        expected = bytes([0x4C, 0x10, 0x08])
        deadline = time.monotonic() + 10.0
        ml = b""
        while time.monotonic() < deadline:
            ml = transport.read_memory(0x0810, 3)
            if bytes(ml) == expected:
                return
            time.sleep(0.2)
        raise AssertionError(
            f"Idle PRG did not install main_loop at $0810 "
            f"(expected {expected.hex()}, got {ml.hex() if ml else '<none>'})"
        )

    def _load_tod_routines(self, transport) -> None:
        """Load TOD start/read routines + wrapper into C64 memory.

        The wrapper:
            JSR tod_start        ; $C200
            loop: LDA $C1E0      ; poll GO_FLAG
                  BEQ loop
                  JSR tod_read
                  LDA #$42
                  STA $C1E1      ; DONE_FLAG
            park: JMP park
        """
        start_code = build_tod_start_code(self.TOD_START_CODE_ADDR)
        read_code = build_tod_read_tenths_code(self.TOD_READ_CODE_ADDR, self.RESULT_ADDR)
        load_code(transport, self.TOD_START_CODE_ADDR, start_code)
        load_code(transport, self.TOD_READ_CODE_ADDR, read_code)

        # Hand-assemble wrapper using the Asm helper (branch fixups)
        from c64_test_harness.bridge_ping import Asm
        w = Asm(org=self.WRAPPER_ADDR)
        # Diagnostic: "wrapper reached"
        w.emit(0xA9, 0x11, 0x8D, self.DIAG_ADDR & 0xFF, (self.DIAG_ADDR >> 8) & 0xFF)
        w.emit(0x78)  # SEI
        # JSR tod_start
        w.emit(0x20, self.TOD_START_CODE_ADDR & 0xFF,
               (self.TOD_START_CODE_ADDR >> 8) & 0xFF)
        # Diagnostic: "tod_start returned"
        w.emit(0xA9, 0x22, 0x8D, self.DIAG_ADDR & 0xFF, (self.DIAG_ADDR >> 8) & 0xFF)
        w.label("poll")
        w.emit(0xAD, self.GO_FLAG & 0xFF, (self.GO_FLAG >> 8) & 0xFF)
        w.branch(0xF0, "poll")  # BEQ poll
        # Diagnostic: "go received"
        w.emit(0xA9, 0x33, 0x8D, self.DIAG_ADDR & 0xFF, (self.DIAG_ADDR >> 8) & 0xFF)
        # JSR tod_read
        w.emit(0x20, self.TOD_READ_CODE_ADDR & 0xFF,
               (self.TOD_READ_CODE_ADDR >> 8) & 0xFF)
        # LDA #$42 / STA DONE
        w.emit(0xA9, 0x42, 0x8D, self.DONE_FLAG & 0xFF,
               (self.DONE_FLAG >> 8) & 0xFF)
        w.label("park")
        w.jmp("park")
        load_code(transport, self.WRAPPER_ADDR, w.build())

    def _measure_tod_ratio(self, client, transport, sleep_s: float) -> float:
        """Run one TOD-primitive measurement; return reported/wall ratio.

        The wrapper (loaded by :meth:`_load_tod_routines`) calls
        ``build_tod_start_code`` first, then polls ``$C1E0`` for a go
        signal, then calls ``build_tod_read_tenths_code``, then stores
        0x42 at ``$C1E1`` (DONE).  We time the interval between the
        wrapper reaching the polling state and our host-side write to
        the go flag, and compare that against the tenths value the
        6502 side reports.
        """
        # Zero flags and result
        write_bytes(transport, self.GO_FLAG, [0x00])
        write_bytes(transport, self.DONE_FLAG, [0x00])
        write_bytes(transport, self.RESULT_ADDR, [0x00, 0x00])
        write_bytes(transport, self.DIAG_ADDR, [0x00])
        # DMA flush
        _ = transport.read_memory(self.DONE_FLAG, 1)

        # Hijack main loop -> JMP wrapper
        write_bytes(
            transport,
            self.MAIN_LOOP,
            bytes([0x4C, self.WRAPPER_ADDR & 0xFF,
                   (self.WRAPPER_ADDR >> 8) & 0xFF]),
        )

        # Wait for wrapper to reach the polling state ($22 diag means
        # tod_start has returned).  We can't assume a fixed sleep time
        # -- busy-poll the diag byte instead.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if transport.read_memory(self.DIAG_ADDR, 1)[0] == 0x22:
                break
            time.sleep(0.02)
        else:
            diag = transport.read_memory(self.DIAG_ADDR, 1)[0]
            raise TimeoutError(
                f"Wrapper did not reach polling state "
                f"(diag=0x{diag:02X})"
            )
        t0 = time.monotonic()
        time.sleep(sleep_s)
        # Poke GO flag -> wrapper will read TOD and set DONE
        write_bytes(transport, self.GO_FLAG, [0x01])
        # Wait for DONE
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if transport.read_memory(self.DONE_FLAG, 1)[0] == 0x42:
                break
            time.sleep(0.05)
        else:
            diag = transport.read_memory(self.DIAG_ADDR, 1)[0]
            ml_now = transport.read_memory(self.MAIN_LOOP, 3)
            wrap_bytes = transport.read_memory(self.WRAPPER_ADDR, 16)
            raise TimeoutError(
                f"TOD wrapper did not signal done: "
                f"diag=0x{diag:02X} main_loop={bytes(ml_now).hex()} "
                f"wrapper_bytes={bytes(wrap_bytes).hex()}"
            )
        # Success path -- also capture raw CIA1 TOD registers via DMA
        # as a second source of truth.  ``$DC08`` etc. are I/O, but the
        # U64 DMA read_memory can still access them.
        self._raw_tod = bytes(transport.read_memory(0xDC08, 4))
        t1 = time.monotonic()
        wall_elapsed = t1 - t0

        result = transport.read_memory(self.RESULT_ADDR, 2)
        reported_tenths = result[0] | (result[1] << 8)
        if reported_tenths == 0xFFFF:
            raise AssertionError(
                f"TOD read reported $FFFF (minutes > 0) after {wall_elapsed:.2f}s "
                f"wall sleep; the wall delay is too long for the 1-minute cap"
            )
        reported_seconds = reported_tenths / 10.0
        return reported_seconds / wall_elapsed

    @pytest.mark.parametrize("mhz", [1, 8, 24, 48])
    def test_tod_runs_at_wall_clock(self, u64_client, mhz: int) -> None:
        from c64_test_harness.backends.ultimate64_helpers import set_turbo_mhz

        client, transport = u64_client
        self._boot_idle_prg(client, transport)
        set_turbo_mhz(client, mhz)
        time.sleep(0.3)
        self._load_tod_routines(transport)

        ratio = self._measure_tod_ratio(client, transport, sleep_s=3.0)
        # Tolerance: +/- 25% to absorb sleep jitter and startup lag
        raw = getattr(self, "_raw_tod", b"")
        assert 0.75 <= ratio <= 1.25, (
            f"TOD/wall ratio at {mhz} MHz is {ratio:.3f}x "
            f"(expected ~1.0x); TOD should be wall-clock regardless of "
            f"turbo speed on U64E hardware. Raw $DC08..$DC0B={raw.hex()}"
        )
