"""``wait_for_memory`` resumes a halting backend and leaves a DMA one alone.

Issue #514.  The VICE behaviour is measured in
``tests/test_wait_for_memory_vice_live.py``; these fakes pin the two
backend contracts a live VICE cannot show:

* a transport whose reads halt the CPU (VICE, or anything that does not
  declare otherwise) is running again on every exit path -- match,
  timeout, a predicate that raises, a read that raises;
* a transport that declares ``halts_cpu_on_access = False`` (the Ultimate
  64, where ``resume()`` is a real ``machine:resume`` that clears a
  deliberate pause, #189) never receives a resume at all.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness import wait_for_memory
from c64_test_harness.backends.hardware import HardwareTransportBase
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.vice_binary import BinaryViceTransport

FLAG = 0xC9F0
DONE = 0xA5
#: A poll loop that ignores its deadline would spin here for ever; the
#: fakes turn that into a failure instead of a hung test run.
READ_CAP = 5000
WALL_CAP = 5.0


class HaltingFake:
    """A guest that advances only while running; every read halts it.

    The flag becomes ``DONE`` once the guest has been resumed
    ``resumes_to_finish`` times -- so a poll loop that never resumes never
    sees it, exactly as #514 measured on VICE.
    """

    def __init__(self, resumes_to_finish: int = 3, *, raise_on_read: int | None = None):
        self.resumes_to_finish = resumes_to_finish
        self.raise_on_read = raise_on_read
        self.halted = False
        self.reads = 0
        self.resumes = 0
        self.born = time.monotonic()

    def read_memory(self, addr: int, length: int) -> bytes:
        self.reads += 1
        if self.reads > READ_CAP or time.monotonic() - self.born > WALL_CAP:
            raise AssertionError("poll loop ignored its timeout")
        self.halted = True
        if self.reads == self.raise_on_read:
            raise OSError("wire dropped mid-read")
        done = self.resumes >= self.resumes_to_finish
        return bytes([DONE if done else 0x00]) * length

    def resume(self) -> None:
        self.halted = False
        self.resumes += 1


class DmaFake:
    """A U64-shaped guest: reads do not halt, and resume must not be sent."""

    halts_cpu_on_access = False

    def __init__(self, reads_to_finish: int = 3):
        self.reads_to_finish = reads_to_finish
        self.reads = 0
        self.resumes = 0
        self.born = time.monotonic()

    def read_memory(self, addr: int, length: int) -> bytes:
        self.reads += 1
        if self.reads > READ_CAP or time.monotonic() - self.born > WALL_CAP:
            raise AssertionError("poll loop ignored its timeout")
        done = self.reads >= self.reads_to_finish
        return bytes([DONE if done else 0x00]) * length

    def resume(self) -> None:
        # Counted, not raised: the helper swallows a failing resume
        # (best-effort, like the screen waiters), so a raise would hide
        # exactly the call this fake exists to catch.
        self.resumes += 1


# -- halting backend -------------------------------------------------------

def test_halting_backend_is_resumed_between_polls_until_the_flag_appears():
    t = HaltingFake(resumes_to_finish=3)
    assert wait_for_memory(t, FLAG, DONE, timeout=5.0, poll_interval=0) == bytes([DONE])
    assert t.reads == 4 and not t.halted


def test_halting_backend_running_after_a_first_poll_match():
    t = HaltingFake(resumes_to_finish=0)
    assert wait_for_memory(t, FLAG, DONE, timeout=5.0) == bytes([DONE])
    assert (t.reads, t.resumes, t.halted) == (1, 1, False)


def test_halting_backend_running_after_timeout_and_deadline_honoured():
    t = HaltingFake(resumes_to_finish=10**9)
    start = time.monotonic()
    assert wait_for_memory(t, FLAG, DONE, timeout=0.3, poll_interval=0.05) is None
    elapsed = time.monotonic() - start
    assert 0.3 <= elapsed < 1.0, elapsed
    assert t.reads > 1 and not t.halted


def test_a_poll_interval_longer_than_the_timeout_does_not_overrun_it():
    t = HaltingFake(resumes_to_finish=10**9)
    start = time.monotonic()
    assert wait_for_memory(t, FLAG, DONE, timeout=0.2, poll_interval=30.0) is None
    assert time.monotonic() - start < 1.0
    assert not t.halted


def test_halting_backend_running_after_the_predicate_raises():
    t = HaltingFake()

    def boom(data: bytes) -> bool:
        raise RuntimeError("predicate failed")

    with pytest.raises(RuntimeError, match="predicate failed"):
        wait_for_memory(t, FLAG, boom, timeout=5.0)
    assert not t.halted


def test_halting_backend_running_after_a_read_raises():
    t = HaltingFake(resumes_to_finish=10**9, raise_on_read=2)
    with pytest.raises(OSError, match="wire dropped"):
        wait_for_memory(t, FLAG, DONE, timeout=5.0, poll_interval=0)
    assert not t.halted


# -- non-halting (Ultimate 64) backend --------------------------------------

@pytest.mark.parametrize("reads_to_finish", [1, 4])
def test_dma_backend_never_resumed_on_a_match(reads_to_finish):
    t = DmaFake(reads_to_finish)
    assert wait_for_memory(t, FLAG, DONE, timeout=5.0, poll_interval=0) == bytes([DONE])
    assert (t.reads, t.resumes) == (reads_to_finish, 0)


def test_dma_backend_never_resumed_on_timeout():
    t = DmaFake(reads_to_finish=10**9)
    assert wait_for_memory(t, FLAG, DONE, timeout=0.1, poll_interval=0.01) is None
    assert t.reads > 1 and t.resumes == 0


def test_dma_backend_never_resumed_when_the_predicate_raises():
    t = DmaFake()

    def boom(data: bytes) -> bool:
        raise RuntimeError("predicate failed")

    with pytest.raises(RuntimeError, match="predicate failed"):
        wait_for_memory(t, FLAG, boom, timeout=5.0)
    assert (t.reads, t.resumes) == (1, 0)


def test_the_real_transports_declare_their_halting_behaviour():
    # What wait_for_memory keys on: the U64 must opt out, VICE must not.
    assert Ultimate64Transport.halts_cpu_on_access is False
    assert BinaryViceTransport.halts_cpu_on_access is True


# -- matching --------------------------------------------------------------

def test_bytes_and_predicate_forms_read_the_requested_length():
    t = HaltingFake(resumes_to_finish=0)
    assert wait_for_memory(t, FLAG, bytes([DONE, DONE]), timeout=1.0) == bytes([DONE, DONE])
    seen = []
    assert wait_for_memory(t, FLAG, lambda d: seen.append(d) or True, length=3,
                           timeout=1.0) == bytes([DONE] * 3)
    assert seen == [bytes([DONE] * 3)]


@pytest.mark.parametrize("kwargs", [
    dict(addr=True, expected=DONE),
    dict(addr=FLAG, expected=256),
    dict(addr=FLAG, expected=True),
    dict(addr=FLAG, expected=b""),
    dict(addr=FLAG, expected=b"\x01\x02", length=3),
    dict(addr=FLAG, expected=DONE, length=2),
    dict(addr=FLAG, expected=DONE, timeout=-1),
])
def test_malformed_arguments_refused_before_the_transport_is_touched(kwargs):
    t = HaltingFake()
    with pytest.raises((ValueError, TypeError)):
        wait_for_memory(t, kwargs.pop("addr"), kwargs.pop("expected"), **kwargs)
    assert (t.reads, t.resumes) == (0, 0)


# -- the documented hardware extension point --------------------------------

class _BareHardware(HardwareTransportBase):
    """A hardware backend built on the extension point, resume() not overridden
    (the base raises NotImplementedError)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read_memory(self, addr: int, length: int) -> bytes:
        self.reads += 1
        return bytes([DONE if self.reads >= 3 else 0]) * length


class _PausableHardware(_BareHardware):
    """One with a real resume(), which would clear a deliberate pause (#189)."""

    def __init__(self) -> None:
        super().__init__()
        self.resumes = 0

    def resume(self) -> None:
        self.resumes += 1


def test_hardware_base_is_not_resumed(caplog):
    t = _PausableHardware()
    with caplog.at_level("WARNING"):
        assert wait_for_memory(t, FLAG, DONE, timeout=5.0, poll_interval=0) == bytes([DONE])
    assert t.resumes == 0
    assert not caplog.records


def test_hardware_base_without_resume_logs_nothing(caplog):
    t = _BareHardware()
    with caplog.at_level("WARNING"):
        assert wait_for_memory(t, FLAG, DONE, timeout=5.0, poll_interval=0) == bytes([DONE])
    assert not caplog.records, [r.getMessage() for r in caplog.records]


# -- arguments and best-effort resume ---------------------------------------

def test_negative_poll_interval_refused_before_the_transport_is_touched():
    t = HaltingFake()
    with pytest.raises(ValueError, match="poll_interval"):
        wait_for_memory(t, FLAG, DONE, poll_interval=-0.1)
    assert t.reads == 0


def test_zero_length_refused_for_a_predicate():
    t = HaltingFake()
    with pytest.raises(ValueError, match="length"):
        wait_for_memory(t, FLAG, lambda d: True, length=0)
    assert t.reads == 0


class _ResumeRaises(HaltingFake):
    def resume(self) -> None:
        self.resumes += 1
        raise OSError("monitor socket closed")


def test_a_resume_that_raises_is_best_effort(caplog):
    # Between polls and in the finally alike: raising there would replace
    # the caller's result (or exception) with the resume's.  Logged, not
    # raised.  Two failing resumes between polls, then the exit one.
    t = _ResumeRaises(resumes_to_finish=2)
    with caplog.at_level("WARNING", logger="c64_test_harness.screen"):
        assert wait_for_memory(t, FLAG, DONE, timeout=5.0,
                               poll_interval=0) == bytes([DONE])
    assert t.resumes == 3
    assert sum("resume() failed" in r.getMessage() for r in caplog.records) == 3


def test_a_resume_that_raises_still_times_out_cleanly():
    t = _ResumeRaises(resumes_to_finish=10**9)
    assert wait_for_memory(t, FLAG, DONE, timeout=0.1, poll_interval=0.01) is None
    assert t.resumes > 1
