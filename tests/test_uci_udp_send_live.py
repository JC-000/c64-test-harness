#!/usr/bin/env python3
"""UCI UDP send empirical probe (live U64E).

Verifies that ``uci_udp_connect`` + ``uci_socket_write`` actually puts
UDP datagrams on the wire AND that each ``uci_socket_write`` call
produces its own discrete datagram (no firmware-side coalescing).

This is the empirical floor for Stream A.  The current ``uci_socket_write``
is capped at 255 bytes per call by 6502 Y-register indexing — that cap
is exercised here too so a later "lift the cap" change can flip the
boundary case without rewriting the probe.

KEY FACT (from Gideon's firmware source): WRITE_SOCKET maps 1:1 onto
``lwip_send`` for UDP — one call == one datagram on the wire — with an
empirical firmware-side ceiling of 892 bytes per datagram (theoretical
``CMD_MAX_COMMAND_LEN - 3`` is 893 but the firmware truncates by one at
that boundary; see ``SOCKET_WRITE_MAX_BYTES`` in uci_network.py).  This
test proves the 1:1 part end-to-end on real hardware.

Usage::

    UCI_UDP_LIVE=1 \\
    ~/.local/share/c64-test-harness/venv/bin/pytest \\
        tests/test_uci_udp_send_live.py -xvs
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
    reason="UCI_UDP_LIVE not set — live UCI UDP send probe disabled",
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
LOCK_TIMEOUT = 600.0  # generous: another agent may hold the device for hours
UCI_CALL_TIMEOUT = 15.0
INTER_WRITE_SLEEP = 0.08  # 80 ms between writes (diagnostic spacing)
RECV_TIMEOUT = 5.0
MAX_PROBE_ATTEMPTS = 2
# UCI_socket_write socket-id sentinel: firmware uses 0xFF as "no socket"
INVALID_SOCKET_ID = 0xFF


def _make_chunks() -> list[bytes]:
    """Five distinguishable UDP payloads, varying length.

    Byte 0 of each chunk is the chunk index (0..4) — used host-side to
    re-pair received datagrams with their sent chunk regardless of
    arrival order (UDP is unordered in general, though on a quiet
    point-to-point LAN they almost always arrive in send order).

    Bytes 1..N form a repeating A-Z pattern.  Chunk 3 hits the current
    255-byte single-call cap exactly (1 index byte + 254 pattern
    bytes).  Chunk 4 is shorter and follows the cap-hitter to catch
    "sticky" firmware state.
    """
    sizes = [50, 100, 200, 255, 100]
    chunks: list[bytes] = []
    for idx, size in enumerate(sizes):
        body = bytes((0x41 + ((i) % 26)) for i in range(size - 1))
        chunks.append(bytes([idx]) + body)
    # Sanity: chunk 3 is exactly 255 bytes (max single-call payload)
    assert len(chunks[3]) == 255
    return chunks


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


def _collect_datagrams(
    sock: socket.socket,
    expected_count: int,
) -> list[tuple[bytes, tuple[str, int], int]]:
    """Recv up to *expected_count* datagrams, return list of
    ``(payload, src_addr, recv_time_ns)``.

    Stops early on the first ``socket.timeout`` from ``recvfrom`` — i.e.
    after ``RECV_TIMEOUT`` seconds of no traffic.  Returns whatever was
    received up to that point (possibly fewer than ``expected_count``).
    """
    received: list[tuple[bytes, tuple[str, int], int]] = []
    for _ in range(expected_count):
        try:
            payload, src = sock.recvfrom(2048)
        except socket.timeout:
            break
        received.append((payload, src, time.time_ns()))
    return received


def _run_probe_once(
    transport: Ultimate64Transport,
    host_ip: str,
    chunks: list[bytes],
) -> tuple[
    list[tuple[bytes, tuple[str, int], int]],
    int,
    int,
]:
    """Single probe pass.

    Returns (received_datagrams, socket_id, listener_port).  Caller is
    responsible for closing the UCI socket via uci_socket_close and
    asserting on the result.  Listener is closed inside this helper.
    """
    listener, listener_port = _open_listener()
    socket_id = INVALID_SOCKET_ID
    try:
        print(
            f"  uci_udp_connect -> {host_ip}:{listener_port}",
            flush=True,
        )
        socket_id = uci_udp_connect(
            transport, host_ip, listener_port, timeout=UCI_CALL_TIMEOUT,
        )
        print(f"    socket_id = 0x{socket_id:02X}", flush=True)
        if socket_id == INVALID_SOCKET_ID:
            pytest.fail(
                f"uci_udp_connect returned 0x{socket_id:02X} "
                "(firmware-side error sentinel); UCI cannot send.  Check "
                "U64 network connectivity from the device side (see "
                "uci_get_ip)."
            )

        for idx, chunk in enumerate(chunks):
            print(
                f"  uci_socket_write chunk[{idx}] len={len(chunk)}",
                flush=True,
            )
            uci_socket_write(
                transport, socket_id, chunk, timeout=UCI_CALL_TIMEOUT,
            )
            time.sleep(INTER_WRITE_SLEEP)

        received = _collect_datagrams(listener, expected_count=len(chunks))
        print(
            f"  host listener received {len(received)} datagram(s)",
            flush=True,
        )
        return received, socket_id, listener_port
    finally:
        listener.close()


def test_uci_udp_send_one_write_per_datagram() -> None:
    """Each ``uci_socket_write`` call -> one discrete UDP datagram.

    Probe shape:
      1. Bind a host-side UDP listener on an ephemeral port.
      2. ``uci_udp_connect`` from U64 to host:port.
      3. Send 5 chunks of varying sizes (50, 100, 200, 255, 100 bytes),
         each tagged with a per-chunk index byte at offset 0.
      4. Host collects up to 5 datagrams (5 s recv timeout).
      5. Assert: exactly 5 datagrams, each payload matches one chunk,
         source IP is the U64.

    Retries the full probe ``MAX_PROBE_ATTEMPTS`` times so a single lost
    UDP datagram doesn't flake the test on a quiet LAN.
    """
    chunks = _make_chunks()
    expected_count = len(chunks)

    print("=" * 60, flush=True)
    print("UCI UDP empirical probe", flush=True)
    print(f"  U64 target  : {U64_HOST}", flush=True)
    print(
        f"  Chunk sizes : "
        f"{[len(c) for c in chunks]} (idx-byte + pattern)",
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
        # $DF1C-$DF1F don't go live until the next machine reset
        # (per enable_uci docstring; matches test_uci_tcp_echo_live.py).
        print("Resetting machine to activate UCI I/O registers...", flush=True)
        client.reset()
        time.sleep(3.0)

        # Sanity: confirm U64 has IP connectivity from its own perspective.
        # If uci_get_ip returns "" or 0.0.0.0, sending UDP is hopeless.
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

        # -----------------------------------------------------------------
        # Probe with retry: UDP is unreliable.  Two attempts; we pass on
        # the first that delivers every chunk.
        # -----------------------------------------------------------------
        last_received: list[tuple[bytes, tuple[str, int], int]] = []
        last_socket_id: int = INVALID_SOCKET_ID
        attempt_success = False
        for attempt in range(1, MAX_PROBE_ATTEMPTS + 1):
            print(
                f"\n--- Probe attempt {attempt}/{MAX_PROBE_ATTEMPTS} ---",
                flush=True,
            )
            received, socket_id, port = _run_probe_once(
                transport, host_ip, chunks,
            )
            last_received = received
            last_socket_id = socket_id

            try:
                if len(received) == expected_count:
                    attempt_success = True
                    break
                print(
                    f"  attempt {attempt}: only "
                    f"{len(received)}/{expected_count} datagrams; "
                    "retrying" if attempt < MAX_PROBE_ATTEMPTS else
                    f"  attempt {attempt}: only "
                    f"{len(received)}/{expected_count} datagrams; "
                    "no more retries",
                    flush=True,
                )
            finally:
                if socket_id != INVALID_SOCKET_ID:
                    try:
                        uci_socket_close(
                            transport, socket_id, timeout=UCI_CALL_TIMEOUT,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"  WARNING: uci_socket_close failed: {exc!r}",
                            flush=True,
                        )

        # -----------------------------------------------------------------
        # Assertions
        # -----------------------------------------------------------------
        if not attempt_success:
            # Dump what we did get for debug
            for i, (payload, src, t_ns) in enumerate(last_received):
                idx = payload[0] if payload else -1
                print(
                    f"  recv[{i}] from={src} len={len(payload)} "
                    f"idx_byte=0x{idx:02X} t_ns={t_ns}",
                    flush=True,
                )
            pytest.fail(
                f"After {MAX_PROBE_ATTEMPTS} attempt(s), only received "
                f"{len(last_received)}/{expected_count} datagrams.  "
                "Either UDP packets are being dropped between U64 and "
                "host, or firmware is coalescing/dropping writes."
            )

        # We have expected_count datagrams.  Pair each to its chunk by
        # the leading index byte (datagram index byte 0 == chunk index).
        by_index: dict[int, tuple[bytes, tuple[str, int], int]] = {}
        for payload, src, t_ns in last_received:
            assert len(payload) >= 1, (
                f"received empty datagram from {src}; firmware bug?"
            )
            idx = payload[0]
            assert 0 <= idx < expected_count, (
                f"datagram from {src} has out-of-range idx byte 0x"
                f"{idx:02X}; not from us"
            )
            assert idx not in by_index, (
                f"duplicate datagram for idx={idx} (one write produced "
                "TWO datagrams — firmware fragmenting?)"
            )
            by_index[idx] = (payload, src, t_ns)

        # Every chunk index accounted for?
        missing = sorted(set(range(expected_count)) - by_index.keys())
        assert not missing, (
            f"missing datagrams for chunk indices {missing} "
            "(write succeeded host-side but datagram never arrived)"
        )

        # Each datagram payload == sent chunk; source IP == U64.
        for idx, chunk in enumerate(chunks):
            payload, src, _t_ns = by_index[idx]
            assert payload == chunk, (
                f"chunk {idx}: payload mismatch.\n"
                f"  expected {len(chunk)} bytes: "
                f"{chunk[:16].hex()}...{chunk[-8:].hex()}\n"
                f"  received {len(payload)} bytes: "
                f"{payload[:16].hex()}...{payload[-8:].hex()}"
            )
            assert src[0] == U64_HOST, (
                f"chunk {idx}: source IP {src[0]!r} != U64 IP "
                f"{U64_HOST!r}.  Routing anomaly?"
            )

        print(
            f"\nPASS: {expected_count} writes -> {expected_count} "
            "discrete UDP datagrams, all payloads intact.",
            flush=True,
        )

        # -----------------------------------------------------------------
        # Sanity: the 892-byte cap (lifted from 255 in Stream B).
        # Boundary cases:
        #   - 893-byte payload: must raise ValueError (over the cap).
        #   - 892-byte payload: must succeed (right at the empirical
        #     firmware ceiling — see SOCKET_WRITE_MAX_BYTES comment for
        #     why 892 and not the theoretical 893).
        # The detailed end-to-end check (one datagram on the wire,
        # payload bytes intact) lives in test_uci_udp_send_large_live.py
        # — this probe just guards against accidental cap regressions.
        # -----------------------------------------------------------------
        # Use a fresh socket for this; the previous one is closed.
        sentinel_listener, sentinel_port = _open_listener()
        sentinel_socket_id = INVALID_SOCKET_ID
        try:
            sentinel_socket_id = uci_udp_connect(
                transport, host_ip, sentinel_port,
                timeout=UCI_CALL_TIMEOUT,
            )
            with pytest.raises(ValueError):
                uci_socket_write(
                    transport, sentinel_socket_id, b"X" * 893,
                    timeout=UCI_CALL_TIMEOUT,
                )
            print(
                "PASS: uci_socket_write(893-byte payload) raises "
                "ValueError as documented.",
                flush=True,
            )
            # 892 must succeed (no exception); we don't validate the
            # datagram contents here — large_live test covers that.
            uci_socket_write(
                transport, sentinel_socket_id, b"X" * 892,
                timeout=UCI_CALL_TIMEOUT,
            )
            # Drain any datagram so it doesn't disturb later state.
            try:
                sentinel_listener.recvfrom(2048)
            except socket.timeout:
                pass
            print(
                "PASS: uci_socket_write(892-byte payload) succeeded "
                "(empirical firmware ceiling).",
                flush=True,
            )
        finally:
            if sentinel_socket_id != INVALID_SOCKET_ID:
                try:
                    uci_socket_close(
                        transport, sentinel_socket_id,
                        timeout=UCI_CALL_TIMEOUT,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  WARNING: cap-test socket close failed: {exc!r}",
                        flush=True,
                    )
            sentinel_listener.close()

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
