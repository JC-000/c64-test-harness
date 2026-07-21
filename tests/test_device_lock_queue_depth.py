"""Tests for DeviceLock queue-depth introspection (issue #130).

Covers the ``<lockfile>.queue/`` intent-directory mechanism:
``lock.queue_depth`` (instance property) and
``DeviceLock.peek_queue_depth`` (classmethod, pre-acquire peek),
stale-entry hygiene, the ``None`` unobservable path, and the guarantee
that introspection never changes ``acquire()`` semantics.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from c64_test_harness.backends.device_lock import DeviceLock


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Provide a temporary lock directory for each test."""
    d = tmp_path / "locks"
    d.mkdir()
    return d


def _queue_dir(lock_dir: Path, device_id: str) -> Path:
    return lock_dir / f"device-{device_id}.lock.queue"


def _wait_for(predicate: Callable[[], bool], deadline: float = 8.0) -> bool:
    """Poll *predicate* every 50ms until true or *deadline* seconds pass."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# -- Depth 0 --


class TestDepthZero:
    def test_no_queue_dir_is_zero(self, lock_dir: Path) -> None:
        lock = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        assert lock.queue_depth == 0
        assert DeviceLock.peek_queue_depth("10.0.0.1", lock_dir=lock_dir) == 0

    def test_held_lock_with_no_waiters_is_zero(self, lock_dir: Path) -> None:
        """The count reflects waiters, not the holder."""
        lock = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            assert lock.queue_depth == 0
            assert (
                DeviceLock.peek_queue_depth("10.0.0.1", lock_dir=lock_dir) == 0
            )
        finally:
            lock.release()

    def test_empty_queue_dir_is_zero(self, lock_dir: Path) -> None:
        _queue_dir(lock_dir, "10.0.0.1").mkdir()
        assert DeviceLock.peek_queue_depth("10.0.0.1", lock_dir=lock_dir) == 0

    def test_uncontended_acquire_registers_no_intent(
        self, lock_dir: Path
    ) -> None:
        """Fast-path acquire never creates the sidecar directory."""
        lock = DeviceLock("10.0.0.1", lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            assert not _queue_dir(lock_dir, "10.0.0.1").exists()
        finally:
            lock.release()


# -- Depth N via intent files created directly --


class TestDepthFromIntentFiles:
    def test_live_pid_entries_counted(self, lock_dir: Path) -> None:
        qdir = _queue_dir(lock_dir, "10.0.0.2")
        qdir.mkdir()
        for token in ("aaaa1111", "bbbb2222", "cccc3333"):
            (qdir / f"waiter-{os.getpid()}-{token}.json").write_text(
                json.dumps({"pid": os.getpid(), "ts": time.time()})
            )
        assert DeviceLock.peek_queue_depth("10.0.0.2", lock_dir=lock_dir) == 3
        lock = DeviceLock("10.0.0.2", lock_dir=lock_dir)
        assert lock.queue_depth == 3

    def test_stale_pid_entries_excluded_and_pruned(
        self, lock_dir: Path
    ) -> None:
        qdir = _queue_dir(lock_dir, "10.0.0.3")
        qdir.mkdir()
        stale = qdir / "waiter-999999999-deadbeef.json"
        stale.write_text(json.dumps({"pid": 999999999, "ts": 0.0}))
        live = qdir / f"waiter-{os.getpid()}-cafef00d.json"
        live.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))

        assert DeviceLock.peek_queue_depth("10.0.0.3", lock_dir=lock_dir) == 1
        # Stale entry garbage-collected on the spot; live entry kept.
        assert not stale.exists()
        assert live.exists()

    def test_foreign_filename_falls_back_to_json_pid(
        self, lock_dir: Path
    ) -> None:
        """An entry not matching the waiter-<pid>-<token> shape is counted
        via its JSON body's pid."""
        qdir = _queue_dir(lock_dir, "10.0.0.4")
        qdir.mkdir()
        (qdir / "ci-bot-intent.json").write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time()})
        )
        assert DeviceLock.peek_queue_depth("10.0.0.4", lock_dir=lock_dir) == 1

    def test_unparseable_entry_pruned_not_counted(
        self, lock_dir: Path
    ) -> None:
        qdir = _queue_dir(lock_dir, "10.0.0.5")
        qdir.mkdir()
        junk = qdir / "garbage.txt"
        junk.write_bytes(b"not json \x00\xff")
        assert DeviceLock.peek_queue_depth("10.0.0.5", lock_dir=lock_dir) == 0
        assert not junk.exists()


# -- None: unobservable queue --


class TestUnobservable:
    def test_queue_path_is_regular_file_returns_none(
        self, lock_dir: Path
    ) -> None:
        """A sidecar path that exists but isn't a directory is unobservable."""
        _queue_dir(lock_dir, "10.0.0.6").write_text("not a directory")
        assert DeviceLock.peek_queue_depth("10.0.0.6", lock_dir=lock_dir) is None
        lock = DeviceLock("10.0.0.6", lock_dir=lock_dir)
        assert lock.queue_depth is None


# -- Real waiters: intent wired into acquire() --


class TestAcquireRegistersIntent:
    def test_in_process_waiter_visible_then_deregistered(
        self, lock_dir: Path
    ) -> None:
        """A thread blocked in acquire() shows up in queue_depth and is
        deregistered after the timeout — acquire() still returns False."""
        host = "10.0.0.7"
        holder = DeviceLock(host, lock_dir=lock_dir)
        assert holder.acquire(timeout=1.0)
        waiter = DeviceLock(host, lock_dir=lock_dir)
        result: dict[str, bool] = {}

        def blocked_acquire() -> None:
            result["acquired"] = waiter.acquire(
                timeout=2.0, progress_window=None
            )

        t = threading.Thread(target=blocked_acquire)
        t.start()
        try:
            assert _wait_for(lambda: waiter.queue_depth == 1), (
                "waiter never appeared in the queue"
            )
            # Peek works without any instance and without touching the lock.
            assert DeviceLock.peek_queue_depth(host, lock_dir=lock_dir) == 1
        finally:
            t.join(timeout=10.0)
        # Timeout semantics unchanged.
        assert result["acquired"] is False
        # Intent removed on exit from acquire().
        assert waiter.queue_depth == 0
        holder.release()

    def test_waiter_deregisters_after_successful_acquire(
        self, lock_dir: Path
    ) -> None:
        host = "10.0.0.8"
        holder = DeviceLock(host, lock_dir=lock_dir)
        assert holder.acquire(timeout=1.0)
        waiter = DeviceLock(host, lock_dir=lock_dir)
        result: dict[str, bool] = {}

        def blocked_acquire() -> None:
            result["acquired"] = waiter.acquire(timeout=5.0)

        t = threading.Thread(target=blocked_acquire)
        t.start()
        try:
            assert _wait_for(lambda: waiter.queue_depth == 1)
            holder.release()
            t.join(timeout=10.0)
            assert result["acquired"] is True
            assert waiter.held
            assert waiter.queue_depth == 0
        finally:
            if t.is_alive():  # pragma: no cover - defensive
                t.join(timeout=2.0)
            waiter.release()
            holder.release()


def _blocked_waiter_worker(
    lock_dir_str: str,
    host: str,
    timeout: float,
) -> None:
    """Child-process worker: block on acquire, release if acquired."""
    lock = DeviceLock(host, lock_dir=Path(lock_dir_str))
    if lock.acquire(timeout=timeout):
        lock.release()


class TestCrossProcessQueueDepth:
    def test_two_subprocess_waiters_counted(self, lock_dir: Path) -> None:
        """Real cross-process waiters blocked in acquire() are visible,
        and the count drains to 0 after they get their turns."""
        host = "10.0.0.9"
        holder = DeviceLock(host, lock_dir=lock_dir)
        assert holder.acquire(timeout=1.0)

        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(
                target=_blocked_waiter_worker,
                args=(str(lock_dir), host, 30.0),
            )
            for _ in range(2)
        ]
        for p in procs:
            p.start()
        try:
            assert _wait_for(
                lambda: DeviceLock.peek_queue_depth(host, lock_dir=lock_dir)
                == 2,
                deadline=15.0,
            ), "subprocess waiters never both appeared in the queue"
            holder.release()
            for p in procs:
                p.join(timeout=30.0)
            assert all(p.exitcode == 0 for p in procs)
            assert DeviceLock.peek_queue_depth(host, lock_dir=lock_dir) == 0
        finally:
            for p in procs:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2.0)
            holder.release()
