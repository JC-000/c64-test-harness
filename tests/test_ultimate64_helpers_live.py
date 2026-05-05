"""Live integration tests against a real Ultimate 64.

Gated by the ``U64_HOST`` environment variable so CI never touches
hardware unintentionally. A single destructive round-trip test
(turbo flip) runs only when ``U64_ALLOW_MUTATE`` is also set.

Device used in development: 192.168.1.81 (Ultimate 64 Elite, fw 3.14).
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    U64StateSnapshot,
    get_reu_config,
    get_sid_config,
    get_turbo_enabled,
    get_turbo_mhz,
    restore_state,
    set_turbo_mhz,
    snapshot_state,
)
from c64_test_harness.backends.ultimate64_schema import (
    CPU_SPEED_VALUES,
    REU_ENABLED_VALUES,
    REU_SIZE_VALUES,
    TURBO_CONTROL_VALUES,
)

_HOST = os.environ.get("U64_HOST")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set — skipping live U64 tests"
)


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Stateless HTTP client for the live device."""
    password = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(_HOST or "")
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    yield Ultimate64Client(host=_HOST or "", password=password, timeout=10.0)
    lock.release()


# --------------------------------------------------------------------------- #
# Read-only assertions                                                        #
# --------------------------------------------------------------------------- #

class TestReadOnly:
    def test_get_turbo_mhz_type(self, client: Ultimate64Client) -> None:
        mhz = get_turbo_mhz(client)
        assert mhz is None or isinstance(mhz, int)
        if isinstance(mhz, int):
            assert mhz in (int(s) for s in CPU_SPEED_VALUES)

    def test_get_turbo_enabled_matches_mhz(self, client: Ultimate64Client) -> None:
        enabled = get_turbo_enabled(client)
        mhz = get_turbo_mhz(client)
        # When disabled, mhz must be None; when enabled, mhz must be int.
        if enabled:
            assert isinstance(mhz, int)
        else:
            assert mhz is None

    def test_get_reu_config(self, client: Ultimate64Client) -> None:
        enabled, size = get_reu_config(client)
        assert isinstance(enabled, bool)
        assert isinstance(size, str)
        # Size should be either empty or a valid enum value.
        assert size in REU_SIZE_VALUES or size == ""

    def test_get_sid_config(self, client: Ultimate64Client) -> None:
        cfg = get_sid_config(client)
        assert isinstance(cfg, dict)
        assert "sockets" in cfg and "addressing" in cfg
        assert len(cfg["sockets"]) > 0
        assert len(cfg["addressing"]) > 0

    def test_snapshot_state_fields(self, client: Ultimate64Client) -> None:
        snap = snapshot_state(client)
        assert isinstance(snap, U64StateSnapshot)
        assert snap.turbo_control in TURBO_CONTROL_VALUES
        assert snap.cpu_speed in CPU_SPEED_VALUES
        assert snap.reu_enabled in REU_ENABLED_VALUES
        assert snap.reu_size in REU_SIZE_VALUES
        # cartridge may be "" on a blank preset list.
        assert isinstance(snap.cartridge, str)


# --------------------------------------------------------------------------- #
# ONE destructive round-trip test (double-gated)                              #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — skipping destructive round-trip",
)
class TestRoundTripTurbo:
    def test_flip_turbo_and_restore(self, client: Ultimate64Client) -> None:
        """Snapshot, turn turbo on at 2 MHz, verify, restore to original."""
        snap = snapshot_state(client)
        try:
            set_turbo_mhz(client, 2)
            now = get_turbo_mhz(client)
            assert now == 2, f"expected 2 MHz, got {now}"
            assert get_turbo_enabled(client) is True
        finally:
            restore_state(client, snap)

        # Verify state is back to original after restore.
        after = snapshot_state(client)
        assert after.turbo_control == snap.turbo_control
        assert after.cpu_speed == snap.cpu_speed
        assert after.reu_enabled == snap.reu_enabled
        assert after.reu_size == snap.reu_size
