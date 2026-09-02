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
