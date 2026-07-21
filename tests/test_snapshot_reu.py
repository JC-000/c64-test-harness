"""Unit tests for the Phase-B REU snapshot layer (issue #134).

Mock-only — no network, no hardware, no VICE.  Covers:

* ``SocketDMAClient.reu_write`` chunking: per-command ceiling (16-bit
  length field minus the 3-byte offset prefix), offset arithmetic,
  boundary sizes (exact ceiling, ceiling+1, 64 KiB, empty), the 16 MB
  path, and 24-bit address-space range checks.
* ``Ultimate64Transport.socket_dma_reu_write``: routing through the
  managed SocketDMA client, connect-failure latch behaviour in both
  directions, and the no-REST-fallback error contract.
* ``extract_reu_contents``: staging-window loop (REC register
  programming, bank reads, RAM stash/restore, pause/resume, overrides).
* ``extract_snapshot(include_reu=...)`` and REU size auto-detection.
* ``restore_snapshot`` REU dispatch: generation-aware enable + SocketDMA
  write on U64-shaped transports, ``SnapshotRestoreError`` on transports
  without the SocketDMA path (the VICE case), explicit ``restore_reu=False``
  opt-out.
* ``Snapshot`` REU field validation, ``.vsf`` neutrality (REU bytes do
  not break or leak into the VICE wire format), and sidecar bundle
  round-trip of ``reu.bin``.

Live byte-fidelity validation (REUWRITE → staging read-back on real
hardware) lives in ``tests/test_socketdma_live.py`` behind
``SOCKETDMA_LIVE``.
"""
from __future__ import annotations

import json
import math
from unittest.mock import MagicMock

import pytest

from c64_test_harness import (
    Snapshot,
    SnapshotFormatError,
    SnapshotRestoreError,
    extract_snapshot,
    restore_snapshot,
)
from c64_test_harness.backends.u64_socket_dma import (
    REU_WRITE_MAX_CHUNK,
    SocketDMAClient,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.snapshot import (
    _REC_C64_BASE,
    _REC_CMD_REU_TO_C64,
    _REC_COMMAND,
    _REU_STAGING_BASE,
    _REU_STAGING_OVERRIDE,
    _REU_STAGING_SIZE,
    extract_reu_contents,
)

_CMD_REUWRITE = 0xFF07


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _pattern(n: int, seed: int = 0) -> bytes:
    return bytes(((i * 7) ^ (i >> 8) ^ seed) & 0xFF for i in range(n))


def _ram_64k(fill: int = 0xEE) -> bytes:
    return bytes([fill]) * 65536


class _RecordingDMAClient(SocketDMAClient):
    """SocketDMAClient with the socket layer replaced by a recorder.

    ``_send`` keeps the real client's 16-bit payload-length contract (so
    a chunking bug that overflows a command fails the test the same way
    the real transport would) but records instead of transmitting.
    """

    def __init__(self) -> None:
        super().__init__("test-host")
        self.sent: list[tuple[int, bytes]] = []
        self._sock = object()  # pretend-connected: reu_write must not close

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        if len(payload) > 0xFFFF:
            raise Ultimate64Error(
                f"SocketDMA payload too large: {len(payload)} bytes (max 65535)"
            )
        self.sent.append((opcode, bytes(payload)))

    def close(self) -> None:
        self._sock = None


class FakeSocketDMAClient:
    """Transport-side fake: records reu_write calls, scripts failures."""

    def __init__(self) -> None:
        self.enter_count = 0
        self.close_count = 0
        self.reu_calls: list[tuple[int, bytes]] = []
        self.connect_error = False
        self.reu_error: Exception | None = None

    def __enter__(self) -> "FakeSocketDMAClient":
        self.enter_count += 1
        if self.connect_error:
            raise Ultimate64Error("fake connect refused")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.close_count += 1

    def reu_write(self, offset: int, data: bytes) -> None:
        if self.reu_error is not None:
            raise self.reu_error
        self.reu_calls.append((offset, bytes(data)))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.host = "192.0.2.1"
    client.password = None
    return client


@pytest.fixture
def install_fake(monkeypatch: pytest.MonkeyPatch):
    fake = FakeSocketDMAClient()
    state = {"constructed": 0}

    def factory(**kwargs: object) -> FakeSocketDMAClient:
        state["constructed"] += 1
        return fake

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64.SocketDMAClient", factory
    )
    return fake, state


class _FakeRecTransport:
    """Transport stub that emulates the REC (REU controller) registers.

    A write to ``$DF02`` latches the transfer parameters; a write of the
    REU→C64 command to ``$DF01`` copies the addressed REU slice into the
    64 KB RAM image.  ``read_memory``/``write_memory`` otherwise behave
    as plain RAM, so the staging stash/restore path is exercised for
    real.
    """

    def __init__(self, reu_image: bytes, ram_fill: int = 0xEE) -> None:
        self.ram = bytearray(_ram_64k(ram_fill))
        self.reu = bytes(reu_image)
        self.writes: list[tuple[int, bytes, str | None]] = []
        self.transfers: list[tuple[int, int, int]] = []  # (c64, reu, len)
        self._rec_regs = bytearray(9)
        self.client = MagicMock()
        self.resume_calls = 0

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(self.ram[addr : addr + length])

    def write_memory(
        self, addr: int, data: bytes, *, override: str | None = None
    ) -> None:
        data = bytes(data)
        self.writes.append((addr, data, override))
        if addr == _REC_C64_BASE and len(data) == 9:
            self._rec_regs[:] = data
            return
        if addr == _REC_COMMAND and data and data[0] == _REC_CMD_REU_TO_C64:
            r = self._rec_regs
            c64_base = r[0] | (r[1] << 8)
            reu_base = r[2] | (r[3] << 8) | (r[4] << 16)
            length = r[5] | (r[6] << 8)
            self.transfers.append((c64_base, reu_base, length))
            self.ram[c64_base : c64_base + length] = self.reu[
                reu_base : reu_base + length
            ]
            return
        # Plain RAM write (staging stash restore etc.).
        if addr + len(data) <= 65536:
            self.ram[addr : addr + len(data)] = data

    def resume(self) -> None:
        self.resume_calls += 1


class _MockTransport:
    """Minimal transport without any SocketDMA surface (the VICE shape)."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, bytes, str | None]] = []

    def write_memory(
        self, addr: int, data: bytes, *, override: str | None = None
    ) -> None:
        self.writes.append((addr, bytes(data), override))

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(length)


# ---------------------------------------------------------------------------
# 1. SocketDMAClient.reu_write chunking
# ---------------------------------------------------------------------------


class TestReuWriteChunking:
    def test_chunk_ceiling_constant(self) -> None:
        # 16-bit command length field minus the 3-byte offset prefix.
        assert REU_WRITE_MAX_CHUNK == 0xFFFF - 3 == 65532

    def test_empty_write_sends_nothing(self) -> None:
        c = _RecordingDMAClient()
        c.reu_write(0x1000, b"")
        assert c.sent == []

    def test_exact_ceiling_is_single_command(self) -> None:
        data = _pattern(REU_WRITE_MAX_CHUNK)
        c = _RecordingDMAClient()
        c.reu_write(0, data)
        assert len(c.sent) == 1
        opcode, payload = c.sent[0]
        assert opcode == _CMD_REUWRITE
        assert len(payload) == 0xFFFF  # exactly fills the length field
        assert payload[:3] == b"\x00\x00\x00"
        assert payload[3:] == data

    def test_ceiling_plus_one_splits_in_two(self) -> None:
        data = _pattern(REU_WRITE_MAX_CHUNK + 1)
        c = _RecordingDMAClient()
        c.reu_write(0, data)
        assert len(c.sent) == 2
        assert c.sent[0][1][3:] == data[:REU_WRITE_MAX_CHUNK]
        # Second command: offset advanced by one full chunk, one data byte.
        offset2 = int.from_bytes(c.sent[1][1][:3], "little")
        assert offset2 == REU_WRITE_MAX_CHUNK
        assert c.sent[1][1][3:] == data[REU_WRITE_MAX_CHUNK:]

    def test_64kib_splits_in_two(self) -> None:
        data = _pattern(65536)
        c = _RecordingDMAClient()
        c.reu_write(0, data)
        assert [len(p) - 3 for _, p in c.sent] == [REU_WRITE_MAX_CHUNK, 4]
        reassembled = b"".join(p[3:] for _, p in c.sent)
        assert reassembled == data

    def test_offset_arithmetic_with_nonzero_base(self) -> None:
        base = 0x010000
        data = _pattern(200_000)
        c = _RecordingDMAClient()
        c.reu_write(base, data)
        expected_offsets = list(range(0, len(data), REU_WRITE_MAX_CHUNK))
        got_offsets = [int.from_bytes(p[:3], "little") for _, p in c.sent]
        assert got_offsets == [base + o for o in expected_offsets]
        assert b"".join(p[3:] for _, p in c.sent) == data

    def test_full_16mb_chunk_math(self) -> None:
        size = 16 * 1024 * 1024
        expected_commands = math.ceil(size / REU_WRITE_MAX_CHUNK)
        assert expected_commands == 257
        c = _RecordingDMAClient()
        c.reu_write(0, bytes(size))
        assert len(c.sent) == expected_commands
        # Last chunk carries the remainder.
        assert len(c.sent[-1][1]) - 3 == size - 256 * REU_WRITE_MAX_CHUNK == 1024
        assert int.from_bytes(c.sent[-1][1][:3], "little") == 256 * REU_WRITE_MAX_CHUNK
        # Every command respects the 16-bit payload-length field.
        assert all(len(p) <= 0xFFFF for _, p in c.sent)

    def test_offset_out_of_24bit_range_rejected(self) -> None:
        c = _RecordingDMAClient()
        with pytest.raises(Ultimate64Error, match="out of range"):
            c.reu_write(0x1000000, b"\x00")
        assert c.sent == []

    def test_write_past_16mb_end_rejected(self) -> None:
        c = _RecordingDMAClient()
        with pytest.raises(Ultimate64Error, match="runs past"):
            c.reu_write(0xFFFFFF, b"\x00\x00")
        assert c.sent == []

    def test_write_ending_exactly_at_16mb_accepted(self) -> None:
        c = _RecordingDMAClient()
        c.reu_write(0xFFFFFF, b"\xAA")
        assert len(c.sent) == 1
        assert c.sent[0][1] == b"\xFF\xFF\xFF\xAA"


# ---------------------------------------------------------------------------
# 2. Ultimate64Transport.socket_dma_reu_write
# ---------------------------------------------------------------------------


class TestTransportReuWrite:
    def test_routes_through_managed_client(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, state = install_fake
        t = Ultimate64Transport(host="h", client=mock_client)
        data = _pattern(1024)

        t.socket_dma_reu_write(0x1234, data)
        t.socket_dma_reu_write(0x8000, data)

        assert fake.reu_calls == [(0x1234, data), (0x8000, data)]
        # One managed client, connection re-entered per call, never a
        # REST fallback.
        assert state["constructed"] == 1
        assert fake.enter_count == 2
        mock_client.write_mem.assert_not_called()

    def test_works_without_socket_dma_master_switch(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, _ = install_fake
        t = Ultimate64Transport(host="h", client=mock_client)
        assert t.socket_dma is False  # fast-path switch off — irrelevant here
        t.socket_dma_reu_write(0, b"\x01")
        assert fake.reu_calls == [(0, b"\x01")]

    def test_empty_data_is_noop(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, state = install_fake
        t = Ultimate64Transport(host="h", client=mock_client)
        t.socket_dma_reu_write(0, b"")
        assert state["constructed"] == 0
        assert fake.reu_calls == []

    def test_connect_failure_raises_actionable_and_latches(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, _ = install_fake
        fake.connect_error = True
        t = Ultimate64Transport(host="h", client=mock_client)

        with pytest.raises(Ultimate64Error) as excinfo:
            t.socket_dma_reu_write(0, b"\x01")
        msg = str(excinfo.value)
        assert "Ultimate DMA Service" in msg
        assert "NO REST fallback" in msg
        assert t._socket_dma_unusable is True

        # Latched: second call fails fast without reconnecting.
        with pytest.raises(Ultimate64Error, match="latched off"):
            t.socket_dma_reu_write(0, b"\x01")
        assert fake.enter_count == 1
        # Never degraded to REST.
        mock_client.write_mem.assert_not_called()

    def test_respects_existing_latch_from_write_fast_path(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, state = install_fake
        t = Ultimate64Transport(host="h", client=mock_client)
        t._socket_dma_unusable = True  # as latched by a prior write_memory

        with pytest.raises(Ultimate64Error, match="Ultimate DMA Service"):
            t.socket_dma_reu_write(0, b"\x01")
        assert state["constructed"] == 0
        assert fake.enter_count == 0

    def test_send_failure_propagates_no_rest_fallback(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, _ = install_fake
        fake.reu_error = Ultimate64Error("fake send failed")
        t = Ultimate64Transport(host="h", client=mock_client)

        with pytest.raises(Ultimate64Error, match="fake send failed"):
            t.socket_dma_reu_write(0, b"\x01")
        # Send failures do NOT latch (matches the write fast path).
        assert t._socket_dma_unusable is False
        mock_client.write_mem.assert_not_called()

    def test_close_closes_managed_client(
        self, mock_client: MagicMock, install_fake
    ) -> None:
        fake, _ = install_fake
        t = Ultimate64Transport(host="h", client=mock_client)
        t.socket_dma_reu_write(0, b"\x01")
        t.close()
        assert fake.close_count >= 1


# ---------------------------------------------------------------------------
# 3. extract_reu_contents — staging-window loop
# ---------------------------------------------------------------------------


class TestExtractReuContents:
    def test_single_bank_extract(self) -> None:
        reu = _pattern(_REU_STAGING_SIZE)
        t = _FakeRecTransport(reu)
        got = extract_reu_contents(t, _REU_STAGING_SIZE, settle=0)
        assert got == reu
        assert t.transfers == [(_REU_STAGING_BASE, 0, _REU_STAGING_SIZE)]

    def test_multi_bank_with_partial_tail(self) -> None:
        size = 40 * 1024  # 32 KiB bank + 8 KiB tail
        reu = _pattern(size, seed=3)
        t = _FakeRecTransport(reu)
        got = extract_reu_contents(t, size, settle=0)
        assert got == reu
        assert t.transfers == [
            (_REU_STAGING_BASE, 0x0000, _REU_STAGING_SIZE),
            (_REU_STAGING_BASE, 0x8000, size - _REU_STAGING_SIZE),
        ]

    def test_bank_offsets_cover_128kb(self) -> None:
        size = 128 * 1024
        t = _FakeRecTransport(_pattern(size, seed=9))
        got = extract_reu_contents(t, size, settle=0)
        assert got == t.reu
        assert [reu for _, reu, _ in t.transfers] == [
            0x00000, 0x08000, 0x10000, 0x18000,
        ]

    def test_staging_window_ram_restored(self) -> None:
        t = _FakeRecTransport(_pattern(_REU_STAGING_SIZE), ram_fill=0xEE)
        original = bytes(t.ram)
        extract_reu_contents(t, _REU_STAGING_SIZE, settle=0)
        # The staging window (and everything else) is back to the
        # pre-extract image; only the REC register file was clobbered.
        assert bytes(t.ram) == original

    def test_all_writes_carry_staging_override(self) -> None:
        t = _FakeRecTransport(_pattern(1024))
        extract_reu_contents(t, 1024, settle=0)
        assert t.writes  # REC programming + stash restore
        assert all(ov == _REU_STAGING_OVERRIDE for _, _, ov in t.writes)

    def test_pause_and_resume_paired(self) -> None:
        t = _FakeRecTransport(_pattern(1024))
        extract_reu_contents(t, 1024, settle=0)
        t.client.pause.assert_called_once_with()
        assert t.resume_calls == 1

    def test_pause_false_skips_pause(self) -> None:
        t = _FakeRecTransport(_pattern(1024))
        extract_reu_contents(t, 1024, settle=0, pause=False)
        t.client.pause.assert_not_called()
        assert t.resume_calls == 0

    def test_resume_even_when_bank_read_fails(self) -> None:
        t = _FakeRecTransport(_pattern(1024))
        real_read = t.read_memory
        state = {"reads": 0}

        def flaky_read(addr: int, length: int) -> bytes:
            state["reads"] += 1
            if state["reads"] == 2:  # first bank read after the stash
                return b""  # short read
            return real_read(addr, length)

        t.read_memory = flaky_read  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="staging window read"):
            extract_reu_contents(t, 1024, settle=0)
        assert t.resume_calls == 1  # resumed despite the failure
        # Stash restore still happened (last write is the 32 KiB window).
        assert t.writes[-1][0] == _REU_STAGING_BASE
        assert len(t.writes[-1][1]) == _REU_STAGING_SIZE

    @pytest.mark.parametrize("bad", [0, -1, 16 * 1024 * 1024 + 1, "16MB", True])
    def test_rejects_bad_sizes(self, bad) -> None:
        t = _FakeRecTransport(b"\x00" * 16)
        with pytest.raises(ValueError):
            extract_reu_contents(t, bad, settle=0)


# ---------------------------------------------------------------------------
# 4. extract_snapshot integration + size auto-detect
# ---------------------------------------------------------------------------


class TestExtractSnapshotReu:
    def test_default_excludes_reu(self) -> None:
        t = _FakeRecTransport(_pattern(1024))
        snap = extract_snapshot(t)
        assert snap.reu_contents is None
        assert snap.reu_size_bytes is None

    def test_include_reu_with_explicit_size(self) -> None:
        size = 64 * 1024
        t = _FakeRecTransport(_pattern(size, seed=5))
        snap = extract_snapshot(
            t, include_reu=True, reu_size_bytes=size, reu_settle=0
        )
        assert snap.reu_size_bytes == size
        assert snap.reu_contents == t.reu

    def test_auto_detect_size_from_device_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        size = 512 * 1024
        t = _FakeRecTransport(_pattern(size, seed=7))
        monkeypatch.setattr(
            "c64_test_harness.backends.ultimate64_helpers.get_reu_config",
            lambda client: (True, "512 KB"),
        )
        snap = extract_snapshot(t, include_reu=True, reu_settle=0)
        assert snap.reu_size_bytes == size
        assert snap.reu_contents == t.reu

    def test_auto_detect_reu_disabled_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        t = _FakeRecTransport(_pattern(16))
        monkeypatch.setattr(
            "c64_test_harness.backends.ultimate64_helpers.get_reu_config",
            lambda client: (False, "512 KB"),
        )
        with pytest.raises(ValueError, match="disabled"):
            extract_snapshot(t, include_reu=True, reu_settle=0)

    def test_auto_detect_without_client_raises(self) -> None:
        t = _MockTransport()
        with pytest.raises(ValueError, match="reu_size_bytes"):
            extract_snapshot(t, include_reu=True)


# ---------------------------------------------------------------------------
# 5. restore_snapshot REU dispatch
# ---------------------------------------------------------------------------


def _snap_with_reu(size: int = 128 * 1024) -> Snapshot:
    return Snapshot(
        ram=_ram_64k(0x00),
        cpu_port_data=0x37,
        cpu_port_dir=0x2F,
        reu_contents=_pattern(size),
    )


class _FakeU64RestoreTransport(_MockTransport):
    """U64-shaped restore target: RAM writes + SocketDMA REU surface."""

    def __init__(self) -> None:
        super().__init__()
        self.client = MagicMock()
        self.client.get_config_item.return_value = None  # probe inconclusive
        self.events: list[str] = []
        self.reu_calls: list[tuple[int, bytes]] = []
        self.reu_error: Exception | None = None

    def write_memory(
        self, addr: int, data: bytes, *, override: str | None = None
    ) -> None:
        super().write_memory(addr, data, override=override)
        self.events.append("ram")

    def socket_dma_reu_write(self, offset: int, data: bytes) -> None:
        if self.reu_error is not None:
            raise self.reu_error
        self.reu_calls.append((offset, bytes(data)))
        self.events.append("reu")


class TestRestoreSnapshotReu:
    def test_u64_restore_enables_then_writes_reu(self) -> None:
        snap = _snap_with_reu(128 * 1024)
        t = _FakeU64RestoreTransport()

        restore_snapshot(t, snap)

        # Generation-aware enable went through set_reu → set_config_items
        # with the REU items (Cartridge presence is probe-dependent).
        t.client.set_config_items.assert_called_once()
        _category, items = t.client.set_config_items.call_args.args
        assert items["RAM Expansion Unit"] == "Enabled"
        assert items["REU Size"] == "128 KB"
        # Full contents at offset 0 through the SocketDMA path.
        assert t.reu_calls == [(0, snap.reu_contents)]
        # REU restore happens after the RAM image.
        assert t.events.index("reu") > t.events.index("ram")

    def test_restore_reu_false_skips_layer(self) -> None:
        snap = _snap_with_reu()
        t = _FakeU64RestoreTransport()
        restore_snapshot(t, snap, restore_reu=False)
        assert t.reu_calls == []
        t.client.set_config_items.assert_not_called()
        assert t.writes  # RAM restore still ran

    def test_no_reu_contents_never_touches_reu_path(self) -> None:
        snap = Snapshot(ram=_ram_64k(), cpu_port_data=0x37, cpu_port_dir=0x2F)
        t = _FakeU64RestoreTransport()
        restore_snapshot(t, snap)
        assert t.reu_calls == []
        t.client.set_config_items.assert_not_called()

    def test_transport_without_socketdma_raises_clear_error(self) -> None:
        """The VICE shape: REU restore must fail loudly, not skip."""
        snap = _snap_with_reu()
        t = _MockTransport()
        with pytest.raises(SnapshotRestoreError) as excinfo:
            restore_snapshot(t, snap)
        msg = str(excinfo.value)
        assert "no REST or VICE-monitor fallback" in msg
        assert "restore_reu=False" in msg

    def test_dma_unavailable_error_propagates(self) -> None:
        snap = _snap_with_reu()
        t = _FakeU64RestoreTransport()
        t.reu_error = Ultimate64Error("SocketDMA connect failed; ...")
        with pytest.raises(Ultimate64Error, match="connect failed"):
            restore_snapshot(t, snap)

    def test_invalid_reu_size_rejected_before_any_write(self) -> None:
        # 100 KiB is not a firmware REU Size — reu_size_enum must reject
        # it during the enable step, before any REUWRITE goes out.
        snap = _snap_with_reu(100 * 1024)
        t = _FakeU64RestoreTransport()
        with pytest.raises(ValueError, match="REU"):
            restore_snapshot(t, snap)
        assert t.reu_calls == []


# ---------------------------------------------------------------------------
# 6. Snapshot REU fields, .vsf neutrality, sidecar bundle
# ---------------------------------------------------------------------------


class TestSnapshotReuFields:
    def test_size_derived_from_contents(self) -> None:
        snap = _snap_with_reu(256 * 1024)
        assert snap.reu_size_bytes == 256 * 1024

    def test_size_contents_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="reu_size_bytes"):
            Snapshot(
                ram=_ram_64k(),
                cpu_port_data=0,
                cpu_port_dir=0,
                reu_size_bytes=1024,
                reu_contents=b"\x00" * 512,
            )

    def test_oversize_contents_rejected(self) -> None:
        with pytest.raises(ValueError):
            Snapshot(
                ram=_ram_64k(),
                cpu_port_data=0,
                cpu_port_dir=0,
                reu_contents=b"\x00" * (16 * 1024 * 1024 + 1),
            )

    def test_bytearray_contents_coerced_immutable(self) -> None:
        snap = Snapshot(
            ram=_ram_64k(),
            cpu_port_data=0,
            cpu_port_dir=0,
            reu_contents=bytearray(b"\x01\x02"),
        )
        assert isinstance(snap.reu_contents, bytes)

    def test_vsf_roundtrip_unaffected_by_reu(self) -> None:
        """The VICE wire format neither carries nor chokes on REU bytes."""
        snap = _snap_with_reu()
        vsf = snap.to_vsf()  # must not raise
        back = Snapshot.from_vsf(vsf)
        assert back.ram == snap.ram
        assert back.reu_contents is None  # REU travels via sidecar only
        # Byte-identical .vsf with and without the REU layer.
        bare = Snapshot(
            ram=snap.ram,
            cpu_port_data=snap.cpu_port_data,
            cpu_port_dir=snap.cpu_port_dir,
        )
        assert vsf == bare.to_vsf()


class TestSidecarBundle:
    def test_roundtrip_with_reu(self, tmp_path) -> None:
        snap = _snap_with_reu(128 * 1024)
        bundle = snap.to_bundle(tmp_path / "snap")
        assert (bundle / "snapshot.vsf").is_file()
        assert (bundle / "reu.bin").read_bytes() == snap.reu_contents
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["reu"] == {"file": "reu.bin", "size_bytes": 128 * 1024}

        back = Snapshot.from_bundle(bundle)
        assert back.ram == snap.ram
        assert back.reu_contents == snap.reu_contents
        assert back.reu_size_bytes == snap.reu_size_bytes

    def test_roundtrip_without_reu(self, tmp_path) -> None:
        snap = Snapshot(ram=_ram_64k(), cpu_port_data=0x37, cpu_port_dir=0x2F)
        bundle = snap.to_bundle(tmp_path / "snap")
        assert not (bundle / "reu.bin").exists()
        back = Snapshot.from_bundle(bundle)
        assert back.reu_contents is None
        assert back.reu_size_bytes is None

    def test_missing_vsf_rejected(self, tmp_path) -> None:
        (tmp_path / "empty").mkdir()
        with pytest.raises(SnapshotFormatError, match="snapshot.vsf"):
            Snapshot.from_bundle(tmp_path / "empty")

    def test_manifest_size_mismatch_rejected(self, tmp_path) -> None:
        snap = _snap_with_reu(128 * 1024)
        bundle = snap.to_bundle(tmp_path / "snap")
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["reu"]["size_bytes"] = 999
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(SnapshotFormatError, match="manifest declares"):
            Snapshot.from_bundle(bundle)
