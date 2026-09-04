"""Tests for execution control functions (execute.py)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from c64_test_harness.execute import (
    delete_breakpoint,
    goto,
    jsr,
    load_code,
    run_subroutine,
    set_breakpoint,
    set_register,
    wait_for_pc,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.transport import (
    TimeoutError,
    TransportError,
)
from conftest import MockTransport


class BinaryMockTransport(MockTransport):
    """MockTransport that mimics BinaryViceTransport's checkpoint/register methods."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._next_checkpoint_id = 1
        self._checkpoints: dict[int, int] = {}  # id -> addr
        # Every address ever passed to set_checkpoint, in call order.
        # _checkpoints only holds *live* checkpoints — jsr() deletes its
        # checkpoint before returning, so assertions about "a checkpoint
        # was set at $XXXX" must consult this history instead.
        self._checkpoint_history: list[int] = []
        self._set_registers_calls: list[dict[str, int]] = []
        self._resume_count = 0
        self._stopped_pc: int | None = None  # PC value for wait_for_stopped
        # BinaryViceTransport (VICE-only) exposes CPU registers; mock the
        # same shape here.  read_registers is intentionally not on the
        # cross-backend C64Transport protocol, so the base MockTransport
        # does not carry _registers.
        self._registers: dict[str, int] = {
            "PC": 0x0800, "A": 0, "X": 0, "Y": 0, "SP": 0xFF,
        }

    def read_registers(self) -> dict[str, int]:
        return dict(self._registers)

    def set_registers(self, regs: dict[str, int]) -> None:
        self._set_registers_calls.append(dict(regs))
        for name, value in regs.items():
            self._registers[name.upper()] = value

    def set_checkpoint(self, addr: int, **kwargs) -> int:
        cp_id = self._next_checkpoint_id
        self._next_checkpoint_id += 1
        self._checkpoints[cp_id] = addr
        self._checkpoint_history.append(addr)
        return cp_id

    def delete_checkpoint(self, checkpoint_num: int) -> None:
        self._checkpoints.pop(checkpoint_num, None)

    def resume(self) -> None:
        self._resume_count += 1

    def wait_for_stopped(self, timeout: float | None = None) -> int:
        if self._stopped_pc is not None:
            self._registers["PC"] = self._stopped_pc
            return self._stopped_pc
        raise TimeoutError("No stopped event")


class PollBinaryMockTransport(BinaryMockTransport):
    """BinaryMockTransport where wait_for_stopped returns the checkpoint address."""

    def __init__(self, stop_pc: int, **kwargs):
        super().__init__(**kwargs)
        self._stopped_pc = stop_pc


# -- load_code ---------------------------------------------------------------

def test_load_code_delegates_to_write_memory():
    t = MockTransport()
    load_code(t, 0xC000, b"\xa9\x00\x60")
    assert len(t.written_memory) == 1
    assert t.written_memory[0] == (0xC000, [0xA9, 0x00, 0x60])


def test_load_code_accepts_list():
    t = MockTransport()
    load_code(t, 0xC000, [0xA9, 0x00, 0x60])
    assert len(t.written_memory) == 1
    assert t.written_memory[0] == (0xC000, [0xA9, 0x00, 0x60])


# -- set_register ------------------------------------------------------------

def test_set_register_a():
    t = BinaryMockTransport()
    set_register(t, "A", 0x42)
    assert t._set_registers_calls == [{"A": 0x42}]


def test_set_register_pc():
    t = BinaryMockTransport()
    set_register(t, "PC", 0xC000)
    assert t._set_registers_calls == [{"PC": 0xC000}]


def test_set_register_all_valid():
    t = BinaryMockTransport()
    for reg in ("A", "X", "Y", "SP", "PC"):
        set_register(t, reg, 0)
    assert len(t._set_registers_calls) == 5


def test_set_register_case_insensitive():
    t = BinaryMockTransport()
    set_register(t, "a", 0x10)
    assert t._set_registers_calls == [{"A": 0x10}]


def test_set_register_invalid_raises():
    t = BinaryMockTransport()
    with pytest.raises(ValueError, match="Unknown register"):
        set_register(t, "Z", 0)


# -- goto --------------------------------------------------------------------

class StrictRegisterMockTransport(BinaryMockTransport):
    """A mock that rejects register names the way VICE's map does.

    ``BinaryViceTransport.set_registers`` checks every name against the
    register map VICE reported at connect time and raises *before*
    sending anything.  The permissive base mock accepts whatever it is
    handed, so the "nothing is applied" half of ``goto(cold=True)``
    cannot be exercised against it.  ``FL`` is deliberately absent here:
    the harness already tolerates a map without it (``_RESTORED_REGS``).
    """

    known = frozenset({"PC", "A", "X", "Y", "SP"})

    def set_registers(self, regs: dict[str, int]) -> None:
        for name in regs:
            if name.upper() not in self.known:
                raise ValueError(
                    f"Unknown register {name!r}; known: {sorted(self.known)}"
                )
        super().set_registers(regs)


class OpLogMockTransport(BinaryMockTransport):
    """Records the order of register writes and resumes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ops: list[str] = []

    def set_registers(self, regs: dict[str, int]) -> None:
        super().set_registers(regs)
        self.ops.append("set_registers")

    def resume(self) -> None:
        super().resume()
        self.ops.append("resume")


def test_goto_sets_pc_and_resumes():
    t = BinaryMockTransport()
    goto(t, 0xC000)
    assert t._set_registers_calls == [{"PC": 0xC000}]
    assert t._resume_count == 1


def test_goto_default_leaves_sp_and_flags_alone():
    # The pre-existing contract: a plain goto() touches PC and nothing
    # else, so the target inherits the halted machine's stack and flags.
    t = BinaryMockTransport()
    t._registers["SP"] = 0x78     # descended, as a mid-IRQ halt leaves it
    t._registers["FL"] = 0x26     # I set: issue #183's frozen-jiffy state
    goto(t, 0xC000)
    assert t._registers["SP"] == 0x78
    assert t._registers["FL"] == 0x26


def test_goto_cold_rebuilds_sp_and_clears_i_and_d():
    # cold=True is the opt-in for "start this as if nothing was running":
    # full stack page, IRQs unmasked, binary arithmetic.
    t = BinaryMockTransport()
    t._registers["SP"] = 0x78
    t._registers["FL"] = 0xFF     # every flag set, I and D included
    goto(t, 0xC000, cold=True)
    assert t._registers["PC"] == 0xC000
    assert t._registers["SP"] == 0xFF
    assert t._registers["FL"] & 0x04 == 0, "I must be clear"
    assert t._registers["FL"] & 0x08 == 0, "D must be clear"
    assert t._resume_count == 1


def test_goto_cold_writes_every_register_in_one_command():
    # One wire command, so the machine is never briefly half-built.
    t = BinaryMockTransport()
    goto(t, 0xC000, cold=True)
    assert t._set_registers_calls == [{"PC": 0xC000, "SP": 0xFF, "FL": 0x20}]


def test_goto_cold_applies_the_state_before_resuming():
    # Order is load-bearing: SP written after the resume would land on a
    # CPU already running the target.
    t = OpLogMockTransport()
    goto(t, 0xC000, cold=True)
    assert t.ops == ["set_registers", "resume"]


def test_goto_cold_on_a_transport_without_fl_refuses_and_does_not_resume():
    t = StrictRegisterMockTransport()
    t._registers["SP"] = 0x78
    with pytest.raises(ValueError, match="goto\\(cold=True\\)"):
        goto(t, 0xC000, cold=True)
    # Nothing applied, nothing running: the caller still has a halted
    # machine on its original PC to do something else with.
    assert t._set_registers_calls == []
    assert t._registers["PC"] == 0x0800
    assert t._registers["SP"] == 0x78
    assert t._resume_count == 0


def test_goto_without_cold_does_not_rewrite_a_register_error():
    # The wrapped message is specific to cold=True; an unrelated
    # rejection must reach the caller as the transport phrased it.
    class NoPC(StrictRegisterMockTransport):
        known = frozenset({"A"})

    t = NoPC()
    with pytest.raises(ValueError, match="Unknown register 'PC'") as exc:
        goto(t, 0xC000)
    assert "cold=True" not in str(exc.value)


# -- set_breakpoint ----------------------------------------------------------

def test_set_breakpoint_returns_checkpoint_id():
    t = BinaryMockTransport()
    bp_id = set_breakpoint(t, 0xC000)
    assert bp_id == 1
    assert t._checkpoints == {1: 0xC000}


def test_set_breakpoint_increments_id():
    t = BinaryMockTransport()
    bp1 = set_breakpoint(t, 0xC000)
    bp2 = set_breakpoint(t, 0xC010)
    assert bp1 == 1
    assert bp2 == 2


# -- delete_breakpoint -------------------------------------------------------

def test_delete_breakpoint_removes_checkpoint():
    t = BinaryMockTransport()
    bp_id = set_breakpoint(t, 0xC000)
    delete_breakpoint(t, bp_id)
    assert bp_id not in t._checkpoints


# -- wait_for_pc -------------------------------------------------------------

def test_wait_for_pc_immediate_match():
    t = PollBinaryMockTransport(stop_pc=0xC000)
    t._registers["PC"] = 0xC000
    regs = wait_for_pc(t, 0xC000, timeout=1.0)
    assert regs["PC"] == 0xC000


def test_wait_for_pc_timeout():
    t = BinaryMockTransport()
    # wait_for_stopped will raise TimeoutError
    with pytest.raises(TimeoutError):
        wait_for_pc(t, 0xC000, timeout=0.1)


# -- jsr ---------------------------------------------------------------------

def test_jsr_trampoline_and_breakpoint():
    """Verify jsr writes trampoline, sets checkpoint, sets PC, resumes."""
    t = PollBinaryMockTransport(stop_pc=0x0337)
    t._registers["PC"] = 0x0337

    regs = jsr(t, 0xC000, timeout=1.0, scratch_addr=0x0334)

    # Check trampoline written: JSR $C000 (0x20, 0x00, 0xC0), NOP, NOP
    assert len(t.written_memory) == 1
    addr, data = t.written_memory[0]
    assert addr == 0x0334
    assert data == [0x20, 0x00, 0xC0, 0xEA, 0xEA]

    # Checkpoint was set at scratch_addr + 3 (jsr() deletes it before
    # returning, so check the history, not the live checkpoint table).
    assert 0x0337 in t._checkpoint_history
    # PC was set to scratch_addr
    assert {"PC": 0x0334} in t._set_registers_calls
    # Resume was called
    assert t._resume_count >= 1
    # Checkpoint was cleaned up
    assert len(t._checkpoints) == 0
    assert regs["PC"] == 0x0337


# -- run_subroutine ----------------------------------------------------------
#
# Cross-backend primitive (issues #80, #82). VICE path delegates to jsr();
# U64 path installs a flag-driven trampoline and host-polls the done flag.
# All tests below are mock-based — no live device.


class _ViceLikeTarget:
    """Duck-typed TestTarget with a non-Ultimate64Transport transport.

    Because ``run_subroutine``'s backend dispatch checks
    ``isinstance(target.transport, Ultimate64Transport)``, anything that
    is *not* an Ultimate64Transport routes through the VICE/jsr path.
    """

    def __init__(self, transport):
        self.transport = transport
        self.backend = "vice"


class _U64LikeTarget:
    """Duck-typed TestTarget whose transport satisfies the U64 isinstance check.

    Uses ``MagicMock(spec=Ultimate64Transport)`` so the isinstance check in
    ``run_subroutine`` returns True without needing a live device.
    """

    def __init__(self, *, done_sequence: list[int], running_value: int = 0x01):
        # spec= makes isinstance(mock, Ultimate64Transport) True.
        self.transport = MagicMock(spec=Ultimate64Transport)
        self._done_sequence = list(done_sequence)
        self._running_value = running_value
        self.read_memory_calls: list[tuple[int, int]] = []
        self.write_memory_calls: list[tuple[int, bytes]] = []
        self.inject_keys_calls: list[list[int]] = []

        def _read_memory(address: int, length: int) -> bytes:
            self.read_memory_calls.append((address, length))
            # The implementation reads the running flag at $03F0 only on
            # timeout, and the done flag at $03F1 every poll.
            if address == 0x03F0:
                return bytes([self._running_value])
            if address == 0x03F1:
                if self._done_sequence:
                    val = self._done_sequence.pop(0)
                else:
                    # Stuck — keep returning the last value (or 0).
                    val = 0x00
                return bytes([val])
            return bytes(length)

        def _write_memory(address: int, data) -> None:
            if isinstance(data, list):
                data = bytes(data)
            self.write_memory_calls.append((address, bytes(data)))

        def _inject_keys(codes) -> None:
            self.inject_keys_calls.append(list(codes))

        self.transport.read_memory.side_effect = _read_memory
        self.transport.write_memory.side_effect = _write_memory
        self.transport.inject_keys.side_effect = _inject_keys


def test_run_subroutine_vice_dispatches_to_jsr():
    """VICE-backed target: run_subroutine should call the existing jsr() path,
    which writes the JSR/NOP/NOP trampoline at the configured scratch_addr."""
    t = PollBinaryMockTransport(stop_pc=0x0363)
    t._registers["PC"] = 0x0363
    target = _ViceLikeTarget(t)

    run_subroutine(target, 0xC000, timeout=1.0, trampoline_addr=0x0360)

    # The VICE jsr path writes a 5-byte trampoline at trampoline_addr.
    assert len(t.written_memory) == 1
    addr, data = t.written_memory[0]
    assert addr == 0x0360
    assert data == [0x20, 0x00, 0xC0, 0xEA, 0xEA]
    # PC was steered through the scratch trampoline.
    assert {"PC": 0x0360} in t._set_registers_calls
    assert t._resume_count >= 1


def test_run_subroutine_u64_installs_trampoline_and_polls():
    """U64-backed target: run_subroutine should install the 14-byte trampoline,
    inject ``SYS <addr>\\r`` to trigger it, then poll the done flag at $03F1."""
    target = _U64LikeTarget(done_sequence=[0x00, 0x00, 0x02])

    run_subroutine(
        target, 0xC000,
        timeout=1.0, poll_cadence=0.001, trampoline_addr=0x0360,
    )

    # Two write_memory calls: flag clear, then the trampoline body.
    addrs_written = [addr for addr, _ in target.write_memory_calls]
    assert 0x03F0 in addrs_written, "flag clear write missing"
    assert 0x0360 in addrs_written, "trampoline body not written at trampoline_addr"

    # Verify trampoline bytes: LDA #$01, STA $03F0, JSR $C000, LDA #$02,
    # STA $03F1, RTS.
    tramp_writes = [data for addr, data in target.write_memory_calls if addr == 0x0360]
    assert len(tramp_writes) == 1
    body = tramp_writes[0]
    expected = bytes([
        0xA9, 0x01, 0x8D, 0xF0, 0x03,
        0x20, 0x00, 0xC0,
        0xA9, 0x02, 0x8D, 0xF1, 0x03,
        0x60,
    ])
    assert body == expected

    # Trigger keys were injected (PETSCII for "SYS 864" + return).
    assert target.inject_keys_calls, "no SYS keystrokes injected"

    # Done flag at $03F1 was polled at least until 0x02 appeared.
    done_polls = [c for c in target.read_memory_calls if c == (0x03F1, 1)]
    assert len(done_polls) >= 3


def test_run_subroutine_u64_timeout():
    """If the done flag never reaches 0x02, raise TimeoutError after `timeout`."""
    # Done flag stays at 0x00 forever; running flag = 0x01 (i.e. "started but
    # never returned").
    target = _U64LikeTarget(done_sequence=[0x00] * 1000, running_value=0x01)

    start = time.monotonic()
    with pytest.raises(TimeoutError) as exc:
        run_subroutine(
            target, 0xC000,
            timeout=0.1, poll_cadence=0.005, trampoline_addr=0x0360,
        )
    elapsed = time.monotonic() - start

    # Should respect the timeout (with reasonable slack for sleep granularity).
    assert 0.08 <= elapsed < 1.0
    # Error message distinguishes "stuck mid-call" from "trampoline never ran".
    msg = str(exc.value)
    assert "did not return" in msg
    assert "$01" in msg  # running flag


def test_run_subroutine_u64_timeout_trampoline_never_ran():
    """If the running flag stays at 0x00, surface that as a distinct error."""
    target = _U64LikeTarget(done_sequence=[0x00] * 1000, running_value=0x00)

    with pytest.raises(TimeoutError) as exc:
        run_subroutine(
            target, 0xC000,
            timeout=0.1, poll_cadence=0.005, trampoline_addr=0x0360,
        )

    msg = str(exc.value)
    assert "never started" in msg
    assert "BASIC READY" in msg


def test_run_subroutine_poll_cadence_respected():
    """At a 10ms cadence over ~80ms before completion, we should see roughly
    8 polls (allow generous slack for OS sleep granularity)."""
    # ~12 zeros then a 0x02. With 10ms cadence + some scheduling slop,
    # we expect <= 14 polls and >= 5 polls before completion.
    target = _U64LikeTarget(done_sequence=[0x00] * 12 + [0x02])

    start = time.monotonic()
    run_subroutine(
        target, 0xC000,
        timeout=5.0, poll_cadence=0.010, trampoline_addr=0x0360,
    )
    elapsed = time.monotonic() - start

    done_polls = [c for c in target.read_memory_calls if c == (0x03F1, 1)]
    # At 10ms cadence, 13 polls of done flag should take >= ~80ms but less
    # than the full 5s timeout. The exact count depends on sleep granularity,
    # so we use a loose "at least N polls in M seconds" check per spec.
    assert len(done_polls) >= 5, f"expected at least 5 polls, got {len(done_polls)}"
    # And the run took at least poll_cadence * (polls_before_done - 1) seconds.
    # Lower bound: with 12 zero-polls each followed by at most poll_cadence
    # sleep, we should see > 50ms wall-clock.
    assert elapsed >= 0.05, f"polling completed too fast ({elapsed:.3f}s)"


def test_run_subroutine_custom_trampoline_addr():
    """Passing a non-default trampoline_addr should write the body at that
    address."""
    target = _U64LikeTarget(done_sequence=[0x02])

    run_subroutine(
        target, 0xD000,
        timeout=1.0, poll_cadence=0.001, trampoline_addr=0x0380,
    )

    tramp_writes = [data for addr, data in target.write_memory_calls if addr == 0x0380]
    assert len(tramp_writes) == 1
    body = tramp_writes[0]
    # JSR $D000 in the middle.
    assert body[5:8] == bytes([0x20, 0x00, 0xD0])
    # No write at the default $0360.
    default_writes = [data for addr, data in target.write_memory_calls if addr == 0x0360]
    assert default_writes == []


# -- RoutineHung / jsr(recover_on_timeout=True) ------------------------------
#
# Issue #156: after a hung routine times out, the CPU is still spinning in
# it and the stack carries the trampoline's return frame.  Opt-in recovery
# restores SP and proves the trampoline is live again before re-raising.
# All mock-based; the live counterpart is tests/test_jsr_recovery_live.py.

from c64_test_harness.execute import (  # noqa: E402
    RECOVERY_PROBE_TIMEOUT,
    RoutineHung,
)


def test_routine_hung_is_a_transport_timeout_error():
    """Existing ``except TimeoutError`` callers must keep catching hangs."""
    exc = RoutineHung(0xC100, recovered=True, elapsed=2.0, detail="ok")
    assert isinstance(exc, TimeoutError)
    assert isinstance(exc, TransportError)


def test_routine_hung_carries_recovery_facts():
    # addr != hung_pc so the message's "(CPU at $xxxx)" clause is checked
    # on its own, not satisfied by the routine address.
    exc = RoutineHung(0xC100, recovered=False, elapsed=1.5,
                      detail="recovery jsr timed out", hung_pc=0xC1A0)
    assert exc.addr == 0xC100
    assert exc.recovered is False
    assert exc.elapsed == 1.5
    assert exc.detail == "recovery jsr timed out"
    assert exc.hung_pc == 0xC1A0
    msg = str(exc)
    assert "$C100" in msg
    assert "(CPU at $C1A0)" in msg
    assert "1.5" in msg
    assert "not recovered" in msg
    assert "recovery jsr timed out" in msg


def test_routine_hung_message_says_recovered():
    exc = RoutineHung(0xC100, recovered=True, elapsed=2.0, detail="")
    assert "recovered" in str(exc)
    assert "not recovered" not in str(exc)


class ScriptedStopMockTransport(BinaryMockTransport):
    """BinaryMockTransport whose successive ``wait_for_stopped`` calls follow
    a script.

    Each entry is either an int (the PC to stop at) or an exception to
    raise.  ``hang_sp`` is the SP the mock reports once the first wait has
    raised — a hung routine has pushed frames, so SP is *lower* than it was
    before the call; that is what makes the SP-restore assertion falsifiable.
    """

    def __init__(self, script: list, *, hang_sp: int = 0xF3, **kwargs):
        super().__init__(**kwargs)
        self._script = list(script)
        self._hang_sp = hang_sp
        self.wait_calls: list[float | None] = []

    def wait_for_stopped(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            # A hung routine has been running: SP is lower, A/X/Y hold
            # its temporaries, and it may have SEI'd (I = $04 in FL).
            self._registers.update(
                SP=self._hang_sp, PC=0xC101, A=0x11, X=0x22, Y=0x33,
                FL=self._registers.get("FL", 0x24) | 0x04,
            )
            raise step
        self._registers["PC"] = step
        return step


#: Register file before the hung call, as a VICE read_registers() reports
#: it (FL is the real name of the status register, mon_register6502.c:65).
_PRE_CALL_REGS = {"A": 0xA0, "X": 0xB0, "Y": 0xC0, "SP": 0xF7, "FL": 0x24}


def _hang_then_recover(**kwargs) -> ScriptedStopMockTransport:
    """First jsr hangs; the recovery probe lands on the $0337 breakpoint."""
    t = ScriptedStopMockTransport(
        [TimeoutError("No stopped event within 2.0s"), 0x0337], **kwargs,
    )
    t._registers.update(_PRE_CALL_REGS)
    return t


def _restore_calls(t: BinaryMockTransport) -> list[dict[str, int]]:
    """The set_registers calls that touched SP -- recovery's restore."""
    return [c for c in t._set_registers_calls if "SP" in c]


def test_jsr_default_timeout_is_a_plain_timeout_error_and_does_not_recover():
    """recover_on_timeout defaults to False: behaviour is exactly today's."""
    t = _hang_then_recover()
    with pytest.raises(TimeoutError) as excinfo:
        jsr(t, 0xC100, timeout=2.0)
    assert not isinstance(excinfo.value, RoutineHung)
    # One trampoline write, no RTS probe, no SP restore, one resume.
    assert t.written_memory == [(0x0334, [0x20, 0x00, 0xC1, 0xEA, 0xEA])]
    assert not any("SP" in call for call in t._set_registers_calls)
    assert t._resume_count == 1
    assert len(t.wait_calls) == 1
    assert len(t._checkpoints) == 0


def test_jsr_recover_on_timeout_success():
    t = _hang_then_recover()
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value
    assert exc.recovered is True
    assert exc.addr == 0xC100
    assert exc.hung_pc == 0xC101
    assert exc.elapsed >= 0.0
    assert "$0337" in exc.detail

    # (b) The register file restored to what was captured before the call,
    # in one set_registers, after the hang had changed every one of them.
    assert _restore_calls(t) == [_PRE_CALL_REGS]
    assert t._registers["SP"] == 0xF7
    assert t._registers["FL"] == 0x24, "I flag left set by the SEI'd hang"
    # (c)+(d) the probe reuses the five trampoline bytes and nothing else:
    # JSR $0338; NOP; RTS -- the second NOP becomes the RTS, the JSR
    # targets it, and the checkpoint on the first NOP catches the return.
    assert t.written_memory == [
        (0x0334, [0x20, 0x00, 0xC1, 0xEA, 0xEA]),   # the hung call
        (0x0334, [0x20, 0x38, 0x03, 0xEA, 0x60]),   # the probe
    ]
    assert t._checkpoint_history == [0x0337, 0x0337]
    assert t._resume_count == 2
    # The probe used its own short timeout, not the caller's 2.0.
    assert t.wait_calls == [2.0, RECOVERY_PROBE_TIMEOUT]
    # Nothing left armed.
    assert len(t._checkpoints) == 0


def test_jsr_recover_on_timeout_restores_sp_before_probing():
    """SP must be restored *before* the probe JSR pushes its own frame."""
    t = _hang_then_recover()
    with pytest.raises(RoutineHung):
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    calls = t._set_registers_calls
    sp_idx = calls.index(_restore_calls(t)[0])
    probe_pc_idx = max(i for i, c in enumerate(calls) if c == {"PC": 0x0334})
    assert sp_idx < probe_pc_idx


def test_jsr_recover_on_timeout_restores_only_registers_the_transport_reports():
    """A transport whose read_registers lacks FL (older map) still gets SP,
    A, X, Y back; recovery must not invent a register the wire cannot set."""
    t = _hang_then_recover()
    del t._registers["FL"]
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    assert excinfo.value.recovered is True
    expected = {k: v for k, v in _PRE_CALL_REGS.items() if k != "FL"}
    assert _restore_calls(t) == [expected]


def test_jsr_after_recovery_works_on_same_transport():
    t = ScriptedStopMockTransport(
        [TimeoutError("No stopped event within 2.0s"), 0x0337, 0x0337],
    )
    t._registers["SP"] = 0xF7
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    assert excinfo.value.recovered is True

    regs = jsr(t, 0xC110, timeout=1.0)
    assert regs["PC"] == 0x0337
    assert t.written_memory[-1] == (0x0334, [0x20, 0x10, 0xC1, 0xEA, 0xEA])


def test_jsr_recover_on_timeout_probe_times_out():
    t = ScriptedStopMockTransport([
        TimeoutError("No stopped event within 2.0s"),
        TimeoutError("No stopped event within 5.0s"),
    ])
    t._registers["SP"] = 0xF7
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value
    assert exc.recovered is False
    assert exc.addr == 0xC100
    assert "No stopped event within 5.0s" in exc.detail
    assert "not recovered" in str(exc)
    assert len(t._checkpoints) == 0


def test_jsr_recover_on_timeout_probe_lands_elsewhere():
    """The probe stopping anywhere but the post-RTS landing is a failure."""
    t = ScriptedStopMockTransport([
        TimeoutError("No stopped event within 2.0s"), 0x1234,
    ])
    t._registers["SP"] = 0xF7
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value
    assert exc.recovered is False
    assert "$1234" in exc.detail
    assert "$0337" in exc.detail


def test_jsr_recover_on_timeout_register_read_failure():
    """If binmon does not answer, recovery reports that step and stops."""
    t = _hang_then_recover()
    calls = {"n": 0}
    real_read = t.read_registers

    def flaky_read():
        calls["n"] += 1
        # First read is the pre-call SP capture; the second is the
        # post-timeout read that the docstring's invariant says must work.
        if calls["n"] == 2:
            raise TransportError("socket closed")
        return real_read()

    t.read_registers = flaky_read
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value
    assert exc.recovered is False
    assert exc.hung_pc is None
    assert "socket closed" in exc.detail
    # No probe was attempted on a machine that does not answer: the only
    # write is the hung call's own trampoline.
    assert len(t.written_memory) == 1
    assert t._resume_count == 1


def test_jsr_recovery_writes_nothing_outside_the_callers_trampoline():
    """A custom scratch_addr is the only memory recovery may touch: every
    byte written during the whole call lies in [scratch_addr, scratch_addr+5).
    The harness claims no second slot for the RTS."""
    scratch = 0x0360
    t = ScriptedStopMockTransport(
        [TimeoutError("No stopped event within 2.0s"), scratch + 3],
    )
    t._registers["SP"] = 0xF7
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, scratch_addr=scratch,
            recover_on_timeout=True)
    assert excinfo.value.recovered is True
    assert len(t.written_memory) == 2
    for addr, data in t.written_memory:
        assert addr == scratch
        assert scratch <= addr and addr + len(data) <= scratch + 5, (
            f"write at ${addr:04X} x{len(data)} escapes the trampoline"
        )
    assert f"${scratch + 3:04X}" in excinfo.value.detail


def test_jsr_recovery_trampoline_is_jsr_to_its_own_rts():
    """The probe trampoline is exactly ``20 lo hi EA 60`` with the JSR
    target at scratch_addr + 4 -- the RTS that replaced the second NOP."""
    scratch = 0x0360
    t = ScriptedStopMockTransport(
        [TimeoutError("No stopped event within 2.0s"), scratch + 3],
    )
    t._registers["SP"] = 0xF7
    with pytest.raises(RoutineHung):
        jsr(t, 0xC100, timeout=2.0, scratch_addr=scratch,
            recover_on_timeout=True)
    target = scratch + 4
    assert t.written_memory[-1] == (
        scratch, [0x20, target & 0xFF, target >> 8, 0xEA, 0x60],
    )
    assert t.written_memory[-1] == (0x0360, [0x20, 0x64, 0x03, 0xEA, 0x60])
    # Probe PC was steered to the trampoline start; landing is +3.
    assert t._set_registers_calls[-1] == {"PC": scratch}
    assert t._checkpoint_history[-1] == scratch + 3


def test_routine_hung_is_exported_from_package_root():
    import c64_test_harness as pkg

    assert "RoutineHung" in pkg.__all__
    assert pkg.RoutineHung is RoutineHung


def test_package_root_exports_have_no_duplicate_names():
    """Thousands of downstream tests import from the root; a re-export
    that shadows an existing name would silently swap behaviour.  Walk the
    AST rather than the runtime namespace: at runtime a duplicate has
    already won, so nothing is left to detect."""
    import ast
    import collections
    import c64_test_harness as pkg

    tree = ast.parse(open(pkg.__file__, encoding="utf-8").read())
    bound = collections.Counter()
    all_names = collections.Counter()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound[alias.asname or alias.name] += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    for elt in node.value.elts:
                        all_names[elt.value] += 1
    assert bound["RoutineHung"] == 1
    dup_bound = sorted(n for n, c in bound.items() if c > 1)
    dup_all = sorted(n for n, c in all_names.items() if c > 1)
    assert dup_bound == [], f"names imported more than once: {dup_bound}"
    assert dup_all == [], f"names listed in __all__ more than once: {dup_all}"
    # Every re-export is advertised, and vice versa.
    assert set(all_names) - {"__version__"} <= set(bound)


# -- mutation survivors from review (S4) --------------------------------------

def test_jsr_recover_on_timeout_does_not_engage_for_a_jam():
    """A JAM is a TransportError, not a timeout: the docstring promises
    recovery does not engage.  Kills the mutation ``except TimeoutError``
    -> ``except TransportError`` in jsr()."""
    # The 0x0337 is bait: if recovery wrongly engages, its probe "lands"
    # and the failure is the RoutineHung assertion below, not an
    # exhausted script.
    t = ScriptedStopMockTransport(
        [TransportError("The 6510 jammed at $c100"), 0x0337],
    )
    t._registers.update(_PRE_CALL_REGS)
    with pytest.raises(TransportError) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    assert not isinstance(excinfo.value, RoutineHung)
    assert _restore_calls(t) == []
    assert t._resume_count == 1
    assert len(t.written_memory) == 1


def test_jsr_recovery_lets_a_harness_bug_escape_rather_than_folding_it():
    """Only the wire and the policies are folded into RoutineHung.detail.
    A ValueError from set_registers is a harness bug and must surface as
    itself.  Kills the mutation ``_RECOVERY_ERRORS = (Exception,)``."""
    t = _hang_then_recover()
    real_set = t.set_registers

    def bad_set(regs):
        if "SP" in regs:
            raise ValueError("Unknown register 'FL'")
        real_set(regs)

    t.set_registers = bad_set
    with pytest.raises(ValueError, match="Unknown register"):
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)


# -- review nits --------------------------------------------------------------

def test_routine_hung_survives_a_pickle_round_trip():
    """Exceptions cross process boundaries in run_parallel and in pytest's
    xdist-style reporting; a kw-only __init__ breaks the default reduce."""
    import pickle

    exc = RoutineHung(0xC100, recovered=False, elapsed=1.5,
                      detail="recovery probe failed: x", hung_pc=0xC1A0)
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is RoutineHung
    assert (back.addr, back.recovered, back.elapsed, back.detail, back.hung_pc) == (
        0xC100, False, 1.5, "recovery probe failed: x", 0xC1A0,
    )
    assert str(back) == str(exc)


def test_routine_hung_chains_the_original_timeout_as_cause():
    """The transport's own message ('No stopped event within 2.0s', or
    wait_for_pc's 'PC did not reach ... (stopped at $xxxx)' when a user
    checkpoint fired inside the routine) must not be lost."""
    t = _hang_then_recover()
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    cause = excinfo.value.__cause__
    assert isinstance(cause, TimeoutError)
    assert not isinstance(cause, RoutineHung)
    assert str(cause) == "No stopped event within 2.0s"


def test_jsr_recover_on_timeout_with_a_routine_that_returns_is_a_plain_call():
    """The opt-in must cost nothing when the routine behaves: one trampoline
    write, one resume, no *recovery*, the normal return value.

    ``preserve_state`` (default) does put PC/SP/FL back afterwards -- that
    is a plain call's behaviour now, not recovery's -- so the assertion is
    that no *probe* ran, not that no register was written.
    """
    t = PollBinaryMockTransport(stop_pc=0x0337)
    t._registers.update(_PRE_CALL_REGS)

    regs = jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)

    assert regs["PC"] == 0x0337
    assert t.written_memory == [(0x0334, [0x20, 0x00, 0xC1, 0xEA, 0xEA])]
    # The only SP-bearing write is preserve_state's restore of the
    # pre-call file; recovery's restore would also carry A/X/Y.
    assert _restore_calls(t) == [
        {"PC": _PRE_CALL_REGS.get("PC", 0x0800), "SP": 0xF7, "FL": 0x24},
    ]
    assert t._set_registers_calls[0] == {"PC": 0x0334}
    assert t._resume_count == 1
    assert len(t._checkpoints) == 0


def test_jsr_recovery_folds_a_memory_policy_refusal_into_detail():
    """A MemoryPolicy that refuses the probe's trampoline rewrite (the
    second write_memory of the call) is a recovery failure to report, not
    an exception to escape.  Kills ``_RECOVERY_ERRORS = (TransportError,)``."""
    from c64_test_harness.memory_policy import MemoryPolicyError

    t = _hang_then_recover()
    real_write = t.write_memory
    seen = {"writes": 0}

    def guarded(*args, **kwargs):
        seen["writes"] += 1
        if seen["writes"] == 2:
            raise MemoryPolicyError(0x0334, 5, "refused by policy")
        real_write(*args, **kwargs)

    t.write_memory = guarded
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value
    assert exc.recovered is False
    assert "refused by policy" in exc.detail
    assert seen["writes"] == 2


def test_routine_hung_elapsed_is_wall_clock_from_resume_to_timeout(monkeypatch):
    import types
    import c64_test_harness.execute as ex

    ticks = iter([100.0, 102.5])
    monkeypatch.setattr(
        ex, "time", types.SimpleNamespace(monotonic=lambda: next(ticks),
                                          sleep=time.sleep),
    )
    t = _hang_then_recover()
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    assert excinfo.value.elapsed == 2.5


def test_jsr_recovery_refuses_when_the_transport_reports_no_sp():
    """Without a captured SP there is nothing to restore the stack to;
    recovery must say so rather than probe on a leaked frame."""
    t = _hang_then_recover()
    del t._registers["SP"]
    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, 0xC100, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value
    assert exc.recovered is False
    assert "transport reports no SP; cannot drop the hung frames" in exc.detail
    assert exc.hung_pc == 0xC101
    # Nothing was attempted: no restore, no probe, one resume.
    assert _restore_calls(t) == []
    assert t._resume_count == 1
    assert len(t.written_memory) == 1


# -- jsr() must not abandon the interrupt it hijacked -------------------------
#
# ``jsr()`` forces PC at a CPU the binary monitor halted wherever it
# happened to be.  When that "wherever" is inside an interrupt handler,
# forcing PC means the handler never reaches its RTI: its stack frame is
# abandoned and the I flag interrupt entry set is never cleared.
#
# Measured on a stock x64sc (warp off), 1400 calls per arm against a
# controlled idle loop, by scripts/jsr_mid_irq_occupancy_probe.py -- two
# runs, figures given as run 1 / run 2:
#   * main loop with CLI: 16 / 16 calls (1.14%) were issued while the CPU
#     was halted inside the KERNAL IRQ handler; SP walked 124 / 126 bytes,
#     i.e. 7.8 bytes per abandoned frame.
#   * main loop without CLI: one such call, at iteration 125 in both runs,
#     masked IRQs for good -- the jiffy clock at $A0-$A2 stopped after
#     ~250 ticks and never moved again.
#   * control arm, preserve_state=True on the same workload: 18 / 20
#     mid-IRQ calls, SP walk zero.
# These supersede the 10-call / 119-byte figures #183 published; see #188
# and the probe's own docstring for why that run undercounted the events.


class FlagBinaryMockTransport(PollBinaryMockTransport):
    """PollBinaryMockTransport whose register file also carries ``FL``.

    ``BinaryViceTransport`` reports the 6502 status register as ``FL``
    (VICE's own name for it, ``mon_register6502.c:65``); the base mock
    omits it, and the abandoned-interrupt bug is as much about ``FL`` as
    about ``SP``.
    """

    def __init__(self, stop_pc: int = 0x0337, **kwargs):
        super().__init__(stop_pc=stop_pc, **kwargs)
        self._registers["FL"] = 0x20

    def park_inside_irq(self) -> None:
        """Pose as a CPU halted inside the KERNAL IRQ handler.

        ``$EA7B`` is inside the standard handler; ``I`` is set (``$24``)
        because interrupt entry set it; ``SP`` already carries the
        hardware's PCH/PCL/P push plus the ``$FF48`` dispatcher's A/X/Y.
        """
        self._registers.update({"PC": 0xEA7B, "SP": 0xF2, "FL": 0x24})

    def wait_for_stopped(self, timeout: float | None = None) -> int:
        # Pose as the routine having run: it leaves its own A behind, and
        # its own (balanced, but different) SP/FL.
        pc = super().wait_for_stopped(timeout=timeout)
        self._registers.update({"A": 0x42, "SP": 0xFD, "FL": 0x33})
        return pc


def test_jsr_restores_the_interrupted_pc_sp_and_flags():
    """A call issued mid-IRQ must leave the machine able to finish it."""
    t = FlagBinaryMockTransport()
    t.park_inside_irq()

    jsr(t, 0xC000, timeout=1.0)

    assert t._set_registers_calls[-1] == {"PC": 0xEA7B, "SP": 0xF2, "FL": 0x24}
    # And the live register file agrees: resume() would re-enter the
    # handler at $EA7B with its frame intact and I still set.
    assert t._registers["PC"] == 0xEA7B
    assert t._registers["SP"] == 0xF2
    assert t._registers["FL"] == 0x24


def test_jsr_returns_the_routines_registers_not_the_restored_ones():
    """The restore must not corrupt what callers read out of jsr()."""
    t = FlagBinaryMockTransport()
    t.park_inside_irq()

    regs = jsr(t, 0xC000, timeout=1.0)

    assert regs["PC"] == 0x0337   # the post-RTS landing, as documented
    assert regs["A"] == 0x42      # the routine's accumulator
    assert regs["SP"] == 0xFD     # the routine's SP, not the restored $F2

    # The three asserts above hold with or without a restore, so on their
    # own they prove nothing.  What only a restore can produce is
    # *divergence*: the dict on the routine's register file, the machine
    # back on the pre-call one.
    assert t._registers["SP"] == 0xF2, "machine was not restored"
    assert regs["SP"] != t._registers["SP"], "restore reached into the dict"


def test_jsr_preserve_state_false_keeps_the_old_behaviour():
    t = FlagBinaryMockTransport()
    t.park_inside_irq()

    jsr(t, 0xC000, timeout=1.0, preserve_state=False)

    # Only the hijack itself; nothing put back, CPU left on the trampoline.
    assert t._set_registers_calls == [{"PC": 0x0334}]
    assert t._registers["PC"] == 0x0337


def test_jsr_restores_only_registers_the_transport_reports():
    """A transport without FL still gets PC and SP back."""
    t = PollBinaryMockTransport(stop_pc=0x0337)   # no FL in _registers
    t._registers.update({"PC": 0xEA7B, "SP": 0xF2})

    jsr(t, 0xC000, timeout=1.0)

    assert t._set_registers_calls[-1] == {"PC": 0xEA7B, "SP": 0xF2}


class WalkingStackMockTransport(FlagBinaryMockTransport):
    """A mock whose stack actually descends when a hijack abandons a frame.

    ``FlagBinaryMockTransport`` re-poses ``SP`` on every
    ``wait_for_stopped``, which makes accumulation impossible to observe:
    every iteration starts from the same number whatever the code under
    test did.  Here the hijack -- a ``PC``-only ``set_registers`` -- costs
    six bytes, the hardware's ``PCH``/``PCL``/``P`` push plus the
    ``$FF48`` dispatcher's ``A``/``X``/``Y``, and the balanced
    ``JSR``/``RTS`` gives none of them back.  Only the restore does.
    """

    def set_registers(self, regs: dict[str, int]) -> None:
        super().set_registers(regs)
        if set(regs) == {"PC"}:
            self._registers["SP"] = (self._registers["SP"] - 6) & 0xFF

    def wait_for_stopped(self, timeout: float | None = None) -> int:
        # Deliberately skips the parent's SP pose: a balanced call leaves
        # SP where the hijack left it, six bytes down.
        pc = PollBinaryMockTransport.wait_for_stopped(self, timeout=timeout)
        self._registers.update({"A": 0x42, "FL": 0x33})
        return pc


def test_jsr_over_many_calls_does_not_walk_the_stack_pointer_down():
    """The regression the measurement found, in miniature.

    The machine is parked mid-IRQ *once*.  Each call abandons a frame and
    only the restore puts SP back, so without it the pointer descends six
    bytes per call and never recovers -- which is the measured bug.
    """
    t = WalkingStackMockTransport()
    t.park_inside_irq()

    for i in range(20):
        jsr(t, 0xC000, timeout=1.0)
        assert t._registers["SP"] == 0xF2, f"SP walked at iteration {i}"
        assert t._registers["FL"] & 0x04, f"I flag lost at iteration {i}"
