"""Live test for the storage helpers (``ultimate64_storage``) on a real device.

Runs the helpers against the firmware's FTP server. It writes, reads back,
overwrites and deletes one file under ``/<volume>/c64-test-harness-live/``
on the first removable volume the device lists (not ``/Temp``, not
``/Flash``). The directory is reused across runs and the file is always
deleted in teardown.

Env gates (all unset -> skips cleanly):

* ``STORAGE_LIVE=1`` — master switch for this module.
* ``U64_HOST``       — device hostname/IP.

No config writes, no resets, and no REST bodies, so this test costs no
``/Temp`` attachment. FTP File Service must already be enabled; if it is
not, the test fails with the storage error that names the setting.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_storage import (
    WRITE_REFUSED_VOLUMES,
    Ultimate64StorageVolumeError,
    storage_delete_file,
    storage_get_file,
    storage_mkdir,
    storage_put_file,
    storage_volumes,
)
from live_fixture_teardown import raise_teardown_failures, teardown_then_release

_LIVE = os.environ.get("STORAGE_LIVE")
_HOST = os.environ.get("U64_HOST")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="STORAGE_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

_DIR = "c64-test-harness-live"
# ADF-sized and position-dependent, so a dropped or reordered block changes the hash.
_DATA = hashlib.sha256(b"seed").digest() * (901120 // 32)


@pytest.fixture(scope="module")
def locked():
    """Hold the device lock for the module."""
    assert _HOST is not None
    lock = DeviceLock(_HOST, allow_nested=True)
    try:
        lock.acquire_or_raise(timeout=300.0, progress_window=60.0)
    except DeviceLockTimeout as exc:
        pytest.skip(str(exc))
    failures: list = []
    try:
        yield _HOST
    finally:
        failures = teardown_then_release([], lock.release)
    raise_teardown_failures("storage lock teardown", failures)


@pytest.fixture(scope="module")
def volume_dir(locked):
    """``/<first usable removable volume>/<_DIR>``; the test file is deleted after."""
    vols = [v for v in storage_volumes(_HOST) if v.casefold() not in WRITE_REFUSED_VOLUMES]
    if not vols:
        pytest.skip(f"{_HOST} has no usable removable storage volume")
    target = f"/{vols[0]}/{_DIR}"
    failures: list = []
    try:
        yield target
    finally:
        failures = teardown_then_release(
            [(
                "delete test file",
                lambda: storage_delete_file(_HOST, f"{target}/roundtrip.bin", missing_ok=True),
            )],
            lambda: None,
        )
    raise_teardown_failures("storage teardown", failures)


def test_put_get_overwrite_delete_round_trip(volume_dir: str) -> None:
    path = f"{volume_dir}/roundtrip.bin"
    storage_delete_file(_HOST, path, missing_ok=True)

    put = storage_put_file(_HOST, path, _DATA)
    assert put.verified and not put.replaced
    assert put.size == len(_DATA)
    assert storage_get_file(_HOST, path) == _DATA

    with pytest.raises(FileExistsError):
        storage_put_file(_HOST, path, b"other")
    assert storage_get_file(_HOST, path) == _DATA

    again = storage_put_file(_HOST, path, b"shorter", overwrite=True)
    assert again.replaced and again.verified
    assert storage_get_file(_HOST, path) == b"shorter"

    assert storage_delete_file(_HOST, path) is True
    with pytest.raises(FileNotFoundError):
        storage_get_file(_HOST, path)
    with pytest.raises(FileNotFoundError):
        storage_delete_file(_HOST, path)


def test_a_disk_image_is_a_file(volume_dir: str) -> None:
    """CWD into a .d64 mounts it on the firmware; MLST must still say file (#526 review)."""
    path = f"{volume_dir}/probe.d64"
    storage_delete_file(_HOST, path, missing_ok=True)
    image = bytes(174848)
    try:
        storage_put_file(_HOST, path, image)
        assert storage_get_file(_HOST, path) == image
        assert storage_put_file(_HOST, path, b"\x01" * 174848, overwrite=True).replaced
        with pytest.raises(FileExistsError):
            storage_mkdir(_HOST, f"{path}/sub")
    finally:
        assert storage_delete_file(_HOST, path, missing_ok=True)


def test_mkdir_is_idempotent_and_a_directory_is_not_a_file(volume_dir: str) -> None:
    storage_mkdir(_HOST, volume_dir)
    assert storage_mkdir(_HOST, volume_dir) is False
    with pytest.raises(IsADirectoryError):
        storage_get_file(_HOST, volume_dir)
    with pytest.raises(IsADirectoryError):
        storage_delete_file(_HOST, volume_dir)


def test_a_missing_volume_is_a_typed_error(locked) -> None:
    usable = storage_volumes(_HOST)
    with pytest.raises(Ultimate64StorageVolumeError) as ei:
        storage_put_file(_HOST, "/NoSuchVolume/x.bin", b"x")
    assert ei.value.available == usable
