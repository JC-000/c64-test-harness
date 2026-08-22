"""Live-hardware test for the /Temp attachment GC (issue #153).

Verifies ``gc_temp_folder()`` against a real device's FTP server: leaks a
few managed attachments the way ``run_prg`` does (small ``run_prg``
uploads land as ``temp####`` files), then confirms the GC pass finds and
trims them, keeping only the requested youngest count.

Env gates (all unset -> skips cleanly):

* ``TEMP_GC_LIVE=1``     — master switch for this module.
* ``U64_HOST``           — device hostname/IP.
* ``U64_PASSWORD``       — optional; used for REST auth (FTP itself is
                            anonymous on bench devices; override via
                            ``U64_TEMP_GC_FTP_USER``/``_PASSWORD`` if not).
* ``U64_ALLOW_MUTATE=1`` — required; this test uploads PRGs and deletes
                            files in the device's ``/Temp`` folder.

Never: ``save_config_to_flash``, ``poweroff``, ``reboot``, or a machine
reset. Only ``^temp\\d+$``-named files are ever deleted — see the
pattern guard test, which is unit-level (no live device needed).
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_temp_gc import gc_temp_folder

_LIVE = os.environ.get("TEMP_GC_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="TEMP_GC_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
    pytest.mark.skipif(
        not _ALLOW_MUTATE,
        reason="U64_ALLOW_MUTATE not set — this test uploads PRGs and deletes /Temp files",
    ),
]

# Minimal viable PRG: load address $0801 (BASIC start) + RTS. Same one
# ultimate64_helpers._HEALTH_CHECK_PRG uses -- cheap, side-effect-free
# on the C64 side, and (per issue #153) still leaks a managed attachment.
_NOOP_PRG = bytes([0x01, 0x08, 0x60])


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Locked, stateless HTTP client for the live device."""
    assert _HOST is not None
    lock = DeviceLock(_HOST, allow_nested=True)
    try:
        lock.acquire_or_raise(timeout=120.0, progress_window=60.0)
    except DeviceLockTimeout as exc:
        pytest.skip(str(exc))
    try:
        yield Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
    finally:
        lock.release()


def test_gc_trims_leaked_attachments_after_repeated_run_prg(client: Ultimate64Client) -> None:
    """Several run_prg cycles leak attachments; gc_temp_folder trims them to `keep`."""
    for _ in range(5):
        client.run_prg(_NOOP_PRG)

    result = gc_temp_folder(client.host, keep=2)

    assert result.ok, f"FTP hygiene pass failed: {result.error}"
    assert len(result.kept) <= 2
    # Re-running immediately must be idempotent -- nothing left to
    # delete beyond what a fresh run_prg cycle adds.
    second = gc_temp_folder(client.host, keep=2)
    assert second.ok
    assert len(second.kept) <= 2


def test_gc_is_a_noop_on_a_temp_folder_already_within_budget(client: Ultimate64Client) -> None:
    """Calling gc_temp_folder twice in a row shouldn't delete a growing set each time."""
    first = gc_temp_folder(client.host, keep=2)
    assert first.ok
    second = gc_temp_folder(client.host, keep=2)
    assert second.ok
    assert second.deleted == []
