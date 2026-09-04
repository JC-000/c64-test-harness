"""The shared binary-monitor screen pollers leave the CPU running (#186).

``conftest.binary_wait_for_text`` / ``binary_wait_for_load_complete`` are
what the disk, ethernet and bridge suites poll with.  Before this they were
four separate copies of one loop that resumed at the *top* and returned the
match straight after the screen read that halted the machine -- so a
successful wait handed back a stopped C64, the #184 defect that PR #185
fixed only in the library waiters.

These are the mocked tests for that contract; the live disk/ethernet suites
that use the helpers need VICE and are not run here.
"""

from __future__ import annotations

from conftest import binary_wait_for_load_complete, binary_wait_for_text
from test_screen import ResumeCountingTransport, _screen_with

POLL = 0.01


def _load_complete_screen() -> ResumeCountingTransport:
    return _screen_with("LOADING" + " " * 33 + "READY.")


def test_wait_for_text_match_leaves_the_cpu_running():
    t = _screen_with("READY.")
    grid = binary_wait_for_text(t, "READY.", timeout=1.0, poll_interval=POLL)
    assert grid is not None
    assert t.ops[-1] == "resume", t.ops


def test_wait_for_text_timeout_leaves_the_cpu_running():
    t = ResumeCountingTransport()
    assert binary_wait_for_text(t, "NEVER", timeout=0.05,
                                poll_interval=POLL) is None
    assert t.ops[-1] == "resume", t.ops


def test_wait_for_load_complete_match_leaves_the_cpu_running():
    """The two-marker LOADING/READY. predicate is why this one could not
    just become a call to the library ``wait_for_text``."""
    t = _load_complete_screen()
    grid = binary_wait_for_load_complete(t, timeout=1.0, poll_interval=POLL)
    assert grid is not None
    assert t.ops[-1] == "resume", t.ops


def test_wait_for_load_complete_timeout_leaves_the_cpu_running():
    t = _screen_with("LOADING")  # never reaches READY.
    assert binary_wait_for_load_complete(t, timeout=0.05,
                                         poll_interval=POLL) is None
    assert t.ops[-1] == "resume", t.ops


def test_reads_that_raise_still_leave_the_cpu_running():
    t = ResumeCountingTransport(fail_reads=True)
    assert binary_wait_for_text(t, "READY.", timeout=0.05,
                                poll_interval=POLL) is None
    assert t.ops[-1] == "resume", t.ops


def test_the_screen_read_precedes_the_final_resume():
    """Ordering, not counting: these helpers resumed between polls long
    before the fix, so ``resume in ops`` passes on the broken version too.
    What changed is that the *last* op is a resume rather than the read
    that halted the machine."""
    t = _screen_with("READY.")
    binary_wait_for_text(t, "READY.", timeout=1.0, poll_interval=POLL)
    assert t.ops[-2:] == ["read", "resume"], t.ops
