"""``_machine_failure_report`` must not blame this failure on an old JAM.

The ``binary_transport`` fixture is module-scoped and ``_event_queue`` is
drained only by ``wait_for_stopped``, so a ``0x61`` JAM event left by an
earlier test stays queued for every later failure in the module.  The
report has to count only events tagged at or after the resume generation
the failing wait began with.  No VICE needed: a stub transport is enough
because every probe in the report is wrapped in try/except.
"""
from __future__ import annotations

from collections import deque

from c64_test_harness.backends.vice_binary import _Response

import test_vice_core as tc

JAM = _Response(response_type=0x61, error_code=0, request_id=0xFFFFFFFF, body=b"")


class _Stub:
    """Just enough transport for the report to run every probe."""

    def __init__(self, queue, generation):
        self._event_queue = deque(queue)
        self._resume_generation = generation

    def read_registers(self):
        return {"PC": 0x0087, "A": 0, "X": 0, "Y": 0, "SP": 0xF6, "FL": 0x21,
                "LIN": 12, "CYC": 2}

    def resume(self):
        pass

    def read_memory(self, addr, n):
        return bytes(n)

    def checkpoint_list(self):
        return []


def test_stale_jam_from_an_earlier_test_is_not_reported_as_this_failure():
    tc._LAST_POLL_START_GEN[:] = [45]
    report = tc._machine_failure_report(_Stub([(0, JAM)], generation=50), "5")
    assert "1 JAM event(s) (0x61) queued" not in report
    assert "older JAM" in report  # still visible, correctly attributed


def test_jam_since_the_wait_began_is_reported():
    tc._LAST_POLL_START_GEN[:] = [45]
    report = tc._machine_failure_report(_Stub([(48, JAM)], generation=50), "5")
    assert "1 JAM event(s) (0x61) queued" in report


def test_wait_records_its_starting_generation():
    """The generation the report filters on is the one the wait began at."""
    class _Never(_Stub):
        screen_base, screen_cols, screen_rows = 0x0400, 40, 25

        def read_screen_codes(self):
            return [0x20] * 1000

    t = _Never([], generation=7)
    tc._wait_for_text_binary(t, "X", timeout=0.0)
    assert tc._LAST_POLL_START_GEN == [7]


# ---------------------------------------------------------------------------
# _emulator_is_stalled: progress across acknowledged resumes (#504)
# ---------------------------------------------------------------------------


class _Machine:
    """A fake VICE as the binary monitor presents it (measured, #504).

    Every read halts the machine at the monitor's frame phase, so ``LIN``
    reads 12 whatever happens.  ``resume()`` is always acknowledged; what
    it *does* depends on the machine's state:

    * ``irq=True``  -- the KERNAL IRQ runs, so the jiffy clock at
      ``$A0-$A2`` advances by a frame's worth per resume.
    * ``pcs``       -- the PC read after each resume, cycled.
    * ``emulating=False`` -- upstream bug 6 or a jam: the resume is
      acknowledged and nothing moves.
    * ``cycs``      -- ``CYC`` per read, cycled (jitter, not motion).
    * ``fail_reads_after`` -- register reads raise after that many.
    * ``fail_resumes_after`` -- ``resume()`` raises after that many.
    """

    def __init__(self, *, emulating=True, irq=True, pcs=(0xE5CF,),
                 cycs=(2,), fail_reads_after=None, fail_resumes_after=None):
        self.emulating, self.irq = emulating, irq
        self.pcs, self.cycs = pcs, cycs
        self.fail_reads_after = fail_reads_after
        self.fail_resumes_after = fail_resumes_after
        self.jiffy = 0x001234
        self.steps = 0
        self.reads = 0
        self.resumes = 0

    def read_registers(self):
        if self.fail_reads_after is not None and self.reads >= self.fail_reads_after:
            raise ConnectionError("monitor went away")
        cyc = self.cycs[self.reads % len(self.cycs)]
        self.reads += 1
        return {"PC": self.pcs[self.steps % len(self.pcs)], "LIN": 12, "CYC": cyc}

    def read_memory(self, addr, n):
        assert (addr, n) == (0x00A0, 3)
        return self.jiffy.to_bytes(3, "big")

    def resume(self):
        if (self.fail_resumes_after is not None
                and self.resumes >= self.fail_resumes_after):
            raise ConnectionError("resume not acknowledged")
        self.resumes += 1
        if self.emulating:
            self.steps += 1
            if self.irq:
                self.jiffy = (self.jiffy + 6) & 0xFFFFFF


def _no_sleep(monkeypatch):
    monkeypatch.setattr(tc.time, "sleep", lambda s: None)


def test_healthy_machine_sampled_at_one_frame_phase_is_not_stalled(monkeypatch):
    """Same raster and same idle-loop PC on every read, jiffy ticking."""
    _no_sleep(monkeypatch)
    stalled, seen = tc._emulator_is_stalled(_Machine())
    assert stalled is False, seen


def test_emulator_that_acknowledges_resumes_but_never_runs_is_stalled(monkeypatch):
    _no_sleep(monkeypatch)
    m = _Machine(emulating=False, pcs=(0xCF00,))
    stalled, seen = tc._emulator_is_stalled(m)
    assert stalled is True, seen
    assert m.resumes >= 3  # the verdict rests on acknowledged resumes


def test_jam_with_cyc_jitter_is_still_no_progress(monkeypatch):
    """Measured: CYC can change once right after a jam; that is not motion."""
    _no_sleep(monkeypatch)
    m = _Machine(emulating=False, pcs=(0xC000,), cycs=(3, 2, 2, 2))
    stalled, seen = tc._emulator_is_stalled(m)
    assert stalled is True, seen


def test_a_pc_change_counts_as_progress(monkeypatch):
    """Jiffy clock frozen, PC moving between reads: progress."""
    _no_sleep(monkeypatch)
    m = _Machine(irq=False, pcs=(0xC010, 0xC013, 0xC016))
    stalled, seen = tc._emulator_is_stalled(m)
    assert stalled is False, seen


def test_one_readable_sample_is_inconclusive(monkeypatch):
    _no_sleep(monkeypatch)
    m = _Machine(emulating=False, fail_reads_after=1)
    stalled, seen = tc._emulator_is_stalled(m)
    assert stalled is None, seen
    assert len(seen) == 1


def test_unacknowledged_resume_ends_the_run_inconclusive(monkeypatch):
    """No comparison may rest on a resume that did not return."""
    _no_sleep(monkeypatch)
    m = _Machine(emulating=False, fail_resumes_after=0)
    stalled, seen = tc._emulator_is_stalled(m)
    assert stalled is None, seen
    assert len(seen) == 1


def test_run_cut_short_is_judged_on_the_comparisons_it_made(monkeypatch):
    """Reads fail after two samples: one acknowledged comparison, no change."""
    _no_sleep(monkeypatch)
    m = _Machine(emulating=False, fail_reads_after=2)
    stalled, seen = tc._emulator_is_stalled(m)
    assert stalled is True, seen
    assert len(seen) == 2


def test_report_says_inconclusive_without_a_comparison(monkeypatch):
    _no_sleep(monkeypatch)
    tc._LAST_POLL_START_GEN[:] = []
    m = _Machine(emulating=False, fail_resumes_after=0)
    m._event_queue, m._resume_generation = deque(), 0
    report = tc._machine_failure_report(m, "5")
    line = next(l for l in report.splitlines() if "acknowledged resumes" in l)
    assert "inconclusive" in line and "progressing" not in line, line
