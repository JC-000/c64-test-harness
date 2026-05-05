"""Execution control convenience functions for running 6502 code in VICE.

Stateless functions following the ``memory.py`` pattern — ``transport`` is
always the first argument, no hidden state.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

from .transport import TransportError, TimeoutError, ConnectionError as TransportConnectionError

_VALID_REGS = {"A", "X", "Y", "SP", "PC"}


def load_code(transport: C64Transport, addr: int, code: bytes | list[int]) -> None:
    """Write executable code into memory.

    Semantic alias for ``transport.write_memory()`` — makes intent clear
    when loading machine code rather than data.
    """
    transport.write_memory(addr, code)


def set_register(transport: C64Transport, name: str, value: int) -> None:
    """Set a single CPU register via the VICE monitor.

    *name* must be one of ``A``, ``X``, ``Y``, ``SP``, or ``PC``
    (case-insensitive).
    """
    name = name.upper()
    if name not in _VALID_REGS:
        raise ValueError(f"Unknown register {name!r}; expected one of {_VALID_REGS}")
    transport.raw_command(f"r {name} = ${value:02x}" if name != "PC"
                          else f"r PC = ${value:04x}")


def goto(transport: C64Transport, addr: int) -> None:
    """Set PC to *addr* and resume CPU execution.

    With VICE's remote monitor, the CPU resumes automatically when the
    TCP connection from ``set_register`` closes — no explicit ``resume()``
    is needed.  An extra ``resume()`` would re-enter the monitor and
    immediately exit, potentially skipping past a breakpoint.
    """
    set_register(transport, "PC", addr)


def set_breakpoint(transport: C64Transport, addr: int) -> int:
    """Set an execution breakpoint at *addr*.

    Returns the breakpoint ID assigned by VICE.
    """
    resp = transport.raw_command(f"break ${addr:04x}")
    m = re.search(r"BREAK:\s+(\d+)", resp)
    if not m:
        raise TransportError(f"Failed to parse breakpoint response: {resp!r}")
    return int(m.group(1))


def delete_breakpoint(transport: C64Transport, bp_id: int) -> None:
    """Remove a breakpoint by its ID."""
    transport.raw_command(f"delete {bp_id}")


def wait_for_pc(
    transport: C64Transport,
    addr: int,
    timeout: float = 5.0,
    poll_interval: float = 0.2,
) -> dict[str, int]:
    """Poll registers until PC equals *addr*.

    Each ``read_registers()`` call pauses the CPU (VICE text monitor
    behaviour).  When PC doesn't match, ``resume()`` is called so the
    CPU can continue executing toward the target address.

    Returns the register dict when PC matches.  The CPU is **paused**
    at that point, so memory reads are safe.

    Raises ``TimeoutError`` if *addr* is not reached within *timeout*
    seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            regs = transport.read_registers()
        except TransportConnectionError:
            # VICE monitor port may not be ready yet (e.g. right after
            # resume, before a breakpoint fires and re-opens the port).
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"PC did not reach ${addr:04X} within {timeout}s "
                    f"(could not connect to monitor)"
                )
            time.sleep(poll_interval)
            continue
        if regs.get("PC") == addr:
            return regs
        if time.monotonic() >= deadline:
            pc = regs.get("PC")
            pc_str = f"${pc:04X}" if pc is not None else "unknown"
            raise TimeoutError(
                f"PC did not reach ${addr:04X} within {timeout}s "
                f"(last PC={pc_str})"
            )
        transport.resume()
        time.sleep(poll_interval)


def jsr(
    transport: C64Transport,
    addr: int,
    timeout: float = 5.0,
    *,
    scratch_addr: int = 0x0334,
    poll_interval: float = 0.2,
) -> dict[str, int]:
    """Call a subroutine at *addr* and wait for it to return.

    Uses a tiny trampoline written at *scratch_addr* (default ``$0334``,
    the C64 cassette buffer — safe after BASIC boot)::

        JSR addr    ; 3 bytes
        NOP         ; 1 byte  <- breakpoint here
        NOP         ; 1 byte

    A breakpoint is placed at *scratch_addr + 3*.  After the subroutine
    executes ``RTS``, execution resumes at the ``NOP`` and the breakpoint
    fires.

    *poll_interval* controls how often registers are polled while waiting
    for the subroutine to return.  Increase for long-running computations
    to reduce overhead from monitor connections pausing the CPU.

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
        goto(transport, scratch_addr)
        return wait_for_pc(transport, bp_addr, timeout=timeout,
                           poll_interval=poll_interval)
    finally:
        # Retry delete in case the monitor port isn't ready yet
        for attempt in range(5):
            try:
                delete_breakpoint(transport, bp_id)
                break
            except TransportConnectionError:
                time.sleep(0.2)


def jsr_poll(
    transport: C64Transport,
    addr: int,
    timeout: float = 10.0,
    *,
    scratch_addr: int = 0x0334,
    poll_interval: float = 0.5,
) -> dict[str, int]:
    """Call a subroutine at *addr* using flag-based completion detection.

    An alternative to :func:`jsr` for long-running subroutines (e.g. heavy
    computation in warp mode).  Instead of breakpoints, a memory flag is
    polled to detect when the subroutine returns.  This avoids VICE monitor
    unresponsiveness that can occur when the CPU is busy during long
    warp-mode computations.

    For short subroutines, prefer :func:`jsr` — it provides more precise
    register capture since the CPU is paused at the breakpoint.

    A 17-byte trampoline is written at *scratch_addr*::

        LDA #$00           ; clear flag
        STA flag_addr      ; flag_addr = scratch_addr + 16
        JSR addr           ; call subroutine
        LDA #$FF           ; set flag
        STA flag_addr
        JMP loop_addr      ; loop_addr = scratch_addr + 15
        BRK                ; flag byte (offset +16)

    *poll_interval* controls the trade-off between responsiveness and
    overhead — higher values reduce monitor connection attempts but
    increase latency after the subroutine finishes.

    Returns the register state if readable after completion, or an empty
    dict if the monitor is not yet available.
    """
    flag_addr = scratch_addr + 16
    loop_addr = scratch_addr + 15

    addr_lo = addr & 0xFF
    addr_hi = (addr >> 8) & 0xFF
    flag_lo = flag_addr & 0xFF
    flag_hi = (flag_addr >> 8) & 0xFF
    loop_lo = loop_addr & 0xFF
    loop_hi = (loop_addr >> 8) & 0xFF

    trampoline = bytes([
        0xA9, 0x00,                     # LDA #$00
        0x8D, flag_lo, flag_hi,         # STA flag_addr
        0x20, addr_lo, addr_hi,         # JSR addr
        0xA9, 0xFF,                     # LDA #$FF
        0x8D, flag_lo, flag_hi,         # STA flag_addr
        0x4C, loop_lo, loop_hi,         # JMP loop_addr
        0x00,                           # BRK (flag byte)
    ])

    transport.write_memory(scratch_addr, trampoline)
    # Explicitly clear the flag
    transport.write_memory(flag_addr, bytes([0x00]))

    # Start execution — set_register disconnects the monitor, letting
    # the CPU run freely without re-listening.
    set_register(transport, "PC", scratch_addr)

    deadline = time.monotonic() + timeout
    while True:
        time.sleep(poll_interval)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Subroutine at ${addr:04X} did not return within {timeout}s"
            )
        try:
            flag = transport.read_memory(flag_addr, 1)
        except TransportConnectionError:
            continue
        if flag[0] == 0xFF:
            try:
                return transport.read_registers()
            except TransportConnectionError:
                return {}
