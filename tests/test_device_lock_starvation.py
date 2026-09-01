"""#162: a waiter must not be reset forever by a *chain* of holders.

``acquire``'s queue-aware deadline is re-armed on every poll while the
holder looks healthy. That is deliberate for one long holder -- a C64U
REUWRITE drain runs to 19s, ``reboot()`` ~8s, a full capture minutes,
and failing those would land on the waiter, which has done nothing
wrong. It is *not* deliberate for a handoff chain: ``release()`` bumps
mtime before dropping the flock, and the incoming holder's own
``_write_metadata`` bumps it again, so two lanes trading the device read
as one continuously-progressing holder. A third waiter is then starved
by a "holder" whose identity keeps changing, with nobody to diagnose.

Identity, not freshness, is what separates the two.

**What these tests cannot do.** A real handoff chain is a race between
independent processes; producing one on demand, at a rate that beats a
100ms poll, is not something a test can do reliably, and a test that
appeared to would be measuring its own scheduling. So the holder
*identity sequence* is scripted at ``_holder_progress`` -- the single
place the loop learns who the holder is -- while a genuinely held flock
keeps the acquire blocked. The chain is simulated; the loop's reaction
to it is real.

Every test that waits pairs its assertion with the opposite case. "The
acquire finished within N seconds" passes against both behaviours on its
own, so each bounded case is stated alongside an unbounded one.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from c64_test_harness.backends import device_lock as dl
from c64_test_harness.backends.device_lock import DeviceLock

HOST = "10.0.0.64"


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    d = tmp_path / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def blocked(lock_dir: Path):
    """A held lock, so the waiter under test can never actually acquire."""
    holder = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
    assert holder.acquire(timeout=5.0)
    try:
        yield holder
    finally:
        holder.release()


def _waiter_thread(lock: DeviceLock, **kwargs) -> tuple[threading.Thread, dict]:
    box: dict[str, object] = {}

    def run() -> None:
        started = time.monotonic()
        try:
            box["value"] = lock.acquire(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
        box["elapsed"] = time.monotonic() - started

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _script(pids):
    """Return a ``_holder_progress`` stand-in yielding *pids* in order."""
    seq = list(pids)
    idx = {"i": 0}

    def progress(progress_window):
        i = min(idx["i"], len(seq) - 1)
        idx["i"] += 1
        return True, seq[i]

    return progress


class TestIdentityTracking:
    def test_single_holder_still_extends_indefinitely(
        self, lock_dir: Path, blocked
    ) -> None:
        """The documented case. Must not become bounded.

        This is the control for every bounded case below: same timeout,
        same poll rate, only the identity sequence differs.
        """
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(waiter, "_holder_progress", _script([4242])):
            t, box = _waiter_thread(waiter, timeout=0.3, progress_window=60.0)
            t.join(2.0)
            still_waiting = t.is_alive()
        assert still_waiting, (
            f"a single unchanging holder stopped extending after "
            f"{box.get('elapsed')}s -- the documented long-hold case broke"
        )

    def test_a_chain_of_holders_stops_extending(
        self, lock_dir: Path, blocked
    ) -> None:
        """Same timeout as the control; only the identity keeps changing."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        chain = _script(range(1000, 9999))
        with patch.object(waiter, "_holder_progress", chain):
            t, box = _waiter_thread(waiter, timeout=0.3, progress_window=60.0)
            t.join(10.0)
        assert not t.is_alive(), "a handoff chain extended the deadline forever"
        assert box["value"] is False

    def test_a_handoff_does_not_collapse_the_budget(
        self, lock_dir: Path, blocked
    ) -> None:
        """Being overtaken must not fail the waiter on the spot.

        The waiter is entitled to the timeout it asked for, measured
        from when the chain was first apparent.
        """
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(waiter, "_holder_progress", _script(range(1000, 9999))):
            t, box = _waiter_thread(waiter, timeout=0.5, progress_window=60.0)
            t.join(10.0)
        assert not t.is_alive()
        assert box["elapsed"] >= 0.5, (
            f"returned after {box['elapsed']:.2f}s, less than the 0.5s asked "
            f"for -- the budget collapsed on the first handoff"
        )

    def test_a_few_handoffs_are_tolerated_then_a_long_holder_waits(
        self, lock_dir: Path, blocked
    ) -> None:
        """Arriving as a short hold ends is ordinary, not pathological.

        Two handoffs, then one holder settles in. That holder must get
        the same indefinite extension it would have got had the waiter
        arrived a moment later.
        """
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        seq = [111, 111, 222, 222, 333] + [333] * 5000
        with patch.object(waiter, "_holder_progress", _script(seq)):
            t, box = _waiter_thread(waiter, timeout=0.3, progress_window=60.0)
            t.join(2.0)
            still_waiting = t.is_alive()
        assert still_waiting, (
            f"two handoffs before a settled holder ended the wait after "
            f"{box.get('elapsed')}s -- ordinary timing was treated as a chain"
        )

    def test_unreadable_identity_does_not_count_as_a_handoff(
        self, lock_dir: Path, blocked
    ) -> None:
        """A missed read is not evidence the holder changed.

        ``None`` means we could not tell who holds it. Treating that as
        a new identity would turn a flaky read into a starvation verdict.
        """
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        # Alternates often enough that miscounting None as a change would
        # blow the handoff budget several times over. Two alternations
        # would not: they only produce two miscounts, under the limit,
        # so the test would pass against the bug.
        seq = [777, None] * 8 + [777] * 5000
        with patch.object(waiter, "_holder_progress", _script(seq)):
            t, box = _waiter_thread(waiter, timeout=0.3, progress_window=60.0)
            t.join(2.0)
            still_waiting = t.is_alive()
        assert still_waiting, (
            f"intermittent unreadable identity was counted as handoffs and "
            f"ended the wait after {box.get('elapsed')}s"
        )


class TestStarvationWarning:
    def test_extending_waiter_logs_who_it_is_behind(
        self, lock_dir: Path, blocked, caplog
    ) -> None:
        """A starving waiter must be able to say what it is behind."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.WARNING, logger=dl.__name__):
            with patch.object(
                waiter, "_holder_progress", _script([4242])
            ), patch.object(dl, "_EXTENSION_LOG_INTERVAL", 0.2):
                t, _ = _waiter_thread(waiter, timeout=0.3, progress_window=60.0)
                t.join(1.5)
        messages = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert messages, f"a waiter extended silently: {caplog.text!r}"
        joined = " ".join(messages)
        assert "4242" in joined, f"the holder PID is not in {joined!r}"
        assert HOST in joined, f"the device is not named in {joined!r}"

    def test_warning_reports_the_changing_identity(
        self, lock_dir: Path, blocked, caplog
    ) -> None:
        """A chain must be visibly different from a long hold in the log.

        Two separate messages carry that: the periodic one says the
        identity has been changing, and a final one says extension has
        stopped and why. Both are asserted, because each alone leaves
        the other free to be dropped without any test noticing.
        """
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.WARNING, logger=dl.__name__):
            with patch.object(
                waiter, "_holder_progress", _script(range(1000, 9999))
            ), patch.object(dl, "_EXTENSION_LOG_INTERVAL", 0.0):
                t, _ = _waiter_thread(waiter, timeout=0.4, progress_window=60.0)
                t.join(10.0)
        messages = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        joined = " ".join(messages)
        # Pinned on the periodic message specifically ("still queued"),
        # not just on the word appearing anywhere: the final message also
        # says "handoff", so a looser assertion let the periodic clause be
        # dropped with no test noticing.
        assert any(
            "still queued" in m.lower() and "changed" in m.lower()
            for m in messages
        ), (
            f"while extending, nothing distinguishes a chain from a long "
            f"hold: {joined!r}"
        )
        assert any("no longer extending" in m.lower() for m in messages), (
            f"the waiter stopped extending without saying so: {joined!r}"
        )


class TestHolderProgress:
    """The identity read itself.

    The holder runs on another thread throughout: a same-thread hold is
    short-circuited by the self-deadlock guard (``_held_by_this_thread``)
    and reports ``(False, None)`` by design, so it cannot be used to
    exercise the identity read.
    """

    @staticmethod
    def _hold_elsewhere(lock_dir: Path):
        acquired = threading.Event()
        release = threading.Event()
        state: dict[str, object] = {}

        def hold() -> None:
            lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            state["got"] = lock.acquire(timeout=5.0)
            acquired.set()
            release.wait(10.0)
            lock.release()

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        assert acquired.wait(10.0) and state.get("got")
        return t, release

    def test_reports_the_live_holder_pid(self, lock_dir: Path) -> None:
        import os

        thread, release = self._hold_elsewhere(lock_dir)
        try:
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
            progressing, pid = waiter._holder_progress(60.0)
            assert progressing is True
            assert pid == os.getpid()
        finally:
            release.set()
            thread.join(10.0)

    def test_reports_none_when_unreadable(self, lock_dir: Path) -> None:
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        waiter._lock_path.write_text("null")
        assert waiter._holder_progress(60.0) == (False, None)

    def test_is_progressing_wrapper_still_works(self, lock_dir: Path) -> None:
        """Existing callers and tests use the boolean form."""
        thread, release = self._hold_elsewhere(lock_dir)
        try:
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
            assert waiter._holder_is_progressing(60.0) is True
        finally:
            release.set()
            thread.join(10.0)

    def test_self_thread_hold_still_reports_not_progressing(
        self, lock_dir: Path
    ) -> None:
        """The self-deadlock guard must survive the tuple split."""
        holder = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
        assert holder.acquire(timeout=5.0)
        try:
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
            assert waiter._holder_progress(60.0) == (False, None)
        finally:
            holder.release()
