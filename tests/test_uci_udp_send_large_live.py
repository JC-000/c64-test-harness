#!/usr/bin/env python3
"""UCI UDP large-payload end-to-end test (live U64E).

Verifies that the lifted 892-byte single-call cap actually puts a
single, intact UDP datagram on the wire — the firmware accepts
``WRITE_SOCKET`` payloads up to the EMPIRICAL ceiling of 892 bytes
(theoretical ``CMD_MAX_COMMAND_LEN - 3`` is 893 but the firmware
truncates by one at that boundary; see ``SOCKET_WRITE_MAX_BYTES`` in
``uci_network.py``).  Stream B's 16-bit ``LDA abs,Y`` inner loop in
:func:`build_socket_write` lets the host-side helper reach that
ceiling cleanly.

This test exercises:
  - 800-byte payload (mid-range; crosses two 6502 page boundaries) —
    must arrive intact on the host as ONE datagram.
  - 892-byte payload (empirical firmware ceiling) — must arrive intact
    as ONE datagram.
  - 893-byte payload — must raise ``ValueError`` host-side (the check
    is in Python, no device contact needed).

A 1-byte cap regression in :func:`uci_socket_write` would cause:
  - The 893 test to NOT raise (or to raise differently).
  - The 800/892 tests to either raise locally (if cap dropped) or to
    silently truncate/fragment on the wire (if 6502 loop broke).

Usage::

    UCI_UDP_LIVE=1 \\
    ~/.local/share/c64-test-harness/venv/bin/pytest \\
        tests/test_uci_udp_send_large_live.py -xvs
"""
from __future__ import annotations

import os
import socket
import sys
import time

import pytest

UCI_UDP_LIVE = os.environ.get("UCI_UDP_LIVE")
pytestmark = pytest.mark.skipif(
    not UCI_UDP_LIVE,
    reason="UCI_UDP_LIVE not set — live UCI UDP large-payload test disabled",
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.device_lock import DeviceLock  # noqa: E402
from c64_test_harness.backends.ultimate64 import Ultimate64Transport  # noqa: E402
from c64_test_harness.backends.ultimate64_client import (  # noqa: E402
    Ultimate64Client,
)
from c64_test_harness.uci_network import (  # noqa: E402
    disable_uci,
    enable_uci,
    uci_get_ip,
    uci_socket_close,
    uci_socket_write,
    uci_udp_connect,
)

# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------
U64_HOST = "10.43.23.81"
LOCK_TIMEOUT = 600.0     # generous: another holder may hold the device for hours
UCI_CALL_TIMEOUT = 20.0  # large WRITE_SOCKETs take longer on real HW
RECV_TIMEOUT = 5.0
MAX_PROBE_ATTEMPTS = 2
INVALID_SOCKET_ID = 0xFF

# Boundary cases.
MID_PAYLOAD_SIZE = 800   # crosses 6502 page boundaries; well past the old 255 cap
MAX_PAYLOAD_SIZE = 892   # empirical firmware ceiling (theoretical 893 truncates)
OVER_CAP_SIZE    = 893   # one byte over — must raise ValueError host-side


def _detect_local_ip(target: str) -> str:
    """Detect our IP on the same subnet as *target* (no traffic sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _open_listener() -> tuple[socket.socket, int]:
    """Bind a UDP listener on an ephemeral port; return (sock, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.settimeout(RECV_TIMEOUT)
    return s, port


def _make_pattern(size: int) -> bytes:
    """Recognizable repeating pattern, easy to diff if truncation happens.

    Byte ``i`` is ``(i * 251 + 17) % 256`` — varies fast enough that a
    page-misindex or shift-by-one would show up immediately in the
    first diff position.  ``251`` is prime mod 256 so the cycle length
    is 256, not a sub-multiple.
    """
    return bytes(((i * 251 + 17) & 0xFF) for i in range(size))


def _send_and_assert_one_datagram(
    transport: Ultimate64Transport,
    host_ip: str,
    payload: bytes,
    label: str,
) -> None:
    """One uci_socket_write -> one datagram, intact.

    Wraps the whole open-listener/connect/write/recv/close cycle in a
    retry loop so a single UDP drop on the LAN doesn't flake the test.
    Each attempt uses a fresh listener AND a fresh UCI socket (some
    firmware revs grow sticky after a write — defence-in-depth).
    """
    print(
        f"  [{label}] sending {len(payload)}-byte payload "
        f"(first 8 bytes: {payload[:8].hex()})",
        flush=True,
    )
    last_err: str | None = None
    for attempt in range(1, MAX_PROBE_ATTEMPTS + 1):
        print(
            f"    attempt {attempt}/{MAX_PROBE_ATTEMPTS}",
            flush=True,
        )
        listener, listener_port = _open_listener()
        socket_id = INVALID_SOCKET_ID
        try:
            socket_id = uci_udp_connect(
                transport, host_ip, listener_port,
                timeout=UCI_CALL_TIMEOUT,
            )
            if socket_id == INVALID_SOCKET_ID:
                last_err = (
                    f"uci_udp_connect returned 0x{socket_id:02X} "
                    "(firmware-side error sentinel)"
                )
                continue
            uci_socket_write(
                transport, socket_id, payload,
                timeout=UCI_CALL_TIMEOUT,
            )
            # One recv: expect exactly one datagram of len(payload) bytes.
            try:
                received, src = listener.recvfrom(len(payload) + 64)
            except socket.timeout:
                last_err = (
                    f"no datagram received within {RECV_TIMEOUT}s "
                    f"(host listener on port {listener_port}); "
                    "the U64 may have dropped the write, the LAN may "
                    "have dropped the packet, or firmware fragmented."
                )
                continue

            # First check: source IP.
            if src[0] != U64_HOST:
                last_err = (
                    f"source IP {src[0]!r} != U64 IP {U64_HOST!r} "
                    "(routing anomaly?)"
                )
                continue

            # Length must match exactly.  A shorter datagram means
            # firmware truncated; a longer one means another sender
            # crashed our listener — both are fail.
            if len(received) != len(payload):
                last_err = (
                    f"datagram length {len(received)} != "
                    f"expected {len(payload)} (truncated or "
                    "concatenated?)"
                )
                continue

            # Byte-for-byte comparison.  If this fails, the 6502 loop
            # is most likely misindexing (page-wrap bug, SMC operand
            # corruption) — locate the first mismatch and show 16
            # bytes of context for diagnosis.
            if received != payload:
                first_diff = next(
                    (i for i in range(len(payload)) if received[i] != payload[i]),
                    -1,
                )
                ctx_start = max(0, first_diff - 4)
                ctx_end = min(len(payload), first_diff + 12)
                last_err = (
                    f"datagram contents differ at byte {first_diff}\n"
                    f"  expected[{ctx_start}:{ctx_end}] = "
                    f"{payload[ctx_start:ctx_end].hex()}\n"
                    f"  received[{ctx_start}:{ctx_end}] = "
                    f"{received[ctx_start:ctx_end].hex()}"
                )
                continue

            # Best-effort: make sure no SECOND datagram arrives within
            # the recv timeout window (would indicate firmware
            # fragmented one write into multiple sends).  We can't
            # block the full RECV_TIMEOUT here without doubling the
            # test runtime, so use a short non-blocking peek.
            listener.settimeout(0.5)
            try:
                stray, stray_src = listener.recvfrom(2048)
                last_err = (
                    f"unexpected SECOND datagram from {stray_src} "
                    f"(len={len(stray)}); firmware fragmented one "
                    "write into multiple datagrams?"
                )
                continue
            except socket.timeout:
                pass  # good — no second datagram

            print(
                f"    PASS: 1 datagram, "
                f"{len(received)} bytes, contents OK",
                flush=True,
            )
            return  # success
        finally:
            if socket_id != INVALID_SOCKET_ID:
                try:
                    uci_socket_close(
                        transport, socket_id, timeout=UCI_CALL_TIMEOUT,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"    WARNING: uci_socket_close failed: {exc!r}",
                        flush=True,
                    )
            listener.close()

    pytest.fail(
        f"[{label}] after {MAX_PROBE_ATTEMPTS} attempts: {last_err}"
    )


def test_uci_udp_send_large_payload() -> None:
    """End-to-end: 800/892-byte one-call writes -> single datagrams.

    Sequence:
      1. Acquire device lock (queue-aware, 600s timeout).
      2. enable_uci + reset + 3s settle (the activation pattern;
         omitting this makes every UCI command time out — see
         CLAUDE.md and test_uci_tcp_echo_live.py for details).
      3. Send a 800-byte payload, verify one intact datagram.
      4. Send a 892-byte payload (empirical firmware ceiling), verify
         one intact datagram.
      5. Verify ``uci_socket_write(..., 893 bytes)`` raises ValueError
         in Python before touching the wire.
    """
    print("=" * 60, flush=True)
    print("UCI UDP large-payload end-to-end test", flush=True)
    print(f"  U64 target  : {U64_HOST}", flush=True)
    print(
        f"  Payloads    : {MID_PAYLOAD_SIZE} (mid), "
        f"{MAX_PAYLOAD_SIZE} (ceiling), "
        f"{OVER_CAP_SIZE} (over -> ValueError)",
        flush=True,
    )
    print("=" * 60, flush=True)

    lock = DeviceLock(U64_HOST)
    print(
        f"Acquiring device lock (timeout={LOCK_TIMEOUT:.0f}s, queue-aware)...",
        flush=True,
    )
    if not lock.acquire(timeout=LOCK_TIMEOUT):
        pytest.fail(
            f"Could not acquire device lock for {U64_HOST} within "
            f"{LOCK_TIMEOUT:.0f}s (another holder may be stuck)."
        )

    client: Ultimate64Client | None = None
    transport: Ultimate64Transport | None = None
    uci_was_enabled = False
    try:
        client = Ultimate64Client(host=U64_HOST, timeout=30.0)
        transport = Ultimate64Transport(
            host=U64_HOST, timeout=30.0, client=client,
        )

        print("Enabling UCI (Command Interface)...", flush=True)
        enable_uci(client)
        uci_was_enabled = True
        # enable_uci flips the config item but the I/O registers at
        # $DF1C-$DF1F do not go live until the next machine reset.
        # (See enable_uci docstring; matches the pattern used by
        # test_uci_tcp_echo_live.py and test_uci_udp_send_live.py.)
        print("Resetting machine to activate UCI I/O registers...", flush=True)
        client.reset()
        time.sleep(3.0)

        # Sanity: confirm the U64 has IP connectivity from its side
        # before we try to send hundreds of bytes through it.
        try:
            u64_ip = uci_get_ip(transport, timeout=UCI_CALL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"uci_get_ip raised before probe: {exc!r}.  UCI may not be "
                "active or the device may not have a network link."
            )
        print(f"  U64 self-reported IP: {u64_ip!r}", flush=True)
        if not u64_ip or u64_ip.startswith("0.0.0.0"):
            pytest.fail(
                f"U64 reports no IP ({u64_ip!r}); UDP send cannot work.  "
                "Check device DHCP / link status."
            )

        host_ip = _detect_local_ip(U64_HOST)
        print(f"  Host IP (this side): {host_ip}", flush=True)

        # ---------- 800-byte payload (mid-range, crosses pages) -----------
        _send_and_assert_one_datagram(
            transport, host_ip,
            _make_pattern(MID_PAYLOAD_SIZE),
            label=f"mid-{MID_PAYLOAD_SIZE}B",
        )

        # ---------- 892-byte payload (empirical firmware ceiling) --------
        _send_and_assert_one_datagram(
            transport, host_ip,
            _make_pattern(MAX_PAYLOAD_SIZE),
            label=f"ceiling-{MAX_PAYLOAD_SIZE}B",
        )

        # ---------- 893-byte payload (over cap; host-side raise) ---------
        # No device contact needed for this — the cap check is in
        # ``uci_socket_write`` before any write_memory call — but we
        # still need a valid socket-id-like value, so we use a placeholder
        # (the function should raise before touching the transport).
        print(
            f"  [over-{OVER_CAP_SIZE}B] expect ValueError "
            "(Python-side cap check)",
            flush=True,
        )
        with pytest.raises(ValueError):
            uci_socket_write(
                transport, 0x00, b"X" * OVER_CAP_SIZE,
                timeout=UCI_CALL_TIMEOUT,
            )
        print(
            f"    PASS: uci_socket_write({OVER_CAP_SIZE}-byte) raised "
            "ValueError as required.",
            flush=True,
        )

        print(
            "\nALL PASS: 16-bit inner loop delivers 800/892-byte "
            "payloads as single intact UDP datagrams; over-cap "
            "rejected host-side.",
            flush=True,
        )

    finally:
        if uci_was_enabled and client is not None:
            try:
                print("Disabling UCI...", flush=True)
                disable_uci(client)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"WARNING: failed to disable UCI on teardown: {exc!r}",
                    flush=True,
                )
        lock.release()
