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

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backends.unified_manager import TestTarget
    from .backends.vice_binary import BinaryViceTransport

from .execution_policy import ExecutionPolicyError, check_execution_policy
from .memory_policy import MemoryPolicyError
from .transport import TransportError, TimeoutError

_VALID_REGS = {"A", "X", "Y", "SP", "PC"}

#: What hang recovery folds into ``RoutineHung.detail`` instead of letting
#: escape: the wire (TransportError, OSError) and the two policies that
#: can refuse the RTS write or the probe call.  Anything else is a bug in
#: the harness and should surface as itself.
_RECOVERY_ERRORS = (TransportError, OSError, MemoryPolicyError,
                    ExecutionPolicyError)


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


def goto(transport: BinaryViceTransport, addr: int) -> None:
    """Set PC to *addr* and resume CPU execution."""
    transport.set_registers({"PC": addr})
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
) -> dict[str, int]:
    """One trampoline round-trip; the machinery :func:`jsr` documents."""
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
        return wait_for_pc(transport, bp_addr, timeout=timeout)
    finally:
        delete_breakpoint(transport, bp_id)


def _recover_from_hang(
    transport: BinaryViceTransport,
    saved_sp: int,
    scratch_addr: int,
) -> tuple[bool, str, int | None]:
    """Put a machine whose routine never returned back into a callable state.

    Returns ``(recovered, detail, hung_pc)``.  Never raises for the
    failures recovery is there to absorb (:data:`_RECOVERY_ERRORS`);
    each is folded into *detail* for the :class:`RoutineHung` the caller
    is about to raise.
    """
    landing = scratch_addr + 3
    # (a) Read registers.  Binmon services commands while the CPU spins,
    # so this both proves the transport is alive and records where the
    # routine was stuck.
    try:
        hung_pc: int | None = transport.read_registers().get("PC")
    except _RECOVERY_ERRORS as e:
        return False, f"register read after timeout failed: {e!r}", None
    try:
        # (b) Drop the hung routine's frames and the trampoline's return
        # address in one move.
        transport.set_registers({"SP": saved_sp})
        # (c)+(d) Prove the trampoline is live rather than assume it,
        # using only the caller's five bytes: JSR <scratch+4>; NOP; RTS.
        # The JSR must push, the RTS must pop, and the checkpoint on the
        # NOP must fire at the post-RTS landing.  _jsr_once so a probe
        # hang cannot recurse.
        rts_addr = scratch_addr + _PROBE_RTS_OFFSET
        regs = _jsr_once(transport, rts_addr, RECOVERY_PROBE_TIMEOUT,
                         scratch_addr, tail=_TAIL_NOP_RTS)
    except _RECOVERY_ERRORS as e:
        return False, f"recovery probe failed: {e}", hung_pc
    pc = regs.get("PC")
    if pc != landing:
        return (False,
                f"recovery probe stopped at ${pc:04X}, expected the post-RTS "
                f"landing ${landing:04X}",
                hung_pc)
    return (True,
            f"SP restored to ${saved_sp:02X}; RTS probe via ${rts_addr:04X} "
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
) -> dict[str, int]:
    """Call a subroutine at *addr* and wait for it to return.

    Uses a tiny trampoline written at *scratch_addr* (default ``$0334``,
    the C64 cassette buffer — safe after BASIC boot)::

        JSR addr    ; 3 bytes
        NOP         ; 1 byte  <- breakpoint here
        NOP         ; 1 byte

    A checkpoint is placed at *scratch_addr + 3*.  After the subroutine
    executes ``RTS``, execution resumes at the ``NOP`` and the checkpoint
    fires.

    Returns the register state after the subroutine returns.  The CPU is
    paused when this function returns.

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
    safe.  With ``recover_on_timeout=True`` the SP is captured before the
    call and, on timeout, the harness (a) reads the registers, (b)
    restores SP, (c) rewrites the trampoline as ``JSR scratch_addr+4;
    NOP; RTS`` — the never-executed second ``NOP`` becomes the ``RTS``,
    so the probe touches no byte outside the five the caller already
    owns — (d) runs it with :data:`RECOVERY_PROBE_TIMEOUT` and checks
    the CPU stops at the post-``RTS`` landing (*scratch_addr* + 3,
    ``$0337`` by default), then
    (e) raises :class:`RoutineHung` — a ``TimeoutError`` subclass — whose
    ``recovered`` says whether the next call is safe.

    *Invariant this relies on:* the binary monitor services commands
    while the CPU spins — every command pauses the machine — so register
    and memory writes land inside a hung routine and a resume from a new
    PC takes effect.  *Its limit:* recovery re-arms the trampoline at
    *scratch_addr* and probes through it, so a routine that scribbles
    over the cassette buffer (the trampoline, or the ``RTS`` slot)
    defeats it; the probe then reports ``recovered=False`` rather than
    pretending.  A JAM opcode is a different failure altogether — the
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
        return _jsr_once(transport, addr, timeout, scratch_addr)

    saved_sp = transport.read_registers()["SP"]
    started = time.monotonic()
    try:
        return _jsr_once(transport, addr, timeout, scratch_addr)
    except TimeoutError:
        elapsed = time.monotonic() - started
    recovered, detail, hung_pc = _recover_from_hang(
        transport, saved_sp, scratch_addr,
    )
    raise RoutineHung(
        addr, recovered=recovered, elapsed=elapsed, detail=detail,
        hung_pc=hung_pc,
    )


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

    return isinstance(target.transport, Ultimate64Transport)


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
