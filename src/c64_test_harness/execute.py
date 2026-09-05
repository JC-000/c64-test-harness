"""Execution control convenience functions for running 6502 code in VICE.

Stateless functions following the ``memory.py`` pattern — ``transport`` is
always the first argument, no hidden state.

Most functions use BinaryViceTransport native methods (checkpoints,
set_registers, wait_for_stopped) for breakpoint and register operations.
The cross-backend :func:`run_subroutine` accepts a ``TestTarget`` and
dispatches to ``jsr`` on VICE or a flag-driven trampoline + host poll on
Ultimate 64.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backends.unified_manager import TestTarget
    from .backends.vice_binary import BinaryViceTransport

from .execution_policy import check_execution_policy
from .memory_policy import MemoryPolicyError
from .transport import TransportError, TimeoutError

logger = logging.getLogger(__name__)

_VALID_REGS = {"A", "X", "Y", "SP", "PC"}

#: What hang recovery folds into ``RoutineHung.detail`` instead of letting
#: escape: the wire (TransportError, OSError) and the memory policy, which
#: can refuse the trampoline rewrite.  Not the execution policy: the probe
#: goes through ``_jsr_once``, which never consults it.  Anything else is
#: a bug in the harness and should surface as itself.
_RECOVERY_ERRORS = (TransportError, OSError, MemoryPolicyError)


class RoutineHung(TimeoutError):
    """A routine called through :func:`jsr` never returned (issue #156).

    Raised only when ``jsr(..., recover_on_timeout=True)`` was asked for.
    It subclasses the transport :class:`~.transport.TimeoutError` on
    purpose: every existing ``except TimeoutError`` caller keeps catching
    hangs exactly as before, and a caller that wants to turn the hang into
    a result row and carry on inspects the extra fields.

    :ivar addr: The routine that was called.
    :ivar recovered: ``True`` if the machine was put back into a callable
        state -- SP restored, and a probe ``RTS`` proved the trampoline
        live by landing on its post-``RTS`` breakpoint.  ``False`` means
        the next ``jsr`` on this transport is not safe to attempt; treat
        the boot as lost.
    :ivar elapsed: Wall-clock seconds between resuming into the routine
        and the timeout.
    :ivar detail: What recovery observed -- the probe's landing PC on
        success, the failing step and its exception on failure.
    :ivar hung_pc: Where the CPU was when the timeout fired, or ``None``
        if even that register read failed.
    """

    def __init__(
        self,
        addr: int,
        *,
        recovered: bool,
        elapsed: float,
        detail: str = "",
        hung_pc: int | None = None,
    ) -> None:
        self.addr = addr
        self.recovered = recovered
        self.elapsed = elapsed
        self.detail = detail
        self.hung_pc = hung_pc
        where = f" (CPU at ${hung_pc:04X})" if hung_pc is not None else ""
        state = "recovered" if recovered else "not recovered"
        tail = f": {detail}" if detail else ""
        super().__init__(
            f"routine at ${addr:04X} did not return within {elapsed:.1f}s"
            f"{where}; machine {state}{tail}"
        )

    def __reduce__(self):
        # BaseException's default reduce replays ``cls(*self.args)``, which
        # cannot satisfy the keyword-only signature; exceptions cross
        # process boundaries in run_parallel, so spell out the rebuild.
        return (
            _rebuild_routine_hung,
            (self.addr, self.recovered, self.elapsed, self.detail, self.hung_pc),
        )


def _rebuild_routine_hung(addr, recovered, elapsed, detail, hung_pc):
    """Unpickle target for :class:`RoutineHung` (module-level so it resolves)."""
    return RoutineHung(addr, recovered=recovered, elapsed=elapsed,
                       detail=detail, hung_pc=hung_pc)


def load_code(transport: BinaryViceTransport, addr: int, code: bytes | list[int]) -> None:
    """Write executable code into memory.

    Semantic alias for ``transport.write_memory()`` — makes intent clear
    when loading machine code rather than data.
    """
    transport.write_memory(addr, code)


def set_register(transport: BinaryViceTransport, name: str, value: int) -> None:
    """Set a single CPU register.

    *name* must be one of ``A``, ``X``, ``Y``, ``SP``, or ``PC``
    (case-insensitive).
    """
    name = name.upper()
    if name not in _VALID_REGS:
        raise ValueError(f"Unknown register {name!r}; expected one of {_VALID_REGS}")
    transport.set_registers({name: value})


#: Stack pointer :func:`goto` writes for a cold entry.  What the KERNAL's
#: own reset path leaves before it hands control anywhere (``$FCE2``:
#: ``SEI`` / ``CLD`` / ``LDX #$FF`` / ``TXS``), so the target starts with
#: the whole stack page in front of it rather than the remains of
#: whatever the binary monitor happened to halt.
_COLD_SP = 0xFF

#: Status register :func:`goto` writes for a cold entry.  Bit 5 is the
#: 6502's hardwired unused bit; every other bit is clear.  The two that
#: matter are ``I`` and ``D``:
#:
#: * ``I`` clear, because the monitor's halt can land inside the KERNAL
#:   interrupt handler, whose ``RTI`` then never runs.  Entering a target
#:   with ``I`` still set silently stops the jiffy clock, the keyboard
#:   scan, and any interrupt-driven code in the program under test -- the
#:   severe half of issue #183, which :func:`jsr` avoids by restoring
#:   ``FL`` and :func:`goto` has no return point to restore at.
#: * ``D`` clear, because a halt inside a decimal-mode computation would
#:   otherwise hand the target an ``ADC``/``SBC`` that quietly does BCD.
#:
#: ``N``/``V``/``Z``/``C`` are cleared for definiteness only; a jump
#: target has no business reading them.
_COLD_FL = 0x20


def goto(transport: BinaryViceTransport, addr: int, *, cold: bool = False) -> None:
    """Set PC to *addr* and resume CPU execution.

    Unlike :func:`jsr` this is a one-way jump: control never comes back,
    so there is no point at which anything could be restored.  ``goto()``
    does **not** have the :func:`jsr` defect of issue #183 -- that fix is
    save-and-restore, and a restore needs a moment when control returns.
    Here there is none, and the abandoned frame is what ``goto()`` is
    for, not a defect in it.  (Issue #183's text says otherwise; it is
    wrong, and carries a correction.  See #192.)

    What is true is that by default the target inherits whatever the
    binary monitor happened to halt -- its stack frame and its ``I``
    flag, interrupt handler included.  Two ways out:

    * *cold* ``=True`` writes :data:`_COLD_SP` and :data:`_COLD_FL`
      alongside ``PC`` in the same register write, so the target begins
      with a full stack page, ``I`` clear and ``D`` clear: "start this as
      if nothing was running".  This is **not** :func:`jsr`'s
      ``preserve_state``, which is the opposite request; do not reach for
      one expecting the other.
    * Or rebuild the machine in the target itself.  The warm-start
      ``JMP ($A002)`` idiom rebuilds ``SP`` by re-entering BASIC; see
      ``sid_player.py``, which wants BASIC running afterwards and so
      cannot use *cold*.

    What *cold* does not do, and no register write could:

    * It does not reset the machine.  No KERNAL init, no I/O
      reinitialisation, no zero page, no vectors -- ``$0314``/``$0315``
      and the CIA/VIC state are exactly as the previous program left
      them.
    * It does not acknowledge a pending interrupt.  If the halt landed in
      an IRQ handler that had not yet read ``$DC0D``, the source is still
      asserting, so clearing ``I`` means the target takes that interrupt
      almost immediately, through whatever handler is currently vectored.
      That is the point -- it is how the machine drains the state
      ``goto()`` abandoned -- but a target with a tight timing
      requirement in its first instructions should know it.

    *cold* needs ``SP`` and ``FL`` in the transport's register map.
    ``BinaryViceTransport`` validates every name before it sends
    anything, so a transport that lacks them applies nothing at all: the
    CPU stays halted where it was and ``ValueError`` is raised, rather
    than the jump happening onto a half-built machine.
    """
    regs = {"PC": addr}
    if cold:
        regs["SP"] = _COLD_SP
        regs["FL"] = _COLD_FL
    try:
        transport.set_registers(regs)
    except ValueError as e:
        if not cold:
            raise
        raise ValueError(
            f"goto(cold=True) needs 'SP' and 'FL' in the transport's "
            f"register map; the write was refused and nothing was applied "
            f"({e}).  Use goto(transport, addr) and have the target rebuild "
            f"its own stack."
        ) from e
    transport.resume()


def set_breakpoint(transport: BinaryViceTransport, addr: int) -> int:
    """Set an execution breakpoint at *addr*.

    Returns the checkpoint ID assigned by VICE.
    """
    return transport.set_checkpoint(addr)


def delete_breakpoint(transport: BinaryViceTransport, bp_id: int) -> None:
    """Remove a breakpoint by its checkpoint ID."""
    transport.delete_checkpoint(bp_id)


def wait_for_pc(
    transport: BinaryViceTransport,
    addr: int,
    timeout: float = 5.0,
) -> dict[str, int]:
    """Wait for the CPU to stop at *addr*.

    Uses the binary monitor's async stopped events rather than polling.
    A checkpoint should already be set at *addr* before calling this.

    Returns the register dict when PC matches.  The CPU is **paused**
    at that point, so memory reads are safe.

    Raises ``TimeoutError`` if *addr* is not reached within *timeout*
    seconds.
    """
    pc = transport.wait_for_stopped(timeout=timeout)
    regs = transport.read_registers()
    if regs.get("PC") != addr:
        raise TimeoutError(
            f"PC did not reach ${addr:04X} within {timeout}s "
            f"(stopped at ${pc:04X})"
        )
    return regs


#: Seconds the hang-recovery probe waits for its ``RTS`` to land.  The
#: probe executes three instructions; anything approaching this is the
#: emulator or the transport, not the probe.
RECOVERY_PROBE_TIMEOUT = 5.0

#: Registers ``jsr()`` puts back after a normal (non-hanging) call so
#: that whatever the CPU was doing when the harness hijacked it can carry
#: on.  ``PC`` returns the machine to the instruction the binary monitor
#: happened to halt on; ``SP`` and ``FL`` return the stack pointer and the
#: status register that instruction was running with.
#:
#: Without them, a call issued while the CPU sat inside an interrupt
#: **abandons that interrupt's frame for good**: the hardware's 3-byte
#: ``PCH/PCL/P`` push -- plus whatever the handler pushed on top of it --
#: is never popped, because the ``RTI`` that would pop it never executes,
#: and the ``I`` flag interrupt entry set is never restored.  So IRQs stay
#: masked and the jiffy clock at ``$A0-$A2`` stops.
#:
#: Measured on a stock ``x64sc`` (warp off), 1400 calls per arm, CPU
#: parked in a controlled idle loop, by
#: ``scripts/jsr_mid_irq_occupancy_probe.py`` -- two runs, run 1 / run 2:
#:
#: * Main loop that re-enables interrupts: **16 / 16 of 1400 calls**
#:   (1.14%) were issued while the CPU was halted inside the KERNAL IRQ
#:   handler, and the stack pointer walked down **124 / 126 bytes** --
#:   **7.8 bytes per abandoned frame**, in near-uniform steps of 8.
#: * Main loop that does not re-enable them: *one* such call, at iteration
#:   125 in both runs, masked IRQs permanently; the jiffy clock at
#:   ``$A0-$A2`` stopped after ~250 ticks and never moved again.
#: * Control, ``preserve_state=True`` on the same workload: 18 / 20
#:   mid-IRQ calls and **no SP walk at all**.
#:
#: Halt depth explains the per-event cost: sampled at the moment the
#: monitor halts, an in-handler frame is 3 to 8 bytes deep (median 6) --
#: the hardware's PCH/PCL/P, plus the ``$FF48`` dispatcher's A/X/Y, plus
#: 2 more inside a ``JSR`` the handler made.
#:
#: These figures supersede the 10-call / 119-byte set issue #183
#: published, which is not reproducible: 119 bytes over 10 events implies
#: 11.9 bytes per frame, and the deepest frame this path can abandon is 8.
#: The byte totals of every recorded run agree (119 / 123 / 124 / 126); it
#: is the event count that was low, and the likely reason is
#: classification -- the IRQ-path PCs actually observed include
#: ``$F69D-$F6DC``, ``$EB26-$EB47`` and ``$FFEA``, which a range test
#: written around ``$EA31-$EA87`` would miss.  The probe classifies by
#: whether PC is inside the parked loop instead, so it cannot miss any.
#: See issue #188.
_PRESERVED_REGS = ("PC", "SP", "FL")

#: Trampoline tails.  The normal call parks two NOPs after the JSR; the
#: breakpoint sits on the first, so the second is never executed.  The
#: hang-recovery probe turns that spare byte into an RTS and JSRs it, so
#: the whole probe fits in the five bytes the caller already owns.
_TAIL_NOP_NOP = bytes([0xEA, 0xEA])
_TAIL_NOP_RTS = bytes([0xEA, 0x60])
#: Offset of that spare byte from the trampoline start.
_PROBE_RTS_OFFSET = 4


def _jsr_once(
    transport: BinaryViceTransport,
    addr: int,
    timeout: float,
    scratch_addr: int,
    tail: bytes = _TAIL_NOP_NOP,
    preserve_state: bool = True,
    saved: dict[str, int] | None = None,
) -> dict[str, int]:
    """One trampoline round-trip; the machinery :func:`jsr` documents.

    With *preserve_state* the pre-call ``PC``/``SP``/``FL`` are captured
    before the hijack and put back once the routine has returned -- see
    :data:`_PRESERVED_REGS` for why.  The returned register dict is the
    routine's post-``RTS`` state, read *before* the restore, so callers
    that read ``A``/``X``/``Y``/``PC`` out of it are unaffected.
    """
    if not preserve_state:
        saved = None
    elif saved is None:
        # One extra round trip per call (~1 ms on a local binary monitor).
        # It buys the machine's right to carry on afterwards.  A transport
        # that does not report registers cannot be preserved and is left
        # exactly as it was before: ``read_registers`` is deliberately not
        # part of the cross-backend ``C64Transport`` protocol.
        reader = getattr(transport, "read_registers", None)
        saved = reader() if callable(reader) else None

    # Build trampoline: JSR $xxxx; NOP; NOP   (or NOP; RTS for the probe)
    lo = addr & 0xFF
    hi = (addr >> 8) & 0xFF
    trampoline = bytes([0x20, lo, hi]) + tail
    transport.write_memory(scratch_addr, trampoline)

    bp_addr = scratch_addr + 3
    bp_id = set_breakpoint(transport, bp_addr)
    try:
        transport.set_registers({"PC": scratch_addr})
        transport.resume()
        regs = wait_for_pc(transport, bp_addr, timeout=timeout)
    finally:
        delete_breakpoint(transport, bp_id)

    if saved is not None:
        restore = {k: saved[k] for k in _PRESERVED_REGS if k in saved}
        if restore:
            transport.set_registers(restore)
    return regs


#: The register file recovery puts back, when the transport reports it.
#: SP drops the hung frames; A/X/Y are the caller's; FL undoes a SEI (or
#: SED) the routine executed before hanging -- without it every later
#: test in the boot runs with IRQs off.  PC is deliberately absent: the
#: probe sets it.  ``FL`` is VICE's name for the status register
#: (mon_register6502.c:65); a map without it just restores the rest.
_RESTORED_REGS = ("A", "X", "Y", "SP", "FL")


def _recover_from_hang(
    transport: BinaryViceTransport,
    saved_regs: dict[str, int],
    scratch_addr: int,
) -> tuple[bool, str, int | None]:
    """Put a machine whose routine never returned back into a callable state.

    Returns ``(recovered, detail, hung_pc)``.  Never raises for the
    failures recovery is there to absorb (:data:`_RECOVERY_ERRORS`);
    each is folded into *detail* for the :class:`RoutineHung` the caller
    is about to raise.
    """
    landing = scratch_addr + 3
    restore = {k: saved_regs[k] for k in _RESTORED_REGS if k in saved_regs}
    # (a) Read registers.  Binmon services commands while the CPU spins,
    # so this both proves the transport is alive and records where the
    # routine was stuck.
    try:
        hung_pc: int | None = transport.read_registers().get("PC")
    except _RECOVERY_ERRORS as e:
        return False, f"register read after timeout failed: {e!r}", None
    if "SP" not in restore:
        # Without the pre-call SP there is nothing to unwind the stack to;
        # probing on a leaked frame would "succeed" and hide the leak.
        return (False,
                "transport reports no SP; cannot drop the hung frames",
                hung_pc)
    try:
        # (b) Put the register file back in one command: SP drops the
        # hung routine's frames and the trampoline's return address, FL
        # undoes a SEI, A/X/Y are the caller's again.
        transport.set_registers(restore)
        # (c)+(d) Prove the trampoline is live rather than assume it,
        # using only the caller's five bytes: JSR <scratch+4>; NOP; RTS.
        # The JSR must push, the RTS must pop, and the checkpoint on the
        # NOP must fire at the post-RTS landing.  _jsr_once so a probe
        # hang cannot recurse.
        rts_addr = scratch_addr + _PROBE_RTS_OFFSET
        # preserve_state=False: recovery has just *deliberately* rewritten
        # the register file (step b).  Capturing it again here and putting
        # it back after the probe would restore the hung PC and undo that.
        regs = _jsr_once(transport, rts_addr, RECOVERY_PROBE_TIMEOUT,
                         scratch_addr, tail=_TAIL_NOP_RTS,
                         preserve_state=False)
    except _RECOVERY_ERRORS as e:
        return False, f"recovery probe failed: {e}", hung_pc
    pc = regs.get("PC")
    if pc != landing:
        return (False,
                f"recovery probe stopped at ${pc:04X}, expected the post-RTS "
                f"landing ${landing:04X}",
                hung_pc)
    restored = " ".join(f"{k}=${v:02X}" for k, v in restore.items())
    return (True,
            f"restored {restored}; RTS probe via ${rts_addr:04X} "
            f"landed at ${landing:04X}",
            hung_pc)


def jsr(
    transport: BinaryViceTransport,
    addr: int,
    timeout: float = 5.0,
    *,
    scratch_addr: int = 0x0334,
    override: str | None = None,
    recover_on_timeout: bool = False,
    preserve_state: bool = True,
) -> dict[str, int]:
    """Call a subroutine at *addr* and wait for it to return.

    Uses a tiny trampoline written at *scratch_addr* (default ``$0334``,
    in the unused KERNAL RAM at ``$0334-$033B`` just below the cassette
    buffer — safe after BASIC boot)::

        JSR addr    ; 3 bytes
        NOP         ; 1 byte  <- breakpoint here
        NOP         ; 1 byte

    A checkpoint is placed at *scratch_addr + 3*.  After the subroutine
    executes ``RTS``, execution resumes at the ``NOP`` and the checkpoint
    fires.

    Returns the register state after the subroutine returns.  The CPU is
    paused when this function returns.

    **Not disturbing the machine it hijacked** (*preserve_state*, default
    ``True``).  Forcing ``PC`` at a CPU the binary monitor halted wherever
    it happened to be is destructive when that "wherever" is inside an
    interrupt: the handler never reaches its ``RTI``, so its stack frame
    is abandoned and the ``I`` flag it set is never cleared.  With
    *preserve_state* the pre-call ``PC``, ``SP`` and ``FL`` are read
    before the trampoline is written and put back once the routine has
    returned, so a later :meth:`~.transport.C64Transport.resume` re-enters
    the interrupted instruction stream at the right address, with its
    stack frame and interrupt-disable state intact.  See
    :data:`_PRESERVED_REGS` for the measured cost of not doing this.

    Three limits on that.  ``A``/``X``/``Y`` are **not** put back: they
    hold whatever the called routine left, which is what makes a return
    value in ``A`` readable off the machine.  **Nothing** is put back when
    the call times out -- the restore is deliberately outside the
    ``finally``, because silently unwinding a hung routine would make a
    hard failure look benign; use *recover_on_timeout* for that path.  And
    restoring ``FL``/``SP`` **reverts the called routine's own effect on
    them**: a routine whose point is its ``SEI``/``CLI``, or one that
    rebuilds the stack with ``LDX #$FF / TXS``, needs
    ``preserve_state=False`` or its work is undone on return.

    Two consequences worth knowing.  The **returned dict is still the
    routine's** post-``RTS`` register state -- it is read before the
    restore, so ``regs["A"]`` and friends are unchanged.  And the machine
    is no longer left parked on the trampoline's stale ``NOP``s, which is
    the hazard ``sid_player.play_sid_vice`` works around by hand.  Pass
    ``preserve_state=False`` for the old behaviour: one fewer round trip
    per call, and the CPU left at *scratch_addr* + 3 with the routine's
    own ``SP``/``FL``.

    If the transport carries an :class:`~.execution_policy.ExecutionPolicy`
    and *addr* falls in a span the caller declared dead, this raises
    :class:`~.execution_policy.ExecutionPolicyError` **before anything
    reaches the machine** — no trampoline is written, no breakpoint set,
    no resume issued. That is the point: calling reclaimed code wedges
    the CPU somewhere unpredictable and surfaces only as a bare
    ``TimeoutError``, so the guard has to be prevention rather than
    recovery. ``override="<reason>"`` permits one call and is logged at
    WARNING. Permissive by default: no policy, no behaviour change.

    **Surviving a hang** (issue #156).  When the routine never returns,
    the default behaviour is unchanged: a bare
    :class:`~.transport.TimeoutError` after *timeout* seconds, with the
    CPU still spinning inside the routine and the stack still holding the
    trampoline's return frame — the next ``jsr`` on that transport is not
    safe.  With ``recover_on_timeout=True`` the register file (A, X, Y,
    SP, FL) is captured before the call and, on timeout, the harness (a)
    reads the registers, (b) restores that file in one command — SP
    drops the hung frames, FL undoes a ``SEI`` the routine executed —
    (c) rewrites the trampoline as ``JSR scratch_addr+4;
    NOP; RTS`` — the never-executed second ``NOP`` becomes the ``RTS``,
    so the probe touches no byte outside the five the caller already
    owns — (d) runs it with :data:`RECOVERY_PROBE_TIMEOUT` and checks
    the CPU stops at the post-``RTS`` landing (*scratch_addr* + 3,
    ``$0337`` by default), then
    (e) raises :class:`RoutineHung` — a ``TimeoutError`` subclass — whose
    ``recovered`` says whether the next call is safe.

    *Invariant this relies on:* the binary monitor services commands
    while the CPU spins — every command halts the machine at an
    instruction boundary and it stays halted between commands — so
    register and memory writes land inside a hung routine and a resume
    from a new PC takes effect.  Because the trampoline is rewritten
    while the CPU is halted, a routine that scribbled over it *before*
    hanging is recovered fine.

    *Its limits* are the state recovery does not put back: the ``$01``
    banking register, zero page, and the RAM IRQ/NMI/BRK vectors at
    ``$0314-$0319``.  A routine that hijacked ``$0314`` and hung with I
    clear makes the probe fail (the IRQ fires into the hijack before the
    ``RTS`` lands) — reported as ``recovered=False``, never silent.  A
    routine that banked out RAM under the trampoline or corrupted zero
    page leaves later tests to find that out for themselves.  A stop at
    a PC other than the landing — the caller's own checkpoint inside the
    routine — reaches this path too, because :func:`wait_for_pc` reports
    it as a ``TimeoutError``; the message that distinguishes the two
    ("No stopped event within Ns" versus "PC did not reach ... (stopped
    at $xxxx)") is chained as ``__cause__``.  A JAM opcode is a different failure altogether — the
    transport reports it as a :class:`~.transport.TransportError`, not a
    timeout, and recovery does not engage.

    :param override: Reason string permitting a call into a declared
        dead span. A bare ``True`` is rejected.
    :param recover_on_timeout: Opt in to SP restore + trampoline probe on
        timeout, raising :class:`RoutineHung` instead of a bare
        ``TimeoutError``.  Default ``False``.
    """
    # Guard first — before the trampoline write, so a refused call leaves
    # the machine exactly as it was.
    check_execution_policy(transport, addr, override=override)

    if not recover_on_timeout:
        return _jsr_once(transport, addr, timeout, scratch_addr,
                         preserve_state=preserve_state)

    saved_regs = transport.read_registers()
    started = time.monotonic()
    try:
        # The register file is already in hand; no need to read it twice.
        return _jsr_once(transport, addr, timeout, scratch_addr,
                         preserve_state=preserve_state, saved=saved_regs)
    except TimeoutError as exc:
        elapsed = time.monotonic() - started
        cause = exc  # the name ``exc`` is unbound once the block ends
    recovered, detail, hung_pc = _recover_from_hang(
        transport, saved_regs, scratch_addr,
    )
    raise RoutineHung(
        addr, recovered=recovered, elapsed=elapsed, detail=detail,
        hung_pc=hung_pc,
    ) from cause


# ---------------------------------------------------------------------------
# Cross-backend run_subroutine — VICE jsr() or U64 trampoline + host poll
# ---------------------------------------------------------------------------

# Sentinel-flag values written by the U64 trampoline.
_RUN_FLAG_IDLE = 0x00
_RUN_FLAG_RUNNING = 0x01
_RUN_FLAG_DONE = 0x02


def _build_u64_trampoline(
    target_addr: int,
    running_flag_addr: int,
    done_flag_addr: int,
) -> bytes:
    """Build the 14-byte sentinel trampoline.

    Layout::

        LDA #$01            A9 01
        STA running_flag    8D lo hi
        JSR target          20 lo hi
        LDA #$02            A9 02
        STA done_flag       8D lo hi
        RTS                 60

    The 24-byte budget in #80 is a comfortable upper bound; this fits in
    14 and lives inside the cassette buffer at ``trampoline_addr``.
    """
    tlo = target_addr & 0xFF
    thi = (target_addr >> 8) & 0xFF
    rlo = running_flag_addr & 0xFF
    rhi = (running_flag_addr >> 8) & 0xFF
    dlo = done_flag_addr & 0xFF
    dhi = (done_flag_addr >> 8) & 0xFF
    return bytes([
        0xA9, _RUN_FLAG_RUNNING,    # LDA #$01
        0x8D, rlo, rhi,             # STA running_flag
        0x20, tlo, thi,             # JSR target
        0xA9, _RUN_FLAG_DONE,       # LDA #$02
        0x8D, dlo, dhi,             # STA done_flag
        0x60,                       # RTS
    ])


def _is_u64_target(target: Any) -> bool:
    """Backend dispatch: True if *target* is U64-backed.

    Uses ``isinstance`` against ``Ultimate64Transport`` so duck-typed
    mocks that mimic the U64 surface (i.e. expose a ``.client`` attribute)
    must inherit from the real transport class to be classified as U64.
    Unit tests use ``unittest.mock.Mock(spec=Ultimate64Transport)`` to
    satisfy the check without spinning up real hardware.
    """
    from .backends.ultimate64 import Ultimate64Transport

    # Accept a TestTarget or a bare transport: run_prg_via_sys takes either.
    return isinstance(getattr(target, "transport", target), Ultimate64Transport)


def run_subroutine(
    target: TestTarget,
    addr: int,
    *,
    timeout: float = 30.0,
    poll_cadence: float = 0.005,
    trampoline_addr: int = 0x0360,
    override: str | None = None,
) -> None:
    """Run subroutine at *addr* and wait for it to return. Backend-agnostic.

    VICE
        Thin wrapper around :func:`jsr` — leverages the binary monitor
        checkpoint mechanism for an instant, sub-frame round-trip.

    Ultimate 64
        Installs a small flag-driven trampoline at *trampoline_addr*
        (default ``$0360`` in the cassette buffer; see issue #80) that
        sets a "running" flag, ``JSR``s *addr*, sets a "done" flag, and
        ``RTS``s. The host then triggers the trampoline by injecting
        ``SYS <addr>\\n`` into the keyboard buffer (assumes BASIC READY
        state) and polls the done flag with ``read_memory(done, 1)`` at
        *poll_cadence* seconds.

    Parameters
    ----------
    target:
        A ``TestTarget`` (from ``UnifiedManager.acquire()``).
    addr:
        Address of the 6502 subroutine to invoke. Must end in ``RTS``.
    override:
        Reason string permitting a call into a span declared dead by an
        ``ExecutionPolicy`` on the target's transport. A bare ``True`` is
        rejected. Permissive by default.
    timeout:
        Wall-clock seconds to wait for the subroutine to return. On U64
        a ``TimeoutError`` is raised if the done flag never reaches
        ``0x02`` within this window. Default 30.0.
    poll_cadence:
        Seconds between U64 done-flag polls. Sub-millisecond values are
        permitted (and useful for short routines per #82). Default
        ``0.005`` (5 ms) — a balance for ~100µs–100ms target durations.
        Ignored on VICE.
    trampoline_addr:
        Base address of the U64 trampoline. Default ``$0360`` (cassette
        buffer; safe after BASIC boot). The trampoline is 14 bytes; the
        running and done flag bytes live at ``$03F0``/``$03F1`` by
        default (still in the cassette buffer). Ignored on VICE.

    Raises
    ------
    TimeoutError
        On U64 only, if the done flag never reaches ``0x02`` within
        *timeout*. The exception message includes the elapsed time and
        last-seen flag value, distinguishing "subroutine never started"
        (running flag still ``0x00``) from "subroutine started but
        never returned" (running flag ``0x01`` but done flag never
        ``0x02``).
    TransportError
        Propagated from the underlying transport on hard failures.
    """
    # Guard before backend dispatch: on either backend this ends up
    # JSRing *addr*, and a call into reclaimed code wedges the machine
    # rather than faulting. See jsr() and execution_policy.
    check_execution_policy(getattr(target, "transport", target), addr,
                           override=override)
    if _is_u64_target(target):
        _run_subroutine_u64(
            target,
            addr,
            timeout=timeout,
            poll_cadence=poll_cadence,
            trampoline_addr=trampoline_addr,
        )
        return

    # VICE path — `jsr` requires BinaryViceTransport-shaped transport.
    jsr(target.transport, addr, timeout=timeout, scratch_addr=trampoline_addr)


def _run_subroutine_u64(
    target: TestTarget,
    addr: int,
    *,
    timeout: float,
    poll_cadence: float,
    trampoline_addr: int,
) -> None:
    """U64 trampoline + host-poll implementation of :func:`run_subroutine`."""
    # Flag bytes live in the cassette buffer just past the trampoline.
    # $03F0/$03F1 are well clear of the default $0360 trampoline body
    # and of the BASIC scratch areas in zero-page.
    running_flag_addr = 0x03F0
    done_flag_addr = 0x03F1

    transport = target.transport
    trampoline = _build_u64_trampoline(
        addr, running_flag_addr, done_flag_addr,
    )

    # 1. Clear flags then install the trampoline.
    transport.write_memory(
        running_flag_addr,
        bytes([_RUN_FLAG_IDLE, _RUN_FLAG_IDLE]),
    )
    transport.write_memory(trampoline_addr, trampoline)

    # 2. Trigger the trampoline. We use the harness top-level send_text
    # (which lowers to inject_keys on the transport) so this works
    # regardless of whether Agent B has added a client-level send_text
    # convenience yet.
    from .keyboard import send_text as _send_text

    _send_text(transport, f"SYS {trampoline_addr}\r")

    # 3. Poll the done flag at the configured cadence.
    deadline = time.monotonic() + timeout
    last_flag = _RUN_FLAG_IDLE
    last_running = _RUN_FLAG_IDLE
    while True:
        flag_byte = transport.read_memory(done_flag_addr, 1)
        last_flag = flag_byte[0] if flag_byte else 0
        if last_flag == _RUN_FLAG_DONE:
            return

        now = time.monotonic()
        if now >= deadline:
            elapsed = timeout - (deadline - now)
            running_byte = transport.read_memory(running_flag_addr, 1)
            last_running = running_byte[0] if running_byte else 0
            if last_running == _RUN_FLAG_IDLE:
                # Trampoline never executed — keyboard injection or BASIC
                # state issue, not a stuck subroutine.
                raise TimeoutError(
                    f"run_subroutine: trampoline at ${trampoline_addr:04X} "
                    f"never started after {elapsed:.3f}s "
                    f"(running flag=${last_running:02X}, done flag=${last_flag:02X}); "
                    "is the C64 at BASIC READY?"
                )
            raise TimeoutError(
                f"run_subroutine: subroutine at ${addr:04X} did not return "
                f"within {elapsed:.3f}s "
                f"(running flag=${last_running:02X}, done flag=${last_flag:02X})"
            )

        # Sleep no longer than the cadence, but never past the deadline.
        sleep_for = min(poll_cadence, max(deadline - now, 0.0))
        if sleep_for > 0:
            time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Starting a PRG without losing the expansion port (issue #211)
# ---------------------------------------------------------------------------

#: BASIC tokens for ``SYS`` and ``REM``.
_BASIC_TOKEN_SYS = 0x9E
_BASIC_TOKEN_REM = 0x8F


def parse_basic_sys_address(prg: bytes, *, basic_start: int = 0x0801) -> int | None:
    """Return the address a PRG's BASIC stub ``SYS``es to, or ``None``.

    Walks the tokenised BASIC program the way the interpreter does --
    ``link(2) line#(2) tokens... $00`` per line, stopping at a zero link --
    and looks for the ``SYS`` token (``$9E``) **only inside token bytes**,
    reading its decimal operand (optionally parenthesised).  A PRG whose
    load address is not *basic_start* is not a BASIC program and yields
    ``None`` regardless of its contents.

    The structure matters (adversarial review, 2026-09-05): a search for
    the first ``$9E`` byte anywhere in the body turned a stubless
    machine-code PRG containing ``A2 9E 31 32`` into ``SYS12`` -- a jump
    into zero page -- and mistook line number 158 (stored as ``9E 00``)
    for the token.  ``chr(b).isdigit()`` also accepted ``$B2``/``$B3``/
    ``$B9`` (superscript digits), so the operand parse could raise.
    """
    if len(prg) < 2:
        return None
    load = prg[0] | (prg[1] << 8)
    if load != basic_start:
        return None
    body = prg[2:]
    pos = 0
    while pos + 4 <= len(body):
        link = body[pos] | (body[pos + 1] << 8)
        if link == 0:
            return None
        end = body.find(b"\x00", pos + 4)
        if end < 0:
            return None
        tokens = body[pos + 4:end]
        # Scan the line as the interpreter would: bytes inside quotes and
        # everything after a REM token ($8F) are literal text, not tokens.
        i, in_string = -1, False
        for k, b in enumerate(tokens):
            if b == 0x22:
                in_string = not in_string
            elif in_string:
                continue
            elif b == _BASIC_TOKEN_REM:
                break
            elif b == _BASIC_TOKEN_SYS:
                i = k
                break
        if i >= 0:
            j = i + 1
            while j < len(tokens) and tokens[j] in (0x20, 0x28):   # space, '('
                j += 1
            digits = bytearray()
            while j < len(tokens) and 0x30 <= tokens[j] <= 0x39:
                digits.append(tokens[j])
                j += 1
            return int(digits.decode("ascii")) if digits else None
        nxt = link - load
        if nxt <= pos or nxt > len(body):
            return None
        pos = nxt
    return None


#: How much of the program head :func:`_write_prg_body_verified` re-checks.
_PRG_HEAD_VERIFY_BYTES = 64

#: Seconds after ``READY.`` before a U64 can be trusted not to zero the
#: BASIC program pointer.  The event lands between ~2 s and ~5 s after the
#: banner (see :func:`_write_prg_body_verified`); 6 s leaves a margin.
_U64_POST_READY_SETTLE = 6.0


def _write_prg_body_verified(transport: Any, load_addr: int, body: bytes,
                             *, ready_at: float, settle_after_ready: float,
                             timeout: float = 10.0) -> None:
    """Write *body* at *load_addr* and only return once its head is intact
    **after the machine has settled**.

    On the U64, ``READY.`` appearing on screen is not the machine being
    ready.  A single post-reset event zeroes ``$0801/$0802`` -- BASIC's
    program pointer -- once, between ~2 s and ~5 s after the banner is
    drawn.  The control that established this had no write in it at all:
    stamp ``$DEAD`` at ``$0801``, wait 8 s, read ``00 00``.  It is
    address-specific (the same 6086-byte image at ``$4000`` or ``$C000`` is
    untouched), identical with ``Cartridge Preference`` ``Auto`` and
    ``External``, and unrelated to write length (c64-wireguard, paired
    trials with the no-write control, 2026-09-05).  A write that lands
    before the event loses its first two bytes; one that lands after it
    survives.  Nothing was observed at ``$4000+``; this bench's earlier
    "post-reset RAM-walk clobbers DMA writes at ``$4000+``" note is a
    different phenomenon, if it is one.

    Tracked as issue #216 (filed by the c64-wireguard project).  So a
    single successful read-back is **not** sufficient -- it can pass at
    1 s and be erased at 3 s.  This helper re-verifies the head until
    at least *settle_after_ready* seconds have elapsed since *ready_at*,
    rewriting it whenever it is wrong, and refuses to return (raising
    :class:`TransportError`) if it is still wrong *timeout* seconds after
    the window closed.  With ``settle_after_ready=0`` (VICE, or a caller
    that skipped the reset) one intact read-back suffices.

    The re-verification is the guarantee; the settle constant is only an
    optimisation on top of it.  The 2-5 s window is n=4 on one device and
    one firmware (U64E ``601A96``, ``4011c97c`` / fpga 125), so a bare
    sleep tuned to it would decay silently on another model or firmware --
    do not "simplify" this to a sleep.  No wait when the caller skipped
    the reset, because the event is reset-triggered, not time-triggered:
    the no-reset trial was clean.

    Only the head is re-verified and re-written, because that is where
    every observed clobber landed.  A final full read-back compare raises
    on any other mismatch rather than retrying, since that would be a
    different problem and a retry loop that swallowed it would look like
    it had checked.
    """
    from .memory import write_bytes

    write_bytes(transport, load_addr, body)
    head = body[:_PRG_HEAD_VERIFY_BYTES]
    quiet_after = ready_at + settle_after_ready
    give_up = max(quiet_after, time.monotonic()) + timeout
    rewrites = 0
    while True:
        intact = transport.read_memory(load_addr, len(head)) == head
        now = time.monotonic()
        if intact and now >= quiet_after:
            break
        if not intact:
            if now > give_up:
                raise TransportError(
                    f"program head at ${load_addr:04X} never read back intact: "
                    f"{rewrites} rewrite(s), {timeout}s past the {settle_after_ready}s "
                    "post-READY settle -- something is still clobbering RAM "
                    "(see _write_prg_body_verified)"
                )
            rewrites += 1
            write_bytes(transport, load_addr, head)
            time.sleep(0.5)
        else:
            time.sleep(min(0.5, max(0.05, quiet_after - now)))
    if rewrites:
        logger.warning(
            "run_prg_via_sys: program head at $%04X was clobbered after the "
            "reset and rewritten (%d time(s))", load_addr, rewrites,
        )
    tail_from = len(head)
    if len(body) > tail_from:
        got = transport.read_memory(load_addr + tail_from, len(body) - tail_from)
        if got != body[tail_from:]:
            first = next(i for i, (a, b) in enumerate(zip(got, body[tail_from:])) if a != b)
            raise TransportError(
                f"program body mismatch at ${load_addr + tail_from + first:04X} "
                "after a clean head -- not the known head-clobber; refusing to SYS"
            )

_CARTRIDGE_CATEGORY = "C64 and Cartridge Settings"
_CARTRIDGE_PREFERENCE_ITEM = "Cartridge Preference"


def _reselect_external_cartridge(transport: Any) -> bool:
    """Re-PUT ``Cartridge Preference`` on a U64 if it reads ``External``.

    Why (issue #217, measured on a U64E fw 3.15 fork with an RR-Net, n=3
    per arm): the firmware's runner load path -- ``load_prg`` as much as
    ``run_prg`` -- deselects an external cartridge, and the deselection is
    *sticky*: it survives every ``reset()`` while the config item still
    reads ``External``, and only another PUT of that item (same value)
    re-selects the cartridge.  The PUT alone is enough; no reset is
    needed afterwards.  The REST reset and host DMA writes (REST or
    SocketDMA) never touch it.

    Measured: the PUT leaves the machine running.  With a marker at
    ``$C000`` and the jiffy clock (``$A0-$A2``) read before and ~1 s
    after the PUT, 6/6 trials (3 with the cartridge already selected, 3
    right after a ``run_prg`` had deselected it) kept the marker, advanced
    the jiffy by 64-70 ticks without wrapping to zero, and still showed
    ``READY.``; in the deselected arm the cartridge answered ``$630E``
    after the PUT with no reset, 3/3.  So calling this with
    ``reset=False`` on a machine that is busy does not reset or pause it;
    the firmware's change hook only re-applies the expansion-port
    selection.

    So a single ``client.run_prg`` anywhere in a session would leave every
    later ``run_prg_via_sys`` on this device blind to the cartridge.  One
    GET + one PUT per call buys immunity from that.  Returns ``True`` when
    a PUT was issued.  Best-effort: a config-path failure is logged, not
    raised, because it must not turn a working load into an error on a
    device whose config API is briefly busy.
    """
    client = getattr(transport, "client", None)
    if client is None:
        return False
    try:
        cat = client.get_config_category(_CARTRIDGE_CATEGORY)
        items = cat.get(_CARTRIDGE_CATEGORY) if isinstance(cat, dict) else None
        pref = items.get(_CARTRIDGE_PREFERENCE_ITEM) if isinstance(items, dict) else None
        if pref != "External":
            return False
        client.set_config_item(_CARTRIDGE_CATEGORY, _CARTRIDGE_PREFERENCE_ITEM, pref)
    except Exception as exc:  # noqa: BLE001 -- best-effort by contract
        logger.warning(
            "run_prg_via_sys: could not re-select the external cartridge "
            "(%s: %s). If a prior run_prg/load_prg left it deselected the "
            "program will see $DE00 as an empty slot; remedy: "
            "client.set_config_item('C64 and Cartridge Settings', "
            "'Cartridge Preference', 'External') (issue #217)",
            type(exc).__name__, exc,
        )
        return False
    logger.debug("run_prg_via_sys: re-PUT %s=External (issue #217)",
                 _CARTRIDGE_PREFERENCE_ITEM)
    return True


def run_prg_via_sys(
    target: Any,
    prg: bytes,
    *,
    sys_addr: int | None = None,
    reset: bool = True,
    boot_timeout: float = 25.0,
    verify_timeout: float = 10.0,
    settle_after_ready: float | None = None,
    reselect_cartridge: bool = True,
) -> int:
    """Load *prg* into RAM and start it with a ``SYS`` typed at BASIC.

    The reason this exists rather than
    :meth:`~.ultimate64_client.Ultimate64Client.run_prg`: on the U64,
    after ``run_prg`` an **external cartridge is left deselected**.  A
    program it loads sees the whole ``$DE00`` I/O window as zeros even
    while ``Cartridge Preference`` still reads ``External``, so anything
    driving a cartridge — an RR-Net, most obviously — fails at its first
    register read.  Stock ip65 reports ``INIT DRIVER: FAILED`` that way.
    The cause is the firmware's runner load path itself (issue #217,
    measured n=3 per arm on a U64E fw 3.15 fork): ``load_prg`` alone
    deselects the cartridge just as ``run_prg`` does, the REST reset and
    host DMA writes (REST or SocketDMA) do not, and the deselection is
    sticky -- it survives every ``reset()`` until ``Cartridge Preference``
    is PUT again (same value; no reset needed after the PUT).  Writing
    the program into RAM and typing ``SYS`` leaves the cartridge on the
    bus, and on a U64 this helper re-PUTs the preference first (see
    *reselect_cartridge*) so a neighbour's earlier ``run_prg`` cannot
    leave it deselected for this run.

    Works on either backend: ``write_memory`` plus keystrokes, then a
    resume so the typed line actually runs under VICE.

    :param target: A ``TestTarget`` or a bare transport.
    :param prg: Raw PRG bytes, including the two-byte load address.
    :param sys_addr: Entry point.  Defaults to the address parsed out of
        the PRG's own BASIC stub via :func:`parse_basic_sys_address`.
    :param reset: Reset and wait for ``READY.`` first.  Pass ``False`` if
        the machine is already at a BASIC prompt.
    :param boot_timeout: Seconds to wait for ``READY.`` after the reset.
    :param verify_timeout: Seconds past the settle window to keep
        re-writing the head of the program until it reads back intact
        (see :func:`_write_prg_body_verified`).
    :param settle_after_ready: Seconds after ``READY.`` before the
        program head is trusted.  ``None`` (default) picks
        :data:`_U64_POST_READY_SETTLE` on an Ultimate 64 after a reset --
        the U64 zeroes ``$0801/$0802`` once, 2-5 s after the banner --
        and ``0`` otherwise.
    :param reselect_cartridge: On an Ultimate 64, re-PUT ``Cartridge
        Preference`` when it reads ``External`` before doing anything else
        (one GET, at most one PUT).  This undoes the sticky deselection a
        previous ``client.run_prg``/``load_prg`` leaves behind (issue
        #217).  Measured not to reset or pause the 6510 (marker + jiffy
        clock, 6/6; see :func:`_reselect_external_cartridge`), so it is
        safe with ``reset=False`` too.  Ignored on VICE.  Pass ``False``
        to skip the config round-trip.
    :returns: The SYS address used.
    :raises ValueError: if *prg* is too short, or no entry point was given
        and none could be parsed from the stub.
    :raises TimeoutError: if the machine never reaches ``READY.``.
    :raises TransportError: if the program never reads back intact.
    """
    from .keyboard import send_text
    from .memory import write_bytes
    from .screen import _resume_quietly, wait_for_text

    if len(prg) < 3:
        raise ValueError(f"PRG too short to contain a load address: {len(prg)} bytes")

    transport = getattr(target, "transport", target)

    if sys_addr is None:
        sys_addr = parse_basic_sys_address(prg)
        if sys_addr is None:
            raise ValueError(
                "no SYS token found in the PRG's BASIC stub; pass sys_addr="
                "<entry point> explicitly"
            )

    if reselect_cartridge and _is_u64_target(transport):
        _reselect_external_cartridge(transport)

    if reset:
        transport.reset()
        if wait_for_text(
            transport, "READY.", timeout=boot_timeout, poll_interval=0.3,
            verbose=False,
        ) is None:
            raise TimeoutError(
                f"machine did not reach READY. within {boot_timeout}s; "
                "cannot type SYS"
            )
    ready_at = time.monotonic()
    if settle_after_ready is None:
        settle_after_ready = (
            _U64_POST_READY_SETTLE if reset and _is_u64_target(transport) else 0.0
        )

    load_addr = prg[0] | (prg[1] << 8)
    _write_prg_body_verified(
        transport, load_addr, prg[2:], ready_at=ready_at,
        settle_after_ready=settle_after_ready, timeout=verify_timeout,
    )
    send_text(transport, f"SYS{sys_addr}\r")
    # Under VICE both write_memory and inject_keys are monitor commands
    # and each halts the 6510; without a resume the typed SYS sits in the
    # keyboard buffer on a stopped machine until the caller happens to
    # resume (found by the red/green review, 2026-09-05: a live VICE test
    # that sleeps and then reads a marker fails without this).  The U64
    # halts on neither, which is why the hardware validation never saw it.
    _resume_quietly(transport)
    return sys_addr
