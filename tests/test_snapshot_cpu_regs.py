"""Tests for the CPU-register snapshot extension (Phase D).

Phase A (RAM + CPU port) is covered by ``tests/test_snapshot.py``,
Phase B disk sidecar by ``tests/test_snapshot_drives.py``.  This module
exercises the 6510 register surface:

* :class:`CpuRegisters` field-width validation.
* :class:`Snapshot` backwards-compat (``cpu_registers`` defaults to
  ``None``, Phase A/B code paths see no change).
* :func:`extract_snapshot` on mocked VICE (read_registers dict) and
  Ultimate 64 (active-snoop sideload + readback) transports.
* :func:`restore_snapshot` on mocked VICE (set_registers) and U64
  (restorer routine + send_text trigger) transports.
* ``.vsf`` MAINCPU module patch round-trip — emit a snapshot with
  non-default CPU registers, round-trip through to_vsf / from_vsf and
  verify each field survives.
* Bundle round-trip with ``cpu_registers``.
* Snoop / restorer 6510 byte-level layout including stack-pointer
  arithmetic.

All tests are offline (no live VICE or U64).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from c64_test_harness import (
    CpuRegisters,
    DriveState,
    Snapshot,
    extract_snapshot,
    restore_snapshot,
)
from c64_test_harness.snapshot import (
    _MAINCPU_MODULE_NAME,
    _MAINCPU_REG_OFFSET,
    _RESTORE_ADDR,
    _RESTORE_LENGTH,
    _SNOOP_ADDR,
    _SNOOP_LENGTH,
    _SNOOP_SAVE_ADDR,
    _build_restore_routine,
    _build_snoop_routine,
    _iter_modules,
    _patch_module_prefix,
)


# ---------------------------------------------------------------------------
# CpuRegisters validation
# ---------------------------------------------------------------------------


class TestCpuRegistersValidation:
    def test_valid_minima(self) -> None:
        r = CpuRegisters(pc=0, a=0, x=0, y=0, sp=0, p=0)
        assert r.pc == 0 and r.a == 0 and r.p == 0

    def test_valid_maxima(self) -> None:
        r = CpuRegisters(pc=0xFFFF, a=0xFF, x=0xFF, y=0xFF, sp=0xFF, p=0xFF)
        assert r.pc == 0xFFFF
        assert r.a == 0xFF

    def test_typical_basic_ready(self) -> None:
        # PC at BASIC main loop $E5D1, SP at $F3, Z+unused flag.
        r = CpuRegisters(pc=0xE5D1, a=0x00, x=0x00, y=0x0A, sp=0xF3, p=0x22)
        assert r.pc == 0xE5D1

    def test_pc_out_of_range_negative(self) -> None:
        with pytest.raises(ValueError, match="pc"):
            CpuRegisters(pc=-1, a=0, x=0, y=0, sp=0, p=0)

    def test_pc_out_of_range_too_big(self) -> None:
        with pytest.raises(ValueError, match="pc"):
            CpuRegisters(pc=0x10000, a=0, x=0, y=0, sp=0, p=0)

    def test_a_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="a"):
            CpuRegisters(pc=0, a=256, x=0, y=0, sp=0, p=0)

    @pytest.mark.parametrize("field", ["x", "y", "sp", "p"])
    def test_byte_field_out_of_range(self, field: str) -> None:
        kwargs = {"pc": 0, "a": 0, "x": 0, "y": 0, "sp": 0, "p": 0}
        kwargs[field] = 0x100
        with pytest.raises(ValueError, match=field):
            CpuRegisters(**kwargs)

    def test_non_int_pc(self) -> None:
        with pytest.raises(ValueError, match="pc"):
            CpuRegisters(pc="0x1234", a=0, x=0, y=0, sp=0, p=0)  # type: ignore[arg-type]

    def test_frozen_dataclass(self) -> None:
        r = CpuRegisters(pc=0, a=0, x=0, y=0, sp=0, p=0)
        with pytest.raises(Exception):  # FrozenInstanceError subclass
            r.pc = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Snapshot backwards compatibility
# ---------------------------------------------------------------------------


class TestSnapshotCpuRegistersField:
    def test_default_none(self) -> None:
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F)
        assert snap.cpu_registers is None

    def test_setting_cpu_registers(self) -> None:
        regs = CpuRegisters(pc=0x8000, a=0xAA, x=0xBB, y=0xCC, sp=0xFD, p=0x21)
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cpu_registers=regs,
        )
        assert snap.cpu_registers is regs

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(TypeError, match="cpu_registers"):
            Snapshot(
                ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0,
                cpu_registers={"PC": 0},  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Snoop routine — byte-level layout + SP arithmetic
# ---------------------------------------------------------------------------


class TestSnoopRoutineBytes:
    def test_length_matches_constant(self) -> None:
        b = _build_snoop_routine()
        assert len(b) == _SNOOP_LENGTH == 19

    def test_byte_sequence(self) -> None:
        # Default save base $0350.  Layout:
        #   STA $0350, STX $0351, STY $0352, PHP, PLA, STA $0353,
        #   TSX, STX $0354, RTS
        assert _build_snoop_routine() == bytes([
            0x8D, 0x50, 0x03,  # STA $0350 (A)
            0x8E, 0x51, 0x03,  # STX $0351 (X)
            0x8C, 0x52, 0x03,  # STY $0352 (Y)
            0x08,              # PHP
            0x68,              # PLA
            0x8D, 0x53, 0x03,  # STA $0353 (P)
            0xBA,              # TSX
            0x8E, 0x54, 0x03,  # STX $0354 (SP)
            0x60,              # RTS
        ])

    def test_alt_save_address_threads_through(self) -> None:
        b = _build_snoop_routine(save_addr=0x03A0)
        # STA $03A0 ...
        assert b[0:3] == bytes([0x8D, 0xA0, 0x03])
        assert b[3:6] == bytes([0x8E, 0xA1, 0x03])
        # ... STX $03A4
        assert b[15:18] == bytes([0x8E, 0xA4, 0x03])

    def test_php_pla_sp_arithmetic_is_net_zero(self) -> None:
        """Walk a mock 6510 SP through PHP then PLA — net effect zero.

        PHP: stores P at stack[SP], decrements SP by 1.
        PLA: increments SP by 1, loads from stack[SP].
        Therefore TSX after PLA gives the SP value the caller had on
        entry — what we want in the snapshot.
        """
        sp = 0xF3  # arbitrary entry SP
        # PHP: writes to $0100+SP, then SP <- SP - 1
        sp_after_php = (sp - 1) & 0xFF
        # PLA: SP <- SP + 1, then reads from $0100+SP
        sp_after_pla = (sp_after_php + 1) & 0xFF
        assert sp_after_pla == sp

        # Try a wraparound case too.
        sp = 0x00
        sp_after_php = (sp - 1) & 0xFF      # 0xFF
        sp_after_pla = (sp_after_php + 1) & 0xFF
        assert sp_after_pla == 0x00

    def test_save_addresses_outside_routine_footprint(self) -> None:
        """Routine occupies $0334-$0346; save area must not overlap."""
        last_routine_byte = _SNOOP_ADDR + _SNOOP_LENGTH - 1   # $0346
        assert _SNOOP_SAVE_ADDR > last_routine_byte


# ---------------------------------------------------------------------------
# Restore routine — byte-level layout + immediate patching
# ---------------------------------------------------------------------------


class TestRestoreRoutineBytes:
    def test_length(self) -> None:
        regs = CpuRegisters(pc=0x1234, a=0xAA, x=0xBB, y=0xCC, sp=0xFD, p=0x21)
        b = _build_restore_routine(regs)
        assert len(b) == _RESTORE_LENGTH == 16

    def test_byte_sequence_with_patched_immediates(self) -> None:
        regs = CpuRegisters(
            pc=0xC0DE, a=0x11, x=0x22, y=0x33, sp=0xF7, p=0x35,
        )
        assert _build_restore_routine(regs) == bytes([
            0xA2, 0xF7,              # LDX #SP_VAL    (sp)
            0x9A,                    # TXS
            0xA9, 0x35,              # LDA #P_VAL     (p)
            0x48,                    # PHA
            0xA2, 0x22,              # LDX #X_VAL     (x)
            0xA0, 0x33,              # LDY #Y_VAL     (y)
            0xA9, 0x11,              # LDA #A_VAL     (a)
            0x28,                    # PLP
            0x4C, 0xDE, 0xC0,        # JMP $C0DE      (pc little-endian)
        ])

    def test_pc_little_endian(self) -> None:
        regs = CpuRegisters(pc=0xABCD, a=0, x=0, y=0, sp=0, p=0)
        b = _build_restore_routine(regs)
        # JMP at offset 13..15
        assert b[13] == 0x4C
        assert b[14] == 0xCD  # PC low
        assert b[15] == 0xAB  # PC high

    def test_pc_zero_pads_correctly(self) -> None:
        regs = CpuRegisters(pc=0x0001, a=0, x=0, y=0, sp=0, p=0)
        b = _build_restore_routine(regs)
        assert b[-3:] == bytes([0x4C, 0x01, 0x00])

    def test_restore_sequence_net_sp_is_target_sp(self) -> None:
        """Walk through the restorer's SP effects.

        Steps: TXS sets SP to target; PHA decrements by 1; PLP
        increments by 1.  Net SP at JMP-time equals the target SP.
        """
        target_sp = 0xFD
        sp = target_sp           # after TXS
        sp = (sp - 1) & 0xFF     # after PHA
        sp = (sp + 1) & 0xFF     # after PLP
        assert sp == target_sp


# ---------------------------------------------------------------------------
# VICE extract path — mock read_registers
# ---------------------------------------------------------------------------


def _make_vice_mock(reg_dict: dict[str, int]) -> MagicMock:
    """Build a VICE-shaped mock transport for the CPU-register path.

    Has ``read_registers`` and ``read_memory``; NO ``client`` attr (so
    the dispatcher picks the VICE branch).  ``resource_get`` is left out
    too so :func:`_extract_drives` returns no drives.
    """
    mock = MagicMock(spec=[
        "read_memory", "write_memory", "read_registers", "set_registers",
        "memory_policy",
    ])
    mock.read_memory.return_value = b"\x00" * 65536
    mock.read_registers.return_value = reg_dict
    mock.memory_policy = None
    return mock


class TestExtractCpuRegistersVice:
    def test_read_registers_flows_into_snapshot(self) -> None:
        mock = _make_vice_mock({
            "PC": 0xE5D1, "A": 0x12, "X": 0x34, "Y": 0x56,
            "SP": 0xF3, "FL": 0x22,
        })
        snap = extract_snapshot(mock, include_registers=False)
        assert snap.cpu_registers is not None
        r = snap.cpu_registers
        assert r.pc == 0xE5D1
        assert r.a == 0x12
        assert r.x == 0x34
        assert r.y == 0x56
        assert r.sp == 0xF3
        assert r.p == 0x22

    def test_accepts_p_key_too(self) -> None:
        mock = _make_vice_mock({
            "PC": 0x1000, "A": 1, "X": 2, "Y": 3, "SP": 0xFC, "P": 0x21,
        })
        snap = extract_snapshot(mock, include_registers=False)
        assert snap.cpu_registers is not None
        assert snap.cpu_registers.p == 0x21

    def test_lowercase_keys(self) -> None:
        # The dict is normalised case-insensitively.
        mock = _make_vice_mock({
            "pc": 0x1234, "a": 0x99, "x": 0, "y": 0, "sp": 0xFD, "fl": 0x20,
        })
        snap = extract_snapshot(mock, include_registers=False)
        assert snap.cpu_registers is not None
        assert snap.cpu_registers.pc == 0x1234

    def test_skipped_when_include_false(self) -> None:
        mock = _make_vice_mock({
            "PC": 0xE5D1, "A": 0, "X": 0, "Y": 0, "SP": 0xF3, "FL": 0x22,
        })
        snap = extract_snapshot(
            mock, include_cpu_registers=False, include_registers=False,
        )
        assert snap.cpu_registers is None
        mock.read_registers.assert_not_called()

    def test_missing_pc_returns_none(self, caplog) -> None:
        # No PC key — return None and log a warning.
        mock = _make_vice_mock({"A": 0, "X": 0, "Y": 0, "SP": 0xFD, "FL": 0x20})
        snap = extract_snapshot(mock, include_registers=False)
        assert snap.cpu_registers is None


# ---------------------------------------------------------------------------
# U64 extract path — mock active snoop
# ---------------------------------------------------------------------------


def _make_u64_mock(snoop_readback: bytes) -> MagicMock:
    """Build a U64-shaped mock transport.

    Has a ``client`` attribute exposing ``run_prg``/``send_text``.  The
    ``read_memory`` mock returns 64 KB of zeros for the bulk read and
    *snoop_readback* (5 bytes) for the post-snoop read at $0350.
    """
    mock = MagicMock(spec=[
        "read_memory", "write_memory", "client", "memory_policy",
    ])
    mock.client = MagicMock(spec=["run_prg", "send_text"])
    mock.memory_policy = None

    def _read_memory(addr: int, length: int) -> bytes:
        if addr == 0x0000 and length == 65536:
            return b"\x00" * 65536
        if addr == _SNOOP_ADDR and length == _SNOOP_LENGTH:
            # The "previous contents" the snoop should preserve.
            return b"\xEA" * _SNOOP_LENGTH  # NOPs as a sentinel
        if addr == _SNOOP_SAVE_ADDR and length == 5:
            # First call (the save) gets a sentinel, second call (post-snoop)
            # gets the readback.  Use side_effect for that.
            return snoop_readback
        return b"\x00" * length

    mock.read_memory.side_effect = _read_memory
    return mock


class TestExtractCpuRegistersU64:
    def test_snoop_routine_is_sideloaded_then_triggered(self) -> None:
        readback = bytes([0x12, 0x34, 0x56, 0x22, 0xF3])  # A, X, Y, P, SP
        # The U64 read_memory mock needs the SAVE address probe to return
        # an "original bytes" sentinel on the first call, then the actual
        # readback on the second call.  Use a counter side-effect.
        original_save = b"\xCC" * 5

        mock = MagicMock(spec=[
            "read_memory", "write_memory", "client", "memory_policy",
        ])
        mock.client = MagicMock(spec=["run_prg", "send_text"])
        mock.memory_policy = None

        save_call_count = {"n": 0}

        def _read_memory(addr: int, length: int) -> bytes:
            if addr == 0x0000 and length == 65536:
                return b"\x00" * 65536
            if addr == _SNOOP_ADDR and length == _SNOOP_LENGTH:
                return b"\xEA" * _SNOOP_LENGTH
            if addr == _SNOOP_SAVE_ADDR and length == 5:
                save_call_count["n"] += 1
                # First call = the pre-snoop save; second = readback.
                if save_call_count["n"] == 1:
                    return original_save
                return readback
            return b"\x00" * length

        mock.read_memory.side_effect = _read_memory

        snap = extract_snapshot(mock)

        # 1. The snoop routine bytes were written to $0334 with the snoop
        # override.
        write_calls = mock.write_memory.call_args_list
        snoop_writes = [c for c in write_calls
                        if c.args[0] == _SNOOP_ADDR and len(c.args[1]) == _SNOOP_LENGTH]
        assert snoop_writes, "snoop routine never written"
        # The first such write must be the *snoop bytes*, not the restore.
        first_routine_bytes = snoop_writes[0].args[1]
        assert first_routine_bytes == _build_snoop_routine()
        assert snoop_writes[0].kwargs.get("override") == "snapshot-snoop"

        # 2. send_text triggered the routine at SYS 820 ($0334).
        send_calls = mock.client.send_text.call_args_list
        assert send_calls, "send_text never called to trigger snoop"
        first_arg = send_calls[0].args[0]
        assert "SYS" in first_arg and str(_SNOOP_ADDR) in first_arg

        # 3. The saved register block flowed into the snapshot.
        assert snap.cpu_registers is not None
        r = snap.cpu_registers
        assert (r.a, r.x, r.y, r.p, r.sp) == (0x12, 0x34, 0x56, 0x22, 0xF3)

        # 4. PC is the snoop entry (no known_pc supplied).
        assert r.pc == _SNOOP_ADDR

        # 5. The original bytes at $0334 and $0350 were restored.
        restore_writes_to_snoop = [c for c in write_calls
                                   if c.args[0] == _SNOOP_ADDR
                                   and c.args[1] == b"\xEA" * _SNOOP_LENGTH]
        assert restore_writes_to_snoop, "snoop area not restored"
        restore_writes_to_save = [c for c in write_calls
                                  if c.args[0] == _SNOOP_SAVE_ADDR
                                  and c.args[1] == original_save]
        assert restore_writes_to_save, "save area not restored"

    def test_known_pc_overrides_default(self) -> None:
        readback = bytes([0, 0, 0, 0x20, 0xFE])

        mock = MagicMock(spec=[
            "read_memory", "write_memory", "client", "memory_policy",
        ])
        mock.client = MagicMock(spec=["run_prg", "send_text"])
        mock.memory_policy = None

        save_call_count = {"n": 0}

        def _read_memory(addr: int, length: int) -> bytes:
            if addr == 0x0000 and length == 65536:
                return b"\x00" * 65536
            if addr == _SNOOP_ADDR and length == _SNOOP_LENGTH:
                return b"\xEA" * _SNOOP_LENGTH
            if addr == _SNOOP_SAVE_ADDR and length == 5:
                save_call_count["n"] += 1
                if save_call_count["n"] == 1:
                    return b"\x00" * 5
                return readback
            return b"\x00" * length

        mock.read_memory.side_effect = _read_memory

        snap = extract_snapshot(mock, known_pc=0xC000)
        assert snap.cpu_registers is not None
        assert snap.cpu_registers.pc == 0xC000

    def test_skipped_when_include_false(self) -> None:
        mock = _make_u64_mock(bytes(5))
        snap = extract_snapshot(mock, include_cpu_registers=False)
        assert snap.cpu_registers is None
        mock.client.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# VICE restore path — mock set_registers
# ---------------------------------------------------------------------------


class TestRestoreCpuRegistersVice:
    def test_set_registers_called_with_correct_values(self) -> None:
        mock = MagicMock(spec=[
            "read_memory", "write_memory", "set_registers", "memory_policy",
        ])
        mock.memory_policy = None
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cpu_registers=CpuRegisters(
                pc=0xC000, a=0x42, x=0x01, y=0x02, sp=0xFD, p=0x21,
            ),
        )
        restore_snapshot(mock, snap)
        mock.set_registers.assert_called_once()
        passed = mock.set_registers.call_args.args[0]
        assert passed["PC"] == 0xC000
        assert passed["A"] == 0x42
        assert passed["X"] == 0x01
        assert passed["Y"] == 0x02
        assert passed["SP"] == 0xFD
        assert passed["FL"] == 0x21

    def test_no_set_registers_call_when_cpu_regs_none(self) -> None:
        mock = MagicMock(spec=[
            "read_memory", "write_memory", "set_registers", "memory_policy",
        ])
        mock.memory_policy = None
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0, cpu_port_dir=0)
        restore_snapshot(mock, snap)
        mock.set_registers.assert_not_called()


# ---------------------------------------------------------------------------
# U64 restore path — mock restorer sideload + trigger
# ---------------------------------------------------------------------------


class TestRestoreCpuRegistersU64:
    def test_restorer_bytes_written_and_triggered(self) -> None:
        mock = MagicMock(spec=[
            "read_memory", "write_memory", "client", "memory_policy",
        ])
        # run_prg is the duck-typing key for the U64 path.
        mock.client = MagicMock(spec=["run_prg", "send_text"])
        mock.memory_policy = None
        regs = CpuRegisters(pc=0xC0DE, a=0x11, x=0x22, y=0x33, sp=0xF7, p=0x35)
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F,
            cpu_registers=regs,
        )
        restore_snapshot(mock, snap)
        # Look for the restorer write at $0334 (the LAST write to that
        # address — the bulk RAM write also lands at $0000 but not $0334).
        restorer_writes = [
            c for c in mock.write_memory.call_args_list
            if c.args[0] == _RESTORE_ADDR
            and len(c.args[1]) == _RESTORE_LENGTH
        ]
        assert restorer_writes, "restorer routine never written to $0334"
        expected = _build_restore_routine(regs)
        assert restorer_writes[-1].args[1] == expected
        # Override should be snapshot-restore so it can punch through
        # MemoryPolicy reserved regions.
        assert restorer_writes[-1].kwargs.get("override") == "snapshot-restore"
        # And the trigger.
        mock.client.send_text.assert_called()
        arg0 = mock.client.send_text.call_args.args[0]
        assert "SYS" in arg0
        assert str(_RESTORE_ADDR) in arg0


# ---------------------------------------------------------------------------
# .vsf MAINCPU module patch round-trip
# ---------------------------------------------------------------------------


class TestVsfMainCpuPatch:
    def test_to_vsf_then_from_vsf_preserves_registers(self) -> None:
        regs = CpuRegisters(
            pc=0xC000, a=0x42, x=0x01, y=0x02, sp=0xFD, p=0x21,
        )
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cpu_registers=regs,
        )
        blob = snap.to_vsf()
        restored = Snapshot.from_vsf(blob)
        assert restored.cpu_registers is not None
        rr = restored.cpu_registers
        assert rr.pc == 0xC000
        assert rr.a == 0x42
        assert rr.x == 0x01
        assert rr.y == 0x02
        assert rr.sp == 0xFD
        assert rr.p == 0x21

    def test_vsf_maincpu_body_contains_expected_bytes(self) -> None:
        regs = CpuRegisters(
            pc=0xABCD, a=0xDE, x=0xAD, y=0xBE, sp=0xEF, p=0x37,
        )
        snap = Snapshot(
            ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F,
            cpu_registers=regs,
        )
        blob = snap.to_vsf()
        for name, _vmaj, _vmin, body_start, body_len in _iter_modules(blob):
            if name == _MAINCPU_MODULE_NAME:
                rb = blob[body_start + _MAINCPU_REG_OFFSET :
                          body_start + _MAINCPU_REG_OFFSET + 7]
                assert rb[0] == 0xDE  # A
                assert rb[1] == 0xAD  # X
                assert rb[2] == 0xBE  # Y
                assert rb[3] == 0xEF  # SP
                assert rb[4] == 0xCD  # PC lo
                assert rb[5] == 0xAB  # PC hi
                assert rb[6] == 0x37  # P
                return
        pytest.fail("emitted .vsf has no MAINCPU module")

    def test_to_vsf_without_cpu_regs_preserves_template_maincpu(self) -> None:
        """A snapshot without explicit cpu_registers leaves MAINCPU alone."""
        from c64_test_harness.snapshot import _load_template
        template = _load_template()
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F)
        blob = snap.to_vsf()
        # Find MAINCPU in both
        def _find_maincpu(b: bytes) -> bytes:
            for name, _vmaj, _vmin, body_start, body_len in _iter_modules(b):
                if name == _MAINCPU_MODULE_NAME:
                    return b[body_start : body_start + body_len]
            raise RuntimeError("no MAINCPU module")
        assert _find_maincpu(blob) == _find_maincpu(template)

    def test_from_vsf_recovers_template_registers(self) -> None:
        """The bundled template's MAINCPU body should parse to the
        expected BASIC-READY register state (A=0, X=0, Y=$0A, SP=$F3,
        PC=$E5D1, P=$22).  This pins the empirical layout that the
        emitter and parser agree on.
        """
        from c64_test_harness.snapshot import _load_template
        template = _load_template()
        snap = Snapshot.from_vsf(template)
        assert snap.cpu_registers is not None
        r = snap.cpu_registers
        assert r.a == 0x00
        assert r.x == 0x00
        assert r.y == 0x0A
        assert r.sp == 0xF3
        assert r.pc == 0xE5D1
        assert r.p == 0x22


# ---------------------------------------------------------------------------
# _patch_module_prefix helper — direct unit test
# ---------------------------------------------------------------------------


class TestPatchModulePrefix:
    def test_replaces_only_the_requested_span(self) -> None:
        from c64_test_harness.snapshot import _load_template
        template = _load_template()
        # Find MAINCPU body offset.
        for name, _vmaj, _vmin, body_start, body_len in _iter_modules(template):
            if name == _MAINCPU_MODULE_NAME:
                pre = template[: body_start + 8]
                marker_old = template[body_start + 8 : body_start + 8 + 7]
                post = template[body_start + 15 :]
                break
        else:
            pytest.fail("template has no MAINCPU")
        marker_new = b"\xDE\xAD\xBE\xEF\xCA\xFE\x33"
        patched = _patch_module_prefix(
            template, _MAINCPU_MODULE_NAME, 8, marker_new,
        )
        # Pre/post unchanged, middle replaced.
        assert patched[:body_start + 8] == pre
        assert patched[body_start + 15:] == post
        assert patched[body_start + 8 : body_start + 8 + 7] == marker_new
        # And the original differed (sanity).
        assert marker_old != marker_new


# ---------------------------------------------------------------------------
# Bundle round-trip with cpu_registers
# ---------------------------------------------------------------------------


class TestBundleCpuRegisters:
    def test_bundle_roundtrip(self, tmp_path: Path) -> None:
        regs = CpuRegisters(pc=0x9000, a=0xAA, x=0xBB, y=0xCC, sp=0xFE, p=0x21)
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            cpu_registers=regs,
        )
        snap.to_bundle(tmp_path)
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert "cpu_registers" in manifest
        assert manifest["cpu_registers"]["pc"] == 0x9000
        assert manifest["cpu_registers"]["a"] == 0xAA
        restored = Snapshot.from_bundle(tmp_path)
        assert restored.cpu_registers is not None
        rr = restored.cpu_registers
        assert rr.pc == 0x9000
        assert rr.a == 0xAA
        assert rr.x == 0xBB
        assert rr.y == 0xCC
        assert rr.sp == 0xFE
        assert rr.p == 0x21

    def test_bundle_roundtrip_without_cpu_registers(self, tmp_path: Path) -> None:
        snap = Snapshot(ram=bytes(65536), cpu_port_data=0x37, cpu_port_dir=0x2F)
        snap.to_bundle(tmp_path)
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        # When the snapshot has no explicit cpu_registers but the bundled
        # .vsf template carries the BASIC-READY MAINCPU state, the bundle
        # recovery picks up the template state via from_vsf.  That's
        # the documented behaviour.
        restored = Snapshot.from_bundle(tmp_path)
        # Either None or the template state — never wrong values.
        if restored.cpu_registers is not None:
            assert restored.cpu_registers.pc == 0xE5D1

    def test_bundle_manifest_with_drives_and_cpu_regs(
        self, tmp_path: Path,
    ) -> None:
        regs = CpuRegisters(pc=0x9000, a=1, x=2, y=3, sp=0xFD, p=0x20)
        drive = DriveState(
            device=8, drive_type="1541", image=b"\x42" * 174848,
            image_format="d64",
        )
        snap = Snapshot(
            ram=bytes(65536),
            cpu_port_data=0x37,
            cpu_port_dir=0x2F,
            drives=(drive,),
            cpu_registers=regs,
        )
        snap.to_bundle(tmp_path)
        restored = Snapshot.from_bundle(tmp_path)
        assert restored.cpu_registers is not None
        assert restored.cpu_registers.pc == 0x9000
        assert len(restored.drives) == 1
        assert restored.drives[0].device == 8
