"""Execution control convenience functions for running 6502 code in VICE.

Stateless functions following the ``memory.py`` pattern — ``transport`` is
always the first argument, no hidden state.

All functions use BinaryViceTransport native methods (checkpoints,
set_registers, wait_for_stopped) for breakpoint and register operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
