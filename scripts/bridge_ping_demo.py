#!/usr/bin/env python3
"""Visible two-VICE bridge ping demo.

Launches two VICE instances side by side on the br-c64 bridge (RR-Net
ethernet, normal speed, not minimized) and runs the ICMP round-trip
exchange in a loop so you can watch them ping each other. Each VICE
screen shows a banner identifying instance A (pinger) / B (responder),
their IP, a running counter, and the latest round-trip result.

Prereqs:
  sudo scripts/setup-bridge-tap.sh
  DISPLAY must be set (VICE windows render on your desktop)

Run:
  PYTHONPATH=src python3 scripts/bridge_ping_demo.py          # loop forever
  PYTHONPATH=src python3 scripts/bridge_ping_demo.py --count 5 # 5 pings then quit
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness import ScreenGrid, write_bytes
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.bridge_ping import (
    build_echo_request_frame,
    build_icmp_responder_code,
    build_ping_and_wait_code,
    cs8900a_read_linectl_code,
    cs8900a_rxctl_code,
    cs8900a_write_linectl_code,
)
from c64_test_harness.ethernet import set_cs8900a_mac
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.memory import read_bytes

# Reuse the same MAC/IP constants the test suite uses
MAC_A = bytes.fromhex("02C640000001")
MAC_B = bytes.fromhex("02C640000002")
IP_A = bytes([10, 0, 65, 2])
IP_B = bytes([10, 0, 65, 3])

PING_ID = 0xBEEF
PING_PAYLOAD = b"PING_FROM_VICE_A"

CODE = 0xC000
SCRATCH = 0xC1E0
RESULT = 0xC1F0
TX_FRAME_BUF = 0xC500
RX_FRAME_BUF = 0xC700

SCREEN_BASE = 0x0400
COLOR_BASE = 0xD800
BORDER_COLOR_ADDR = 0xD020
BG_COLOR_ADDR = 0xD021
COLS = 40

# C64 colors
BLACK = 0
WHITE = 1
LIGHT_BLUE = 14
YELLOW = 7
GREEN = 5
RED = 2


def connect(port: int, proc: ViceProcess, timeout: float = 20.0) -> BinaryViceTransport:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return BinaryViceTransport(port=port)
        except Exception as e:
            last_err = e
            if proc._proc is not None and proc._proc.poll() is not None:
                raise RuntimeError(f"VICE on port {port} exited early") from e
            time.sleep(0.25)
    raise RuntimeError(f"could not connect to VICE on port {port}: {last_err}")


def ascii_to_screen(text: str) -> bytes:
    out = bytearray()
    for ch in text.upper():
        c = ord(ch)
        if ord("A") <= c <= ord("Z"):
            out.append(c - ord("A") + 1)
        elif ord("0") <= c <= ord("9"):
            out.append(c - ord("0") + 0x30)
        elif ch == " ":
            out.append(0x20)
        elif ch == ".":
            out.append(0x2E)
        elif ch == "-":
            out.append(0x2D)
        elif ch == ":":
            out.append(0x3A)
        elif ch == "/":
            out.append(0x2F)
        elif ch == "!":
            out.append(0x21)
        else:
            out.append(0x20)
    return bytes(out)


def write_row(transport: BinaryViceTransport, row: int, col: int, text: str, color: int) -> None:
    codes = ascii_to_screen(text)
    if not codes:
        return
    write_bytes(transport, SCREEN_BASE + row * COLS + col, codes)
    write_bytes(transport, COLOR_BASE + row * COLS + col, bytes([color] * len(codes)))


def clear_screen(transport: BinaryViceTransport, color: int) -> None:
    write_bytes(transport, SCREEN_BASE, bytes([0x20] * (COLS * 25)))
    write_bytes(transport, COLOR_BASE, bytes([color] * (COLS * 25)))


def format_ip(ip: bytes) -> str:
    return ".".join(str(b) for b in ip)


def draw_banner(
    transport: BinaryViceTransport,
    role: str,
    my_ip: bytes,
    peer_ip: bytes,
    border: int,
    text_color: int,
) -> None:
    write_bytes(transport, BG_COLOR_ADDR, bytes([BLACK]))
    write_bytes(transport, BORDER_COLOR_ADDR, bytes([border]))
    clear_screen(transport, text_color)
    write_row(transport, 2, 13, f"INSTANCE {role}", text_color)
    write_row(transport, 4, 8, f"MY IP:   {format_ip(my_ip)}", text_color)
    write_row(transport, 5, 8, f"PEER IP: {format_ip(peer_ip)}", text_color)
    write_row(transport, 7, 8, "RR-NET BRIDGE DEMO", text_color)
    write_row(transport, 9, 8, f"ROLE: {'PINGER' if role == 'A' else 'RESPONDER'}", text_color)
    write_row(transport, 22, 8, "PINGS:   0", text_color)
    write_row(transport, 23, 8, "LAST:    --", text_color)


def update_status(
    transport: BinaryViceTransport,
    count: int,
    last: str,
    last_color: int,
    text_color: int,
) -> None:
    write_row(transport, 22, 8, f"PINGS:   {count:<6d}", text_color)
    write_bytes(transport, SCREEN_BASE + 23 * COLS + 8, bytes([0x20] * 28))
    write_row(transport, 23, 8, f"LAST:    {last}", last_color)


def wait_for_ready(transport: BinaryViceTransport, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            transport.resume()
            time.sleep(1.5)
            grid = ScreenGrid.from_transport(transport)
            if "READY" in grid.continuous_text().upper():
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("BASIC READY prompt not found")


def init_cs8900a(transport: BinaryViceTransport) -> None:
    load_code(transport, CODE, cs8900a_rxctl_code())
    jsr(transport, CODE, timeout=5.0)
    load_code(transport, CODE, cs8900a_read_linectl_code(SCRATCH))
    jsr(transport, CODE, timeout=5.0)
    linectl = read_bytes(transport, SCRATCH, 2)
    load_code(transport, CODE, cs8900a_write_linectl_code(linectl[0] | 0xC0, linectl[1]))
    jsr(transport, CODE, timeout=5.0)


def run_one_ping(
    transport_a: BinaryViceTransport,
    transport_b: BinaryViceTransport,
    seq: int,
) -> tuple[bool, str]:
    """Run a single round-trip ping. Returns (success, detail_string)."""
    echo = build_echo_request_frame(
        src_mac=MAC_A, dst_mac=MAC_B,
        src_ip=IP_A, dst_ip=IP_B,
        identifier=PING_ID, sequence=seq,
        payload=PING_PAYLOAD,
    )

    responder_code = build_icmp_responder_code(
        load_addr=CODE, rx_buf=RX_FRAME_BUF, my_ip=IP_B, result_addr=RESULT,
    )
    load_code(transport_b, CODE, responder_code)
    write_bytes(transport_b, RESULT, [0x00])
    write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

    ping_code = build_ping_and_wait_code(
        load_addr=CODE,
        tx_frame_buf=TX_FRAME_BUF, tx_frame_len=len(echo.frame),
        rx_buf=RX_FRAME_BUF, result_addr=RESULT,
        identifier=PING_ID, sequence=seq,
    )
    load_code(transport_a, CODE, ping_code)
    write_bytes(transport_a, TX_FRAME_BUF, echo.frame)
    write_bytes(transport_a, RESULT, [0x00])
    write_bytes(transport_a, RX_FRAME_BUF, [0x00] * 256)

    rx_error: list[Exception] = []
    tx_error: list[Exception] = []

    def responder_worker() -> None:
        try:
            jsr(transport_b, CODE, timeout=15.0)
        except Exception as e:
            rx_error.append(e)

    def ping_worker() -> None:
        try:
            time.sleep(0.6)
            jsr(transport_a, CODE, timeout=10.0)
        except Exception as e:
            tx_error.append(e)

    t0 = time.monotonic()
    tr = threading.Thread(target=responder_worker, daemon=True)
    tt = threading.Thread(target=ping_worker, daemon=True)
    tr.start()
    tt.start()
    tr.join(timeout=20.0)
    tt.join(timeout=20.0)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if rx_error or tx_error:
        err = (rx_error or tx_error)[0]
        return False, f"ERR {type(err).__name__}"

    a_result = read_bytes(transport_a, RESULT, 1)[0]
    b_result = read_bytes(transport_b, RESULT, 1)[0]

    if b_result != 0x01:
        return False, f"B FAIL 0X{b_result:02X}"
    if a_result != 0x01:
        return False, f"A FAIL 0X{a_result:02X}"
    return True, f"OK {elapsed_ms}MS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=0,
                        help="Number of pings to run (0 = loop until Ctrl+C)")
    parser.add_argument("--interval", type=float, default=1.5,
                        help="Seconds between pings")
    args = parser.parse_args()

    if shutil.which("x64sc") is None:
        print("ERROR: x64sc not on PATH", file=sys.stderr)
        return 1
    for iface in ("tap-c64-0", "tap-c64-1"):
        if not os.path.isdir(f"/sys/class/net/{iface}"):
            print(f"ERROR: {iface} not found — run sudo scripts/setup-bridge-tap.sh",
                  file=sys.stderr)
            return 1

    print("=== Bridge Ping Demo (RR-Net, normal speed) ===")
    print("Launching two VICE instances on br-c64...")

    allocator = PortAllocator(port_range_start=6560, port_range_end=6580)
    port_a = allocator.allocate()
    port_b = allocator.allocate()
    res_a = allocator.take_socket(port_a)
    if res_a is not None:
        res_a.close()
    res_b = allocator.take_socket(port_b)
    if res_b is not None:
        res_b.close()

    config_a = ViceConfig(
        port=port_a, warp=False, sound=False, minimize=False,
        ethernet=True, ethernet_mode="rrnet",
        ethernet_interface="tap-c64-0", ethernet_driver="tuntap",
    )
    config_b = ViceConfig(
        port=port_b, warp=False, sound=False, minimize=False,
        ethernet=True, ethernet_mode="rrnet",
        ethernet_interface="tap-c64-1", ethernet_driver="tuntap",
    )

    vice_a = ViceProcess(config_a)
    vice_b = ViceProcess(config_b)

    try:
        vice_a.start()
        vice_b.start()
        print(f"  A: port {port_a}, PID {vice_a._proc.pid if vice_a._proc else '?'}")
        print(f"  B: port {port_b}, PID {vice_b._proc.pid if vice_b._proc else '?'}")

        transport_a = connect(port_a, vice_a)
        transport_b = connect(port_b, vice_b)
        try:
            print("Waiting for BASIC READY on both instances...")
            wait_for_ready(transport_a)
            wait_for_ready(transport_b)

            print("Initialising CS8900a (RxCTL, LineCTL, MAC)...")
            init_cs8900a(transport_a)
            init_cs8900a(transport_b)
            set_cs8900a_mac(transport_a, MAC_A)
            set_cs8900a_mac(transport_b, MAC_B)

            print("Drawing banners...")
            draw_banner(transport_a, "A", IP_A, IP_B, border=LIGHT_BLUE, text_color=LIGHT_BLUE)
            draw_banner(transport_b, "B", IP_B, IP_A, border=YELLOW, text_color=YELLOW)

            print("\nLook at the two VICE windows on your desktop.")
            print("A = pinger (light blue)   B = responder (yellow)")
            if args.count > 0:
                print(f"Running {args.count} pings, {args.interval}s apart.\n")
            else:
                print("Pinging forever — press Ctrl+C to stop.\n")

            count = 0
            ok = 0
            seq = 1
            try:
                while args.count == 0 or count < args.count:
                    count += 1
                    try:
                        success, detail = run_one_ping(transport_a, transport_b, seq)
                    except (ConnectionError, BrokenPipeError, OSError) as e:
                        print(f"\nVICE connection lost ({type(e).__name__}) — "
                              "did you close a window? Shutting down.")
                        break
                    if success:
                        ok += 1
                    last_color = GREEN if success else RED
                    try:
                        update_status(transport_a, count, detail, last_color, LIGHT_BLUE)
                        update_status(transport_b, count, detail, last_color, YELLOW)
                    except (ConnectionError, BrokenPipeError, OSError):
                        print(f"  [{count:4d}] {'OK ' if success else 'ERR'} {detail}  "
                              f"({ok}/{count} pass)")
                        print("VICE window closed — shutting down.")
                        break
                    marker = "OK " if success else "ERR"
                    print(f"  [{count:4d}] {marker} {detail}  ({ok}/{count} pass)")
                    seq = (seq + 1) & 0xFFFF
                    if seq == 0:
                        seq = 1
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nInterrupted.")

            print(f"\nFinal: {ok}/{count} pings succeeded.")
            print("Keeping VICE windows open for 3 seconds so you can see final state...")
            time.sleep(3.0)
        finally:
            transport_a.close()
            transport_b.close()
    finally:
        vice_a.stop()
        vice_b.stop()
        allocator.release(port_a)
        allocator.release(port_b)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
