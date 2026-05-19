"""Tests for the REU side-channel snapshot extension (Phase C).

Layered like the other snapshot test files:

* :class:`Snapshot` validation for the new ``reu_size_bytes`` /
  ``reu_contents`` fields.
* Offline ``.vsf`` codec round-trip with a REU module — emit through
  ``to_vsf`` and parse back through ``from_vsf``.
* Bundle codec with the new ``reu.bin`` sidecar.
* Mocked extract path: verifies the staging-window stash, the correct
  ``$DF02..$DF0A`` register-write sequence per bank, and restore of the
  staging window via try/finally.
* Mocked restore path: verifies the ``SocketDMAClient.reu_write`` chunk
  sequence and the ``set_reu(enabled=True, size=...)`` precondition.
* Backwards compat: Phase A/B test patterns keep working with
  ``reu_size_bytes`` defaulting to 0.

All tests are offline — no live VICE or U64 fixture required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness import (
    DriveState,
    Snapshot,
    SnapshotFormatError,
    extract_snapshot,
    restore_snapshot,
)

# Reach into module internals for the format-verification probes.
from c64_test_harness.snapshot import (
    _REU_CMD_REU_TO_C64,
    _REU_MODULE_NAME,
    _REU_PREAMBLE_LEN,
    _REU_SIZE_BYTES,
    _REU_STAGING_ADDR,
    _REU_STAGING_LEN,
    _build_reu_module,
    _inject_reu_module,
    _iter_modules,
    _parse_reu_module,
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestReuValidation:
    def test_default_no_reu(self) -> None:
        """Phase A/B backwards compat: REU fields default to empty."""
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0)
        assert snap.reu_size_bytes == 0
        assert snap.reu_contents == b""

    def test_valid_128kb_reu(self) -> None:
        contents = b"x" * (128 * 1024)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
            reu_size_bytes=128 * 1024, reu_contents=contents,
        )
        assert snap.reu_size_bytes == 128 * 1024
        assert snap.reu_contents == contents

    def test_valid_16mb_reu(self) -> None:
        """16 MB is the largest legal REU size."""
        contents = bytes(16 * 1024 * 1024)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
            reu_size_bytes=16 * 1024 * 1024, reu_contents=contents,
        )
        assert snap.reu_size_bytes == 16 * 1024 * 1024
        assert len(snap.reu_contents) == 16 * 1024 * 1024

    def test_size_zero_with_nonempty_contents_rejected(self) -> None:
        with pytest.raises(ValueError, match="reu_size_bytes=0"):
            Snapshot(
                ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
                reu_size_bytes=0, reu_contents=b"\x00" * 128,
            )

    def test_size_not_in_enum_rejected(self) -> None:
        with pytest.raises(ValueError, match="not one of the REU enum"):
            Snapshot(
                ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
                reu_size_bytes=64 * 1024,  # not in enum
                reu_contents=b"\x00" * (64 * 1024),
            )

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="reu_contents has"):
            Snapshot(
                ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
                reu_size_bytes=128 * 1024,
                reu_contents=b"\x00" * 1000,  # wrong length
            )

    def test_contents_not_bytes_rejected(self) -> None:
        with pytest.raises(TypeError, match="reu_contents must be bytes"):
            Snapshot(
                ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
                reu_size_bytes=128 * 1024,
                reu_contents="not bytes" * 1000,  # type: ignore[arg-type]
            )

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            Snapshot(
                ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
                reu_size_bytes=-1, reu_contents=b"",
            )


# ---------------------------------------------------------------------------
# VSF codec with REU
# ---------------------------------------------------------------------------


def _pattern_reu(size_bytes: int) -> bytes:
    """Recognisable REU pattern: bank-keyed, byte-keyed."""
    out = bytearray(size_bytes)
    for i in range(size_bytes):
        out[i] = ((i >> 16) ^ (i >> 8) ^ i) & 0xFF
    return bytes(out)


class TestVsfReuCodec:
    def test_module_emit_roundtrip(self) -> None:
        """Build a REU1764 module then parse it back."""
        contents = _pattern_reu(128 * 1024)
        regs = bytes([0x00, 0x10, 0, 0, 0, 0, 0, 0xF8, 0xFF, 0xFF, 0x1F])
        mod = _build_reu_module(contents, regs)
        # Skip the 22-byte module header for parse_reu_module (it wants body only)
        from c64_test_harness.snapshot import _MODULE_HEADER_LEN
        body = mod[_MODULE_HEADER_LEN:]
        size, parsed = _parse_reu_module(body)
        assert size == 128 * 1024
        assert parsed == contents

    def test_to_vsf_with_reu_roundtrips(self) -> None:
        contents = _pattern_reu(128 * 1024)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F,
            reu_size_bytes=128 * 1024, reu_contents=contents,
        )
        blob = snap.to_vsf()

        # Confirm a REU1764 module appears in the emitted .vsf.
        names = {name for name, *_ in _iter_modules(blob)}
        assert _REU_MODULE_NAME in names, (
            f"expected REU1764 in modules, got {names}"
        )

        # And round-trips through from_vsf.
        restored = Snapshot.from_vsf(blob)
        assert restored.reu_size_bytes == 128 * 1024
        assert restored.reu_contents == contents

    def test_to_vsf_without_reu_emits_no_reu_module(self) -> None:
        """When the snapshot has no REU, the .vsf must match the existing
        template's module set exactly — no REU1764 added."""
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        blob = snap.to_vsf()
        names = {name for name, *_ in _iter_modules(blob)}
        assert _REU_MODULE_NAME not in names

    def test_inject_reu_replaces_existing(self) -> None:
        """Injecting REU twice should replace, not duplicate."""
        contents_a = _pattern_reu(128 * 1024)
        contents_b = b"\x42" * (128 * 1024)
        regs = bytes([0x00] * 11)

        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
            reu_size_bytes=128 * 1024, reu_contents=contents_a,
        )
        blob = snap.to_vsf()
        # Inject again with a different payload — should replace.
        blob2 = _inject_reu_module(blob, reu_contents=contents_b, control_regs=regs)

        reu_modules = [
            (start, length)
            for name, _vmaj, _vmin, start, length in _iter_modules(blob2)
            if name == _REU_MODULE_NAME
        ]
        assert len(reu_modules) == 1, "REU1764 was duplicated, not replaced"
        size, parsed = _parse_reu_module(
            blob2[reu_modules[0][0] : reu_modules[0][0] + reu_modules[0][1]]
        )
        assert parsed == contents_b

    def test_from_vsf_handles_missing_reu(self) -> None:
        """Snapshots without a REU1764 module deserialize with empty REU."""
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F)
        restored = Snapshot.from_vsf(snap.to_vsf())
        assert restored.reu_size_bytes == 0
        assert restored.reu_contents == b""

    def test_parse_reu_module_size_kb_preamble(self) -> None:
        """The first 3 bytes of the preamble must be the size in KB LE."""
        for size in (_REU_SIZE_BYTES[0], _REU_SIZE_BYTES[3], _REU_SIZE_BYTES[-1]):
            mod = _build_reu_module(bytes(size), bytes(11))
            body = mod[22:]  # skip module header
            kb = int.from_bytes(body[0:3], "little")
            assert kb * 1024 == size, f"size {size} got {kb} KB in preamble"

    def test_parse_rejects_short_body(self) -> None:
        with pytest.raises(SnapshotFormatError, match="too short"):
            _parse_reu_module(b"\x00" * (_REU_PREAMBLE_LEN - 1))

    def test_parse_rejects_size_mismatch(self) -> None:
        """A preamble claiming 256 KB on a body holding 128 KB → error."""
        body = bytearray(_REU_PREAMBLE_LEN + 128 * 1024)
        # Lie: claim 256 KB
        body[0:3] = (256).to_bytes(3, "little")
        with pytest.raises(SnapshotFormatError, match="does not match"):
            _parse_reu_module(bytes(body))


# ---------------------------------------------------------------------------
# Bundle codec with reu.bin sidecar
# ---------------------------------------------------------------------------


class TestBundleReu:
    def test_bundle_writes_reu_bin(self, tmp_path: Path) -> None:
        contents = _pattern_reu(128 * 1024)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
            reu_size_bytes=128 * 1024, reu_contents=contents,
        )
        out = snap.to_bundle(tmp_path / "snap")
        assert (out / "reu.bin").is_file()
        assert (out / "reu.bin").read_bytes() == contents
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["reu_size_bytes"] == 128 * 1024

    def test_bundle_no_reu_writes_no_file(self, tmp_path: Path) -> None:
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0)
        out = snap.to_bundle(tmp_path / "snap")
        assert not (out / "reu.bin").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["reu_size_bytes"] == 0

    def test_bundle_round_trip(self, tmp_path: Path) -> None:
        contents = _pattern_reu(256 * 1024)
        d = DriveState(
            device=8, drive_type="1541",
            image=b"\xff" * 100, image_format="d64",
        )
        snap = Snapshot(
            ram=b"\x00" * 65536, cpu_port_data=0x37, cpu_port_dir=0x2F,
            drives=(d,),
            reu_size_bytes=256 * 1024, reu_contents=contents,
        )
        out = snap.to_bundle(tmp_path / "snap")
        loaded = Snapshot.from_bundle(out)
        assert loaded.reu_size_bytes == 256 * 1024
        assert loaded.reu_contents == contents
        assert len(loaded.drives) == 1
        assert loaded.cpu_port_data == 0x37

    def test_bundle_manifest_without_sidecar(self, tmp_path: Path) -> None:
        """manifest says REU is present but reu.bin is missing →
        falls back to the .vsf's REU bytes if any, else errors."""
        snap = _snap_with_reu(128 * 1024)
        # Write the bundle, then strip reu.bin to simulate a damaged bundle.
        out = snap.to_bundle(tmp_path / "snap")
        (out / "reu.bin").unlink()
        # The .vsf carries the REU bytes too — from_bundle should fall back.
        loaded = Snapshot.from_bundle(out)
        assert loaded.reu_size_bytes == 128 * 1024
        assert loaded.reu_contents == snap.reu_contents


def _snap_with_reu(size_bytes: int) -> Snapshot:
    return Snapshot(
        ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
        reu_size_bytes=size_bytes, reu_contents=_pattern_reu(size_bytes),
    )


# ---------------------------------------------------------------------------
# Mocked extract — U64 staging-window dance
# ---------------------------------------------------------------------------


def _make_u64_reu_mock(reu_enabled: bool, reu_size_str: str = "128 KB") -> MagicMock:
    """U64-shaped mock with REU enabled and a fake REU backing store."""
    mock = MagicMock(spec=[
        "read_memory", "write_memory", "client", "memory_policy",
    ])
    mock.memory_policy = None
    mock.client = MagicMock(spec=[
        "host", "password", "list_drives",
        "get_config_category", "pause", "resume",
    ])
    mock.client.host = "127.0.0.1"
    mock.client.password = None
    mock.client.list_drives.return_value = {"drives": []}
    # get_reu_config consults get_config_category(CAT_CART) and pulls the
    # _ITEM_REU_ENABLED + _ITEM_REU_SIZE keys.  Reproduce the shape.
    inner = {
        "RAM Expansion Unit": "Enabled" if reu_enabled else "Disabled",
        "REU Size": reu_size_str,
    }
    from c64_test_harness.backends.ultimate64_helpers import CAT_CART
    mock.client.get_config_category.return_value = {CAT_CART: inner}
    return mock


class TestExtractReuU64:
    def test_disabled_returns_empty(self) -> None:
        mock = _make_u64_reu_mock(reu_enabled=False)
        mock.read_memory.return_value = bytes(65536)
        snap = extract_snapshot(mock, include_reu=True, include_registers=False)
        assert snap.reu_size_bytes == 0
        assert snap.reu_contents == b""

    def test_include_reu_false_skips_probe(self) -> None:
        """When include_reu=False, no REU work happens — get_config not called."""
        mock = _make_u64_reu_mock(reu_enabled=True)
        mock.read_memory.return_value = bytes(65536)
        extract_snapshot(mock, include_registers=False)  # default include_reu=False
        mock.client.get_config_category.assert_not_called()
        mock.client.pause.assert_not_called()

    def test_staging_window_stash_and_restore(self) -> None:
        """The staging window's original bytes are stashed and restored."""
        size_bytes = 128 * 1024
        num_banks = size_bytes // _REU_STAGING_LEN  # 4 banks

        # Simulated REU contents — what the extract loop should produce.
        reu_payload = _pattern_reu(size_bytes)

        # Pre-existing staging-window contents (the application's data).
        original_window = bytes(
            (i ^ 0xAB) & 0xFF for i in range(_REU_STAGING_LEN)
        )

        # Build a read_memory mock that returns:
        #   - full 64 KB RAM with the original window bytes at $0800–$87FF
        #     for the first call (extract_snapshot's leading read)
        #   - the original window for the next call (stash)
        #   - then bank-N REU bytes for the loop reads
        full_ram = bytearray(65536)
        full_ram[_REU_STAGING_ADDR : _REU_STAGING_ADDR + _REU_STAGING_LEN] = (
            original_window
        )

        call_state = {"bank": -1, "ram_returned": False, "stashed": False}

        def _read(addr: int, length: int) -> bytes:
            if addr == 0x0000 and length == 65536:
                return bytes(full_ram)
            if addr == _REU_STAGING_ADDR and length == _REU_STAGING_LEN:
                if not call_state["stashed"]:
                    call_state["stashed"] = True
                    return original_window
                # subsequent reads are the bank-N REU bytes
                call_state["bank"] += 1
                b = call_state["bank"]
                return reu_payload[
                    b * _REU_STAGING_LEN : (b + 1) * _REU_STAGING_LEN
                ]
            raise AssertionError(f"unexpected read_memory({addr:#x}, {length})")

        mock = _make_u64_reu_mock(reu_enabled=True, reu_size_str="128 KB")
        mock.read_memory.side_effect = _read

        # Capture every write_memory call.
        writes: list[tuple[int, bytes, str | None]] = []

        def _write(addr: int, data, *, override=None):
            writes.append((addr, bytes(data), override))

        mock.write_memory.side_effect = _write

        snap = extract_snapshot(mock, include_reu=True, include_registers=False)

        # REU contents captured correctly.
        assert snap.reu_size_bytes == size_bytes
        assert snap.reu_contents == reu_payload

        # CPU must be paused for the duration.
        mock.client.pause.assert_called_once()
        mock.client.resume.assert_called_once()

        # The final write must be the staging-window restore (always-last).
        restore_writes = [
            (a, d, o) for a, d, o in writes if a == _REU_STAGING_ADDR
        ]
        assert restore_writes, "staging window never restored"
        last_addr, last_data, last_override = restore_writes[-1]
        assert last_data == original_window
        assert last_override == "reu-snapshot-staging"

    def test_per_bank_register_writes(self) -> None:
        """Each bank loop iteration writes $DF02..$DF0A + the $DF01 command."""
        mock = _make_u64_reu_mock(reu_enabled=True, reu_size_str="128 KB")
        mock.read_memory.return_value = bytes(_REU_STAGING_LEN)

        writes: list[tuple[int, bytes, str | None]] = []
        mock.write_memory.side_effect = lambda a, d, *, override=None: writes.append(
            (a, bytes(d), override)
        )

        # First read is the full RAM image; subsequent reads need to be window-sized.
        def _read(addr: int, length: int) -> bytes:
            if addr == 0x0000 and length == 65536:
                return bytes(65536)
            return bytes(length)

        mock.read_memory.side_effect = _read

        extract_snapshot(mock, include_reu=True, include_registers=False)

        # We expect 4 banks (128 KB / 32 KB), so 4 setup writes at $DF02 and
        # 4 command writes at $DF01.
        df02_writes = [(a, d, o) for a, d, o in writes if a == 0xDF02]
        df01_writes = [(a, d, o) for a, d, o in writes if a == 0xDF01]
        assert len(df02_writes) == 4
        assert len(df01_writes) == 4

        # All REU-staging writes must carry the override.
        for a, d, o in df02_writes + df01_writes:
            assert o == "reu-snapshot-staging", f"write at ${a:04X} missed override"

        # Decode the per-bank setup: REU offset increments by $8000 each loop.
        for bank, (_a, data, _o) in enumerate(df02_writes):
            assert data[0] == _REU_STAGING_ADDR & 0xFF       # DF02
            assert data[1] == (_REU_STAGING_ADDR >> 8) & 0xFF  # DF03
            reu_off = data[2] | (data[3] << 8) | (data[4] << 16)
            assert reu_off == bank * _REU_STAGING_LEN
            length = data[5] | (data[6] << 8)
            assert length == _REU_STAGING_LEN
            assert data[7] == 0  # DF09 int mask
            assert data[8] == 0  # DF0A addr control

        # Command byte is the REU→C64 execute-now command.
        for _a, data, _o in df01_writes:
            assert data == bytes([_REU_CMD_REU_TO_C64])

    def test_staging_restored_even_on_failure(self) -> None:
        """A failure mid-loop must still restore the staging window."""
        size_bytes = 256 * 1024  # 8 banks

        original_window = bytes(_REU_STAGING_LEN)

        call_state = {"reads_since_stash": 0, "stashed": False}

        def _read(addr: int, length: int) -> bytes:
            if addr == 0x0000 and length == 65536:
                return bytes(65536)
            if addr == _REU_STAGING_ADDR and length == _REU_STAGING_LEN:
                if not call_state["stashed"]:
                    call_state["stashed"] = True
                    return original_window
                call_state["reads_since_stash"] += 1
                if call_state["reads_since_stash"] == 3:
                    raise RuntimeError("simulated mid-loop transport failure")
                return bytes(length)
            raise AssertionError(f"unexpected read({addr:#x}, {length})")

        mock = _make_u64_reu_mock(reu_enabled=True, reu_size_str="256 KB")
        mock.read_memory.side_effect = _read

        writes: list[tuple[int, bytes, str | None]] = []
        mock.write_memory.side_effect = lambda a, d, *, override=None: writes.append(
            (a, bytes(d), override)
        )

        with pytest.raises(RuntimeError, match="simulated"):
            extract_snapshot(mock, include_reu=True, include_registers=False)

        # Staging window must be restored despite the abort.
        window_restores = [
            (a, d, o) for a, d, o in writes
            if a == _REU_STAGING_ADDR and len(d) == _REU_STAGING_LEN
        ]
        assert window_restores, "staging window never restored on error path"
        last_a, last_d, last_o = window_restores[-1]
        assert last_d == original_window
        assert last_o == "reu-snapshot-staging"

        # Resume() must still be called.
        mock.client.resume.assert_called_once()


# ---------------------------------------------------------------------------
# Mocked restore — SocketDMA REUWRITE chunking + set_reu
# ---------------------------------------------------------------------------


class TestRestoreReuU64:
    def test_socket_dma_reu_write_called_with_chunks(self) -> None:
        """A 256 KB restore should issue 4 64 KB chunks at offsets 0, 64K, ..."""
        size_bytes = 256 * 1024
        contents = _pattern_reu(size_bytes)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
            reu_size_bytes=size_bytes, reu_contents=contents,
        )

        mock = MagicMock(spec=["client", "write_memory", "memory_policy"])
        mock.memory_policy = None
        mock.client = MagicMock(spec=[
            "host", "password", "mount_disk",
            "set_config_items", "get_config_category",
        ])
        mock.client.host = "192.0.2.5"
        mock.client.password = None

        # Capture SocketDMAClient.reu_write calls.
        with patch(
            "c64_test_harness.backends.u64_socket_dma.SocketDMAClient",
        ) as DMA:
            instance = DMA.return_value.__enter__.return_value
            restore_snapshot(mock, snap)

            # set_reu(enabled=True, size=256*1024) → set_config_items called.
            mock.client.set_config_items.assert_called()

            # SocketDMAClient instantiated with the client's host/password.
            DMA.assert_called_once()
            kwargs = DMA.call_args.kwargs
            assert kwargs.get("host") == "192.0.2.5"
            assert kwargs.get("port") == 64

            # 4 chunks of 64 KB at offsets 0, 65536, 131072, 196608.
            calls = instance.reu_write.call_args_list
            assert len(calls) == 4
            for i, c in enumerate(calls):
                offset = c.args[0] if c.args else c.kwargs["offset"]
                data = c.args[1] if len(c.args) > 1 else c.kwargs["data"]
                assert offset == i * 65536
                assert data == contents[i * 65536 : (i + 1) * 65536]

    def test_set_reu_failure_does_not_abort_restore(self) -> None:
        """If set_reu raises, the restore still attempts the REU push.

        Best-effort principle from the Phase B drive code — the
        caller may have already configured the cartridge preset, and
        we shouldn't lose REU bytes because of a config failure.
        """
        contents = _pattern_reu(128 * 1024)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
            reu_size_bytes=128 * 1024, reu_contents=contents,
        )

        mock = MagicMock(spec=["client", "write_memory", "memory_policy"])
        mock.memory_policy = None
        mock.client = MagicMock(spec=[
            "host", "password", "set_config_items",
        ])
        mock.client.host = "1.2.3.4"
        mock.client.password = None
        mock.client.set_config_items.side_effect = RuntimeError("config wedged")

        with patch(
            "c64_test_harness.backends.u64_socket_dma.SocketDMAClient",
        ) as DMA:
            instance = DMA.return_value.__enter__.return_value
            restore_snapshot(mock, snap)
            assert instance.reu_write.called

    def test_no_reu_no_socket_dma(self) -> None:
        """An empty REU snapshot must not open a SocketDMA connection."""
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0)

        mock = MagicMock(spec=["client", "write_memory", "memory_policy"])
        mock.memory_policy = None
        mock.client = MagicMock(spec=["host", "password"])
        mock.client.host = "1.2.3.4"

        with patch(
            "c64_test_harness.backends.u64_socket_dma.SocketDMAClient",
        ) as DMA:
            restore_snapshot(mock, snap)
            DMA.assert_not_called()


# ---------------------------------------------------------------------------
# Backwards-compat — Phase A / Phase B keep working
# ---------------------------------------------------------------------------


class TestPhaseABcompat:
    def test_phase_a_snapshot_still_works(self) -> None:
        """Phase A only supplied ram + cpu_port — must still construct."""
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        assert snap.reu_size_bytes == 0
        assert snap.reu_contents == b""
        # And round-trip through to_vsf/from_vsf cleanly.
        restored = Snapshot.from_vsf(snap.to_vsf())
        assert restored.reu_size_bytes == 0
        assert restored.reu_contents == b""

    def test_phase_b_bundle_still_works(self, tmp_path: Path) -> None:
        d = DriveState(
            device=8, drive_type="1541", image=b"x" * 100, image_format="d64",
        )
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0, drives=(d,),
        )
        out = snap.to_bundle(tmp_path / "snap")
        loaded = Snapshot.from_bundle(out)
        assert loaded.drives == (d,)
        assert loaded.reu_size_bytes == 0
        assert loaded.reu_contents == b""


# ---------------------------------------------------------------------------
# TODO: live VICE cross-check
# ---------------------------------------------------------------------------
#
# A future test should spawn x64sc with REU enabled, restore a Snapshot
# carrying recognisable REU bytes via undump_snapshot, then DMA-read the
# REU bytes back through the staging-window path to confirm VICE
# actually loaded what we emitted.  Deferred — the offline round-trip
# (Snapshot → to_vsf → from_vsf → Snapshot) already proves the emitter
# is self-consistent, and the staging-loop logic is mocked above.
