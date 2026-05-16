"""Live regression tests for Ultimate64Client.write_mem wire forms.

Exercises both wire forms ``write_mem`` selects between based on payload
size:

  * **PUT path** — ``PUT /v1/machine:writemem?address=&data=<hex>`` for
    payloads at or below ``write_mem_query_threshold`` (auto-detected to
    128 bytes on firmware 3.14*).
  * **POST path** — ``POST /v1/machine:writemem?address=`` with raw
    bytes as ``application/octet-stream`` for payloads above the
    threshold.

The POST path has historically been mock-only in the unit suite; in
practice the firmware's POST handler can enter a degraded state (returns
HTTP 404 ``"Could not read data from attachment"`` and refuses to write)
that a power-cycle clears.  These tests are a live sentinel for that
state — a failure here is usually firmware health, not a harness bug.

Gated by ``U64_HOST``.  Writes only to the documented harness scratch
range ($C000-$C3FF — see ``docs/memory_safety.md``); a failure that
leaves the region in an unexpected state will not corrupt consumer
PRG state.
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="U64_HOST not set — live Ultimate device tests disabled",
)

# Inside the documented harness scratch range ($C000-$C3FF). Picked to
# stay clear of $C000-$C0FF where some harness machinery lives.
_SCRATCH_ADDR = 0xC200


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    lock = DeviceLock(_HOST)
    try:
        lock.acquire_or_raise(timeout=120.0)
    except DeviceLockTimeout as e:
        pytest.skip(str(e))
    c = Ultimate64Client(_HOST, password=_PW, timeout=8.0)
    try:
        yield c
    finally:
        lock.release()


def _zero(client: Ultimate64Client, addr: int, length: int) -> None:
    """Zero a scratch region using small writes so we don't depend on
    the very path we may be testing."""
    chunk = 64  # well under the 128-byte PUT-path ceiling
    for off in range(0, length, chunk):
        n = min(chunk, length - off)
        client.write_mem(addr + off, b"\x00" * n)


def test_write_mem_put_path_round_trips_at_threshold(client: Ultimate64Client) -> None:
    """Small payload (<= threshold) round-trips via the PUT ?data=<hex> form."""
    size = min(64, client.write_mem_query_threshold)
    payload = bytes((0x80 + (i & 0x3F)) for i in range(size))
    _zero(client, _SCRATCH_ADDR, size)
    pre = client.read_mem(_SCRATCH_ADDR, size)
    assert pre == b"\x00" * size, "pre-zero clear did not stick"
    client.write_mem(_SCRATCH_ADDR, payload)
    got = client.read_mem(_SCRATCH_ADDR, size)
    assert got == payload, (
        f"PUT-path round-trip failed: wrote {payload[:16].hex()}..., "
        f"read {got[:16].hex()}..."
    )


def test_write_mem_post_path_round_trips_above_threshold(client: Ultimate64Client) -> None:
    """Large payload (> threshold) round-trips via the POST raw-body form.

    This is the regression sentinel.  When the firmware's POST handler
    enters a degraded state, ``write_mem`` will raise
    ``Ultimate64Error: ... Could not read data from attachment`` — that
    failure here means a power-cycle of the device is needed.
    """
    size = client.write_mem_query_threshold + 72  # well past the threshold
    payload = bytes((0x40 + (i & 0x3F)) for i in range(size))
    _zero(client, _SCRATCH_ADDR, size)
    pre = client.read_mem(_SCRATCH_ADDR, size)
    assert pre == b"\x00" * size, "pre-zero clear did not stick"
    client.write_mem(_SCRATCH_ADDR, payload)
    got = client.read_mem(_SCRATCH_ADDR, size)
    if got != payload:
        # Locate the first divergence to make the failure searchable.
        first = next((i for i in range(size) if got[i] != payload[i]), None)
        raise AssertionError(
            f"POST-path round-trip failed at offset {first}: "
            f"wrote {payload[:16].hex()}..., read {got[:16].hex()}... "
            f"(payload size {size}, threshold {client.write_mem_query_threshold})"
        )
