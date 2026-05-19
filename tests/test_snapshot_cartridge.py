"""Tests for the cartridge side-channel snapshot extension (Phase B).

Covers the :class:`CartridgeState` surface, its integration with the
:class:`Snapshot` dataclass, the bundle-codec round-trip (with and
without a cart), the dispatcher-driven restore paths (VICE
``resource_set`` and U64 ``run_crt``), and the empty-image WARNING
case.

All tests are offline — no live VICE or U64.  Mock transports cover
both backends via duck-typing (the same dispatch shape the disk-sidecar
phase uses).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from c64_test_harness import (
    CartridgeState,
    DriveState,
    Snapshot,
    SnapshotFormatError,
    extract_snapshot,
    restore_snapshot,
)


# ---------------------------------------------------------------------------
# CartridgeState validation
# ---------------------------------------------------------------------------


class TestCartridgeStateValidation:
    def test_minimal(self) -> None:
        c = CartridgeState(image=b"\x00" * 8200)
        assert c.image == b"\x00" * 8200
        assert c.cart_type == ""
        assert c.reset_on_attach is True

    def test_empty_image_allowed(self) -> None:
        """Empty image = metadata-only (parallel to drives Phase B)."""
        c = CartridgeState(image=b"", cart_type="generic")
        assert c.image == b""
        assert c.cart_type == "generic"

    def test_bytearray_coerced(self) -> None:
        c = CartridgeState(image=bytearray(b"\x42\x43"))
        assert isinstance(c.image, bytes)
        assert c.image == b"\x42\x43"

    def test_reset_on_attach_false(self) -> None:
        c = CartridgeState(image=b"x", reset_on_attach=False)
        assert c.reset_on_attach is False

    def test_image_not_bytes_rejected(self) -> None:
        with pytest.raises(TypeError, match="image must be bytes"):
            CartridgeState(image="not bytes")  # type: ignore[arg-type]

    def test_cart_type_not_str_rejected(self) -> None:
        with pytest.raises(TypeError, match="cart_type must be str"):
            CartridgeState(image=b"x", cart_type=123)  # type: ignore[arg-type]

    def test_reset_on_attach_not_bool_rejected(self) -> None:
        with pytest.raises(TypeError, match="reset_on_attach must be bool"):
            CartridgeState(
                image=b"x", reset_on_attach="yes",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Snapshot.cartridge integration
# ---------------------------------------------------------------------------


class TestSnapshotCartridge:
    def test_default_cartridge_none(self) -> None:
        """Phase A backwards compat: cartridge defaults to None."""
        snap = Snapshot(ram=b"\x00" * 65536, cpu_port_data=0x37, cpu_port_dir=0x2F)
        assert snap.cartridge is None

    def test_cartridge_attached(self) -> None:
        c = CartridgeState(image=b"\x00" * 8200, cart_type="generic-8k")
        snap = Snapshot(
            ram=b"\x00" * 65536,
            cpu_port_data=0,
            cpu_port_dir=0,
            cartridge=c,
        )
        assert snap.cartridge is c
        assert snap.cartridge.cart_type == "generic-8k"

    def test_non_cartridge_rejected(self) -> None:
        with pytest.raises(TypeError, match="cartridge must be"):
            Snapshot(
                ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
                cartridge="oops",  # type: ignore[arg-type]
            )

    def test_with_drives_and_cartridge_coexist(self) -> None:
        d = DriveState(device=8, drive_type="1541", image=b"x", image_format="d64")
        c = CartridgeState(image=b"crt-bytes", cart_type="easyflash")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
            drives=(d,), cartridge=c,
        )
        assert snap.drives == (d,)
        assert snap.cartridge == c


# ---------------------------------------------------------------------------
# Bundle codec — cart present and absent
# ---------------------------------------------------------------------------


def _snap_with_cart(image: bytes = b"CRT-PAYLOAD-XYZ") -> Snapshot:
    return Snapshot(
        ram=b"\x00" * 65536,
        cpu_port_data=0x37,
        cpu_port_dir=0x2F,
        cartridge=CartridgeState(
            image=image, cart_type="generic-8k", reset_on_attach=True,
        ),
    )


class TestBundleCodecCartridge:
    def test_round_trip_with_cart(self, tmp_path: Path) -> None:
        snap = _snap_with_cart()
        out = snap.to_bundle(tmp_path / "snap")
        # The .crt file is sibling to snapshot.vsf.
        assert (out / "cartridge.crt").read_bytes() == b"CRT-PAYLOAD-XYZ"
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["cartridge"]["cart_type"] == "generic-8k"
        assert manifest["cartridge"]["reset_on_attach"] is True
        assert manifest["cartridge"]["image_file"] == "cartridge.crt"

        loaded = Snapshot.from_bundle(out)
        assert loaded.cartridge is not None
        assert loaded.cartridge.image == b"CRT-PAYLOAD-XYZ"
        assert loaded.cartridge.cart_type == "generic-8k"
        assert loaded.cartridge.reset_on_attach is True

    def test_round_trip_without_cart(self, tmp_path: Path) -> None:
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        out = snap.to_bundle(tmp_path / "snap")
        assert not (out / "cartridge.crt").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert "cartridge" not in manifest

        loaded = Snapshot.from_bundle(out)
        assert loaded.cartridge is None

    def test_round_trip_cart_metadata_only(self, tmp_path: Path) -> None:
        """Empty image → no .crt file written, manifest image_file=null."""
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
            cartridge=CartridgeState(
                image=b"", cart_type="freezer", reset_on_attach=False,
            ),
        )
        out = snap.to_bundle(tmp_path / "snap")
        assert not (out / "cartridge.crt").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["cartridge"]["image_file"] is None
        assert manifest["cartridge"]["cart_type"] == "freezer"
        assert manifest["cartridge"]["reset_on_attach"] is False

        loaded = Snapshot.from_bundle(out)
        assert loaded.cartridge is not None
        assert loaded.cartridge.image == b""
        assert loaded.cartridge.cart_type == "freezer"
        assert loaded.cartridge.reset_on_attach is False

    def test_missing_cart_file_raises(self, tmp_path: Path) -> None:
        snap = _snap_with_cart()
        out = snap.to_bundle(tmp_path / "snap")
        # Manifest claims a cart file that we delete to simulate corruption.
        (out / "cartridge.crt").unlink()
        with pytest.raises(SnapshotFormatError, match="missing cartridge image"):
            Snapshot.from_bundle(out)


# ---------------------------------------------------------------------------
# Mock transports — VICE and U64 shapes
# ---------------------------------------------------------------------------


def _make_vice_mock() -> MagicMock:
    """VICE shape: resource_get/resource_set, no ``client`` attribute."""
    mock = MagicMock(spec=[
        "read_memory", "write_memory", "resource_get", "resource_set",
        "attach_drive", "memory_policy",
    ])
    mock.read_memory.return_value = b"\x00" * 65536
    mock.memory_policy = None
    mock.resource_get.side_effect = KeyError("no resource probed in this mock")
    return mock


def _make_u64_mock() -> MagicMock:
    """U64 shape: ``client`` attribute with ``run_crt``."""
    mock = MagicMock(spec=["read_memory", "write_memory", "client", "memory_policy"])
    mock.read_memory.return_value = b"\x00" * 65536
    mock.memory_policy = None
    mock.client = MagicMock(spec=[
        "run_crt", "list_drives", "mount_disk", "drive_set_mode", "drive_on",
    ])
    mock.client.list_drives.return_value = {"drives": []}
    return mock


# ---------------------------------------------------------------------------
# restore_snapshot — U64 cart path
# ---------------------------------------------------------------------------


class TestRestoreCartridgeU64:
    def test_run_crt_invoked(self) -> None:
        mock = _make_u64_mock()
        c = CartridgeState(image=b"CARTBYTES", cart_type="generic-8k")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        restore_snapshot(mock, snap)
        mock.client.run_crt.assert_called_once_with(b"CARTBYTES")

    def test_run_crt_runs_before_ram_write(self) -> None:
        """``run_crt`` resets the CPU; ordering matters."""
        mock = _make_u64_mock()
        order: list[str] = []
        mock.client.run_crt.side_effect = lambda data: order.append("run_crt")
        mock.write_memory.side_effect = (
            lambda *args, **kwargs: order.append("write_memory")
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0,
            cartridge=CartridgeState(image=b"x"),
        )
        restore_snapshot(mock, snap)
        # First call must be run_crt; subsequent calls write_memory.
        assert order[0] == "run_crt"
        assert order.count("run_crt") == 1
        assert order.count("write_memory") >= 1

    def test_empty_image_skips_run_crt(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock = _make_u64_mock()
        c = CartridgeState(image=b"", cart_type="generic-8k")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        with caplog.at_level(logging.WARNING):
            restore_snapshot(mock, snap)
        mock.client.run_crt.assert_not_called()
        assert any(
            "cartridge metadata" in rec.message and "no image bytes" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# restore_snapshot — VICE cart path
# ---------------------------------------------------------------------------


class TestRestoreCartridgeVICE:
    def test_resource_set_invoked_generic(self) -> None:
        mock = _make_vice_mock()
        c = CartridgeState(
            image=b"\x43\x36\x34\x20CART", cart_type="generic-8k",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        restore_snapshot(mock, snap)
        # The dispatcher should have called resource_set("CartridgeFile",
        # <temp path>) at least once.
        cart_file_calls = [
            call for call in mock.resource_set.call_args_list
            if call.args and call.args[0] == "CartridgeFile"
        ]
        assert len(cart_file_calls) == 1
        # And the temp file should contain the cart bytes.
        temp_path = Path(cart_file_calls[0].args[1])
        assert temp_path.read_bytes() == b"\x43\x36\x34\x20CART"
        # CartridgeType should have been set to 1 (generic).
        mock.resource_set.assert_any_call("CartridgeType", 1)
        # CartridgeReset should match reset_on_attach=True.
        mock.resource_set.assert_any_call("CartridgeReset", 1)

    def test_reset_on_attach_false_sets_zero(self) -> None:
        mock = _make_vice_mock()
        c = CartridgeState(image=b"x", cart_type="generic", reset_on_attach=False)
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        restore_snapshot(mock, snap)
        mock.resource_set.assert_any_call("CartridgeReset", 0)

    def test_unknown_cart_type_no_type_int_set(self) -> None:
        """Unknown cart_type → VICE auto-detects from the .crt header.

        We should not call ``resource_set("CartridgeType", ...)`` with
        a guessed value — VICE's auto-detection on CartridgeFile is
        the safer path.
        """
        mock = _make_vice_mock()
        # Empty cart_type → no known mapping → no CartridgeType call.
        c = CartridgeState(image=b"x", cart_type="")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        restore_snapshot(mock, snap)
        type_calls = [
            call for call in mock.resource_set.call_args_list
            if call.args and call.args[0] == "CartridgeType"
        ]
        assert type_calls == []
        # CartridgeFile still gets set.
        cart_file_calls = [
            call for call in mock.resource_set.call_args_list
            if call.args and call.args[0] == "CartridgeFile"
        ]
        assert len(cart_file_calls) == 1

    def test_non_runtime_attachable_warns_and_skips(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Freezer / action-replay etc. are not reliable runtime-attach.

        The .crt bytes are still in the snapshot — we just don't push
        them via the binary monitor; the caller is expected to
        relaunch VICE with ``ViceConfig.extra_args=['-cartcrt', path]``.
        """
        mock = _make_vice_mock()
        c = CartridgeState(image=b"x", cart_type="action-replay")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        with caplog.at_level(logging.WARNING):
            restore_snapshot(mock, snap)
        # No CartridgeFile resource_set when we bail out.
        cart_file_calls = [
            call for call in mock.resource_set.call_args_list
            if call.args and call.args[0] == "CartridgeFile"
        ]
        assert cart_file_calls == []
        assert any(
            "not reliable" in rec.message
            and "action-replay" in rec.message
            for rec in caplog.records
        )

    def test_empty_image_skips_attach(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock = _make_vice_mock()
        c = CartridgeState(image=b"", cart_type="generic-8k")
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, cartridge=c,
        )
        with caplog.at_level(logging.WARNING):
            restore_snapshot(mock, snap)
        cart_file_calls = [
            call for call in mock.resource_set.call_args_list
            if call.args and call.args[0] == "CartridgeFile"
        ]
        assert cart_file_calls == []
        assert any(
            "cartridge metadata" in rec.message and "no image bytes" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# extract_snapshot — host_cart_path
# ---------------------------------------------------------------------------


class TestExtractCartridge:
    def test_no_host_path_no_introspection_returns_none(self) -> None:
        mock = _make_vice_mock()
        # No CartridgeType resource (KeyError side effect) and no cart
        # path → cartridge should stay None.
        snap = extract_snapshot(mock, include_registers=False)
        assert snap.cartridge is None

    def test_host_path_picks_up_bytes(self, tmp_path: Path) -> None:
        crt_path = tmp_path / "test.crt"
        crt_path.write_bytes(b"FAKE-CRT-HEADER\x00\x00body")
        mock = _make_vice_mock()
        snap = extract_snapshot(
            mock, host_cart_path=crt_path, cart_type="generic-8k",
            include_registers=False,
        )
        assert snap.cartridge is not None
        assert snap.cartridge.image == b"FAKE-CRT-HEADER\x00\x00body"
        assert snap.cartridge.cart_type == "generic-8k"
        assert snap.cartridge.reset_on_attach is True

    def test_vice_resource_type_inferred_when_no_path(self) -> None:
        """VICE-side: a non-zero CartridgeType resource records *something*.

        We can't get the bytes back, but we know a cart is attached.
        """
        mock = _make_vice_mock()
        # Pretend VICE has a generic-8k cart attached.
        mock.resource_get.side_effect = (
            lambda name: 1 if name == "CartridgeType" else None
        )
        snap = extract_snapshot(mock, include_registers=False)
        assert snap.cartridge is not None
        assert snap.cartridge.image == b""
        # Reverse-lookup picks the first matching key — "generic" maps to 1.
        assert snap.cartridge.cart_type in ("generic", "generic-8k", "action-replay")

    def test_cart_reset_on_attach_kwarg(self, tmp_path: Path) -> None:
        crt_path = tmp_path / "x.crt"
        crt_path.write_bytes(b"x")
        mock = _make_vice_mock()
        snap = extract_snapshot(
            mock, host_cart_path=crt_path, cart_reset_on_attach=False,
            include_registers=False,
        )
        assert snap.cartridge is not None
        assert snap.cartridge.reset_on_attach is False


# ---------------------------------------------------------------------------
# Backwards compatibility — Phase A + disk-sidecar snapshots round-trip
# unchanged through the cart-aware code paths.
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_no_cart_no_drives_bundle_round_trip(self, tmp_path: Path) -> None:
        ram = bytearray(65536)
        ram[0x100] = 0x42
        snap = Snapshot(
            ram=bytes(ram), cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        out = snap.to_bundle(tmp_path / "snap")
        loaded = Snapshot.from_bundle(out)
        assert loaded.ram[0x100] == 0x42
        assert loaded.drives == ()
        assert loaded.cartridge is None

    def test_drives_only_bundle_round_trip(self, tmp_path: Path) -> None:
        d = DriveState(
            device=8, drive_type="1541", image=b"DISK", image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        out = snap.to_bundle(tmp_path / "snap")
        loaded = Snapshot.from_bundle(out)
        assert loaded.cartridge is None
        assert len(loaded.drives) == 1
        assert loaded.drives[0].image == b"DISK"

    def test_restore_without_cartridge_does_not_call_run_crt(self) -> None:
        mock = _make_u64_mock()
        snap = Snapshot(ram=b"\x00" * 65536, cpu_port_data=0, cpu_port_dir=0)
        restore_snapshot(mock, snap)
        mock.client.run_crt.assert_not_called()
