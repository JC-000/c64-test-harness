"""Backend-agnostic host-side wall-clock polling helper.

This module provides :func:`poll_until_ready`, a tiny orchestration loop
that drives a 6502 "peek batch" routine repeatedly from the Python side
until either the device reports an event or a wall-clock deadline expires.

Why host-side?
==============

The C64-side bridge networking code originally used a 3-level inner loop
counter (DEC $F0/$F1/$F2) to bound its CS8900a RX poll to "about 5
seconds".  That budget is denominated in *6502 cycles*, which means it
evaporates almost instantly under VICE warp mode -- by the time TAP
frames physically arrive on the bridge, the C64 has already given up.
Empirical testing also disproved the obvious workaround (CIA TOD): in
our VICE 3.10 + sound-off configuration, both CIA1 and CIA2 TOD registers
read ``01:00:00.00`` permanently and never advance, regardless of warp.

The fix is to push the wall-clock budget out of the 6502 entirely.  The
6502 routine runs only a small bounded "peek" (a fixed number of poll
iterations, returning immediately whether or not the event fired) and
Python decides whether to call it again based on a real :func:`time.monotonic`
deadline.  Each peek round-trip is short (~1 ms of emulated time at
normal speed; effectively just a binary monitor JSR + memory read at
warp), so the wall-clock budget is honoured to within a few milliseconds
in either mode.

Generality
==========

``poll_until_ready`` is deliberately backend-agnostic.  It only knows
how to:

  1. Zero a single result byte (``write_bytes``).
  2. Call a 6502 routine via the harness's ``jsr`` helper.
  3. Read the result byte (``read_bytes``).
  4. Decide whether to loop, succeed, or time out.

This same pattern works for:

  * VICE + CS8900a RX polling (current consumer in
    :mod:`c64_test_harness.bridge_ping`).
  * Future Ultimate 64 Elite UCI networking (UCI socket status registers
    at ``$DF1C-$DF1F``): a UCI peek routine would poll its status register
    instead of the CS8900a RxEvent.
  * Any other "is the device ready yet" pattern where polling cost is
    negligible compared to wait time.

The 6502 peek routine contract
==============================

A peek routine consumed by :func:`poll_until_ready` MUST:

* Take no arguments (it is invoked via plain ``jsr``).
* Run a bounded number of poll iterations (no unbounded loops).
* Write exactly one byte to ``result_addr`` before returning:

  - ``0x01`` -- event fired (success)
  - ``0xFF`` -- batch exhausted without event (will be retried until
    the wall-clock deadline expires)
  - any other value -- device-specific error sentinel (returned to the
    caller immediately, no retry)

* RTS promptly after writing the result.

Python is responsible for zeroing the result byte before each call;
peek routines should not assume a particular incoming value.
"""

from __future__ import annotations

import time
from typing import Protocol


class _PollableTransport(Protocol):
    """Structural type for transports usable with :func:`poll_until_ready`.

    Any harness transport that exposes :func:`memory.read_bytes`,
    :func:`memory.write_bytes`, and :func:`execute.jsr` semantics
    works.  In practice this is :class:`BinaryViceTransport` and
    :class:`Ultimate64Transport`.
    """


def poll_until_ready(
    transport: _PollableTransport,
    code_addr: int,
    result_addr: int,
    *,
    timeout_s: float = 5.0,
    batch_timeout_s: float = 5.0,
) -> int:
    """Poll a pre-loaded 6502 peek routine until it reports an event.

    Calls the routine at ``code_addr`` repeatedly via ``jsr``.  Between
    calls, zeroes ``result_addr``, runs the peek, then reads back the
    result byte.  Loops until one of:

      * Result is ``0x01`` -- success, return ``0x01`` immediately.
      * Result is something other than ``0xFF`` -- device-specific
        sentinel, return it immediately.
      * Wall clock has passed ``timeout_s`` -- return ``0xFF``.

    Args:
        transport: Any harness transport supporting ``read_bytes`` /
            ``write_bytes`` / ``jsr``.
        code_addr: Load address of a peek routine matching the contract
            described in this module's docstring.  The caller is
            responsible for loading the code before calling this
            function.
        result_addr: 1-byte memory location the peek routine writes to.
        timeout_s: Wall-clock budget in seconds.  This is the only
            timer that matters for warp safety.
        batch_timeout_s: Per-``jsr`` timeout passed through to the
            harness.  Defaults to 5s, which is comfortably above the
            expected runtime of any sane peek batch (a few hundred
            iterations at 6502 cycle rate).

    Returns:
        The final result byte: ``0x01`` on event, ``0xFF`` on
        wall-clock timeout, or any device-specific sentinel.

    Note:
        This function imports :mod:`c64_test_harness.execute` and
        :mod:`c64_test_harness.memory` lazily so it can sit at the
        bottom of the import graph alongside the rest of the helpers.
    """
    from .execute import jsr
    from .memory import read_bytes, write_bytes

    deadline = time.monotonic() + timeout_s
    while True:
        write_bytes(transport, result_addr, [0x00])
        jsr(transport, code_addr, timeout=batch_timeout_s)
        result = read_bytes(transport, result_addr, 1)[0]
        if result == 0x01:
            return 0x01
        if result != 0xFF:
            # Device-specific sentinel; let the caller decide what it
            # means.  Do not retry -- a non-0x01/0xFF byte indicates
            # the peek routine wants to surface something explicit.
            return result
        if time.monotonic() >= deadline:
            return 0xFF
