"""Backend-agnostic screen operations.

``ScreenGrid`` is an immutable snapshot of the C64 screen that provides
wrap-aware text search (fixing vicemon.py bug #2) and structured data
extraction.

``wait_for_text()`` and ``wait_for_stable()`` poll the transport and
return a ``ScreenGrid`` on success, so callers can immediately extract data.

**CPU contract.**  On VICE every binary-monitor command halts the 6510.
The two waiters resume between polls *and* in a ``finally``, so they never
hand back a stopped machine on any exit path.  ``ScreenGrid.from_transport``
-- and anything built on it, such as ``debug.dump_screen`` -- does not
resume, by design: it is a snapshot primitive, not a waiter.  A poll loop
built out of bare snapshots never advances the C64, and a machine that is
merely stopped looks exactly like one that is wedged.

Two qualifications on that contract, both stated in full on
``wait_for_text``: the exit resume is *owed* rather than unconditional (it
is skipped when no screen read has halted the machine since the last
resume), and it is *best-effort* (a transport whose ``resume`` raises is
logged at WARNING, never re-raised).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .encoding.screen_codes import SCREEN_CODE_TABLE

if TYPE_CHECKING:
    from .transport import C64Transport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenGrid:
    """Immutable snapshot of C64 screen memory.

    Holds raw screen codes and provides text conversion with wrap-aware
    search (no newlines in ``continuous_text()``).
    """

    codes: tuple[int, ...]
    cols: int
    rows: int

    @classmethod
    def from_transport(cls, transport: C64Transport) -> ScreenGrid:
        """Capture a screen snapshot from a live transport.

        .. warning::

           **On VICE this leaves the CPU paused.**  The binary monitor
           halts the machine to service every memory read and it stays
           halted until something calls
           :meth:`~.transport.C64Transport.resume`.  This method does not.

           So a poll loop built out of bare ``from_transport`` calls never
           advances the C64: the screen is identical every time round, and
           a running machine is indistinguishable from a hung one.  That
           mistake has cost a bogus emulator bug report more than once.
           Use :func:`wait_for_text` or :func:`wait_for_stable`, which
           resume on every exit path, or call ``transport.resume()``
           yourself after each capture.

           On the Ultimate 64 memory access is DMA-backed and the machine
           runs throughout, so a resume is not *needed* there -- but it is
           not free either: it is a real request to the device, so it
           costs a round trip and it will clear a pause the caller set
           deliberately.  The resuming helpers are correct on both
           backends; on hardware they are correct at that price.
        """
        raw = transport.read_screen_codes()
        return cls(
            codes=tuple(raw),
            cols=transport.screen_cols,
            rows=transport.screen_rows,
        )

    @classmethod
    def from_codes(cls, codes: list[int] | tuple[int, ...], cols: int = 40, rows: int = 25) -> ScreenGrid:
        """Create a ScreenGrid from raw codes (useful for testing)."""
        return cls(codes=tuple(codes), cols=cols, rows=rows)

    def text_lines(self) -> list[str]:
        """Return one string per screen row."""
        lines = []
        for row in range(self.rows):
            start = row * self.cols
            end = start + self.cols
            row_codes = self.codes[start:end]
            line = "".join(SCREEN_CODE_TABLE[c & 0xFF] for c in row_codes)
            lines.append(line)
        return lines

    def text(self) -> str:
        """Return screen as newline-separated text (25 lines)."""
        return "\n".join(self.text_lines())

    def continuous_text(self) -> str:
        """Return screen as a single string with NO newlines.

        This is the key fix for vicemon.py bug #2: text that wraps across
        40-column rows is searchable as a continuous string.
        """
        return "".join(self.text_lines())

    def has_text(self, needle: str) -> bool:
        """Case-insensitive, wrap-aware text search."""
        return needle.upper() in self.continuous_text().upper()

    def find_text(self, needle: str) -> int:
        """Return position of *needle* in continuous text, or -1 if absent."""
        return self.continuous_text().upper().find(needle.upper())

    def extract_between(self, start_marker: str, end_marker: str) -> str | None:
        """Extract text between two markers in continuous text.

        Returns ``None`` if *start_marker* is not found.  If *end_marker*
        is not found after the start, extracts to end of screen.
        """
        ct = self.continuous_text()
        upper = ct.upper()
        start_idx = upper.find(start_marker.upper())
        if start_idx < 0:
            return None
        content_start = start_idx + len(start_marker)
        end_idx = upper.find(end_marker.upper(), content_start)
        if end_idx < 0:
            return ct[content_start:].rstrip()
        return ct[content_start:end_idx].rstrip()

    def dump(self, label: str = "") -> str:
        """Format screen for debug output with row numbers."""
        prefix = f" [{label}]" if label else ""
        lines = [f"--- Screen dump{prefix} ---"]
        for i, line in enumerate(self.text_lines()):
            lines.append(f"  {i:2d}| {line}")
        lines.append("---")
        return "\n".join(lines)


def _resume_quietly(transport: C64Transport) -> bool:
    """Resume the CPU; return whether the resume was actually delivered.

    Both waiters poll through the binary monitor, which halts the machine
    for every read; without a resume the C64 does not advance between
    polls and a running program is indistinguishable from a hung one.

    Failures are logged at WARNING and never re-raised.  Two reasons, and
    the second is why ``NotImplementedError`` is not allowed out either
    (issue #191 proposed letting it propagate):

    * Most call sites are ``finally`` bodies.  An exception raised there
      *replaces* whatever exception the caller was already unwinding, so
      a transport that cannot resume would mask the transport error that
      is the actual news -- the same "a symptom hides the cause" shape
      the waiters exist to avoid.
    * A resume that could not be delivered must not turn a successful
      wait into an exception; the grid the caller asked for is valid
      either way.

    The cost is that the waiters' "the CPU is running on return" line is
    a best-effort guarantee, not an enforced one: on a transport whose
    ``resume`` raises -- ``backends/hardware.py`` raises
    ``NotImplementedError`` -- the machine is *not* running on return and
    the WARNING is the only record.  Callers that must know can read the
    return value of this helper or watch the ``c64_test_harness.screen``
    logger.
    """
    try:
        transport.resume()
        return True
    except NotImplementedError:
        # Not a transient: this transport structurally cannot resume, so
        # every waiter run against it silently breaks the contract.
        logger.warning(
            "%s.resume() is not implemented; the screen waiters cannot "
            "guarantee the CPU is running on return for this transport.",
            type(transport).__name__,
        )
        return False
    except Exception as exc:
        logger.warning(
            "%s.resume() failed (%s: %s); the CPU may still be halted.",
            type(transport).__name__,
            type(exc).__name__,
            exc,
        )
        return False


def wait_for_text(
    transport: C64Transport,
    needle: str,
    timeout: float = 60.0,
    poll_interval: float = 2.0,
    verbose: bool = True,
    on_progress: Callable[[float, str], None] | None = None,
) -> ScreenGrid | None:
    """Wait until *needle* appears on screen (wrap-aware, case-insensitive).

    Returns the matching ``ScreenGrid`` so the caller can immediately
    extract data, or ``None`` on timeout.

    *on_progress* replaces hardcoded ``print()`` — receives elapsed seconds
    and a snippet of the last non-blank screen row.

    **CPU state on return: running, on every exit path** — match, timeout,
    or exception.  The binary monitor halts the machine to service each
    screen read, so this loop resumes after every poll; it also resumes
    before handing the match back, which it did not always do.  The
    returned grid was captured before that resume, so the grid itself
    is intact -- but the *coincidence* between grid and machine is not.
    Before this changed, a match handed back a stopped C64 and a following
    ``read_bytes`` saw memory exactly as it stood at the capture instant.
    Now the program runs on until that read halts it again, some
    milliseconds later.  A caller that needs the two to agree should wait
    on a string the program prints only once it is idle, or halt the
    machine explicitly before reading.

    Two qualifications on that sentence, both deliberate:

    *Owed, not unconditional.*  The exit resume fires only when a screen
    read has happened since the last resume — the match path and the
    exception path, never the timeout path, which resumes, sleeps and
    then returns with nothing having touched the machine in between.
    Skipping it there leaves nothing halted (the resume would have been
    for a halt that never happened) and it avoids a second resume that is
    not free: on the Ultimate 64 ``resume`` is a real
    ``PUT /v1/machine:resume`` that clears a pause the caller may have set
    deliberately and costs a full client timeout against an unreachable
    device (issue #189), and on VICE it bumps
    ``BinaryViceTransport._resume_generation``, which discards any queued
    JAM event and so turns a jam into an unattributed later timeout
    (issue #190).  Residue, accepted rather than chased: the *match* path
    must resume, so a JAM queued during the previous poll's resume window
    is still dropped there, and a first-poll match on hardware still
    clears a deliberate pause.  The waiters cannot tell whether a read
    halted this particular backend without asking it what backend it is,
    which is the coupling ``C64Transport`` exists to prevent.

    *Best-effort, not enforced.*  A ``resume`` that raises is logged at
    WARNING and swallowed (see :func:`_resume_quietly` for why raising
    from a ``finally`` is worse), so on a transport that cannot resume —
    ``backends/hardware.py`` raises ``NotImplementedError`` — the CPU is
    not in fact running on return.  Nothing else reports that; the log
    record is the signal (issue #191).

    .. warning::

       **Do not wait on a string that is already on screen.** This checks
       the *current* screen first, so a needle that is already visible
       returns immediately — it does not wait for anything. Using a
       persistent menu banner as an "operation finished" signal is the
       classic form of this bug:

       .. code-block:: python

          send_key(transport, "F")                     # start a long operation
          wait_for_text(transport, "Q=QUIT")           # BUG: banner never left
          send_key(transport, "J")                     # arrives mid-operation

       The second key is delivered into the KERNAL buffer correctly, but
       the program is still busy and typically flushes pending keys when
       it redraws its menu — so the keypress vanishes with no error and no
       screen change. Adding a settle delay does not help; sending a
       throwaway key first appears to "fix" it only because it delays the
       real key until the program is listening again. See issue #138.

       Wait on a *transition* instead: a string that appears only on
       completion (a result value, a "DONE" marker), or first wait for the
       in-progress indicator and then for it to disappear.
    """
    needle_upper = needle.upper()
    start = time.monotonic()
    # True once a screen read has (possibly) halted the machine and no
    # resume has followed it.  See the docstring: the exit resume is owed,
    # not unconditional.
    pending_resume = False
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return None
            try:
                # Set before the read, not after: a read that raised
                # part-way may still have halted the 6510.
                pending_resume = True
                grid = ScreenGrid.from_transport(transport)
                if needle_upper in grid.continuous_text().upper():
                    return grid
                if verbose and on_progress is not None:
                    lines = grid.text_lines()
                    last = ""
                    for line in reversed(lines):
                        if line.strip():
                            last = line.strip()[:60]
                            break
                    on_progress(elapsed, last)
            except Exception:
                pass
            # The binary monitor pauses the CPU on every memory read.
            # Resume so the program can continue executing before we poll
            # again.
            _resume_quietly(transport)
            pending_resume = False
            time.sleep(poll_interval)
    finally:
        # Every exit path, not just the polls: a match used to return with
        # the machine still halted, so the caller's "it is running now"
        # depended on which branch it left by.  Gated so the timeout path
        # -- resume, sleep, return -- does not resume a second time.
        if pending_resume:
            _resume_quietly(transport)


def wait_for_stable(
    transport: C64Transport,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    stable_count: int = 3,
) -> ScreenGrid | None:
    """Wait until screen content stops changing.

    Returns the stable ``ScreenGrid``, or ``None`` on timeout —
    matching ``wait_for_text``'s contract (a non-``None`` return always
    means the condition was met, and the CPU is running on every exit
    path), including its two qualifications: the exit resume is owed
    rather than unconditional, and it is best-effort.  See
    :func:`wait_for_text` for both in full.
    """
    prev_text: str | None = None
    count = 0
    start = time.monotonic()
    pending_resume = False
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return None
            try:
                pending_resume = True
                grid = ScreenGrid.from_transport(transport)
                current = grid.continuous_text()
                if current == prev_text:
                    count += 1
                    if count >= stable_count:
                        return grid
                else:
                    count = 0
                    prev_text = current
            except Exception:
                pass
            # Resume the CPU so the program keeps running between polls
            # (the binary monitor pauses on memory reads).
            _resume_quietly(transport)
            pending_resume = False
            time.sleep(poll_interval)
    finally:
        if pending_resume:
            _resume_quietly(transport)
