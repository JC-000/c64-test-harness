"""Tests for the cross-backend Snapshot module (Phase A).

Covers four layers:

1. Offline VSF codec round-trip — :meth:`Snapshot.to_vsf` →
   :meth:`Snapshot.from_vsf` preserves RAM and CPU port.
2. Cross-check with VICE (RAM → VSF → VICE undump) — proves our emitter
   produces a snapshot VICE 3.x accepts.
3. Reverse cross-check (VICE dump → from_vsf) — proves our parser
   handles real VICE-emitted snapshots.
4. ``restore_snapshot`` mocking — verifies the U64 write path uses the
   ``override="snapshot-restore"`` kwarg and that a MemoryPolicy with
   reserved regions does not block the restore.

TODO(phase-A-followup): add a live U64 round-trip test that takes a
snapshot from VICE, restores into the Ultimate 64 via REST, and
verifies the RAM bytes survive.  Requires hardware so deferred until
the U64 fixture is available.
"""

from __future__ import annotations

import logging
import shutil
import struct
import time
from pathlib import Path

import pytest

from c64_test_harness import (
    MemoryPolicy,
    MemoryRegion,
    Snapshot,
    SnapshotFormatError,
    UnknownPolicy,
    extract_snapshot,
    restore_snapshot,
)
from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator

# Reach into module internals for the format-verification probes.
from c64_test_harness.snapshot import (
    _C64MEM_BODY_LEN,
    _C64MEM_MODULE_NAME,
    _VSF_FILE_HEADER_LEN,
    _VSF_FORMAT_MAJOR,
    _VSF_MAGIC,
    _build_file_header,
    _iter_modules,
    _load_template,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pattern_ram() -> bytes:
    """64 KB of recognisable, address-keyed bytes with a 16-byte marker."""
    buf = bytearray((i ^ 0x5A) & 0xFF for i in range(65536))
    buf[0xC000:0xC010] = bytes(range(0x10, 0x20))
    return bytes(buf)


def _connect_vice(port: int, proc: ViceProcess, timeout: float = 30.0) -> BinaryViceTransport:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc._proc is not None and proc._proc.poll() is not None:
            raise RuntimeError("VICE process exited during connect")
        try:
            return BinaryViceTransport(port=port)
        except Exception as exc:
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"could not connect to VICE: {last_err}")


@pytest.fixture(scope="module")
def vice_transport():
    """Module-scoped VICE for the cross-check tests.

    Reused across the two VICE-touching tests to keep total runtime low.
    """
    if shutil.which("x64sc") is None:
        pytest.skip("x64sc not found on PATH")

    allocator = PortAllocator(port_range_start=6611, port_range_end=6631)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(port=port, warp=True, sound=False)
    with ViceProcess(config) as vice:
        transport = _connect_vice(port, proc=vice)
        try:
            # Let VICE finish coming up so RAM is in a steady state.
            time.sleep(3.0)
            yield transport
        finally:
            transport.close()
            allocator.release(port)


class _MockTransport:
    """Pure-Python transport stub for restore_snapshot assertions.

    Records every write_memory call so the test can verify the bulk
    write went through with the ``override="snapshot-restore"`` kwarg.
    """

    def __init__(self, memory_policy: MemoryPolicy | None = None) -> None:
        self.memory_policy = memory_policy or MemoryPolicy.permissive()
        self.writes: list[tuple[int, bytes, str | None]] = []

    def write_memory(self, addr: int, data, *, override: str | None = None) -> None:
        # Honour the policy so MemoryPolicyError can be raised when the
        # caller doesn't pass override — matches real backend behaviour.
        if not self.memory_policy.is_permissive():
            self.memory_policy.check_write(addr, len(data), override=override)
        self.writes.append((addr, bytes(data), override))

    # extract_snapshot is not exercised by these tests; satisfy the protocol.
    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(length)


# ---------------------------------------------------------------------------
# 1. Offline VSF round-trip
# ---------------------------------------------------------------------------


class TestVsfCodec:
    def test_roundtrip_preserves_ram_and_cpu_port(self) -> None:
        ram = _make_pattern_ram()
        snap = Snapshot(
            ram=ram,
            cpu_port_data=0x35,
            cpu_port_dir=0x2F,
            exrom=1,
            game=1,
        )
        blob = snap.to_vsf()
        restored = Snapshot.from_vsf(blob)
        assert restored.ram == ram
        assert restored.cpu_port_data == 0x35
        assert restored.cpu_port_dir == 0x2F
        assert restored.exrom == 1
        assert restored.game == 1

    def test_emit_carries_valid_file_header(self) -> None:
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F)
        blob = snap.to_vsf()
        assert blob[:19] == _VSF_MAGIC
        assert blob[0x13] == _VSF_FORMAT_MAJOR

    def test_emit_replaces_only_c64mem(self) -> None:
        """Emitting from the bundled template touches only the C64MEM module."""
        template = _load_template()
        snap = Snapshot(ram=b"\x42" * 65536, cpu_port_data=0x37, cpu_port_dir=0x2F)
        blob = snap.to_vsf()

        tmpl_modules = {name: (start, length) for name, _vmaj, _vmin, start, length
                        in _iter_modules(template)}
        emit_modules = {name: (start, length) for name, _vmaj, _vmin, start, length
                        in _iter_modules(blob)}
        assert set(tmpl_modules.keys()) == set(emit_modules.keys()), \
            "emit dropped or added a module compared to template"

        # All non-C64MEM modules must be byte-identical (including header)
        for name, (start, length) in tmpl_modules.items():
            if name == _C64MEM_MODULE_NAME:
                continue
            tmpl_bytes = template[start - 22 : start + length]
            emit_start, emit_len = emit_modules[name]
            emit_bytes = blob[emit_start - 22 : emit_start + emit_len]
            assert tmpl_bytes == emit_bytes, f"non-C64MEM module {name!r} changed"

    def test_emit_c64mem_body_layout(self) -> None:
        """Spot-check the byte layout of the emitted C64MEM body."""
        ram = _make_pattern_ram()
        snap = Snapshot(ram=ram, cpu_port_data=0xAB, cpu_port_dir=0xCD,
                        exrom=1, game=1)
        blob = snap.to_vsf()
        for name, vmaj, vmin, start, length in _iter_modules(blob):
            if name == _C64MEM_MODULE_NAME:
                assert vmaj == 0
                assert vmin == 1
                assert length == _C64MEM_BODY_LEN
                body = blob[start : start + length]
                assert body[0] == 0xAB  # cpu_port_data
                assert body[1] == 0xCD  # cpu_port_dir
                assert body[2] == 1     # exrom
                assert body[3] == 1     # game
                assert body[4 : 4 + 65536] == ram
                assert body[4 + 65536 :] == b"\x00" * 15
                break
        else:
            pytest.fail("no C64MEM module in emitted .vsf")

    def test_from_vsf_rejects_bad_magic(self) -> None:
        bogus = b"NOT_A_VICE_SNAPSHOT" + b"\x00" * 40
        with pytest.raises(SnapshotFormatError, match="magic"):
            Snapshot.from_vsf(bogus)

    def test_from_vsf_rejects_wrong_format_major(self) -> None:
        hdr = bytearray(_build_file_header())
        hdr[0x13] = 99  # bogus format major
        with pytest.raises(SnapshotFormatError, match="format major"):
            Snapshot.from_vsf(bytes(hdr) + b"\x00" * 100)

    def test_from_vsf_rejects_missing_c64mem(self) -> None:
        header = _build_file_header()
        # File header alone, no modules.
        with pytest.raises(SnapshotFormatError, match="no C64MEM"):
            Snapshot.from_vsf(header)

    def test_snapshot_rejects_wrong_ram_size(self) -> None:
        with pytest.raises(ValueError, match="65536"):
            Snapshot(ram=b"\x00" * 1024, cpu_port_data=0, cpu_port_dir=0)

    def test_snapshot_rejects_non_bytes_ram(self) -> None:
        with pytest.raises(TypeError):
            Snapshot(ram="not bytes", cpu_port_data=0, cpu_port_dir=0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. VICE consumes our emitted .vsf (emit cross-check)
# ---------------------------------------------------------------------------


def test_vice_accepts_our_emitted_vsf(vice_transport, tmp_path):
    """Emit a Snapshot to .vsf, load via undump_snapshot, confirm RAM.

    This is the format-verification step the maintainer required: if
    VICE undumps the file without error and the marker survives, our
    emitter is correct.
    """
    ram = _make_pattern_ram()
    snap = Snapshot(ram=ram, cpu_port_data=0x37, cpu_port_dir=0x2F)
    vsf_path = tmp_path / "emit.vsf"
    vsf_path.write_bytes(snap.to_vsf())

    # No "incompatible snapshot" error → emitter is well-formed.
    new_pc = vice_transport.undump_snapshot(str(vsf_path))
    assert isinstance(new_pc, int)
    time.sleep(0.2)

    # The 16-byte marker at $C000 should be visible after the restore.
    readback = vice_transport.read_memory(0xC000, 16)
    assert readback == bytes(range(0x10, 0x20))


# ---------------------------------------------------------------------------
# 3. We parse VICE-emitted .vsf correctly (parse cross-check)
# ---------------------------------------------------------------------------


def test_we_parse_vice_emitted_vsf(vice_transport, tmp_path):
    """VICE dumps a snapshot; we parse it and verify RAM round-trip."""
    marker = bytes(range(0x80, 0x90))  # distinctive from previous test's marker
    vice_transport.write_memory(0xC100, marker, override="snapshot-test")
    time.sleep(0.2)

    vsf_path = tmp_path / "vice_dump.vsf"
    if vsf_path.exists():
        vsf_path.unlink()
    vice_transport.dump_snapshot(str(vsf_path))
    time.sleep(0.2)

    blob = vsf_path.read_bytes()
    snap = Snapshot.from_vsf(blob)

    assert len(snap.ram) == 65536
    assert snap.ram[0xC100 : 0xC100 + len(marker)] == marker
    # CPU port should be the default $37/$2F at the BASIC READY prompt.
    assert snap.cpu_port_data == 0x37
    assert snap.cpu_port_dir == 0x2F


# ---------------------------------------------------------------------------
# 4. restore_snapshot writes through the transport with the right override
# ---------------------------------------------------------------------------


class TestRestoreState:
    def test_restore_uses_snapshot_restore_override(self) -> None:
        snap = Snapshot(
            ram=_make_pattern_ram(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
        )
        mock = _MockTransport()
        restore_snapshot(mock, snap)
        # Every recorded write must carry the snapshot-restore override.
        assert mock.writes, "restore_snapshot issued no writes"
        for addr, data, override in mock.writes:
            assert override == "snapshot-restore", (
                f"write at ${addr:04X} ({len(data)} bytes) "
                f"used override={override!r}"
            )

    def test_restore_writes_full_ram_image(self) -> None:
        snap = Snapshot(
            ram=_make_pattern_ram(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
        )
        mock = _MockTransport()
        restore_snapshot(mock, snap)
        ram_writes = [(a, d) for a, d, _ov in mock.writes if a == 0x0000 and len(d) == 65536]
        assert ram_writes, "no full-RAM write was issued"
        assert ram_writes[0][1] == snap.ram

    def test_restore_writes_cpu_port_explicitly(self) -> None:
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0xAB,
            cpu_port_dir=0xCD,
        )
        mock = _MockTransport()
        restore_snapshot(mock, snap)
        addrs = [(addr, data) for addr, data, _ov in mock.writes]
        # The explicit CPU port writes must appear.
        assert (0x0000, b"\xcd") in addrs
        assert (0x0001, b"\xab") in addrs

    def test_restore_respects_no_override_flag(self) -> None:
        """When override_memory_policy=False, calls pass override=None.

        With a non-permissive policy, the underlying check_write will
        reject the bulk RAM write — verifies the kwarg actually
        threads through.
        """
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x0334, 0x0335, "scratch"),),
            unknown=UnknownPolicy.ALLOW,
        )
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
        )
        mock = _MockTransport(memory_policy=policy)
        with pytest.raises(Exception):  # MemoryPolicyError
            restore_snapshot(mock, snap, override_memory_policy=False)

    def test_restore_overrides_reserved_regions(self, caplog) -> None:
        """A snapshot restore against a guarded policy succeeds via override."""
        policy = MemoryPolicy(
            reserved_regions=(
                MemoryRegion(0x0334, 0x0335, "scratch1"),
                MemoryRegion(0xC000, 0xC400, "scratch2"),
            ),
            unknown=UnknownPolicy.ALLOW,
        )
        snap = Snapshot(
            ram=_make_pattern_ram(),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
        )
        mock = _MockTransport(memory_policy=policy)

        with caplog.at_level(logging.WARNING, logger="c64_test_harness.snapshot"):
            restore_snapshot(mock, snap)  # default override=True

        # No exception raised, every write recorded.
        assert any(
            "bypassing MemoryPolicy reserved regions: 2" in rec.message
            for rec in caplog.records
        ), f"expected WARNING about 2 reserved regions, got {[r.message for r in caplog.records]}"
        # And the writes were all marked with the override.
        for addr, data, override in mock.writes:
            assert override == "snapshot-restore"


# ---------------------------------------------------------------------------
# extract_snapshot — light test with a stub transport
# ---------------------------------------------------------------------------


class TestExtractState:
    def test_extract_reads_full_ram_and_cpu_port(self) -> None:
        ram = _make_pattern_ram()
        ram_buf = bytearray(ram)
        ram_buf[0x00] = 0x2F  # cpu_port_dir
        ram_buf[0x01] = 0x37  # cpu_port_data
        ram = bytes(ram_buf)

        class _Stub:
            def read_memory(self, addr: int, length: int) -> bytes:
                assert addr == 0x0000 and length == 65536
                return ram

        snap = extract_snapshot(_Stub())
        assert snap.ram == ram
        assert snap.cpu_port_dir == 0x2F
        assert snap.cpu_port_data == 0x37
