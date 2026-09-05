"""Live: the SocketDMA IDENTIFY barrier across an idle gap (issue #223).

What was reported: with ``transport.socket_dma = True`` the completion
barrier that finishes each bulk write failed "intermittently" with
``connection closed by peer``, and a caller could not tell "DMA not
applied" from "applied, ack lost".

What it is (firmware ``software/network/socket_dma.cc``, same at v3.15
and on the U64E's post-tag fork): the accepted TCP/64 socket gets
``SO_RCVTIMEO = 1 s`` and the command loop ``break``s -- closing the
socket -- on the first ``recv`` that returns ``<= 0``.  A client that
reuses one connection across writes therefore loses the first command
after any idle gap of a second or more: the ``DMAWRITE`` is accepted by
the host kernel, never read by the device, and the ``IDENTIFY`` that
follows reads EOF.

Measured on the U64E (fw 3.15 fork ``4011c97c``, 2026-09-05, lock held,
scratchpad ``exp223.py``):

=====================================  ============  ==========================
arm (interleaved, 25 writes of 4 KiB)  barrier fail  failed write's DMA applied
=====================================  ============  ==========================
0.2 s between writes, device idle      0/25          --
1.5 s between writes, device idle      25/25         0/25 (REST fallback fixed it 25/25)
0.2 s, REST GET every 100 ms           0/25          --
1.5 s, REST GET every 100 ms           25/25         0/25 (REST fallback fixed it 25/25)
=====================================  ============  ==========================

Raw client, IDENTIFY - sleep - IDENTIFY, n=3 per gap: 0.30/0.60/0.90 s
3/3 ok; 1.00/1.10/1.30/2.00 s 0/3 (closed by peer).  Writing the same
4 KiB twice through the fast path read back identical 3/3 (re-sending
is idempotent).  So it is the idle gap, not load, and not a lost ack.

The fix: :class:`SocketDMAClient` reopens a connection idle for
:data:`IDLE_RECONNECT_SECONDS` (0.8 s) before the next command, and the
transport retries a failed send/barrier once on a fresh connection
before its REST fallback.

Gates (all unset -> the module skips cleanly):

* ``SOCKETDMA_LIVE=1`` -- master switch.
* ``U64_HOST``        -- device hostname/IP (no IPs are committed).
* ``U64_PASSWORD``    -- optional.
* ``U64_ALLOW_MUTATE=1`` -- for the two tests that write 4 KiB of RAM at
  ``$4000``; the raw-client tests only send IDENTIFY.

Needs the device's "Ultimate DMA Service" (TCP 64) enabled.  Never:
``save_config_to_flash``, ``poweroff``, ``reboot``, or a machine reset;
no config item is written.
"""
from __future__ import annotations

import logging
import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.u64_socket_dma import (
    IDLE_RECONNECT_SECONDS,
    SocketDMAClient,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client, Ultimate64Error

_LIVE = os.environ.get("SOCKETDMA_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="SOCKETDMA_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

requires_mutate = pytest.mark.skipif(
    not _ALLOW_MUTATE, reason="U64_ALLOW_MUTATE not set -- skipping RAM-writing test"
)

#: Longer than the firmware's 1 s idle timer, with margin for scheduling.
IDLE_GAP = 1.5
#: Comfortably inside it.
SHORT_GAP = 0.3
ADDR = 0x4000
SIZE = 4096


@pytest.fixture(scope="module")
def device_lock():
    # allow_nested: the autouse device_lock_guard fixture already holds
    # this device's lock for the test (issue #136).
    lock = DeviceLock(_HOST, allow_nested=True)
    try:
        lock.acquire_or_raise(timeout=600.0, progress_window=60.0)
    except DeviceLockTimeout as exc:
        pytest.skip(f"device busy: {exc}")
    try:
        yield lock
    finally:
        lock.release()


@pytest.fixture(scope="module")
def client(device_lock) -> Ultimate64Client:
    c = Ultimate64Client(_HOST, password=_PW)
    try:
        c.get_info()
    except Ultimate64Error as exc:
        pytest.skip(f"device unreachable: {exc}")
    return c


@pytest.fixture(scope="module")
def dma_available(client) -> None:
    try:
        with SocketDMAClient(host=_HOST, password=_PW) as c:
            c.identify()
    except Ultimate64Error as exc:
        pytest.skip(f"SocketDMA (TCP 64) not available: {exc}")


def _pattern(seed: int) -> bytes:
    return bytes((seed + i) & 0xFF for i in range(SIZE))


# --------------------------------------------------------------------------- #
# Firmware fact                                                               #
# --------------------------------------------------------------------------- #


def test_firmware_closes_a_connection_idle_for_a_second(dma_available) -> None:
    """Pin the mechanism: with the client's reconnect disabled, the first
    command after a > 1 s gap finds the socket closed by the device."""
    with SocketDMAClient(host=_HOST, password=_PW, idle_reconnect=None) as c:
        c.identify()
        time.sleep(IDLE_GAP)
        with pytest.raises(Ultimate64Error, match="closed by peer"):
            c.identify()


def test_short_gap_keeps_the_connection(dma_available) -> None:
    """The control: inside the timer the same reused connection answers."""
    with SocketDMAClient(host=_HOST, password=_PW, idle_reconnect=None) as c:
        c.identify()
        time.sleep(SHORT_GAP)
        assert c.identify()["title"]


# --------------------------------------------------------------------------- #
# Client fix                                                                  #
# --------------------------------------------------------------------------- #


def test_client_reconnects_across_an_idle_gap(dma_available) -> None:
    with SocketDMAClient(host=_HOST, password=_PW) as c:
        first = c.identify()
        time.sleep(IDLE_GAP)
        second = c.identify()
        assert second == first
        assert c.idle_reconnects == 1
        assert c.idle_reconnect == IDLE_RECONNECT_SECONDS


# --------------------------------------------------------------------------- #
# Transport fix                                                               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def transport(client, dma_available) -> Ultimate64Transport:
    t = Ultimate64Transport(host=_HOST, client=client, socket_dma=True,
                            socket_dma_min_bytes=1)
    try:
        yield t
    finally:
        t.close()


def _count_rest_writes(transport: Ultimate64Transport) -> list[int]:
    calls = [0]
    real = transport._client.write_mem

    def counting(addr: int, data: bytes) -> None:
        calls[0] += 1
        real(addr, data)

    transport._client.write_mem = counting  # type: ignore[method-assign]
    return calls


@requires_mutate
def test_fast_path_write_survives_an_idle_gap(
    transport: Ultimate64Transport, client: Ultimate64Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Write, wait past the firmware timer, write again: the second write
    must go through SocketDMA (no REST fallback, no barrier warning) and
    read back intact."""
    rest_calls = _count_rest_writes(transport)
    a, b = _pattern(0x11), _pattern(0x22)
    with caplog.at_level(logging.WARNING, logger="c64_test_harness"):
        transport.write_memory(ADDR, a)
        assert client.read_mem(ADDR, SIZE) == a
        time.sleep(IDLE_GAP)
        transport.write_memory(ADDR, b)
    assert client.read_mem(ADDR, SIZE) == b
    assert rest_calls[0] == 0, "fell back to REST"
    barrier_warnings = [r.message for r in caplog.records if "barrier" in r.message.lower()]
    assert not barrier_warnings, barrier_warnings
    assert transport._socket_dma_client is not None
    assert transport._socket_dma_client.idle_reconnects == 1


@requires_mutate
def test_transport_retry_recovers_a_dropped_connection(
    transport: Ultimate64Transport, client: Ultimate64Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defence in depth: with the client's idle reconnect switched off the
    device drops the reused socket, the first attempt's barrier fails,
    and the transport's single retry on a fresh connection still lands
    the write without REST."""
    rest_calls = _count_rest_writes(transport)
    a, b = _pattern(0x33), _pattern(0x44)
    transport.write_memory(ADDR, a)
    sock_client = transport._socket_dma_client
    assert sock_client is not None
    sock_client._idle_reconnect = None      # re-create the pre-fix client
    time.sleep(IDLE_GAP)
    with caplog.at_level(logging.WARNING, logger="c64_test_harness"):
        transport.write_memory(ADDR, b)
    assert client.read_mem(ADDR, SIZE) == b
    assert rest_calls[0] == 0, "fell back to REST"
    assert any("retrying once" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]
