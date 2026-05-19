"""Backend-agnostic live memory watcher (pexpect-for-DMA).

This module provides :func:`watch_progress`, a generator that polls a
caller-supplied set of memory ranges and yields :class:`ProgressEvent`\\ s
when their bytes change, stall, or the wall-clock budget expires. It is a
host-side polling loop with no backend-specific dependencies: the only
primitive it uses is :meth:`C64Transport.read_memory`, so it works
identically against the VICE emulator and Ultimate 64 hardware.

See GitHub issue #108 for the motivating use case (multi-hour crypto
handshakes on the U64E).

Historical note
===============

``watch_progress`` originally lived in
:mod:`c64_test_harness.backends.ultimate64_helpers` and accepted an
:class:`Ultimate64Client`. The underlying primitive (``read_mem``) had
an exact backend-agnostic equivalent on the transport protocol
(:meth:`C64Transport.read_memory`), so the function was lifted to the
package root. A shim in ``ultimate64_helpers`` re-exports the names for
backwards compatibility with the original import path.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Literal, Mapping

from .transport import C64Transport

_log = logging.getLogger(__name__)

__all__ = [
    "ProgressEvent",
    "ProgressEventKind",
    "watch_progress",
]


ProgressEventKind = Literal[
    "Advanced", "Stalled", "Finished", "Timeout", "PollError"
]


@dataclass
class ProgressEvent:
    """One event emitted by :func:`watch_progress`.

    :ivar kind: ``"Advanced"`` — at least one watched range changed since
        the previous poll. ``"Stalled"`` — no watched range has changed
        for ``idle_timeout`` seconds. ``"Finished"`` — ``stop_when``
        callback returned truthy. ``"Timeout"`` — wall-clock
        ``overall_timeout`` elapsed. ``"PollError"`` — a single
        ``read_memory`` call raised; polling continues.
    :ivar changed: Mapping ``name -> (old_bytes, new_bytes)`` of ranges
        that differ from the previous poll. Empty for ``"Stalled"``,
        ``"Timeout"``, and ``"PollError"``.
    :ivar values: Latest known bytes for every watched range (last
        successful read per name). Populated on every event so callers
        can inspect current state without keeping their own copy.
    :ivar elapsed: Seconds since :func:`watch_progress` was called,
        measured against the clock injected via ``_clock``.
    :ivar error: The exception captured on ``"PollError"``. ``None``
        otherwise.
    """

    kind: ProgressEventKind
    changed: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)
    values: dict[str, bytes] = field(default_factory=dict)
    elapsed: float = 0.0
    error: Exception | None = None


# Sentinel returned by stop_when callbacks that are not interested in
# inspecting state; kept as a module-level default so ``is`` checks work.
def _stop_when_never(values: Mapping[str, bytes]) -> bool:  # noqa: ARG001
    return False


def watch_progress(
    transport: C64Transport,
    addresses: Mapping[str, tuple[int, int]],
    *,
    poll_interval: float = 10.0,
    idle_timeout: float = 120.0,
    overall_timeout: float = 5400.0,
    stop_when: Callable[[Mapping[str, bytes]], bool] = _stop_when_never,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ProgressEvent]:
    """Yield :class:`ProgressEvent`\\ s as watched memory ranges change.

    Pexpect-style live event-watching for long-running on-device workloads.
    The C64 program writes progress bytes (sentinel, screen RAM, per-stage
    slots, ...); this helper polls those ranges on a short interval and
    yields events when their bytes change, stall, or the wall-clock budget
    expires. See GitHub issue #108 for the motivating use case (multi-hour
    crypto handshakes on the U64E).

    The generator runs in the calling thread: the caller's loop drives
    iteration via ``next()``. There is no background thread, so closing
    the generator (``break``, ``.close()``, GC) releases any obligations
    immediately — no extra locking, no thread join. Reuses the caller's
    existing transport (and any surrounding ``DeviceLock`` / debug
    capture context) unchanged.

    Operationally:

    * Each poll issues one ``read_memory`` per watched name. On
      hardware (U64) reads contend with the C64 bus via DMA, so
      ``poll_interval`` defaults to a conservative 10 s. Shorten it
      only for workloads where bus contention is known to be safe.
    * A read raising any exception yields a ``"PollError"`` event and
      the generator KEEPS POLLING — one flaky read does not abort the
      watch. Persistent errors are visible as a stream of
      ``"PollError"`` events to the caller.
    * ``idle_timeout`` measures wall-clock since the last ``"Advanced"``
      event (or since :func:`watch_progress` started, if nothing has
      changed yet).
    * ``overall_timeout`` is the hard wall-clock budget; it is the only
      timer that guarantees the generator terminates.
    * ``stop_when(values)`` is consulted after every successful poll;
      when it returns truthy, a ``"Finished"`` event is yielded and the
      generator exits. ``values`` is the latest-bytes dict (one entry
      per watched name). The default never stops; the only built-in
      termination conditions are ``"Timeout"`` and caller-driven break.

    ROM banking note (SHADOW_BSS, $A000-$BFFF, $D000-$DFFF, $E000-$FFFF):
    backends generally read the address space as the 6510 currently sees
    it, with the active $01 banking applied. (On the U64, this is the
    firmware's ``GET /v1/machine:readmem`` behaviour; VICE's binary
    monitor honours the active bank by default too.) If the watched
    addresses fall in ROM-shadowed RAM, the caller MUST arrange for the
    C64 program to bank ROM off (write the appropriate value to $01)
    before the bytes of interest. This helper does NOT temporarily alter
    $01 — touching the banking register from the host would race the
    running program. Pick addresses that are visible under the running
    program's banking, or set up your code to keep ROM banked off across
    the watched region. Addresses below $A000 (e.g. the screen at
    $0400, low RAM at $0334 / $C000-$C3FF) are always RAM and need no
    special handling.

    :param transport: Any :class:`C64Transport` (VICE, U64, future
        backends). Only ``read_memory(addr, length)`` is used.
    :param addresses: Mapping of caller-chosen name to ``(addr, length)``
        tuple. Each entry is read independently every poll. Empty
        mappings raise :class:`ValueError`.
    :param poll_interval: Seconds between polls. Must be positive.
    :param idle_timeout: Seconds of no-change before a ``"Stalled"``
        event is emitted. Must be positive. ``"Stalled"`` is emitted
        repeatedly while the stall persists — one per poll past the
        threshold — so callers should break or take action on the first
        one.
    :param overall_timeout: Total wall-clock budget. After this elapses
        a single ``"Timeout"`` event is yielded and the generator exits.
    :param stop_when: Optional callback consulted after every successful
        poll. Truthy return causes a ``"Finished"`` event and exits the
        generator.
    :param _clock: Test injection point — defaults to
        :func:`time.monotonic`.
    :param _sleep: Test injection point — defaults to :func:`time.sleep`.
    :raises ValueError: If *addresses* is empty, or any timing argument
        is non-positive, or any ``(addr, length)`` is invalid.
    :returns: Generator of :class:`ProgressEvent`.
    """
    if not addresses:
        raise ValueError("addresses must be a non-empty mapping")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be positive, got {poll_interval!r}")
    if idle_timeout <= 0:
        raise ValueError(f"idle_timeout must be positive, got {idle_timeout!r}")
    if overall_timeout <= 0:
        raise ValueError(
            f"overall_timeout must be positive, got {overall_timeout!r}"
        )
    # Validate the address ranges up-front so a typo fails before the
    # generator is iterated (and not on the first poll, mid-test).
    for name, spec in addresses.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"address name must be a non-empty string: {name!r}")
        if (
            not isinstance(spec, tuple)
            or len(spec) != 2
            or not all(isinstance(x, int) for x in spec)
        ):
            raise ValueError(
                f"addresses[{name!r}] must be (addr, length) tuple of ints, "
                f"got {spec!r}"
            )
        addr, length = spec
        if addr < 0 or addr > 0xFFFF:
            raise ValueError(
                f"addresses[{name!r}] addr {addr!r} out of range 0..0xFFFF"
            )
        if length <= 0 or addr + length > 0x10000:
            raise ValueError(
                f"addresses[{name!r}] length {length!r} invalid (must be "
                f"positive and addr+length <= 0x10000)"
            )

    # Snapshot the names + specs in a stable order so the output is
    # deterministic when callers iterate ``event.changed`` / ``values``.
    plan = list(addresses.items())

    def _gen() -> Iterator[ProgressEvent]:
        start = _clock()
        last_change = start
        last_values: dict[str, bytes] = {}
        # Prime: do the first read so subsequent polls have something to
        # diff against. The first read can also surface a PollError.
        first_event_emitted = False
        while True:
            now = _clock()
            elapsed = now - start
            if elapsed >= overall_timeout:
                yield ProgressEvent(
                    kind="Timeout",
                    elapsed=elapsed,
                    values=dict(last_values),
                )
                return

            current: dict[str, bytes] = {}
            poll_error: Exception | None = None
            for name, (addr, length) in plan:
                try:
                    data = transport.read_memory(addr, length)
                except Exception as exc:  # noqa: BLE001
                    poll_error = exc
                    _log.warning(
                        "watch_progress: read_memory(%s @ 0x%04X, %d) raised %r",
                        name, addr, length, exc,
                    )
                    break
                current[name] = bytes(data)

            now = _clock()
            elapsed = now - start

            if poll_error is not None:
                yield ProgressEvent(
                    kind="PollError",
                    elapsed=elapsed,
                    values=dict(last_values),
                    error=poll_error,
                )
                # Keep going: one flaky read must not kill the watcher.
                _sleep(poll_interval)
                continue

            if not first_event_emitted:
                # First successful poll: seed the baseline and emit an
                # initial "Advanced" so callers see the starting state.
                changed_first: dict[str, tuple[bytes, bytes]] = {
                    name: (b"", current[name]) for name in current
                }
                last_values = current
                last_change = now
                first_event_emitted = True
                yield ProgressEvent(
                    kind="Advanced",
                    changed=changed_first,
                    values=dict(last_values),
                    elapsed=elapsed,
                )
                if stop_when(last_values):
                    yield ProgressEvent(
                        kind="Finished",
                        values=dict(last_values),
                        elapsed=elapsed,
                    )
                    return
                _sleep(poll_interval)
                continue

            diff: dict[str, tuple[bytes, bytes]] = {}
            for name in current:
                old = last_values.get(name, b"")
                new = current[name]
                if old != new:
                    diff[name] = (old, new)

            last_values = current

            if diff:
                last_change = now
                yield ProgressEvent(
                    kind="Advanced",
                    changed=diff,
                    values=dict(last_values),
                    elapsed=elapsed,
                )
            elif now - last_change >= idle_timeout:
                yield ProgressEvent(
                    kind="Stalled",
                    values=dict(last_values),
                    elapsed=elapsed,
                )

            if stop_when(last_values):
                yield ProgressEvent(
                    kind="Finished",
                    values=dict(last_values),
                    elapsed=elapsed,
                )
                return

            _sleep(poll_interval)

    return _gen()
