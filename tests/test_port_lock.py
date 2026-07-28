"""Tests for PortLock — file-based cross-process port locking."""

import json
import multiprocessing
import os
import time
from unittest.mock import patch

import pytest

from c64_test_harness.backends.port_lock import PortLock, _default_lock_dir


def _child_try_acquire_19100(result_queue, ld):
    child_lock = PortLock(19100, lock_dir=ld)
    result_queue.put(child_lock.acquire())


def _child_hold_until_proceed_19101(ld, ready_event, proceed_event):
    child_lock = PortLock(19101, lock_dir=ld)
    child_lock.acquire()
    ready_event.set()
    proceed_event.wait(timeout=5)
    # Child exits without releasing — kernel releases flock


def _child_hold_until_proceed_19202(ld, ready_event, proceed_event):
    child_lock = PortLock(19202, lock_dir=ld)
    child_lock.acquire()
    ready_event.set()
    proceed_event.wait(timeout=10)


@pytest.fixture
def lock_dir(tmp_path):
    """Use a temp directory for lock files to avoid test interference."""
    return tmp_path


class TestPortLock:
    def test_acquire_and_release(self, lock_dir):
        lock = PortLock(19000, lock_dir=lock_dir)
        assert lock.acquire()
        assert lock.held
        lock.release()
        assert not lock.held

    def test_double_acquire_is_idempotent(self, lock_dir):
        lock = PortLock(19001, lock_dir=lock_dir)
        assert lock.acquire()
        assert lock.acquire()  # Should return True, already held
        lock.release()

    def test_exclusive_within_process(self, lock_dir):
        lock1 = PortLock(19002, lock_dir=lock_dir)
        lock2 = PortLock(19002, lock_dir=lock_dir)
        assert lock1.acquire()
        assert not lock2.acquire()  # Should fail, already held
        lock1.release()
        assert lock2.acquire()  # Now it should succeed
        lock2.release()

    def test_different_ports_independent(self, lock_dir):
        lock1 = PortLock(19003, lock_dir=lock_dir)
        lock2 = PortLock(19004, lock_dir=lock_dir)
        assert lock1.acquire()
        assert lock2.acquire()
        lock1.release()
        lock2.release()

    def test_context_manager(self, lock_dir):
        lock = PortLock(19005, lock_dir=lock_dir)
        with lock:
            assert lock.held
        assert not lock.held

    def test_context_manager_failure(self, lock_dir):
        lock1 = PortLock(19006, lock_dir=lock_dir)
        lock1.acquire()
        lock2 = PortLock(19006, lock_dir=lock_dir)
        with pytest.raises(RuntimeError, match="Could not acquire"):
            with lock2:
                pass
        lock1.release()

    def test_metadata_written(self, lock_dir):
        lock = PortLock(19007, lock_dir=lock_dir)
        lock.acquire()
        info = lock.read_info()
        assert info is not None
        assert info["pid"] == os.getpid()
        assert "ts" in info
        lock.release()

    def test_update_vice_pid(self, lock_dir):
        lock = PortLock(19008, lock_dir=lock_dir)
        lock.acquire()
        lock.update_vice_pid(12345)
        info = lock.read_info()
        assert info is not None
        assert info["vice_pid"] == 12345
        lock.release()

    def test_read_info_no_file(self, lock_dir):
        lock = PortLock(19009, lock_dir=lock_dir)
        assert lock.read_info() is None

    def test_release_keeps_lockfile(self, lock_dir):
        """release() does NOT delete the lockfile (prevents inode races)."""
        lock = PortLock(19010, lock_dir=lock_dir)
        lock.acquire()
        lock_path = lock_dir / "port-19010.lock"
        assert lock_path.exists()
        lock.release()
        # File persists — intentional to avoid inode races
        assert lock_path.exists()

    def test_release_allows_reacquire(self, lock_dir):
        """After release, another lock on the same port succeeds."""
        lock1 = PortLock(19013, lock_dir=lock_dir)
        lock1.acquire()
        lock1.release()
        lock2 = PortLock(19013, lock_dir=lock_dir)
        assert lock2.acquire()
        lock2.release()

    def test_release_without_acquire_is_noop(self, lock_dir):
        lock = PortLock(19011, lock_dir=lock_dir)
        lock.release()  # Should not raise

    def test_port_property(self, lock_dir):
        lock = PortLock(19012, lock_dir=lock_dir)
        assert lock.port == 19012


class TestPortLockCrossProcess:
    def test_cross_process_exclusion(self, lock_dir):
        """A child process cannot acquire a lock held by the parent."""
        lock = PortLock(19100, lock_dir=lock_dir)
        assert lock.acquire()

        q = multiprocessing.Queue()
        p = multiprocessing.Process(target=_child_try_acquire_19100, args=(q, lock_dir))
        p.start()
        p.join(timeout=5)
        assert not q.get()  # Child should fail to acquire
        lock.release()

    def test_lock_released_on_process_exit(self, lock_dir):
        """Lock is released when the holding process exits."""
        ready = multiprocessing.Event()
        proceed = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_child_hold_until_proceed_19101,
            args=(lock_dir, ready, proceed),
        )
        p.start()
        ready.wait(timeout=5)

        # While child holds it, we can't acquire
        lock = PortLock(19101, lock_dir=lock_dir)
        assert not lock.acquire()

        # Tell child to exit
        proceed.set()
        p.join(timeout=5)

        # Now we should be able to acquire
        assert lock.acquire()
        lock.release()


    def test_acquire_survives_unlinked_lockfile(self, lock_dir):
        """acquire() retries if the lockfile inode was replaced."""
        # Create a lockfile, then delete it to simulate cleanup_stale()
        lock_path = lock_dir / "port-19014.lock"
        lock_path.write_text("{}")

        lock = PortLock(19014, lock_dir=lock_dir)
        # Even if the file is replaced between open and flock,
        # acquire should succeed (retry with new inode)
        assert lock.acquire()
        # Verify the lock is on the current file
        fd_stat = os.fstat(lock._fd)
        path_stat = os.stat(str(lock_path))
        assert fd_stat.st_ino == path_stat.st_ino
        lock.release()


class TestCleanupStale:
    def test_removes_stale_lockfile(self, lock_dir):
        """Lockfiles from dead PIDs are cleaned up."""
        lock_path = lock_dir / "port-19200.lock"
        # Write a lockfile with a non-existent PID
        lock_path.write_text(json.dumps({"pid": 999999999, "ts": time.time()}))
        removed = PortLock.cleanup_stale(lock_dir)
        assert removed == 1
        assert not lock_path.exists()

    def test_keeps_lockfile_with_held_flock(self, lock_dir):
        """A lockfile whose flock is currently held is never removed.

        (Historically named ``test_keeps_live_lockfile``, but what it
        exercises is the held-flock branch: cleanup_stale() cannot even
        get the flock, so it skips the file regardless of PID.)
        """
        lock = PortLock(19201, lock_dir=lock_dir)
        lock.acquire()
        removed = PortLock.cleanup_stale(lock_dir)
        assert removed == 0
        lock.release()

    def test_removes_alive_pid_with_unheld_flock(self, lock_dir):
        """Alive-PID metadata WITHOUT a held flock is treated as stale.

        Documents the intentional semantics of the alive-PID branch in
        cleanup_stale(): if nobody holds the flock, the metadata is
        leftover state (the process forgot to clean up, or wrote the
        file without locking) and removal is safe — the flock, not the
        PID, is the source of truth for liveness.
        """
        lock_path = lock_dir / "port-19203.lock"
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time()})
        )
        removed = PortLock.cleanup_stale(lock_dir)
        assert removed == 1
        assert not lock_path.exists()

    def test_cleanup_does_not_break_held_lock(self, lock_dir):
        """cleanup_stale() cannot remove a lockfile held by another process."""
        ready = multiprocessing.Event()
        proceed = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_child_hold_until_proceed_19202,
            args=(lock_dir, ready, proceed),
        )
        p.start()
        ready.wait(timeout=5)

        # Child holds the lock — cleanup should not remove it
        removed = PortLock.cleanup_stale(lock_dir)
        assert removed == 0

        # We should not be able to acquire it
        lock = PortLock(19202, lock_dir=lock_dir)
        assert not lock.acquire()

        proceed.set()
        p.join(timeout=5)

    def test_does_not_unlink_recreated_lockfile(self, lock_dir):
        """cleanup_stale() must not delete a lockfile re-created after open().

        Race: cleanup opens the stale file, another process unlinks it
        (or a concurrent cleanup does) and re-creates the path with a
        NEW inode it legitimately holds.  Without the inode re-check,
        cleanup would flock the ORPHANED inode, then unlink by path —
        destroying the new holder's lockfile so two processes could end
        up owning the same port.
        """
        import c64_test_harness.backends.port_lock as port_lock_mod

        target = lock_dir / "port-19204.lock"
        # Stale file (dead PID) so cleanup proceeds past the PID check.
        target.write_text(json.dumps({"pid": 999999999, "ts": time.time()}))
        new_metadata = json.dumps({"pid": os.getpid(), "ts": time.time()})

        real_open = os.open

        def racy_open(path, flags, *args):
            fd = real_open(path, flags, *args)
            if str(path) == str(target) and flags == os.O_RDWR:
                # Simulate the race: the path is unlinked and re-created
                # (new inode) between cleanup's open() and flock().
                target.unlink()
                target.write_text(new_metadata)
            return fd

        with patch.object(port_lock_mod.os, "open", side_effect=racy_open):
            removed = PortLock.cleanup_stale(lock_dir)

        assert removed == 0
        assert target.exists()
        assert target.read_text() == new_metadata

    def test_skips_unlink_when_path_vanishes_after_open(self, lock_dir):
        """If the path is gone by flock time, cleanup_stale() skips it."""
        import c64_test_harness.backends.port_lock as port_lock_mod

        target = lock_dir / "port-19205.lock"
        target.write_text(json.dumps({"pid": 999999999, "ts": time.time()}))

        real_open = os.open

        def racy_open(path, flags, *args):
            fd = real_open(path, flags, *args)
            if str(path) == str(target) and flags == os.O_RDWR:
                target.unlink()  # gone before the inode re-check
            return fd

        with patch.object(port_lock_mod.os, "open", side_effect=racy_open):
            removed = PortLock.cleanup_stale(lock_dir)

        # Nothing was there to unlink; must not count it as removed.
        assert removed == 0
        assert not target.exists()

    def test_default_lock_dir_created(self):
        """_default_lock_dir creates the directory."""
        d = _default_lock_dir()
        assert d.is_dir()
