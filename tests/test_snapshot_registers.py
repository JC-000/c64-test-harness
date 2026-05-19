"""Tests for the I/O register snapshot extension (Phase B continuation).

Covers:

* :class:`Snapshot` validation of the four register fields
  (``cia1_regs``, ``cia2_regs``, ``vic_regs``, ``sid_regs``) — empty
  bytes accepted, exact length required otherwise.
* The host-side SID shadow maintained by
  :class:`~c64_test_harness.backends.ultimate64.Ultimate64Transport`:
  every byte written into $D400-$D41F via ``write_memory`` is recorded;
  writes that overlap the SID window are partially captured; writes
  outside the window leave the shadow untouched.
* :func:`extract_snapshot` ``include_registers=True`` captures all four
  banks; ``include_registers=False`` leaves them empty.
* :func:`extract_snapshot` SID asymmetry: U64-shaped transport (with
  ``sid_shadow``) sources from the shadow, VICE-shaped (no shadow)
  sources from ``read_memory($D400, 32)``.
* :func:`restore_snapshot` writes each non-empty bank back through
  ``write_memory(..., override="snapshot-restore")``; empty fields skip
  the corresponding write.
* ``.vsf`` round-trip: a snapshot with non-default register state
  passes through ``to_vsf()`` / ``from_vsf()`` byte-identically for the
  register fields.

All tests are offline.  Live VICE / U64 verification is covered by the
existing live-test suites once the maintainer wires this branch into
them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from c64_test_harness import Snapshot, extract_snapshot, restore_snapshot
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.snapshot import (
    _CIA_REGS_LEN,
    _REGISTER_MODULE_SLICES,
    _SID_REGS_LEN,
    _VIC_REGS_LEN,
    _iter_modules,
    _patch_module_prefix,
)


# ---------------------------------------------------------------------------
# 1. Snapshot field validation
# ---------------------------------------------------------------------------


def _ram_blank() -> bytes:
    return bytes(65536)


class TestRegisterFieldValidation:
    def test_default_empty_bytes_accepted(self) -> None:
        s = Snapshot(ram=_ram_blank(), cpu_port_data=0x37, cpu_port_dir=0x2F)
        assert s.cia1_regs == b""
        assert s.cia2_regs == b""
        assert s.vic_regs == b""
        assert s.sid_regs == b""

    def test_exact_length_accepted(self) -> None:
        s = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cia1_regs=bytes(_CIA_REGS_LEN),
            cia2_regs=bytes(_CIA_REGS_LEN),
            vic_regs=bytes(_VIC_REGS_LEN),
            sid_regs=bytes(_SID_REGS_LEN),
        )
        assert len(s.cia1_regs) == _CIA_REGS_LEN
        assert len(s.cia2_regs) == _CIA_REGS_LEN
        assert len(s.vic_regs) == _VIC_REGS_LEN
        assert len(s.sid_regs) == _SID_REGS_LEN

    @pytest.mark.parametrize(
        "field,bad_len",
        [
            ("cia1_regs", 15),
            ("cia1_regs", 17),
            ("cia2_regs", 1),
            ("vic_regs", 46),
            ("vic_regs", 48),
            ("sid_regs", 31),
            ("sid_regs", 33),
        ],
    )
    def test_wrong_length_rejected(self, field: str, bad_len: int) -> None:
        kwargs = {field: bytes(bad_len)}
        with pytest.raises(ValueError, match=field):
            Snapshot(ram=_ram_blank(), cpu_port_data=0, cpu_port_dir=0, **kwargs)

    def test_non_bytes_type_rejected(self) -> None:
        with pytest.raises(TypeError, match="cia1_regs"):
            Snapshot(
                ram=_ram_blank(),
                cpu_port_data=0,
                cpu_port_dir=0,
                cia1_regs="not bytes",  # type: ignore[arg-type]
            )

    def test_bytearray_coerced_to_bytes(self) -> None:
        s = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0,
            cpu_port_dir=0,
            sid_regs=bytearray(_SID_REGS_LEN),
        )
        assert isinstance(s.sid_regs, bytes)


# ---------------------------------------------------------------------------
# 2. Ultimate64Transport SID shadow
# ---------------------------------------------------------------------------


@pytest.fixture
def u64_client() -> MagicMock:
    c = MagicMock()
    c.read_mem.return_value = b""
    return c


@pytest.fixture
def u64_transport(u64_client: MagicMock) -> Ultimate64Transport:
    return Ultimate64Transport(host="192.0.2.1", client=u64_client)


class TestSidShadow:
    def test_initial_shadow_is_all_zero(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        assert u64_transport.sid_shadow == bytes(32)
        assert len(u64_transport.sid_shadow) == 32

    def test_shadow_is_read_only_property(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        # The returned object is bytes — mutating it doesn't affect the
        # transport's internal state.
        snap1 = u64_transport.sid_shadow
        assert isinstance(snap1, bytes)
        with pytest.raises((AttributeError, TypeError)):
            u64_transport.sid_shadow = bytes(32)  # type: ignore[misc]

    def test_full_sid_write_populates_shadow(
        self, u64_transport: Ultimate64Transport, u64_client: MagicMock
    ) -> None:
        pattern = bytes(range(0x80, 0xA0))  # 32 bytes
        u64_transport.write_memory(0xD400, pattern)
        assert u64_transport.sid_shadow == pattern
        u64_client.write_mem.assert_called_once_with(0xD400, pattern)

    def test_partial_sid_write(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        # Write 4 bytes starting at $D410 — only those 4 bytes change.
        u64_transport.write_memory(0xD410, b"\x11\x22\x33\x44")
        expected = bytearray(32)
        expected[0x10:0x14] = b"\x11\x22\x33\x44"
        assert u64_transport.sid_shadow == bytes(expected)

    def test_overlap_before_sid(
        self, u64_transport: Ultimate64Transport, u64_client: MagicMock
    ) -> None:
        """Write starting at $D3FE, length 4 — only the $D400-$D401
        portion lands in the shadow."""
        u64_transport.write_memory(0xD3FE, b"\xaa\xbb\xcc\xdd")
        expected = bytearray(32)
        expected[0] = 0xCC  # $D400 byte
        expected[1] = 0xDD  # $D401 byte
        assert u64_transport.sid_shadow == bytes(expected)
        # The wire still gets the full 4-byte write.
        u64_client.write_mem.assert_called_once_with(0xD3FE, b"\xaa\xbb\xcc\xdd")

    def test_overlap_after_sid(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        """Write starting at $D41E, length 4 — only the $D41E-$D41F
        portion lands in the shadow."""
        u64_transport.write_memory(0xD41E, b"\xaa\xbb\xcc\xdd")
        expected = bytearray(32)
        expected[0x1E] = 0xAA
        expected[0x1F] = 0xBB
        assert u64_transport.sid_shadow == bytes(expected)

    def test_write_outside_sid_window_does_not_pollute(
        self, u64_transport: Ultimate64Transport, u64_client: MagicMock
    ) -> None:
        u64_transport.write_memory(0x0400, b"\xff" * 100)
        u64_transport.write_memory(0xD500, b"\xff" * 16)
        u64_transport.write_memory(0xD3FD, b"\xff")
        # $D420 is just past the SID window.
        u64_transport.write_memory(0xD420, b"\xff" * 4)
        assert u64_transport.sid_shadow == bytes(32)

    def test_shadow_accepts_list_data(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        u64_transport.write_memory(0xD400, [0xAA, 0xBB, 0xCC])
        expected = bytearray(32)
        expected[0:3] = b"\xaa\xbb\xcc"
        assert u64_transport.sid_shadow == bytes(expected)

    def test_shadow_accepts_bytearray_data(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        u64_transport.write_memory(0xD400, bytearray(b"\x10\x20"))
        expected = bytearray(32)
        expected[0] = 0x10
        expected[1] = 0x20
        assert u64_transport.sid_shadow == bytes(expected)

    def test_repeated_writes_overwrite(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        u64_transport.write_memory(0xD400, b"\xff" * 32)
        u64_transport.write_memory(0xD400, b"\x00")
        expected = bytearray(b"\xff" * 32)
        expected[0] = 0x00
        assert u64_transport.sid_shadow == bytes(expected)

    def test_reset_sid_shadow_clears(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        u64_transport.write_memory(0xD400, b"\xff" * 32)
        assert u64_transport.sid_shadow != bytes(32)
        u64_transport.reset_sid_shadow()
        assert u64_transport.sid_shadow == bytes(32)

    def test_empty_write_no_shadow_update(
        self, u64_transport: Ultimate64Transport
    ) -> None:
        u64_transport.write_memory(0xD400, b"")
        u64_transport.write_memory(0xD400, [])
        assert u64_transport.sid_shadow == bytes(32)


# ---------------------------------------------------------------------------
# 3. extract_snapshot — register-bank capture
# ---------------------------------------------------------------------------


class _StubTransport:
    """Minimal C64Transport stub for extract / restore tests.

    Records every ``read_memory`` and ``write_memory`` call.  Returns
    address-specific patterns from ``read_memory``: each region has a
    distinctive prefix so the test can assert which window each byte
    came from.
    """

    def __init__(self, *, sid_shadow: bytes | None = None) -> None:
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, bytes, str | None]] = []
        if sid_shadow is not None:
            self.sid_shadow = sid_shadow

    def read_memory(self, addr: int, length: int) -> bytes:
        self.reads.append((addr, length))
        # Make each region's bytes recognisable so the test can verify
        # the extract picked them up.
        if addr == 0x0000 and length == 65536:
            return bytes(65536)
        if addr == 0xDC00 and length == _CIA_REGS_LEN:
            return bytes(range(0x10, 0x10 + _CIA_REGS_LEN))
        if addr == 0xDD00 and length == _CIA_REGS_LEN:
            return bytes(range(0x20, 0x20 + _CIA_REGS_LEN))
        if addr == 0xD000 and length == _VIC_REGS_LEN:
            return bytes(range(0x30, 0x30 + _VIC_REGS_LEN))
        if addr == 0xD400 and length == _SID_REGS_LEN:
            return bytes(range(0x40, 0x40 + _SID_REGS_LEN))
        return bytes(length)

    def write_memory(self, addr: int, data, *, override: str | None = None) -> None:
        self.writes.append((addr, bytes(data), override))


class TestExtractRegisters:
    def test_default_captures_all_four_banks(self) -> None:
        t = _StubTransport()
        snap = extract_snapshot(t)
        assert snap.cia1_regs == bytes(range(0x10, 0x10 + _CIA_REGS_LEN))
        assert snap.cia2_regs == bytes(range(0x20, 0x20 + _CIA_REGS_LEN))
        assert snap.vic_regs == bytes(range(0x30, 0x30 + _VIC_REGS_LEN))
        assert snap.sid_regs == bytes(range(0x40, 0x40 + _SID_REGS_LEN))

    def test_include_registers_false_skips_banks(self) -> None:
        t = _StubTransport()
        snap = extract_snapshot(t, include_registers=False)
        assert snap.cia1_regs == b""
        assert snap.cia2_regs == b""
        assert snap.vic_regs == b""
        assert snap.sid_regs == b""
        # And the four register-window reads must not have happened.
        addrs = {a for a, _len in t.reads}
        assert 0xDC00 not in addrs
        assert 0xDD00 not in addrs
        assert 0xD000 not in addrs
        assert 0xD400 not in addrs

    def test_sid_uses_shadow_when_available(self) -> None:
        shadow_pattern = bytes(range(0xC0, 0xC0 + _SID_REGS_LEN))
        t = _StubTransport(sid_shadow=shadow_pattern)
        snap = extract_snapshot(t)
        # SID came from shadow, not from read_memory($D400, 32).
        assert snap.sid_regs == shadow_pattern
        addrs = {a for a, _len in t.reads}
        assert 0xD400 not in addrs

    def test_vice_path_uses_read_memory_for_sid(self) -> None:
        # No sid_shadow attribute on the transport — extract falls back
        # to read_memory.  This is the VICE path.
        t = _StubTransport()
        assert not hasattr(t, "sid_shadow")
        snap = extract_snapshot(t)
        # The read came from $D400.
        assert (0xD400, _SID_REGS_LEN) in t.reads
        assert snap.sid_regs == bytes(range(0x40, 0x40 + _SID_REGS_LEN))

    def test_u64_transport_extract_sources_from_shadow(
        self, u64_client: MagicMock
    ) -> None:
        """End-to-end via a real Ultimate64Transport: writes through
        the transport populate the shadow, then extract_snapshot picks
        up those values without reading $D400 over the wire."""
        # Make read_memory return *correct* lengths so the rest of
        # extract works.
        def _read(addr: int, length: int) -> bytes:
            return bytes(length)
        u64_client.read_mem.side_effect = _read
        t = Ultimate64Transport(host="h", client=u64_client)
        sid_pattern = bytes(range(0x80, 0xA0))
        t.write_memory(0xD400, sid_pattern)
        # Reset the call log so we can prove $D400 is NOT read again.
        u64_client.read_mem.reset_mock()
        u64_client.read_mem.side_effect = _read

        snap = extract_snapshot(t)
        assert snap.sid_regs == sid_pattern
        for call in u64_client.read_mem.call_args_list:
            assert call.args[0] != 0xD400, "should not read SID from wire"


# ---------------------------------------------------------------------------
# 4. restore_snapshot — register-bank write-back
# ---------------------------------------------------------------------------


class TestRestoreRegisters:
    def test_writes_each_non_empty_bank(self) -> None:
        cia1 = bytes(range(0x10, 0x10 + _CIA_REGS_LEN))
        cia2 = bytes(range(0x20, 0x20 + _CIA_REGS_LEN))
        vic = bytes(range(0x30, 0x30 + _VIC_REGS_LEN))
        sid = bytes(range(0x40, 0x40 + _SID_REGS_LEN))
        snap = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cia1_regs=cia1,
            cia2_regs=cia2,
            vic_regs=vic,
            sid_regs=sid,
        )
        t = _StubTransport()
        restore_snapshot(t, snap)

        writes_by_addr = {addr: (data, override) for addr, data, override in t.writes}
        assert writes_by_addr[0xDC00] == (cia1, "snapshot-restore")
        assert writes_by_addr[0xDD00] == (cia2, "snapshot-restore")
        assert writes_by_addr[0xD000] == (vic, "snapshot-restore")
        assert writes_by_addr[0xD400] == (sid, "snapshot-restore")

    def test_vic_written_after_other_io(self) -> None:
        """Order matters: VIC-II is written last so it latches a
        consistent neighbour state."""
        snap = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cia1_regs=bytes(_CIA_REGS_LEN),
            cia2_regs=bytes(_CIA_REGS_LEN),
            vic_regs=bytes(_VIC_REGS_LEN),
            sid_regs=bytes(_SID_REGS_LEN),
        )
        t = _StubTransport()
        restore_snapshot(t, snap)
        # Pull out only the I/O register writes (filter the bulk RAM
        # write and the cpu-port writes).
        io_addrs = [
            addr for addr, _data, _ov in t.writes
            if addr in (0xDC00, 0xDD00, 0xD000, 0xD400)
        ]
        # VIC-II must come last among the I/O register writes.
        assert io_addrs[-1] == 0xD000

    def test_empty_banks_skip_writes(self) -> None:
        snap = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            # All four banks left empty.
        )
        t = _StubTransport()
        restore_snapshot(t, snap)
        addrs = {a for a, _d, _ov in t.writes}
        assert 0xDC00 not in addrs
        assert 0xDD00 not in addrs
        assert 0xD000 not in addrs
        assert 0xD400 not in addrs


# ---------------------------------------------------------------------------
# 5. .vsf round-trip survives the four register banks
# ---------------------------------------------------------------------------


class TestVsfRoundtrip:
    def test_register_state_survives_to_vsf_from_vsf(self) -> None:
        cia1 = bytes(range(0xE0, 0xE0 + _CIA_REGS_LEN))
        cia2 = bytes(range(0xF0, 0xF0 + _CIA_REGS_LEN))
        # VIC-II first byte (= $D000 sprite0X) must survive verbatim.
        vic = bytes(range(0x01, 0x01 + _VIC_REGS_LEN))
        sid = bytes(range(0xA0, 0xA0 + _SID_REGS_LEN))
        snap = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cia1_regs=cia1,
            cia2_regs=cia2,
            vic_regs=vic,
            sid_regs=sid,
        )
        blob = snap.to_vsf()
        restored = Snapshot.from_vsf(blob)
        assert restored.cia1_regs == cia1
        assert restored.cia2_regs == cia2
        assert restored.vic_regs == vic
        assert restored.sid_regs == sid

    def test_empty_banks_omit_patch_but_load_template_values(self) -> None:
        """When the source snapshot has all four banks empty,
        to_vsf() leaves the template's CIA1/CIA2/SID/VIC-II modules
        untouched.  from_vsf() then surfaces whatever the template
        had — so the round-trip captures the template state."""
        snap = Snapshot(
            ram=_ram_blank(), cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        blob = snap.to_vsf()
        restored = Snapshot.from_vsf(blob)
        # Each restored field is the template's slice — non-empty, but
        # equal to what to_vsf preserved verbatim from the template.
        assert len(restored.cia1_regs) == _CIA_REGS_LEN
        assert len(restored.cia2_regs) == _CIA_REGS_LEN
        assert len(restored.vic_regs) == _VIC_REGS_LEN
        assert len(restored.sid_regs) == _SID_REGS_LEN

    def test_patch_only_touches_named_module(self) -> None:
        """``_patch_module_prefix`` overwrites only the requested slice;
        every other byte in the .vsf is preserved."""
        from c64_test_harness.snapshot import _load_template
        tmpl = _load_template()
        payload = bytes(range(0xC0, 0xC0 + _SID_REGS_LEN))
        out = _patch_module_prefix(tmpl, b"SID", 4, payload)
        assert len(out) == len(tmpl)
        # The patched slice matches the payload.
        for name, _vmaj, _vmin, body_start, body_len in _iter_modules(out):
            if name == b"SID":
                assert out[body_start + 4 : body_start + 4 + _SID_REGS_LEN] == payload
                # And the 4-byte prefix is unchanged.
                assert out[body_start : body_start + 4] == tmpl[body_start : body_start + 4]
                break
        else:
            pytest.fail("no SID module after patch")

    def test_patch_missing_module_raises(self) -> None:
        from c64_test_harness.snapshot import _load_template, SnapshotFormatError
        tmpl = _load_template()
        with pytest.raises(SnapshotFormatError, match="NOPE"):
            _patch_module_prefix(tmpl, b"NOPE", 0, b"\x00" * 16)

    def test_patch_oversize_payload_raises(self) -> None:
        from c64_test_harness.snapshot import _load_template, SnapshotFormatError
        tmpl = _load_template()
        # SID body is 36 bytes; patching 64 bytes at offset 4 won't fit.
        with pytest.raises(SnapshotFormatError, match="cannot patch"):
            _patch_module_prefix(tmpl, b"SID", 4, b"\x00" * 64)


# ---------------------------------------------------------------------------
# 6. Bundle codec carries register state
# ---------------------------------------------------------------------------


class TestBundleRegisters:
    def test_bundle_roundtrips_register_state(self, tmp_path: Path) -> None:
        cia1 = bytes(range(0xE0, 0xE0 + _CIA_REGS_LEN))
        cia2 = bytes(range(0xF0, 0xF0 + _CIA_REGS_LEN))
        vic = bytes(range(0x01, 0x01 + _VIC_REGS_LEN))
        sid = bytes(range(0xA0, 0xA0 + _SID_REGS_LEN))
        snap = Snapshot(
            ram=_ram_blank(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cia1_regs=cia1,
            cia2_regs=cia2,
            vic_regs=vic,
            sid_regs=sid,
        )
        bundle = snap.to_bundle(tmp_path / "snap")
        manifest = json.loads((bundle / "manifest.json").read_text())
        # Manifest carries hex strings.
        assert manifest["cia1_regs"] == cia1.hex()
        assert manifest["cia2_regs"] == cia2.hex()
        assert manifest["vic_regs"] == vic.hex()
        assert manifest["sid_regs"] == sid.hex()
        # And from_bundle round-trips them.
        restored = Snapshot.from_bundle(bundle)
        assert restored.cia1_regs == cia1
        assert restored.cia2_regs == cia2
        assert restored.vic_regs == vic
        assert restored.sid_regs == sid

    def test_bundle_omits_empty_banks(self, tmp_path: Path) -> None:
        snap = Snapshot(
            ram=_ram_blank(), cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        bundle = snap.to_bundle(tmp_path / "snap")
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert "cia1_regs" not in manifest
        assert "cia2_regs" not in manifest
        assert "vic_regs" not in manifest
        assert "sid_regs" not in manifest

    def test_bundle_rejects_non_hex_manifest_value(self, tmp_path: Path) -> None:
        snap = Snapshot(
            ram=_ram_blank(), cpu_port_data=0x37, cpu_port_dir=0x2F,
        )
        bundle = snap.to_bundle(tmp_path / "snap")
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["sid_regs"] = "zz" * 32  # not hex
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        from c64_test_harness.snapshot import SnapshotFormatError
        with pytest.raises(SnapshotFormatError, match="not valid hex"):
            Snapshot.from_bundle(bundle)


# ---------------------------------------------------------------------------
# 7. Slice metadata sanity (so the test suite catches accidental edits)
# ---------------------------------------------------------------------------


class TestSliceMetadata:
    def test_register_module_slices_complete(self) -> None:
        """The slice table covers exactly the four I/O register banks."""
        names = {n for n, _o, _l in _REGISTER_MODULE_SLICES}
        assert names == {b"CIA1", b"CIA2", b"SID", b"VIC-II"}

    def test_slice_lengths_match_constants(self) -> None:
        by_name = {n: (o, l) for n, o, l in _REGISTER_MODULE_SLICES}
        assert by_name[b"CIA1"][1] == _CIA_REGS_LEN
        assert by_name[b"CIA2"][1] == _CIA_REGS_LEN
        assert by_name[b"VIC-II"][1] == _VIC_REGS_LEN
        assert by_name[b"SID"][1] == _SID_REGS_LEN

    def test_slice_offsets_documented(self) -> None:
        """Spot-check the empirically-verified offsets:
        CIA1/CIA2 start at 0, SID at 4, VIC-II at 1."""
        by_name = {n: (o, l) for n, o, l in _REGISTER_MODULE_SLICES}
        assert by_name[b"CIA1"][0] == 0
        assert by_name[b"CIA2"][0] == 0
        assert by_name[b"SID"][0] == 4
        assert by_name[b"VIC-II"][0] == 1
