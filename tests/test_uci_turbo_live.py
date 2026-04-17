"""Live UCI turbo-sweep test: verify the delay-loop fence makes UCI work
correctly at U64 CPU speeds of 1, 8, 24, and 48 MHz.

Gated by ``U64_HOST`` and ``U64_ALLOW_MUTATE`` env vars. Requires UCI to be
enabled on the device (this test enables it transiently and restores the
original setting).

Background: on real Ultimate 64 Elite hardware the FPGA behind the UCI
registers ($DF1C-$DF1F) needs ~38 us to latch writes and settle reads. At
turbo speeds the CPU outruns the FPGA, so we must emit a delay-loop fence
after every UCI register access. Without the fence, writes get
double-latched, reads return stale/glitched values, and the protocol
corrupts.

This test exercises the simplest UCI primitive, ``uci_probe`` (a plain
``LDA $DF1D; STA result``), at all four speeds. The unfenced builder works
at 1 MHz but is expected to fail at 48 MHz; the fenced builder is expected
to work everywhere.

Run::

    U64_HOST=192.168.1.81 U64_ALLOW_MUTATE=1 \\
        python3 -m pytest tests/test_uci_turbo_live.py -v
"""

from __future__ import annotations

import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    set_turbo_mhz,
    snapshot_state,
    restore_state,
)
from c64_test_harness.uci_network import (
    UCI_IDENTIFIER,
    enable_uci,
    disable_uci,
    get_uci_enabled,
    uci_probe,
    uci_get_ip,
)


# ---------------------------------------------------------------------------
# Environment gates
# ---------------------------------------------------------------------------

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
    pytest.mark.skipif(not _ALLOW_MUTATE, reason="U64_ALLOW_MUTATE not set"),
]


# ---------------------------------------------------------------------------
# Fixtures — module-scoped so we pay the reboot/lock cost once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def device_lock():
    assert _HOST is not None
    lock = DeviceLock(_HOST)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    yield lock
    lock.release()


@pytest.fixture(scope="module")
def client(device_lock) -> Ultimate64Client:  # noqa: ARG001
    return Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)


@pytest.fixture(scope="module")
def transport(device_lock) -> Ultimate64Transport:  # noqa: ARG001
    t = Ultimate64Transport(host=_HOST, password=_PW, timeout=10.0)
    yield t
    t.close()


@pytest.fixture(scope="module")
def uci_enabled(client: Ultimate64Client):
    """Ensure UCI is on for the duration of the test module.

    Captures the original state and restores it after."""
    snap = snapshot_state(client)
    original_uci = get_uci_enabled(client)
    if not original_uci:
        enable_uci(client)
        # Reset so the UCI registers come online
        client.reset()
        time.sleep(3.0)
    yield
    # Restore original state
    if not original_uci:
        try:
            disable_uci(client)
        except Exception:
            pass
    restore_state(client, snap)
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_speed(client: Ultimate64Client, mhz: int) -> None:
    """Set U64 CPU to *mhz*. 1 means stock speed (turbo off)."""
    if mhz == 1:
        set_turbo_mhz(client, None)
    else:
        set_turbo_mhz(client, mhz)
    # Small settle so the FPGA clock domain catches up before the next UCI op.
    time.sleep(0.3)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# The four speeds the c64-https PR validated.
TURBO_SPEEDS = [1, 8, 24, 48]


@pytest.mark.parametrize("mhz", TURBO_SPEEDS)
def test_uci_probe_turbo_safe(
    client: Ultimate64Client,
    transport: Ultimate64Transport,
    uci_enabled,  # noqa: ARG001
    mhz: int,
) -> None:
    """With the turbo-safe fence, ``uci_probe`` returns 0xC9 at 1/8/24/48 MHz.

    The fence is the whole point of the port: at turbo speeds the unfenced
    path reads stale/glitched bytes, but the fenced path should always
    return the true UCI identifier byte.
    """
    _set_speed(client, mhz)
    try:
        ident = uci_probe(transport, timeout=10.0, turbo_safe=True)
        assert ident == UCI_IDENTIFIER, (
            f"uci_probe returned 0x{ident:02X} at {mhz} MHz "
            f"(expected 0x{UCI_IDENTIFIER:02X})"
        )
    finally:
        _set_speed(client, 1)


@pytest.mark.parametrize("mhz", TURBO_SPEEDS)
def test_uci_get_ip_turbo_safe(
    client: Ultimate64Client,
    transport: Ultimate64Transport,
    uci_enabled,  # noqa: ARG001
    mhz: int,
) -> None:
    """Full UCI round-trip (GET_IPADDR) works at each turbo speed.

    This exercises the full fenced command flow: abort, wait_idle, write
    target+cmd+param, push_wait, check_err, read_response, read_status,
    acknowledge. Any fence-site bug manifests as a corrupted response.
    """
    _set_speed(client, mhz)
    try:
        ip = uci_get_ip(transport, timeout=15.0, turbo_safe=True)
        # Device at 192.168.1.81 returns its own IP; accept any valid-looking
        # dotted quad (exact value depends on the LAN).
        parts = ip.split(".") if ip else []
        assert len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255
                                       for p in parts), (
            f"uci_get_ip returned {ip!r} at {mhz} MHz — expected dotted quad"
        )
    finally:
        _set_speed(client, 1)


def test_unfenced_probe_still_works_at_1mhz(
    client: Ultimate64Client,
    transport: Ultimate64Transport,
    uci_enabled,  # noqa: ARG001
) -> None:
    """The legacy (unfenced) path must continue to work at 1 MHz.

    Backward compatibility: every pre-0.12 caller that didn't pass
    ``turbo_safe`` must still work on a stock-speed U64.
    """
    _set_speed(client, 1)
    ident = uci_probe(transport, timeout=10.0)  # turbo_safe defaults False
    assert ident == UCI_IDENTIFIER
