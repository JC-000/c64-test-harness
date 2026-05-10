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

from .transport import TransportError, TimeoutError

_VALID_REGS = {"A", "X", "Y", "SP", "PC"}


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


def jsr(
    transport: BinaryViceTransport,
    addr: int,
    timeout: float = 5.0,
    *,
    scratch_addr: int = 0x0334,
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
    """
    # Build trampoline: JSR $xxxx; NOP; NOP
    lo = addr & 0xFF
    hi = (addr >> 8) & 0xFF
    trampoline = bytes([0x20, lo, hi, 0xEA, 0xEA])  # JSR, NOP, NOP
    transport.write_memory(scratch_addr, trampoline)

    bp_addr = scratch_addr + 3
    bp_id = set_breakpoint(transport, bp_addr)
    try:
        transport.set_registers({"PC": scratch_addr})
        transport.resume()
        return wait_for_pc(transport, bp_addr, timeout=timeout)
    finally:
        delete_breakpoint(transport, bp_id)


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
