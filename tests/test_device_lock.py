"""Tests for DeviceLock cross-process device locking."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from c64_test_harness.backends.device_lock import (
    DeviceLock,
    _sanitize_device_id,
)


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Provide a temporary lock directory for each test."""
    d = tmp_path / "locks"
    d.mkdir()
    return d


# -- Sanitization --


class TestSanitizeDeviceId:
    def test_ipv4(self) -> None:
        assert _sanitize_device_id("192.168.1.81") == "192.168.1.81"

    def test_hostname(self) -> None:
        assert _sanitize_device_id("my-u64.local") == "my-u64.local"

    def test_special_chars(self) -> None:
        assert _sanitize_device_id("host:8080/path") == "host_8080_path"

    def test_empty(self) -> None:
        assert _sanitize_device_id("") == "unknown"

    def test_collapses_underscores(self) -> None:
        assert _sanitize_device_id("a@@b") == "a_b"


# -- Basic acquire / release --


class TestAcquireRelease:
    def test_acquire_and_release(self, lock_dir: Path) -> None:
        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        assert not lock.held
        assert lock.acquire(timeout=1.0)
        assert lock.held
        lock.release()
        assert not lock.held

    def test_double_acquire_is_noop(self, lock_dir: Path) -> None:
        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        assert lock.acquire(timeout=1.0)  # idempotent
        lock.release()

    def test_double_release_is_noop(self, lock_dir: Path) -> None:
        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        lock.acquire(timeout=1.0)
        lock.release()
        lock.release()  # no error

    def test_device_host_property(self, lock_dir: Path) -> None:
        lock = DeviceLock("10.0.0.5", lock_dir=lock_dir)
        assert lock.device_host == "10.0.0.5"


# -- Context manager --


class TestContextManager:
    def test_context_manager_acquires_and_releases(self, lock_dir: Path) -> None:
        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        with lock:
            assert lock.held
        assert not lock.held

    def test_context_manager_raises_on_failure(self, lock_dir: Path) -> None:
        """Hold the lock in another fd, then context manager should fail.

        Patches __enter__ to use legacy hard-timeout because the live
        in-process holder would otherwise extend the deadline forever.
        """
        lock1 = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        lock1.acquire(timeout=1.0)
        try:
            lock2 = DeviceLock("192.168.1.81", lock_dir=lock_dir)
            # Force the contender into legacy mode to bound this test.
            original_acquire = lock2.acquire
            lock2.acquire = lambda timeout=1.0: original_acquire(  # type: ignore[method-assign]
                timeout=timeout, progress_window=None
            )
            with pytest.raises(RuntimeError, match="Could not acquire lock"):
                with lock2:
                    pass  # pragma: no cover
        finally:
            lock1.release()


# -- Blocking timeout --


class TestBlockingTimeout:
    def test_timeout_when_held(self, lock_dir: Path) -> None:
        """acquire() returns False after timeout when another holder exists.

        Uses ``progress_window=None`` (legacy hard-timeout) because the
        in-process holder writes recent metadata that would otherwise
        extend the deadline under the default queue-aware semantics.
        """
        lock1 = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        lock1.acquire(timeout=1.0)
        try:
            lock2 = DeviceLock("192.168.1.81", lock_dir=lock_dir)
            start = time.monotonic()
            assert not lock2.acquire(timeout=0.3, progress_window=None)
            elapsed = time.monotonic() - start
            assert elapsed >= 0.25  # should have waited ~0.3s
        finally:
            lock1.release()

    def test_acquire_succeeds_after_release(self, lock_dir: Path) -> None:
        """Second lock acquires after the first releases mid-wait."""
        lock1 = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        lock1.acquire(timeout=1.0)
        acquired = threading.Event()

        def release_after_delay() -> None:
            time.sleep(0.2)
            lock1.release()

        t = threading.Thread(target=release_after_delay)
        t.start()

        lock2 = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        assert lock2.acquire(timeout=2.0)
        acquired.set()
        lock2.release()
        t.join()


# -- Metadata --


class TestMetadata:
    def test_metadata_written(self, lock_dir: Path) -> None:
        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)
        lock.acquire(timeout=1.0)
        try:
            info = lock.read_info()
            assert info is not None
            assert info["pid"] == os.getpid()
            assert info["device_host"] == "192.168.1.81"
            assert "ts" in info
        finally:
            lock.release()

    def test_read_info_no_file(self, lock_dir: Path) -> None:
        lock = DeviceLock("nonexistent", lock_dir=lock_dir)
        assert lock.read_info() is None

    def test_read_info_corrupt(self, lock_dir: Path) -> None:
        lock = DeviceLock("corrupt", lock_dir=lock_dir)
        # Write garbage to the lockfile
        path = lock_dir / "device-corrupt.lock"
        path.write_text("NOT JSON")
        assert lock.read_info() is None


# -- Inode verification --


class TestInodeVerification:
    def test_inode_mismatch_retries(self, lock_dir: Path) -> None:
        """If the lockfile is replaced between open() and flock(),
        the inode check should detect it and retry."""
        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)

        original_open = os.open
        call_count = 0

        def patched_open(path_str, flags, mode=0o777):
            nonlocal call_count
            fd = original_open(path_str, flags, mode)
            call_count += 1
            if call_count == 1 and "device-" in str(path_str):
                # Delete and recreate the file after open but before
                # flock to simulate cleanup_stale race.  We can't
                # easily inject between open and flock, so instead
                # we unlink and recreate so the inode changes.
                try:
                    os.unlink(path_str)
                    # Create a new file at the same path (new inode)
                    new_fd = original_open(path_str, os.O_CREAT | os.O_RDWR, 0o600)
                    os.close(new_fd)
                except OSError:
                    pass
            return fd

        with patch("c64_test_harness.backends.device_lock.os.open", side_effect=patched_open):
            result = lock.acquire(timeout=1.0)

        # Should still succeed (retries with new inode)
        assert result
        lock.release()


# -- Different devices don't interfere --


class TestMultipleDevices:
    def test_different_devices_independent(self, lock_dir: Path) -> None:
        lock_a = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        lock_b = DeviceLock("10.0.0.2", lock_dir=lock_dir)
        assert lock_a.acquire(timeout=1.0)
        assert lock_b.acquire(timeout=1.0)
        assert lock_a.held
        assert lock_b.held
        lock_a.release()
        lock_b.release()

    def test_same_device_conflicts(self, lock_dir: Path) -> None:
        lock1 = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        lock2 = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        assert lock1.acquire(timeout=1.0)
        # progress_window=None: in-process holder would otherwise extend deadline.
        assert not lock2.acquire(timeout=0.2, progress_window=None)
        lock1.release()


# -- cleanup_stale --


class TestCleanupStale:
    def test_removes_stale_lockfile(self, lock_dir: Path) -> None:
        """A lockfile with a dead PID and no active flock is stale."""
        path = lock_dir / "device-10.0.0.1.lock"
        meta = {"pid": 999999999, "ts": time.time(), "device_host": "10.0.0.1"}
        path.write_text(json.dumps(meta))
        removed = DeviceLock.cleanup_stale(lock_dir=lock_dir)
        assert removed == 1
        assert not path.exists()

    def test_does_not_remove_held_lock(self, lock_dir: Path) -> None:
        """A lockfile with an active flock should not be removed."""
        lock = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        lock.acquire(timeout=1.0)
        try:
            removed = DeviceLock.cleanup_stale(lock_dir=lock_dir)
            assert removed == 0
        finally:
            lock.release()

    def test_removes_corrupt_metadata(self, lock_dir: Path) -> None:
        """Lockfile with corrupt JSON and no flock holder is cleaned."""
        path = lock_dir / "device-badhost.lock"
        path.write_text("NOT VALID JSON{{{{")
        removed = DeviceLock.cleanup_stale(lock_dir=lock_dir)
        assert removed == 1

    def test_empty_dir(self, lock_dir: Path) -> None:
        """No crash on empty directory."""
        assert DeviceLock.cleanup_stale(lock_dir=lock_dir) == 0

    def test_ignores_port_lockfiles(self, lock_dir: Path) -> None:
        """Only device-*.lock files are touched, not port-*.lock."""
        (lock_dir / "port-6502.lock").write_text("{}")
        assert DeviceLock.cleanup_stale(lock_dir=lock_dir) == 0


# -- Opportunistic cleanup wired into acquire() --


def _hold_device_lock_worker(
    lock_dir_str: str,
    host: str,
    started: "multiprocessing.synchronize.Event",
    stop: "multiprocessing.synchronize.Event",
) -> None:
    """Child-process worker: hold a DeviceLock until told to stop."""
    lock = DeviceLock(host, lock_dir=Path(lock_dir_str))
    if not lock.acquire(timeout=5.0):
        return
    try:
        started.set()
        stop.wait(timeout=10.0)
    finally:
        lock.release()


class TestAcquireCleansStaleOnEntry:
    def test_acquire_cleans_stale_metadata(self, lock_dir: Path) -> None:
        """acquire() opportunistically removes orphan metadata for a dead PID."""
        path = lock_dir / "device-10.0.0.1.lock"
        # PID 999999999 is virtually guaranteed not to exist.
        stale_meta = {
            "pid": 999999999,
            "ts": 0.0,
            "device_host": "10.0.0.1",
        }
        path.write_text(json.dumps(stale_meta))

        lock = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            info = lock.read_info()
            assert info is not None
            assert info["pid"] == os.getpid()
            assert info["device_host"] == "10.0.0.1"
            assert info["ts"] != 0.0
        finally:
            lock.release()

    def test_acquire_does_not_disturb_live_holder(self, lock_dir: Path) -> None:
        """A live cross-process holder must not be disturbed by acquire's cleanup."""
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        stop = ctx.Event()
        proc = ctx.Process(
            target=_hold_device_lock_worker,
            args=(str(lock_dir), "10.0.0.7", started, stop),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0), "child never acquired the lock"

            contender = DeviceLock("10.0.0.7", lock_dir=lock_dir)
            t0 = time.monotonic()
            # Legacy hard-timeout: this test specifically asserts the
            # contender does NOT disturb a live holder over a fixed window.
            assert not contender.acquire(timeout=0.5, progress_window=None)
            assert time.monotonic() - t0 >= 0.4

            # Child still holds it: metadata reflects child PID.
            info = contender.read_info()
            assert info is not None
            assert info["pid"] == proc.pid
        finally:
            stop.set()
            proc.join(timeout=5.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)

    def test_acquire_handles_corrupt_metadata(self, lock_dir: Path) -> None:
        """Junk lockfile content must not prevent acquire."""
        path = lock_dir / "device-10.0.0.2.lock"
        path.write_bytes(b"not json \x00\xff garbage")

        lock = DeviceLock("10.0.0.2", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            info = lock.read_info()
            assert info is not None
            assert info["pid"] == os.getpid()
        finally:
            lock.release()

    def test_acquire_handles_missing_pid_field(self, lock_dir: Path) -> None:
        """Metadata without a pid key is treated as stale and cleaned; acquire succeeds."""
        path = lock_dir / "device-10.0.0.3.lock"
        path.write_text(json.dumps({"ts": 0.0, "device_host": "10.0.0.3"}))

        lock = DeviceLock("10.0.0.3", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            info = lock.read_info()
            assert info is not None
            assert info["pid"] == os.getpid()
            assert info["device_host"] == "10.0.0.3"
        finally:
            lock.release()


# -- F1: queue-aware progress_window --


def _hold_and_bump_worker(
    lock_dir_str: str,
    host: str,
    started: "multiprocessing.synchronize.Event",
    hold_seconds: float,
    bump_interval: float,
) -> None:
    """Hold a DeviceLock for *hold_seconds*, bumping mtime every *bump_interval*."""
    lock = DeviceLock(host, lock_dir=Path(lock_dir_str))
    if not lock.acquire(timeout=5.0, progress_window=None):
        return
    started.set()
    try:
        deadline = time.monotonic() + hold_seconds
        while time.monotonic() < deadline:
            try:
                os.utime(str(lock._lock_path))
            except OSError:
                pass
            time.sleep(bump_interval)
    finally:
        lock.release()


class TestProgressWindow:
    def test_acquire_extends_on_live_progressing_holder(
        self, lock_dir: Path
    ) -> None:
        """A live holder bumping mtime extends the deadline past *timeout*."""
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        proc = ctx.Process(
            target=_hold_and_bump_worker,
            args=(str(lock_dir), "10.0.0.42", started, 4.0, 0.5),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0), "child never acquired the lock"

            contender = DeviceLock("10.0.0.42", lock_dir=lock_dir)
            t0 = time.monotonic()
            # Default progress_window=60.0 should keep extending past
            # the 2.0s timeout because the child is alive and bumping.
            assert contender.acquire(timeout=2.0, progress_window=5.0)
            elapsed = time.monotonic() - t0
            try:
                # Child held for ~4s; total wait should be well past 2.0s.
                assert elapsed >= 3.0, f"acquire returned too early ({elapsed:.2f}s)"
            finally:
                contender.release()
        finally:
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)

    def test_acquire_progress_window_none_legacy_behavior(
        self, lock_dir: Path
    ) -> None:
        """progress_window=None reverts to the hard-timeout legacy behavior."""
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        proc = ctx.Process(
            target=_hold_and_bump_worker,
            args=(str(lock_dir), "10.0.0.43", started, 5.0, 0.2),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0)

            contender = DeviceLock("10.0.0.43", lock_dir=lock_dir)
            t0 = time.monotonic()
            assert not contender.acquire(timeout=0.5, progress_window=None)
            elapsed = time.monotonic() - t0
            assert 0.4 <= elapsed < 1.5, (
                f"legacy timeout did not bound wait correctly: {elapsed:.2f}s"
            )
        finally:
            # Tell the child to stop early — it will just hold its 5s window.
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)

    def test_acquire_dead_holder_counts_against_timeout(
        self, lock_dir: Path
    ) -> None:
        """A dead-PID holder is cleaned at acquire entry; acquire succeeds quickly."""
        path = lock_dir / "device-10.0.0.44.lock"
        # Stale metadata for a virtually-impossible PID.  No live process
        # holds the flock, so cleanup_stale at acquire entry removes it.
        meta = {"pid": 999999999, "ts": 0.0, "device_host": "10.0.0.44"}
        path.write_text(json.dumps(meta))

        lock = DeviceLock("10.0.0.44", lock_dir=lock_dir)
        t0 = time.monotonic()
        assert lock.acquire(timeout=0.5)
        elapsed = time.monotonic() - t0
        try:
            assert elapsed < 0.4, f"dead holder should not extend deadline ({elapsed:.2f}s)"
        finally:
            lock.release()


# -- F2: optional watchdog notifier + cooperative mtime bump --


def _hold_and_release_after(
    lock_dir_str: str,
    host: str,
    started: "multiprocessing.synchronize.Event",
    hold_seconds: float,
) -> None:
    """Acquire the lock, wait *hold_seconds*, then release cleanly."""
    lock = DeviceLock(host, lock_dir=Path(lock_dir_str))
    if not lock.acquire(timeout=5.0, progress_window=None):
        return
    started.set()
    time.sleep(hold_seconds)
    lock.release()


class TestWatchdogNotifier:
    def test_acquire_wakes_on_watchdog_event_when_available(
        self, lock_dir: Path
    ) -> None:
        """When watchdog is installed, acquire wakes on the release fs-event."""
        pytest.importorskip("watchdog")

        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        hold = 0.5
        proc = ctx.Process(
            target=_hold_and_release_after,
            args=(str(lock_dir), "10.0.0.55", started, hold),
        )
        proc.start()
        try:
            assert started.wait(timeout=5.0)

            contender = DeviceLock("10.0.0.55", lock_dir=lock_dir)
            t0 = time.monotonic()
            assert contender.acquire(timeout=5.0)
            elapsed = time.monotonic() - t0
            try:
                # Lower bound: must wait at least until the holder releases.
                assert elapsed >= hold - 0.1
                # Upper bound: with watchdog, wake should be quick after release.
                # Allow margin for spawn/scheduling, but still less than several
                # poll cycles past the release.
                assert elapsed <= hold + 0.5, (
                    f"watchdog wake too slow: {elapsed:.3f}s vs hold={hold}s"
                )
            finally:
                contender.release()
        finally:
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)

    def test_release_bumps_mtime(self, lock_dir: Path) -> None:
        """release() updates the lockfile mtime as a cooperative wake signal."""
        lock = DeviceLock("10.0.0.56", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        path = lock._lock_path
        mtime_before = path.stat().st_mtime
        # Sleep enough for the filesystem mtime granularity to advance.
        # macOS HFS+/APFS supports sub-second; tmpfs on Linux is ns.  10ms is safe.
        time.sleep(0.05)
        lock.release()
        mtime_after = path.stat().st_mtime
        assert mtime_after > mtime_before, (
            f"release() should bump mtime (before={mtime_before}, after={mtime_after})"
        )
