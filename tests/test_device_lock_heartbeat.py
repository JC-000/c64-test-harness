"""Tests for DeviceLock heartbeat (Task A) and DeviceLockTimeout (Task B).

These cover the two behavioural additions in ``device_lock.py``:

1. **Heartbeat** — a daemon thread that periodically bumps the lockfile
   mtime while the lock is held, so cross-process waiters using the
   ``progress_window`` queue-aware semantics see the holder as
   "progressing" indefinitely.
2. **``DeviceLockTimeout``** — a structured exception with holder PID,
   liveness, lockfile age, and REST reachability so callers can
   distinguish "queued behind a healthy holder" from "device wedged or
   unreachable" without guessing.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from c64_test_harness.backends.device_lock import (
    DeviceLock,
    DeviceLockTimeout,
)


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Provide a temporary lock directory for each test."""
    d = tmp_path / "locks"
    d.mkdir()
    return d


# ===========================================================================
# Task A — heartbeat
# ===========================================================================


class TestHeartbeatAdvancesMtime:
    def test_heartbeat_bumps_mtime_while_held(self, lock_dir: Path) -> None:
        """With a short heartbeat interval, mtime advances while held."""
        lock = DeviceLock(
            "10.10.0.1", lock_dir=lock_dir, heartbeat_interval=0.1
        )
        assert lock.acquire(timeout=1.0)
        try:
            path = lock._lock_path
            mtime_initial = path.stat().st_mtime
            # Sleep well past one heartbeat tick.
            time.sleep(0.6)
            mtime_later = path.stat().st_mtime
            assert mtime_later > mtime_initial, (
                f"heartbeat should advance mtime "
                f"(initial={mtime_initial}, later={mtime_later})"
            )
        finally:
            lock.release()

    def test_heartbeat_disabled_with_none(self, lock_dir: Path) -> None:
        """heartbeat_interval=None disables the thread entirely."""
        lock = DeviceLock(
            "10.10.0.2", lock_dir=lock_dir, heartbeat_interval=None
        )
        assert lock.acquire(timeout=1.0)
        try:
            assert lock._heartbeat_thread is None
        finally:
            lock.release()

    def test_heartbeat_disabled_with_zero(self, lock_dir: Path) -> None:
        """heartbeat_interval=0 disables the thread entirely."""
        lock = DeviceLock(
            "10.10.0.3", lock_dir=lock_dir, heartbeat_interval=0
        )
        assert lock.acquire(timeout=1.0)
        try:
            assert lock._heartbeat_thread is None
        finally:
            lock.release()


class TestHeartbeatStopsOnRelease:
    def test_no_mtime_changes_after_release(self, lock_dir: Path) -> None:
        """After release(), the heartbeat thread must stop touching mtime."""
        lock = DeviceLock(
            "10.10.0.4", lock_dir=lock_dir, heartbeat_interval=0.1
        )
        assert lock.acquire(timeout=1.0)
        path = lock._lock_path
        # Let the heartbeat run for a tick first.
        time.sleep(0.3)
        lock.release()
        # Heartbeat thread should be done shortly.
        if lock._heartbeat_thread is not None:
            lock._heartbeat_thread.join(timeout=2.0)
        mtime_post_release = path.stat().st_mtime
        # Sleep past several would-be heartbeats.
        time.sleep(0.5)
        mtime_after = path.stat().st_mtime
        assert mtime_after == mtime_post_release, (
            f"heartbeat must stop on release "
            f"(post_release={mtime_post_release}, after_sleep={mtime_after})"
        )


class TestHeartbeatSurvivesUnlink:
    def test_unlinked_lockfile_does_not_leak_exception(
        self, lock_dir: Path
    ) -> None:
        """If the lockfile is unlinked under the heartbeat, it stops quietly."""
        # Capture any uncaught thread exceptions.
        caught: list[BaseException] = []

        def hook(args: threading.ExceptHookArgs) -> None:  # type: ignore[name-defined]
            caught.append(args.exc_value)

        old_hook = threading.excepthook
        threading.excepthook = hook
        try:
            lock = DeviceLock(
                "10.10.0.5", lock_dir=lock_dir, heartbeat_interval=0.1
            )
            assert lock.acquire(timeout=1.0)
            try:
                # Unlink the lockfile from outside while it's "held".
                os.unlink(str(lock._lock_path))
                # Wait past several intervals so the thread tries to bump.
                time.sleep(0.5)
                # No exceptions should have leaked out of the thread.
                assert not caught, f"heartbeat leaked exceptions: {caught}"
                # Thread should be dead.
                t = lock._heartbeat_thread
                if t is not None:
                    t.join(timeout=2.0)
                    assert not t.is_alive(), "heartbeat thread should have exited"
            finally:
                # release() must not blow up on the unlinked file either.
                lock.release()
        finally:
            threading.excepthook = old_hook


# Subprocess helper for the "waiter does not time out" case.


def _hold_with_heartbeat_worker(
    lock_dir_str: str,
    host: str,
    started: "multiprocessing.synchronize.Event",
    stop: "multiprocessing.synchronize.Event",
    heartbeat_interval: float,
) -> None:
    """Hold a DeviceLock with heartbeat enabled until told to stop."""
    lock = DeviceLock(
        host,
        lock_dir=Path(lock_dir_str),
        heartbeat_interval=heartbeat_interval,
    )
    if not lock.acquire(timeout=5.0):
        return
    started.set()
    try:
        # Sit doing nothing — heartbeat alone is what keeps us "progressing".
        stop.wait(timeout=10.0)
    finally:
        lock.release()


class TestWaiterBlocksOnHeartbeatOnlyHolder:
    def test_waiter_does_not_time_out_against_heartbeat_only_holder(
        self, lock_dir: Path
    ) -> None:
        """The heartbeat alone keeps a sleeping holder "progressing".

        Without the heartbeat, a small ``progress_window`` would expire
        and the waiter's ``acquire()`` would return False.  With the
        heartbeat at ~0.2s vs a ``progress_window`` of 0.5s, the holder
        always looks fresh — so the waiter blocks past its nominal
        ``timeout`` of 1.0s.
        """
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        stop = ctx.Event()
        proc = ctx.Process(
            target=_hold_with_heartbeat_worker,
            args=(str(lock_dir), "10.10.0.6", started, stop, 0.2),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0), "child never acquired the lock"

            contender = DeviceLock("10.10.0.6", lock_dir=lock_dir)

            done = threading.Event()
            outcome: dict[str, object] = {}

            def waiter() -> None:
                try:
                    outcome["result"] = contender.acquire(
                        timeout=1.0, progress_window=0.5
                    )
                except BaseException as exc:  # pragma: no cover
                    outcome["error"] = exc
                finally:
                    done.set()

            t = threading.Thread(target=waiter, daemon=True)
            t.start()

            # If the heartbeat is working, the waiter should still be
            # blocked at the 2-second mark even though its nominal
            # timeout was 1.0s.
            still_waiting = not done.wait(timeout=2.0)
            assert still_waiting, (
                "waiter completed too early — heartbeat is not extending "
                f"the deadline (outcome={outcome!r})"
            )

            # Now release the holder so the waiter can finish cleanly.
            stop.set()
            assert done.wait(timeout=5.0), "waiter never finished"
            # Either it acquired (True) or timed out cleanly (False);
            # if it acquired, release.
            if outcome.get("result") is True:
                contender.release()
        finally:
            stop.set()
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)


# ===========================================================================
# Task B — DeviceLockTimeout
# ===========================================================================


def _hold_live_progressing_worker(
    lock_dir_str: str,
    host: str,
    started: "multiprocessing.synchronize.Event",
    stop: "multiprocessing.synchronize.Event",
) -> None:
    """Hold a DeviceLock with heartbeat enabled until told to stop.

    Used for the "live + progressing holder" diagnostic case.
    """
    lock = DeviceLock(
        host,
        lock_dir=Path(lock_dir_str),
        heartbeat_interval=0.1,
    )
    if not lock.acquire(timeout=5.0):
        return
    started.set()
    try:
        stop.wait(timeout=10.0)
    finally:
        lock.release()


def _hold_stuck_holder_worker(
    lock_dir_str: str,
    host: str,
    started: "multiprocessing.synchronize.Event",
    stop: "multiprocessing.synchronize.Event",
) -> None:
    """Hold a DeviceLock with heartbeat disabled — a stuck holder.

    Used for the "alive but lockfile stale" wedged-holder case.
    """
    lock = DeviceLock(
        host,
        lock_dir=Path(lock_dir_str),
        heartbeat_interval=None,
    )
    if not lock.acquire(timeout=5.0):
        return
    started.set()
    try:
        stop.wait(timeout=10.0)
    finally:
        lock.release()


@pytest.fixture
def _mock_rest_unreachable():
    """Force the REST reachability probe to fail across a test."""
    with patch(
        "c64_test_harness.backends.device_lock.urllib.request.urlopen",
        side_effect=urllib.error.URLError("mocked: no network"),
    ):
        yield


class TestDeviceLockTimeoutLiveProgressing:
    def test_live_progressing_holder_diagnosis(
        self, lock_dir: Path, _mock_rest_unreachable
    ) -> None:
        """Live, heartbeat-progressing holder → "queued behind live, progressing"."""
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        stop = ctx.Event()
        proc = ctx.Process(
            target=_hold_live_progressing_worker,
            args=(str(lock_dir), "10.20.0.1", started, stop),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0)

            contender = DeviceLock("10.20.0.1", lock_dir=lock_dir)
            # progress_window=None forces a hard timeout so we actually hit
            # the error path even though the holder is progressing.
            with pytest.raises(DeviceLockTimeout) as excinfo:
                contender.acquire_or_raise(
                    timeout=0.5, progress_window=None
                )

            err = excinfo.value
            assert err.holder_pid == proc.pid
            assert err.pid_alive is True
            assert err.lockfile_age_seconds is not None
            # Heartbeat is 0.1s → age should be well within a "fresh" window.
            assert err.lockfile_age_seconds < 1.0
            # Default progress_window (60s in message) makes this case
            # "queued behind live, progressing".
            assert "queued behind live, progressing" in str(err)
            assert f"PID {proc.pid}" in str(err)
            assert "device is healthy" in str(err)
            assert "REST API unreachable" in str(err)
        finally:
            stop.set()
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)


class TestDeviceLockTimeoutWedgedHolder:
    def test_wedged_holder_diagnosis(
        self, lock_dir: Path, _mock_rest_unreachable
    ) -> None:
        """Alive holder + lockfile age > progress_window → "may be wedged"."""
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        stop = ctx.Event()
        proc = ctx.Process(
            target=_hold_stuck_holder_worker,
            args=(str(lock_dir), "10.20.0.2", started, stop),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0)

            contender = DeviceLock("10.20.0.2", lock_dir=lock_dir)
            # Let the lockfile age past the progress_window we'll use.
            time.sleep(0.6)
            with pytest.raises(DeviceLockTimeout) as excinfo:
                contender.acquire_or_raise(
                    timeout=0.3, progress_window=0.2
                )

            err = excinfo.value
            assert err.holder_pid == proc.pid
            assert err.pid_alive is True
            assert err.lockfile_age_seconds is not None
            assert err.lockfile_age_seconds > 0.2
            assert "wedged" in str(err)
            assert f"PID {proc.pid}" in str(err)
        finally:
            stop.set()
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)


class TestDeviceLockTimeoutStaleDeadPid:
    def test_dead_holder_diagnosis(
        self, lock_dir: Path, _mock_rest_unreachable
    ) -> None:
        """Dead-PID lockfile → "stale" wording.

        We must prevent acquire()'s opportunistic cleanup from removing
        the stale file before we hit the timeout, so we monkeypatch
        cleanup_stale to a no-op.  We also hold the flock from a
        background fd so acquire() actually times out instead of
        succeeding immediately.
        """
        import fcntl

        host = "10.20.0.3"
        path = lock_dir / f"device-{host}.lock"
        dead_pid = 999_999_999  # virtually impossible PID
        meta = {"pid": dead_pid, "ts": time.time(), "device_host": host}
        path.write_text(json.dumps(meta))

        # Hold the flock in this process via a side fd so acquire() can't
        # take it.  We open with O_RDWR so flock works; we deliberately
        # do NOT rewrite the body, so the PID stays as the dead one.
        fd = os.open(str(path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch.object(DeviceLock, "cleanup_stale", return_value=0):
                contender = DeviceLock(host, lock_dir=lock_dir)
                with pytest.raises(DeviceLockTimeout) as excinfo:
                    contender.acquire_or_raise(
                        timeout=0.3, progress_window=None
                    )

            err = excinfo.value
            assert err.holder_pid == dead_pid
            assert err.pid_alive is False
            assert "stale" in str(err)
            assert f"PID {dead_pid}" in str(err)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestDeviceLockTimeoutRestUnreachable:
    def test_rest_unreachable_message(self, lock_dir: Path) -> None:
        """When the REST probe fails, the message mentions "unreachable"."""
        import fcntl

        host = "10.20.0.4"
        path = lock_dir / f"device-{host}.lock"
        meta = {"pid": os.getpid(), "ts": time.time(), "device_host": host}
        path.write_text(json.dumps(meta))

        fd = os.open(str(path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch(
                "c64_test_harness.backends.device_lock.urllib.request.urlopen",
                side_effect=urllib.error.URLError("mocked: no network"),
            ):
                contender = DeviceLock(host, lock_dir=lock_dir)
                with pytest.raises(DeviceLockTimeout) as excinfo:
                    contender.acquire_or_raise(
                        timeout=0.3, progress_window=None
                    )

            err = excinfo.value
            assert err.device_reachable_rest is False
            assert "REST API unreachable" in str(err)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_rest_reachable_message(self, lock_dir: Path) -> None:
        """When the REST probe succeeds, the message mentions "responsive"."""
        import fcntl
        from unittest.mock import MagicMock

        host = "10.20.0.5"
        path = lock_dir / f"device-{host}.lock"
        meta = {"pid": os.getpid(), "ts": time.time(), "device_host": host}
        path.write_text(json.dumps(meta))

        fd = os.open(str(path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # Build a context-manager-style mock for urlopen.
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.getcode.return_value = 200
            mock_resp.__enter__ = lambda self: self
            mock_resp.__exit__ = lambda self, *a: None
            with patch(
                "c64_test_harness.backends.device_lock.urllib.request.urlopen",
                return_value=mock_resp,
            ):
                contender = DeviceLock(host, lock_dir=lock_dir)
                with pytest.raises(DeviceLockTimeout) as excinfo:
                    contender.acquire_or_raise(
                        timeout=0.3, progress_window=None
                    )

            err = excinfo.value
            assert err.device_reachable_rest is True
            assert "REST API responsive" in str(err)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestAcquireOrRaiseSuccess:
    def test_returns_none_on_successful_acquire(self, lock_dir: Path) -> None:
        """acquire_or_raise returns None on success and the lock is held."""
        lock = DeviceLock("10.20.0.6", lock_dir=lock_dir)
        result = lock.acquire_or_raise(timeout=1.0)
        try:
            assert result is None
            assert lock.held
        finally:
            lock.release()
