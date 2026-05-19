"""Tests for the disk side-channel snapshot extension (Phase B partial).

Phase A (RAM + CPU port) is covered by ``tests/test_snapshot.py``.  This
module exercises the drive-sidecar surface:

* :class:`DriveState` validation.
* :class:`Snapshot` integration with the ``drives`` field (default empty
  preserves Phase A behaviour; duplicate-device detection).
* :meth:`Snapshot.to_bundle` / :meth:`Snapshot.from_bundle` round-trip
  with and without drive payload.
* :func:`extract_snapshot` drive-discovery on mocked VICE and U64
  transports.
* :func:`restore_snapshot` drive-restore on mocked VICE and U64
  transports — and the asymmetry warning when a VICE snapshot has
  drives on devices 10/11 that the U64 cannot host.

All tests are offline (no live VICE or U64).  Mock transports cover
both the duck-typed ``client.mount_disk`` (U64) and ``attach_drive`` /
``resource_get`` (VICE) paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from c64_test_harness import (
    DriveState,
    Snapshot,
    SnapshotFormatError,
    extract_snapshot,
    restore_snapshot,
)


# ---------------------------------------------------------------------------
# DriveState validation
# ---------------------------------------------------------------------------


class TestDriveStateValidation:
    def test_valid_1541_d64(self) -> None:
        d = DriveState(
            device=8, drive_type="1541", image=b"\x00" * 174848,
            image_format="d64",
        )
        assert d.device == 8
        assert d.drive_type == "1541"
        assert d.image_format == "d64"
        assert d.mode == "readwrite"

    def test_valid_1571_d71(self) -> None:
        DriveState(device=9, drive_type="1571", image=b"x", image_format="d71")

    def test_valid_1581_d81(self) -> None:
        DriveState(device=10, drive_type="1581", image=b"x", image_format="d81")

    def test_valid_1541_g64(self) -> None:
        DriveState(device=8, drive_type="1541", image=b"x", image_format="g64")

    def test_empty_image_allowed(self) -> None:
        """Empty image is legal — represents a slot config without bytes
        (e.g. extracted from U64 where REST can't read image bytes).
        """
        d = DriveState(device=8, drive_type="1541", image=b"", image_format="d64")
        assert d.image == b""

    def test_readonly_mode(self) -> None:
        d = DriveState(
            device=8, drive_type="1541", image=b"x",
            image_format="d64", mode="readonly",
        )
        assert d.mode == "readonly"

    def test_unlinked_mode(self) -> None:
        DriveState(
            device=8, drive_type="1541", image=b"x",
            image_format="d64", mode="unlinked",
        )

    def test_invalid_device_7(self) -> None:
        with pytest.raises(ValueError, match="device must be"):
            DriveState(device=7, drive_type="1541", image=b"x", image_format="d64")

    def test_invalid_device_12(self) -> None:
        with pytest.raises(ValueError, match="device must be"):
            DriveState(device=12, drive_type="1541", image=b"x", image_format="d64")

    def test_invalid_drive_type(self) -> None:
        with pytest.raises(ValueError, match="drive_type must be"):
            DriveState(device=8, drive_type="1551", image=b"x", image_format="d64")

    def test_invalid_image_format(self) -> None:
        with pytest.raises(ValueError, match="image_format must be"):
            DriveState(device=8, drive_type="1541", image=b"x", image_format="d99")

    def test_invalid_mode(self) -> None:
        with pytest.raises(ValueError, match="mode must be"):
            DriveState(
                device=8, drive_type="1541", image=b"x",
                image_format="d64", mode="overlay",
            )

    def test_mismatched_1581_d64(self) -> None:
        with pytest.raises(ValueError, match="not compatible"):
            DriveState(device=8, drive_type="1581", image=b"x", image_format="d64")

    def test_mismatched_1541_d81(self) -> None:
        with pytest.raises(ValueError, match="not compatible"):
            DriveState(device=8, drive_type="1541", image=b"x", image_format="d81")

    def test_mismatched_1571_g64(self) -> None:
        with pytest.raises(ValueError, match="not compatible"):
            DriveState(device=8, drive_type="1571", image=b"x", image_format="g64")

    def test_image_not_bytes(self) -> None:
        with pytest.raises(TypeError, match="image must be bytes"):
            DriveState(
                device=8, drive_type="1541", image="not bytes",  # type: ignore[arg-type]
                image_format="d64",
            )


# ---------------------------------------------------------------------------
# Snapshot.drives integration
# ---------------------------------------------------------------------------


class TestSnapshotDrives:
    def test_default_drives_empty(self) -> None:
        """Phase A backwards compat: drives default to empty tuple."""
        snap = Snapshot(ram=b"\x00" * 65536, cpu_port_data=0x37, cpu_port_dir=0x2F)
        assert snap.drives == ()

    def test_drives_tuple(self) -> None:
        d = DriveState(device=8, drive_type="1541", image=b"x", image_format="d64")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        assert snap.drives == (d,)

    def test_drives_list_normalised_to_tuple(self) -> None:
        d = DriveState(device=8, drive_type="1541", image=b"x", image_format="d64")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
            drives=[d],  # type: ignore[arg-type]
        )
        assert isinstance(snap.drives, tuple)

    def test_duplicate_device_rejected(self) -> None:
        d8a = DriveState(device=8, drive_type="1541", image=b"a", image_format="d64")
        d8b = DriveState(device=8, drive_type="1581", image=b"b", image_format="d81")
        with pytest.raises(ValueError, match="duplicate DriveState for device 8"):
            Snapshot(
                ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
                drives=(d8a, d8b),
            )

    def test_non_drivestate_rejected(self) -> None:
        with pytest.raises(TypeError, match="drives must"):
            Snapshot(
                ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
                drives=("not a DriveState",),  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Bundle codec (directory layout)
# ---------------------------------------------------------------------------


def _snap_no_drives() -> Snapshot:
    ram = bytearray(65536)
    ram[0xC000] = 0xAB
    ram[0xC001] = 0xCD
    return Snapshot(
        ram=bytes(ram), cpu_port_data=0x37, cpu_port_dir=0x2F,
    )


class TestBundleCodec:
    def test_round_trip_no_drives(self, tmp_path: Path) -> None:
        snap = _snap_no_drives()
        out = snap.to_bundle(tmp_path / "snap")
        assert (out / "snapshot.vsf").is_file()
        assert (out / "manifest.json").is_file()
        loaded = Snapshot.from_bundle(out)
        assert loaded.ram[0xC000] == 0xAB
        assert loaded.cpu_port_data == 0x37
        assert loaded.drives == ()

    def test_round_trip_with_drives(self, tmp_path: Path) -> None:
        d8 = DriveState(
            device=8, drive_type="1541",
            image=b"\xff" * 1000, image_format="d64",
        )
        d9 = DriveState(
            device=9, drive_type="1581",
            image=b"\xaa" * 2000, image_format="d81", mode="readonly",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
            drives=(d8, d9),
        )
        out = snap.to_bundle(tmp_path / "snap")
        assert (out / "drive8.d64").read_bytes() == b"\xff" * 1000
        assert (out / "drive9.d81").read_bytes() == b"\xaa" * 2000
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["version"] == 1
        assert len(manifest["drives"]) == 2

        loaded = Snapshot.from_bundle(out)
        assert len(loaded.drives) == 2
        by_device = {d.device: d for d in loaded.drives}
        assert by_device[8].image == b"\xff" * 1000
        assert by_device[9].image == b"\xaa" * 2000
        assert by_device[9].mode == "readonly"
        assert by_device[9].drive_type == "1581"

    def test_empty_image_no_file_written(self, tmp_path: Path) -> None:
        """An empty image (from U64 extract) shouldn't produce a zero-byte file."""
        d = DriveState(
            device=8, drive_type="1541", image=b"", image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        out = snap.to_bundle(tmp_path / "snap")
        assert not (out / "drive8.d64").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["drives"][0]["image_file"] is None
        loaded = Snapshot.from_bundle(out)
        assert loaded.drives[0].image == b""

    def test_missing_vsf_raises(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text('{"drives": []}')
        with pytest.raises(SnapshotFormatError, match="no snapshot.vsf"):
            Snapshot.from_bundle(tmp_path)

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        snap = _snap_no_drives()
        (tmp_path / "snapshot.vsf").write_bytes(snap.to_vsf())
        with pytest.raises(SnapshotFormatError, match="no manifest.json"):
            Snapshot.from_bundle(tmp_path)

    def test_malformed_manifest_raises(self, tmp_path: Path) -> None:
        snap = _snap_no_drives()
        (tmp_path / "snapshot.vsf").write_bytes(snap.to_vsf())
        (tmp_path / "manifest.json").write_text("not json {")
        with pytest.raises(SnapshotFormatError, match="manifest.json malformed"):
            Snapshot.from_bundle(tmp_path)

    def test_missing_image_file_raises(self, tmp_path: Path) -> None:
        snap = _snap_no_drives()
        (tmp_path / "snapshot.vsf").write_bytes(snap.to_vsf())
        (tmp_path / "manifest.json").write_text(json.dumps({
            "version": 1,
            "cpu_port_data": 0, "cpu_port_dir": 0, "exrom": 1, "game": 1,
            "drives": [{
                "device": 8, "drive_type": "1541",
                "image_format": "d64", "mode": "readwrite",
                "image_file": "drive8.d64",
            }],
        }))
        with pytest.raises(SnapshotFormatError, match="missing image file"):
            Snapshot.from_bundle(tmp_path)


# ---------------------------------------------------------------------------
# extract_snapshot — VICE path
# ---------------------------------------------------------------------------


def _make_vice_mock(resource_values: dict[str, object]) -> MagicMock:
    """Build a VICE-shaped mock transport.

    No ``client`` attr (so the dispatcher routes to the VICE path), has
    ``resource_get`` and ``read_memory``.  ``resource_values`` is the
    full {resource_name: value} dictionary; unknown names raise
    ``KeyError`` from the side_effect.
    """
    mock = MagicMock(spec=["read_memory", "resource_get", "resource_set",
                            "write_memory", "attach_drive", "memory_policy"])
    mock.read_memory.return_value = b"\x00" * 65536
    mock.memory_policy = None

    def _resource_get(name: str):
        if name in resource_values:
            v = resource_values[name]
            if isinstance(v, Exception):
                raise v
            return v
        raise KeyError(name)

    mock.resource_get.side_effect = _resource_get
    return mock


class TestExtractDrivesVICE:
    def test_no_drives_returns_empty(self) -> None:
        mock = _make_vice_mock({
            "Drive8Type": 0, "Drive9Type": 0,
            "Drive10Type": 0, "Drive11Type": 0,
        })
        snap = extract_snapshot(mock)
        assert snap.drives == ()

    def test_drive_with_host_path(self, tmp_path: Path) -> None:
        image_path = tmp_path / "test.d64"
        image_path.write_bytes(b"\x42" * 174848)
        mock = _make_vice_mock({
            "Drive8Type": 1541, "Drive9Type": 0,
            "Drive10Type": 0, "Drive11Type": 0,
        })
        snap = extract_snapshot(mock, host_image_paths={8: image_path})
        assert len(snap.drives) == 1
        assert snap.drives[0].device == 8
        assert snap.drives[0].drive_type == "1541"
        assert snap.drives[0].image == b"\x42" * 174848
        assert snap.drives[0].image_format == "d64"

    def test_two_drives_different_types(self, tmp_path: Path) -> None:
        d8 = tmp_path / "a.d64"; d8.write_bytes(b"a")
        d9 = tmp_path / "b.d81"; d9.write_bytes(b"b")
        mock = _make_vice_mock({
            "Drive8Type": 1541, "Drive9Type": 1581,
            "Drive10Type": 0, "Drive11Type": 0,
        })
        snap = extract_snapshot(mock, host_image_paths={8: d8, 9: d9})
        assert len(snap.drives) == 2
        by_device = {d.device: d for d in snap.drives}
        assert by_device[8].drive_type == "1541"
        assert by_device[8].image_format == "d64"
        assert by_device[9].drive_type == "1581"
        assert by_device[9].image_format == "d81"

    def test_drive_without_image_path_emits_empty(self) -> None:
        """When no host_image_paths and resource_get can't find the
        image path, the DriveState is emitted with empty bytes — the
        drive_type alone is still useful for restore-time config."""
        mock = _make_vice_mock({"Drive8Type": 1541, "Drive9Type": 0,
                                  "Drive10Type": 0, "Drive11Type": 0})
        snap = extract_snapshot(mock)
        assert len(snap.drives) == 1
        assert snap.drives[0].image == b""
        assert snap.drives[0].drive_type == "1541"


# ---------------------------------------------------------------------------
# extract_snapshot — U64 path
# ---------------------------------------------------------------------------


def _make_u64_mock(list_drives_response: dict) -> MagicMock:
    """Build a U64-shaped mock transport.

    Has a ``client`` attribute exposing ``list_drives`` and
    ``mount_disk`` / ``drive_set_mode`` / ``drive_on``.  The dispatcher
    routes to the U64 path because ``client.mount_disk`` exists.
    """
    mock = MagicMock(spec=["read_memory", "write_memory", "client",
                            "memory_policy"])
    mock.read_memory.return_value = b"\x00" * 65536
    mock.memory_policy = None
    mock.client = MagicMock(spec=[
        "list_drives", "mount_disk", "drive_set_mode", "drive_on",
    ])
    mock.client.list_drives.return_value = list_drives_response
    return mock


class TestExtractDrivesU64:
    def test_no_drives_returns_empty(self) -> None:
        mock = _make_u64_mock({"drives": []})
        snap = extract_snapshot(mock)
        assert snap.drives == ()

    def test_slot_a_only(self, tmp_path: Path) -> None:
        image = tmp_path / "u64.d64"
        image.write_bytes(b"\x99" * 174848)
        mock = _make_u64_mock({"drives": [
            {"a": {"enabled": True, "bus_id_mode": "1541"}},
        ]})
        snap = extract_snapshot(mock, host_image_paths={8: image})
        assert len(snap.drives) == 1
        assert snap.drives[0].device == 8
        assert snap.drives[0].drive_type == "1541"
        assert snap.drives[0].image == b"\x99" * 174848

    def test_both_slots(self, tmp_path: Path) -> None:
        mock = _make_u64_mock({"drives": [
            {"a": {"enabled": True, "bus_id_mode": "1541"}},
            {"b": {"enabled": True, "bus_id_mode": "1581"}},
        ]})
        snap = extract_snapshot(mock)
        # Image bytes empty (U64 REST can't read images back) — drive
        # type still captured for restore-time configuration.
        assert len(snap.drives) == 2
        by_device = {d.device: d for d in snap.drives}
        assert by_device[8].drive_type == "1541"
        assert by_device[9].drive_type == "1581"
        assert all(d.image == b"" for d in snap.drives)

    def test_disabled_slot_skipped(self) -> None:
        mock = _make_u64_mock({"drives": [
            {"a": {"enabled": True, "bus_id_mode": "1541"}},
            {"b": {"enabled": False, "bus_id_mode": "1581"}},
        ]})
        snap = extract_snapshot(mock)
        assert {d.device for d in snap.drives} == {8}


# ---------------------------------------------------------------------------
# restore_snapshot — VICE path
# ---------------------------------------------------------------------------


class TestRestoreDrivesVICE:
    def test_attach_drive_invoked(self) -> None:
        mock = _make_vice_mock({})  # resource_set is auto-mocked too
        d = DriveState(
            device=8, drive_type="1541", image=b"\x11" * 1000,
            image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        mock.attach_drive.assert_called_once()
        args, kwargs = mock.attach_drive.call_args
        assert args[0] == 8                     # device
        assert isinstance(args[1], str)         # temp path
        assert kwargs.get("read_only") is False
        # The temp file path passed in should contain the image bytes.
        path = Path(args[1])
        assert path.read_bytes() == b"\x11" * 1000

    def test_readonly_passthrough(self) -> None:
        mock = _make_vice_mock({})
        d = DriveState(
            device=9, drive_type="1581", image=b"x", image_format="d81",
            mode="readonly",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        _, kwargs = mock.attach_drive.call_args
        assert kwargs.get("read_only") is True

    def test_drive_type_resource_set(self) -> None:
        mock = _make_vice_mock({})
        d = DriveState(
            device=8, drive_type="1571", image=b"x", image_format="d71",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        mock.resource_set.assert_any_call("Drive8Type", 1571)

    def test_empty_image_skips_attach(self) -> None:
        mock = _make_vice_mock({})
        d = DriveState(
            device=8, drive_type="1541", image=b"", image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        mock.attach_drive.assert_not_called()


# ---------------------------------------------------------------------------
# restore_snapshot — U64 path + asymmetry
# ---------------------------------------------------------------------------


class TestRestoreDrivesU64:
    def test_slot_a_mount_invoked(self) -> None:
        mock = _make_u64_mock({"drives": []})
        d = DriveState(
            device=8, drive_type="1541", image=b"\x77" * 500,
            image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        mock.client.mount_disk.assert_called_once_with(
            "a", b"\x77" * 500, "d64", "readwrite",
        )
        mock.client.drive_set_mode.assert_any_call("a", "1541")
        mock.client.drive_on.assert_any_call("a")

    def test_slot_b_mount_invoked(self) -> None:
        mock = _make_u64_mock({"drives": []})
        d = DriveState(
            device=9, drive_type="1581", image=b"x", image_format="d81",
            mode="readonly",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        mock.client.mount_disk.assert_called_once_with(
            "b", b"x", "d81", "readonly",
        )

    def test_device_10_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock = _make_u64_mock({"drives": []})
        d10 = DriveState(
            device=10, drive_type="1541", image=b"x", image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d10,),
        )
        with caplog.at_level(logging.WARNING):
            restore_snapshot(mock, snap)
        mock.client.mount_disk.assert_not_called()
        assert any(
            "cannot be hosted on Ultimate 64" in rec.message
            for rec in caplog.records
        )

    def test_device_11_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock = _make_u64_mock({"drives": []})
        d11 = DriveState(
            device=11, drive_type="1581", image=b"x", image_format="d81",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d11,),
        )
        with caplog.at_level(logging.WARNING):
            restore_snapshot(mock, snap)
        mock.client.mount_disk.assert_not_called()
        assert any(
            "cannot be hosted on Ultimate 64" in rec.message
            for rec in caplog.records
        )

    def test_mixed_8_and_10(self, caplog: pytest.LogCaptureFixture) -> None:
        """Device 8 should mount on slot a; device 10 should warn+skip."""
        mock = _make_u64_mock({"drives": []})
        d8 = DriveState(
            device=8, drive_type="1541", image=b"a", image_format="d64",
        )
        d10 = DriveState(
            device=10, drive_type="1541", image=b"b", image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
            drives=(d8, d10),
        )
        with caplog.at_level(logging.WARNING):
            restore_snapshot(mock, snap)
        mock.client.mount_disk.assert_called_once_with(
            "a", b"a", "d64", "readwrite",
        )
        assert any(
            "cannot be hosted on Ultimate 64" in rec.message
            for rec in caplog.records
        )

    def test_empty_image_skips_mount(self) -> None:
        mock = _make_u64_mock({"drives": []})
        d = DriveState(
            device=8, drive_type="1541", image=b"", image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        restore_snapshot(mock, snap)
        mock.client.mount_disk.assert_not_called()


# ---------------------------------------------------------------------------
# Live VICE round-trip — TODO when fixture is available.
# ---------------------------------------------------------------------------
# A real ``x64sc`` instance + a known ``.d64`` could verify the
# attach_drive path end-to-end. Deferred — the mocks cover the dispatch
# logic; a live test would only re-verify VICE's own attach behaviour.
