"""A lock held by the waiting thread must not extend the acquire deadline.

Queue-aware ``acquire`` extends its deadline for as long as the holder
looks healthy: PID alive and lockfile mtime fresh. When the holder is
the *same thread* that is now waiting, both signals are maximally
healthy by construction -- the PID is alive by definition, and the
holder's own heartbeat thread keeps bumping the mtime -- so the waiter
resets its deadline on every poll and ``timeout`` is never reached. The
result is a permanent hang indistinguishable from a healthy queue.

The fix is narrow on purpose. It does **not** forbid the wait: another
thread holding a reference to the holder can still release it in time,
and ``test_device_lock.py::TestBlockingTimeout::test_acquire_succeeds_after_release``
covers exactly that and must keep passing. It only stops a self-held
lock from counting as *progress*, so the wait is bounded by ``timeout``
again and a genuine self-deadlock ends in a diagnosable timeout instead
of hanging for ever.

The unbounded extension for holders in other threads and other
processes is documented, by design, and unchanged.

Every acquire below runs on a bounded thread, so a regression fails the
test instead of hanging the suite.
"""
from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.device_lock import DeviceLock

HOST = "10.0.0.64"


def _hold_lock(lock_dir: str, ready, stop) -> None:
    """Subprocess entry point: hold the device lock until told to stop."""
    lock = DeviceLock(HOST, Path(lock_dir), heartbeat_interval=0.05)
    if not lock.acquire(timeout=10.0):
        return
    try:
        ready.set()
        stop.wait(30.0)
    finally:
        lock.release()


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "locks"


def _run_bounded(fn, *, join_timeout: float = 10.0):
    """Run *fn* on a daemon thread with a deadline.

    Returns ``("return", value)`` or ``("raise", exc)``. Fails the test
    if *fn* has not come back in time -- which is the regression this
    module exists to catch. The whole scenario runs on the worker
    thread, so a lock taken inside *fn* is held by the same thread that
    later waits on it.
    """
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- reported, not swallowed
            box["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(join_timeout)
    if thread.is_alive():
        pytest.fail(
            f"did not return within {join_timeout}s -- the waiter is extending "
            f"its deadline against a lock its own thread holds"
        )
    if "exc" in box:
        return "raise", box["exc"]
    return "return", box["value"]


class TestSelfHeldDoesNotExtendDeadline:
    def test_self_held_wait_times_out_instead_of_hanging(
        self, lock_dir: Path
    ) -> None:
        """The whole scenario runs on one thread: hold, then wait."""

        def scenario():
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                started = time.monotonic()
                got = inner.acquire(timeout=0.5, progress_window=60.0)
                return got, time.monotonic() - started
            finally:
                outer.release()

        kind, result = _run_bounded(scenario)
        assert kind == "return", f"unexpected raise: {result!r}"
        got, elapsed = result
        assert got is False
        assert elapsed < 5.0, f"waited {elapsed:.1f}s -- deadline still extending"

    def test_helper_thread_can_still_rescue_a_self_held_wait(
        self, lock_dir: Path
    ) -> None:
        """Bounded, not forbidden -- the supported pattern still works.

        Mirrors ``test_acquire_succeeds_after_release``: the waiting
        thread holds the lock, and a second thread releases it mid-wait.
        """

        def scenario() -> bool:
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            threading.Timer(0.2, outer.release).start()
            inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            got = inner.acquire(timeout=5.0, progress_window=60.0)
            if got:
                inner.release()
            return got

        assert _run_bounded(scenario) == ("return", True)

    def test_allow_nested_still_joins(self, lock_dir: Path) -> None:
        """The documented nested-hold path is untouched."""

        def scenario() -> bool:
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(
                    HOST, lock_dir, heartbeat_interval=0.05, allow_nested=True
                )
                got = inner.acquire(timeout=5.0)
                if got:
                    inner.release()
                return got
            finally:
                outer.release()

        assert _run_bounded(scenario) == ("return", True)


class TestProgressSignal:
    """Unit-level checks on the predicate itself."""

    def test_self_thread_hold_is_not_progress(self, lock_dir: Path) -> None:
        holder = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
        assert holder.acquire(timeout=5.0)
        try:
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert waiter._holder_is_progressing(60.0) is False
        finally:
            holder.release()

    def test_other_thread_hold_is_still_progress(self, lock_dir: Path) -> None:
        """A holder in another thread must keep extending the deadline."""
        acquired = threading.Event()
        release = threading.Event()
        result: dict[str, bool] = {}

        def hold() -> None:
            lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            result["got"] = lock.acquire(timeout=5.0)
            acquired.set()
            release.wait(10.0)
            lock.release()

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        try:
            assert acquired.wait(10.0) and result.get("got")
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert waiter._holder_is_progressing(60.0) is True
        finally:
            release.set()
            thread.join(10.0)

    def test_foreign_process_hold_is_still_progress(
        self, lock_dir: Path
    ) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        ready = multiprocessing.Event()
        stop = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_hold_lock, args=(str(lock_dir), ready, stop)
        )
        holder.start()
        try:
            assert ready.wait(10.0), "subprocess never acquired the lock"
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert waiter._holder_is_progressing(60.0) is True
        finally:
            stop.set()
            holder.join(10.0)
            if holder.is_alive():
                holder.terminate()


class TestNoFalsePositives:
    def test_reacquire_after_release_succeeds(self, lock_dir: Path) -> None:
        """``release()`` deliberately leaves the lockfile behind.

        So the lockfile still names this PID after a clean release. The
        thread registry has to be cleared on release, or a later acquire
        in the same thread would wrongly look self-held.
        """
        first = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
        assert first.acquire(timeout=5.0)
        first.release()
        assert list(lock_dir.glob("*.lock")), "release() should leave the lockfile"

        second = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
        assert second._held_by_this_thread() is False
        assert second.acquire(timeout=5.0)
        second.release()

    def test_uncontended_acquire_unaffected(self, lock_dir: Path) -> None:
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
        assert lock.acquire(timeout=5.0)
        lock.release()
