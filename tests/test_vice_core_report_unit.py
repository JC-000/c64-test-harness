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

import pytest

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
# _machine_progress: one cause per verdict (#504)
# ---------------------------------------------------------------------------

MS = tc.MachineState


class _Machine:
    """A fake VICE as the binary monitor presents it (measured, #504).

    Every read halts the machine at the monitor's frame phase, so ``LIN``
    reads 12 whatever happens.  ``resume()`` is acknowledged (it returns)
    unless ``fail_resumes_after`` says otherwise; what it *does*:

    * ``clocked``   -- VICE is emulating, so CIA1 Timer A counts down.
      ``False`` is upstream bug 6: acknowledged, nothing clocked.
    * ``cpu_runs``  -- the 6510 advances: the PC steps through ``pcs``.
    * ``irq``       -- the KERNAL IRQ is serviced, so the jiffy clock at
      ``$A0-$A2`` advances (needs ``clocked`` and ``cpu_runs``).
    * ``jam_events`` -- each resume queues a ``0x61`` JAM event.
    * ``memory``    -- bytes the host peek returns (default NOP).
    * ``cycs``      -- ``CYC`` per read, cycled (jitter, not motion).
    """

    def __init__(self, *, clocked=True, cpu_runs=True, irq=True,
                 pcs=(0xE5CF,), cycs=(2,), jam_events=False, memory=None,
                 fail_reads_after=None, fail_resumes_after=None):
        self.clocked, self.cpu_runs, self.irq = clocked, cpu_runs, irq
        self.pcs, self.cycs = pcs, cycs
        self.jam_events = jam_events
        self.memory = dict(memory or {})
        self.fail_reads_after = fail_reads_after
        self.fail_resumes_after = fail_resumes_after
        self.jiffy = 0x001234
        self.timer_a = 0x4000
        self.steps = self.reads = self.resumes = 0
        self._event_queue = deque()
        self._resume_generation = 10

    @property
    def pc(self):
        return self.pcs[self.steps % len(self.pcs)]

    def read_registers(self):
        if self.fail_reads_after is not None and self.reads >= self.fail_reads_after:
            raise ConnectionError("monitor went away")
        cyc = self.cycs[self.reads % len(self.cycs)]
        self.reads += 1
        return {"PC": self.pc, "LIN": 12, "CYC": cyc}

    def read_memory(self, addr, n):
        if (addr, n) == (0x00A0, 3):
            return self.jiffy.to_bytes(3, "big")
        if (addr, n) == (0xDC04, 2):
            return self.timer_a.to_bytes(2, "little")
        return bytes(self.memory.get(addr + i, 0xEA) for i in range(n))

    def resume(self):
        if (self.fail_resumes_after is not None
                and self.resumes >= self.fail_resumes_after):
            raise ConnectionError("resume not acknowledged")
        self.resumes += 1
        self._resume_generation += 1
        if not self.clocked:
            return
        self.timer_a = (self.timer_a - 0x0157) & 0xFFFF
        if self.jam_events:
            self._event_queue.append((self._resume_generation, JAM))
        if self.cpu_runs:
            self.steps += 1
            if self.irq:
                self.jiffy = (self.jiffy + 6) & 0xFFFFFF


def _healthy():
    return _Machine()


def _bug6():
    return _Machine(clocked=False, cpu_runs=False, pcs=(0xCF00,))


def _kil_jam():
    # Measured: CYC can change once right after a jam; that is not motion.
    return _Machine(cpu_runs=False, pcs=(0xC000,), cycs=(3, 2, 2, 2),
                    memory={0xC000: 0x02})


def _masked_spin():
    # SEI; JMP * -- the monitor's frame-phase halt reads one PC every time.
    return _Machine(cpu_runs=False, pcs=(0xC001,),
                    memory={0xC001: 0x4C, 0xC002: 0x01, 0xC003: 0xC0})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(tc.time, "sleep", lambda s: None)


def test_healthy_machine_sampled_at_one_frame_phase_is_running():
    """Same raster and same idle-loop PC on every read, jiffy ticking."""
    state, seen = tc._machine_progress(_healthy())
    assert state is MS.RUNNING, seen


def test_a_pc_change_counts_as_running():
    """Jiffy clock frozen, PC moving between reads."""
    m = _Machine(irq=False, pcs=(0xC010, 0xC013, 0xC016))
    state, seen = tc._machine_progress(m)
    assert state is MS.RUNNING, seen


def test_nothing_clocked_across_acknowledged_resumes_is_emulator_stopped():
    m = _bug6()
    state, seen = tc._machine_progress(m)
    assert state is MS.EMULATOR_STOPPED, seen
    assert m.resumes >= 3  # the verdict rests on acknowledged resumes


def test_clocked_machine_on_a_kil_opcode_is_jammed():
    state, seen = tc._machine_progress(_kil_jam())
    assert state is MS.JAMMED, seen


def test_jam_event_queued_during_sampling_is_jammed():
    """PC not on a KIL byte (e.g. banked view differs), 0x61 queued."""
    m = _Machine(cpu_runs=False, pcs=(0xC000,), jam_events=True)
    state, seen = tc._machine_progress(m)
    assert state is MS.JAMMED, seen


def test_clocked_machine_spinning_without_a_jam_is_a_masked_spin():
    state, seen = tc._machine_progress(_masked_spin())
    assert state is MS.MASKED_SPIN, seen


def test_a_jam_event_from_before_this_call_does_not_make_a_spin_a_jam():
    m = _masked_spin()
    m._event_queue.append((m._resume_generation - 1, JAM))
    state, seen = tc._machine_progress(m)
    assert state is MS.MASKED_SPIN, seen


def test_bug6_on_a_kil_opcode_is_still_emulator_stopped():
    """A frozen Timer A outranks jam evidence: nothing is being clocked."""
    m = _Machine(clocked=False, cpu_runs=False, pcs=(0xC000,),
                 memory={0xC000: 0x02})
    state, seen = tc._machine_progress(m)
    assert state is MS.EMULATOR_STOPPED, seen


def test_one_readable_sample_is_inconclusive():
    m = _Machine(clocked=False, fail_reads_after=1)
    state, seen = tc._machine_progress(m)
    assert state is None, seen
    assert len(seen) == 1


def test_unacknowledged_resume_ends_the_run_inconclusive():
    """No comparison may rest on a resume that did not return."""
    m = _Machine(clocked=False, fail_resumes_after=0)
    state, seen = tc._machine_progress(m)
    assert state is None, seen
    assert len(seen) == 1


def test_run_cut_short_is_judged_on_the_comparisons_it_made():
    """Reads fail after two samples: one acknowledged comparison, no change."""
    m = _bug6()
    m.fail_reads_after = 2
    state, seen = tc._machine_progress(m)
    assert state is MS.EMULATOR_STOPPED, seen
    assert len(seen) == 2


_MARKERS = {
    None: "inconclusive",
    MS.RUNNING: "(running",
    MS.EMULATOR_STOPPED: "EMULATOR STOPPED",
    MS.JAMMED: "JAMMED",
    MS.MASKED_SPIN: "SPINNING WITH IRQs MASKED",
}


@pytest.mark.parametrize("make, expected", [
    (lambda: _Machine(fail_resumes_after=0), None),
    (_healthy, MS.RUNNING),
    (_bug6, MS.EMULATOR_STOPPED),
    (_kil_jam, MS.JAMMED),
    (_masked_spin, MS.MASKED_SPIN),
])
def test_report_states_exactly_one_cause(make, expected):
    tc._LAST_POLL_START_GEN[:] = []
    report = tc._machine_failure_report(make(), "5")
    line = next(l for l in report.splitlines()
                if l.startswith("machine across acknowledged resumes"))
    hits = [s for s, marker in _MARKERS.items() if marker in line]
    assert hits == [expected], line
