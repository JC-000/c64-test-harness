"""Live-hardware tests for the SocketDMA write path and C64U REU enablement.

Two features land on branch ``feat/socketdma-write-path-c64u-reu`` and are
exercised here against a real device:

1. **SocketDMA fast write path.** With the C64 Ultimate's "Ultimate DMA
   Service" (Network Settings) enabled, TCP port 64 speaks the SocketDMA
   binary protocol. ``Ultimate64Transport`` gains ``socket_dma`` (default
   ``False``) and ``socket_dma_min_bytes`` (default ``8192``): when
   ``socket_dma`` is on, ``write_memory`` routes payloads at/above the
   threshold through a SocketDMA DMAWRITE (with a tail read-back verify),
   falling back to REST on failure. The REST POST path takes >6 s for a
   16 KiB block on the C64U, so a sub-2 s write is proof the fast path
   engaged.

2. **Generation-aware REU enablement.** ``set_reu(client, True, ...)`` now
   probes the ``Cartridge`` item's presets and omits the ``Cartridge:
   "REU"`` write on devices that don't offer it (the C64U, which rejects
   that write with HTTP 400). ``restore_state`` similarly skips restoring a
   non-settable cartridge value.

Env gates (all unset -> everything skips cleanly):

* ``SOCKETDMA_LIVE=1`` — master switch for this module.
* ``U64_HOST``         — device hostname/IP (no IPs are committed).
* ``U64_PASSWORD``     — optional; used for REST and SocketDMA auth.
* ``U64_ALLOW_MUTATE=1`` — required for the three mutating tests; the
                           SocketDMA identify smoke test runs without it.

What the mutating tests touch:

* ``test_transport_fast_path_16k`` — writes a 16 KiB pattern to RAM at
  ``$4000`` and reads it back. RAM only; no config writes, no reset.
* ``test_set_reu_c64u_contract`` — writes ``RAM Expansion Unit`` / ``REU
  Size`` in ``C64 and Cartridge Settings``, then restores exactly those two
  raw item values (never the ``Cartridge`` item). No flash write.
* ``test_restore_state_cartridge_safe`` — snapshots and immediately restores
  turbo/REU/cartridge state; a no-op round-trip regression check.

Never: ``save_config_to_flash``, ``poweroff``, ``reboot``, or a machine
reset.
"""
from __future__ import annotations

import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.u64_socket_dma import SocketDMAClient
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64_helpers import (
    get_reu_config,
    restore_state,
    set_reu,
    snapshot_state,
)


# --------------------------------------------------------------------------- #
# Environment gates                                                           #
# --------------------------------------------------------------------------- #

_LIVE = os.environ.get("SOCKETDMA_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="SOCKETDMA_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

requires_mutate = pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — skipping mutating SocketDMA/REU test",
)

_CAT_CART = "C64 and Cartridge Settings"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Locked, stateless HTTP client for the live device.

    Queue-aware lock: a live, progressing holder extends the wait
    indefinitely; a genuinely stuck/dead holder trips the timeout and
    becomes a clean skip (never a reboot/recover).
    """
    assert _HOST is not None
    lock = DeviceLock(_HOST)
    try:
        lock.acquire_or_raise(timeout=120.0, progress_window=60.0)
    except DeviceLockTimeout as exc:
        pytest.skip(str(exc))
    try:
        yield Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
    finally:
        lock.release()


def _category_items(client: Ultimate64Client, category: str) -> dict:
    """Return the unwrapped item dict for a config *category*."""
    resp = client.get_config_category(category)
    inner = resp.get(category)
    return inner if isinstance(inner, dict) else {}


# --------------------------------------------------------------------------- #
# SocketDMA identify smoke test (runs without U64_ALLOW_MUTATE)              #
# --------------------------------------------------------------------------- #

def test_socketdma_identify_smoke(client: Ultimate64Client) -> None:  # noqa: ARG001
    """IDENTIFY over TCP 64 returns a titled dict — or the service is off.

    A refused TCP connect is a valid device configuration (Ultimate DMA
    Service disabled), not a failure, so it becomes a clean skip. The
    ``client`` fixture is taken only to hold the device lock for the
    duration of the connect.
    """
    dma = SocketDMAClient(host=_HOST or "", password=_PW, timeout=5.0)
    try:
        with dma:
            info = dma.identify()
    except Ultimate64Error as exc:
        if "connect" in str(exc).lower():
            pytest.skip(
                f"Ultimate DMA Service disabled on device (TCP 64): {exc}"
            )
        raise
    assert isinstance(info, dict)
    title = info.get("title")
    assert isinstance(title, str) and title, f"expected a non-empty title, got {info!r}"


# --------------------------------------------------------------------------- #
# SocketDMA fast write path (mutate)                                          #
# --------------------------------------------------------------------------- #

@requires_mutate
def test_transport_fast_path_16k(client: Ultimate64Client) -> None:  # noqa: ARG001
    """A 16 KiB write via the SocketDMA fast path is byte-exact and fast.

    The 16 KiB payload is at/above the default ``socket_dma_min_bytes``
    (8192), so enabling ``socket_dma`` alone routes the write through
    DMAWRITE. The read-back proves correctness; the <2 s wall-time bound
    proves the fast path engaged (the REST fallback needs >6 s for this
    size on the C64U). The ``client`` fixture is taken only for the lock;
    the transport owns its own client so ``close()`` here is safe.
    """
    size = 16 * 1024
    addr = 0x4000
    pattern = bytes((i & 0xFF) for i in range(size))

    transport = Ultimate64Transport(host=_HOST or "", password=_PW, timeout=30.0)
    transport.socket_dma = True
    try:
        start = time.monotonic()
        transport.write_memory(addr, pattern)
        elapsed = time.monotonic() - start

        readback = transport.read_memory(addr, size)
        assert readback == pattern, (
            f"read-back mismatch: {len(readback)} bytes, "
            f"first diff at index "
            f"{next((i for i in range(min(len(readback), size)) if readback[i:i+1] != pattern[i:i+1]), 'n/a')}"
        )
        assert elapsed < 2.0, (
            f"16 KiB write took {elapsed:.2f}s (>=2.0s) — SocketDMA fast path "
            f"likely did not engage and it fell back to REST"
        )
    finally:
        transport.close()


# --------------------------------------------------------------------------- #
# C64U REU enablement contract (mutate)                                       #
# --------------------------------------------------------------------------- #

@requires_mutate
def test_set_reu_c64u_contract(
    client: Ultimate64Client,
    record_property,
) -> None:
    """``set_reu(True)`` succeeds on both generations; restore only what we changed.

    On the C64 Ultimate this previously raised HTTP 400 because ``set_reu``
    tried to write ``Cartridge: "REU"`` (an unsettable mirror value). With
    the preset probe it must now succeed with the ``RAM Expansion Unit``
    write alone. On the U64 Elite the same call also succeeds (it has the
    ``"REU"`` preset). Restore touches only the two raw items we wrote —
    never the ``Cartridge`` item.
    """
    info = client.get_info()
    record_property("product", str(info.get("product", "")))

    items = _category_items(client, _CAT_CART)
    orig_enabled = items.get("RAM Expansion Unit")
    orig_size = items.get("REU Size")
    record_property("orig_reu_enabled", str(orig_enabled))
    record_property("orig_reu_size", str(orig_size))

    try:
        set_reu(client, True, size="512 KB")
        enabled, size = get_reu_config(client)
        assert enabled is True, (
            f"set_reu(True) did not report REU enabled: get_reu_config="
            f"{(enabled, size)!r}"
        )
    finally:
        restore: dict = {}
        if isinstance(orig_enabled, str):
            restore["RAM Expansion Unit"] = orig_enabled
        if isinstance(orig_size, str):
            restore["REU Size"] = orig_size
        if restore:
            client.set_config_items(_CAT_CART, restore)


# --------------------------------------------------------------------------- #
# restore_state cartridge-safety regression (mutate)                          #
# --------------------------------------------------------------------------- #

@requires_mutate
def test_restore_state_cartridge_safe(client: Ultimate64Client) -> None:
    """A snapshot -> immediate restore no-op must not raise on either generation.

    Regression guard for the mirrored ``Cartridge`` value: on the C64U the
    snapshot's cartridge string is a non-settable mirror, and restoring it
    verbatim used to raise HTTP 400. ``restore_state`` must now skip that
    write and complete cleanly.
    """
    snap = snapshot_state(client)
    # No changes in between — a pure round-trip.
    restore_state(client, snap)
