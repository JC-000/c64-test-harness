"""Tests for screen.py — ScreenGrid, wrap-aware search, extract_between."""

import logging

import pytest

from c64_test_harness.screen import (
    ScreenGrid,
    _resume_quietly,
    wait_for_stable,
    wait_for_text,
)
from conftest import MockTransport


def _text_to_screen_codes(text: str, cols: int = 40, rows: int = 25) -> list[int]:
    """Convert a simple ASCII string to C64 screen codes for testing.

    Uppercase A-Z → 1-26, digits → 0x30-0x39, space → 32.
    Only handles the chars needed for testing.
    """
    total = cols * rows
    # Pad to fill screen
    text = text.ljust(total)[:total]
    codes = []
    for ch in text:
        if "A" <= ch <= "Z":
            codes.append(ord(ch) - ord("A") + 1)
        elif "0" <= ch <= "9":
            codes.append(ord(ch))
        elif ch == " ":
            codes.append(32)
        elif ch == ".":
            codes.append(0x2E)
        elif ch == ":":
            codes.append(0x3A)
        elif ch == "/":
            codes.append(0x2F)
        elif ch == "=":
            codes.append(0x3D)
        elif ch == "-":
            codes.append(0x2D)
        elif ch == "@":
            codes.append(0)
        elif ch == "\n":
            codes.append(32)
        else:
            codes.append(32)
    return codes


class TestScreenGrid:
    def test_from_codes(self):
        codes = [32] * 1000
        grid = ScreenGrid.from_codes(codes)
        assert grid.cols == 40
        assert grid.rows == 25
        assert len(grid.codes) == 1000

    def test_text_lines(self):
        codes = [32] * 1000
        codes[0] = 8  # 'H'
        codes[1] = 9  # 'I'
        grid = ScreenGrid.from_codes(codes)
        lines = grid.text_lines()
        assert len(lines) == 25
        assert lines[0].startswith("HI")

    def test_continuous_text_no_newlines(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        ct = grid.continuous_text()
        assert "\n" not in ct
        assert len(ct) == 1000

    def test_wrap_aware_search(self):
        """Text spanning two 40-col rows should be findable (bug fix #2)."""
        codes = [32] * 1000
        # Place "EMAIL AD" at end of row 2 (positions 72-79)
        # and "DRESS:" at start of row 3 (positions 80-85)
        text_before = "EMAIL AD"
        text_after = "DRESS:"
        for i, ch in enumerate(text_before):
            codes[72 + i] = ord(ch) - ord("A") + 1 if ch.isalpha() else 32
        for i, ch in enumerate(text_after):
            codes[80 + i] = ord(ch) - ord("A") + 1 if ch.isalpha() else 0x3A
        grid = ScreenGrid.from_codes(codes)

        # Should fail with line-by-line text (the original bug)
        assert "EMAIL ADDRESS:" not in grid.text()
        # Should succeed with continuous text
        assert grid.has_text("EMAIL ADDRESS:")

    def test_has_text_case_insensitive(self):
        codes = _text_to_screen_codes("HELLO WORLD" + " " * 989)
        grid = ScreenGrid.from_codes(codes)
        assert grid.has_text("hello world")
        assert grid.has_text("HELLO WORLD")

    def test_find_text(self):
        codes = _text_to_screen_codes("  READY." + " " * 992)
        grid = ScreenGrid.from_codes(codes)
        pos = grid.find_text("READY.")
        assert pos == 2

    def test_find_text_not_found(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        assert grid.find_text("ABSENT") == -1

    def test_extract_between(self):
        text = "KEY: ABCDEF1234 SUBJECT: /CN=TEST"
        codes = _text_to_screen_codes(text + " " * (1000 - len(text)))
        grid = ScreenGrid.from_codes(codes)
        result = grid.extract_between("KEY: ", " SUBJECT")
        assert result is not None
        assert "ABCDEF1234" in result

    def test_extract_between_not_found(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        assert grid.extract_between("KEY:", "END") is None

    def test_extract_between_no_end_marker(self):
        text = "START:HELLO WORLD"
        codes = _text_to_screen_codes(text + " " * (1000 - len(text)))
        grid = ScreenGrid.from_codes(codes)
        result = grid.extract_between("START:", "ZZZZZ")
        assert result is not None
        assert "HELLO" in result

    def test_dump_format(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        dump = grid.dump("test")
        assert "[test]" in dump
        assert "0|" in dump
        assert "24|" in dump

    def test_from_transport(self):
        transport = MockTransport()
        grid = ScreenGrid.from_transport(transport)
        assert grid.cols == 40
        assert grid.rows == 25
        assert len(grid.codes) == 1000


class TestWaitForText:
    def test_immediate_match(self):
        codes = _text_to_screen_codes("READY." + " " * 994)
        transport = MockTransport(screen_codes=codes)
        grid = wait_for_text(transport, "READY.", timeout=1, poll_interval=0.1, verbose=False)
        assert grid is not None
        assert grid.has_text("READY.")

    def test_timeout_returns_none(self):
        transport = MockTransport()  # blank screen
        grid = wait_for_text(transport, "NEVER", timeout=0.3, poll_interval=0.1, verbose=False)
        assert grid is None


class TestWaitForStable:
    def test_stable_returns_grid(self):
        codes = _text_to_screen_codes("STABLE" + " " * 994)
        transport = MockTransport(screen_codes=codes)
        grid = wait_for_stable(transport, timeout=2, poll_interval=0.1, stable_count=2)
        assert grid is not None

    def test_changing_screen_eventually_stabilizes(self):
        transport = MockTransport()
        # Screen changes once then stays stable
        call_count = 0
        original_read = transport.read_screen_codes

        def changing_read():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return [call_count] * 1000
            return [99] * 1000

        transport.read_screen_codes = changing_read
        grid = wait_for_stable(transport, timeout=3, poll_interval=0.1, stable_count=2)
        assert grid is not None

    def test_timeout_returns_none(self):
        """A screen that never stabilizes must return None, per the
        docstring contract — not the last still-changing grid."""
        transport = MockTransport()
        call_count = 0

        def always_changing_read():
            nonlocal call_count
            call_count += 1
            return [call_count % 256] * 1000

        transport.read_screen_codes = always_changing_read
        grid = wait_for_stable(
            transport, timeout=0.3, poll_interval=0.05, stable_count=3
        )
        assert grid is None


# -- the CPU must be running on every exit path -------------------------------
#
# On VICE the binary monitor halts the machine to service each screen read
# and it stays halted until something resumes it.  Both waiters resumed
# between polls but not before handing a match back, so "is the C64 running
# after this call?" depended on which branch the function left by -- the
# shape that makes a running machine look hung and has cost a bogus
# emulator bug report.  ``ScreenGrid.from_transport`` never resumes at all;
# that is now documented rather than changed, because it is a snapshot
# primitive, not a waiter.


class ResumeCountingTransport(MockTransport):
    """MockTransport that logs screen reads and resumes in order.

    ``ops`` is the ordering record, and ordering is the whole point: the
    waiters resumed *between polls* long before this fix, so a bare
    ``resume_count >= 1`` is satisfied by that old behaviour and passes
    whether or not the match path was fixed.  The falsifiable assertion
    is that the *last* thing done to the transport before returning was
    a resume -- that is what "the CPU is running on return" means.
    """

    def __init__(self, *args, fail_reads: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ops: list[str] = []
        self.fail_reads = fail_reads

    @property
    def resume_count(self) -> int:
        return self.ops.count("resume")

    def resume(self) -> None:
        self.ops.append("resume")

    def read_screen_codes(self) -> list[int]:
        self.ops.append("read")
        if self.fail_reads:
            raise RuntimeError("transport is unhappy")
        return super().read_screen_codes()


def _screen_with(text: str) -> ResumeCountingTransport:
    t = ResumeCountingTransport()
    codes = [ord(c) & 0x3F for c in text]
    grid = list(t.screen_codes)
    grid[: len(codes)] = codes
    t.screen_codes = grid
    return t


def test_wait_for_text_resumes_the_cpu_before_returning_a_match():
    """The match path is the one that used to leave the machine halted."""
    t = _screen_with("READY.")
    grid = wait_for_text(t, "READY.", timeout=1.0, poll_interval=0.01,
                         verbose=False)
    assert grid is not None
    assert t.ops[-1] == "resume", t.ops


def test_wait_for_text_resumes_the_cpu_before_returning_none():
    t = ResumeCountingTransport()
    assert wait_for_text(t, "NEVER", timeout=0.2, poll_interval=0.05,
                         verbose=False) is None
    assert t.ops[-1] == "resume", t.ops


def test_wait_for_stable_resumes_the_cpu_before_returning_a_grid():
    t = ResumeCountingTransport()
    grid = wait_for_stable(t, timeout=1.0, poll_interval=0.01, stable_count=2)
    assert grid is not None
    assert t.ops[-1] == "resume", t.ops


def test_waiters_resume_even_when_every_screen_read_fails():
    """A dead read must not leave the machine frozen as well as unread."""
    t = ResumeCountingTransport(fail_reads=True)
    assert wait_for_text(t, "READY.", timeout=0.2, poll_interval=0.05,
                         verbose=False) is None
    assert t.ops[-1] == "resume", t.ops

    t2 = ResumeCountingTransport(fail_reads=True)
    assert wait_for_stable(t2, timeout=0.2, poll_interval=0.05,
                           stable_count=2) is None
    assert t2.ops[-1] == "resume", t2.ops


def test_screen_grid_from_transport_does_not_resume():
    """Pinning the documented asymmetry: the snapshot primitive is not a
    waiter, so it leaves the CPU exactly as it found it -- halted, on
    VICE.  The docstring says so; this makes it a fact the suite defends
    rather than a comment that can rot."""
    t = _screen_with("READY.")
    ScreenGrid.from_transport(t)
    assert t.resume_count == 0


# -- the exit resume is *owed*, not unconditional -----------------------------
#
# Issues #189 / #190.  Resuming on every exit path is right; resuming when
# nothing halted the machine since the last resume is not.  The timeout
# path is exactly that case: it resumes, sleeps, re-enters the loop, sees
# the deadline and returns, so the pre-fix ``finally`` fired a second
# resume with no read in between.  That second resume is a real
# ``PUT /v1/machine:resume`` on the Ultimate 64 (clears a deliberate pause,
# costs a client timeout against a dead device) and a second
# ``_resume_generation`` bump on VICE, which drops a queued JAM event.


def _consecutive_resumes(ops: list[str]) -> list[int]:
    """Indices where a resume immediately follows another resume."""
    return [
        i for i in range(1, len(ops))
        if ops[i] == "resume" and ops[i - 1] == "resume"
    ]


def test_wait_for_text_timeout_does_not_resume_twice():
    """No two resumes back to back: each one costs a round trip and a
    ``_resume_generation`` bump, and nothing halted the machine between
    the loop's resume and the return."""
    t = ResumeCountingTransport()
    assert wait_for_text(t, "NEVER", timeout=0.2, poll_interval=0.05,
                         verbose=False) is None
    assert t.ops[-1] == "resume", t.ops
    assert _consecutive_resumes(t.ops) == [], t.ops


def test_wait_for_stable_timeout_does_not_resume_twice():
    # stable_count high enough that the screen never counts as settled.
    t = ResumeCountingTransport()
    assert wait_for_stable(t, timeout=0.2, poll_interval=0.05,
                           stable_count=99) is None
    assert t.ops[-1] == "resume", t.ops
    assert _consecutive_resumes(t.ops) == [], t.ops


def test_failing_reads_do_not_resume_twice_either():
    """A read that raised may still have halted the machine, so the
    exception path keeps its resume -- but only one of them."""
    t = ResumeCountingTransport(fail_reads=True)
    assert wait_for_text(t, "READY.", timeout=0.2, poll_interval=0.05,
                         verbose=False) is None
    assert t.ops[-1] == "resume", t.ops
    assert _consecutive_resumes(t.ops) == [], t.ops


class JamGenerationTransport(ResumeCountingTransport):
    """Models VICE's resume-generation event bookkeeping (issue #190).

    ``BinaryViceTransport.resume`` increments ``_resume_generation``
    *before* sending CMD_EXIT (``vice_binary.py:793``), so an unsolicited
    event arriving in that ack window is tagged at the new generation;
    ``wait_for_stopped`` then drops every queued event tagged below the
    current generation (``vice_binary.py:1008-1010``).  A JAM is therefore
    visible only until the *next* resume.

    A resume that follows a poll opens a run window, and this machine
    jams in it: the event is queued at the generation that resume just
    created, replacing whatever was queued in the window before.  The
    ``finally`` resume on the timeout path follows no poll -- the waiter
    returns immediately after it -- so it queues nothing and only bumps
    the generation, which is precisely how it discards the jam.  "Is the
    jam still attributable when the waiter returns?" therefore reduces to
    "did the waiter resume again after its last poll?".
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.generation = 0
        self.jam_generation: int | None = None
        self._polled_since_resume = False

    def read_screen_codes(self) -> list[int]:
        self._polled_since_resume = True
        return super().read_screen_codes()

    def resume(self) -> None:
        self.generation += 1
        if self._polled_since_resume:
            self.jam_generation = self.generation
            self._polled_since_resume = False
        super().resume()

    @property
    def jam_still_queued(self) -> bool:
        return (
            self.jam_generation is not None
            and self.jam_generation >= self.generation
        )


def test_timeout_leaves_a_jam_from_the_last_poll_attributable():
    """The trailing resume discarded the only signal that separates a
    6510 jam from upstream VICE bug 6 (a stalled emulator), so the jam
    resurfaced as an unattributed timeout further down the test."""
    t = JamGenerationTransport()
    assert wait_for_text(t, "NEVER", timeout=0.2, poll_interval=0.05,
                         verbose=False) is None
    assert t.jam_still_queued, (
        f"a JAM queued during the last poll's resume window was discarded "
        f"by a later resume (jam gen {t.jam_generation}, now at generation "
        f"{t.generation}); ops={t.ops}"
    )


def test_match_path_still_resumes_and_that_residue_is_accepted():
    """The match path must resume -- the caller gets a running machine --
    so a JAM queued by the *previous* poll is still dropped there.  Pinned
    so the trade-off in the docstring is a tested fact, not a hope."""
    t = JamGenerationTransport()
    codes = [ord(c) & 0x3F for c in "READY."]
    grid_codes = list(t.screen_codes)
    grid_codes[: len(codes)] = codes
    t.screen_codes = grid_codes
    grid = wait_for_text(t, "READY.", timeout=1.0, poll_interval=0.01,
                         verbose=False)
    assert grid is not None
    assert t.ops[-1] == "resume", t.ops


# -- the guarantee is best-effort, and says so out loud -----------------------
#
# Issue #191.  ``_resume_quietly`` swallowed everything, including the
# ``NotImplementedError`` that ``backends/hardware.py`` raises, so a
# waiter could document "the CPU is running on return" while nothing had
# resumed anything.  The swallow stays (raising from a ``finally`` would
# replace the caller's in-flight exception), but it is now audible.


class UnresumableTransport(ResumeCountingTransport):
    """A transport on the hardware base class: ``resume`` is not implemented."""

    def resume(self) -> None:
        self.ops.append("resume-attempted")
        raise NotImplementedError


class BrokenResumeTransport(ResumeCountingTransport):
    """A transport whose resume fails transiently (socket gone, HTTP 500)."""

    def resume(self) -> None:
        self.ops.append("resume-attempted")
        raise RuntimeError("connection reset by peer")


def test_unimplemented_resume_is_logged_not_raised(caplog):
    t = UnresumableTransport()
    codes = [ord(c) & 0x3F for c in "READY."]
    grid_codes = list(t.screen_codes)
    grid_codes[: len(codes)] = codes
    t.screen_codes = grid_codes
    with caplog.at_level(logging.WARNING, logger="c64_test_harness.screen"):
        grid = wait_for_text(t, "READY.", timeout=1.0, poll_interval=0.01,
                             verbose=False)
    assert grid is not None, "a resume failure must not lose the match"
    assert t.ops[-1] == "resume-attempted", t.ops
    messages = [r.getMessage() for r in caplog.records]
    assert any("not implemented" in m for m in messages), messages
    assert any("UnresumableTransport" in m for m in messages), messages


def test_failing_resume_is_logged_not_raised(caplog):
    t = BrokenResumeTransport()
    with caplog.at_level(logging.WARNING, logger="c64_test_harness.screen"):
        assert wait_for_text(t, "NEVER", timeout=0.2, poll_interval=0.05,
                             verbose=False) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("connection reset by peer" in m for m in messages), messages
    assert any("may still be halted" in m for m in messages), messages


def test_resume_quietly_reports_delivery():
    """The bool is what a caller who needs certainty can read."""
    assert _resume_quietly(ResumeCountingTransport()) is True
    assert _resume_quietly(UnresumableTransport()) is False
    assert _resume_quietly(BrokenResumeTransport()) is False
