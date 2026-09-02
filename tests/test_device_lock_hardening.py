"""Hardening of ``device_lock.py``: issues #160, #161, #163, #164, #165.

Each class names its issue and the mechanism it pins. Line references
are to this branch, which sits on top of the self-deadlock fix and so
runs ~65 lines later than the numbers quoted in the issues.

Two of these guard races. Neither test reproduces its race: a test that
reliably hits a sub-millisecond window is usually not testing the window
at all. Instead each pins the *invariant* that makes the race
unobservable -- record completeness for #161, write ordering for #165 --
and says so in its docstring.
"""
from __future__ import annotations

import json
import logging
import os
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
    return tmp_path / "locks"


def _lockfile(lock_dir: Path) -> Path:
    return next(lock_dir.glob("device-*.lock"))


# =========================================================================== #
# #163 -- non-object JSON must not escape the guard                           #
# =========================================================================== #
class TestNonObjectJson:
    """``_holder_is_progressing`` calls ``.get`` on whatever json returns.

    ``json.loads`` of ``null`` / ``[]`` / ``42`` all succeed and return
    non-dicts, whose ``.get`` raises ``AttributeError`` -- not in the
    ``(OSError, JSONDecodeError, ValueError)`` guard, so it escapes the
    ``acquire()`` poll loop.

    Latent today: the only writer emits an object. Reachable the moment
    anything else writes that path -- triage by hand, a debugging tool,
    a future partial write.
    """

    @pytest.mark.parametrize("body", ["null", "[]", "42", '"a string"', "true"])
    def test_holder_is_progressing_returns_false(
        self, lock_dir: Path, body: str
    ) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        lock._lock_path.write_text(body)
        assert lock._holder_is_progressing(60.0) is False

    @pytest.mark.parametrize("body", ["null", "[]", "42"])
    def test_read_info_returns_none(self, lock_dir: Path, body: str) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        lock._lock_path.write_text(body)
        assert lock.read_info() is None

    def test_acquire_survives_a_non_object_lockfile(
        self, lock_dir: Path
    ) -> None:
        """The whole point: this must not escape a routine acquire."""
        lock_dir.mkdir(parents=True, exist_ok=True)
        holder = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert holder.acquire(timeout=5.0)
        try:
            holder._lock_path.write_text("null")
            waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
            assert waiter.acquire(timeout=0.3) is False
        finally:
            holder.release()


# =========================================================================== #
# #164 -- cleanup_stale unlinks every UNHELD lockfile, not every dead one     #
# =========================================================================== #
class TestCleanupStaleRule:
    """Characterisation. The docstring said "whose holding PID is dead";
    the PID is read and discarded and both branches unlink.

    The behaviour is correct -- the unlink is gated on taking
    ``LOCK_EX|LOCK_NB`` first, which proves nobody holds the device, and
    the residual race is caught by the inode recheck in
    ``_try_acquire_once``. These tests pin the real rule so the next
    reader does not reason from the docstring again.
    """

    def test_unlinks_a_lockfile_whose_pid_is_alive(
        self, lock_dir: Path
    ) -> None:
        """The case the old docstring said would survive."""
        lock_dir.mkdir(parents=True, exist_ok=True)
        path = lock_dir / "device-10_0_0_99.lock"
        path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
        assert _pid_is_alive(os.getpid())

        removed = DeviceLock.cleanup_stale(lock_dir=lock_dir)
        assert removed == 1
        assert not path.exists()

    def test_does_not_unlink_a_held_lockfile(self, lock_dir: Path) -> None:
        """Held is the only thing that saves a file."""
        lock_dir.mkdir(parents=True, exist_ok=True)
        holder = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert holder.acquire(timeout=5.0)
        try:
            removed = DeviceLock.cleanup_stale(lock_dir=lock_dir)
            assert removed == 0
            assert holder._lock_path.exists()
        finally:
            holder.release()

    def test_sweeps_other_devices_lockfiles_too(self, lock_dir: Path) -> None:
        """It globs ``device-*.lock``, so it is machine-global.

        Acquiring one device sweeps every other device's unheld file.
        """
        lock_dir.mkdir(parents=True, exist_ok=True)
        for name in ("device-a.lock", "device-b.lock", "device-c.lock"):
            (lock_dir / name).write_text(json.dumps({"pid": 999999}))
        removed = DeviceLock.cleanup_stale(lock_dir=lock_dir)
        assert removed == 3
        assert list(lock_dir.glob("device-*.lock")) == []


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# =========================================================================== #
# #161 -- metadata record must never be observable as empty or truncated      #
# =========================================================================== #
class TestMetadataRecord:
    """``_write_metadata`` was lseek + ftruncate + write, and every
    reader but ``cleanup_stale`` reads unlocked. Between the truncate
    and the write the file is zero bytes.

    **These tests do not reproduce the race.** The window is a few
    microseconds and hitting it reliably would mean the test was
    measuring something else. The issue carries the reproduction (a
    tight writer loop against an unlocked reader: 23-44% empty reads,
    torn reads only above ~8 KB, i.e. reader-side buffering). What is
    pinned here is the invariant that makes the window unobservable: the
    record is fixed-length and rewritten in place, so a reader sees the
    whole previous record or the whole new one, never nothing.
    """

    def test_no_truncate_in_the_write_path(self, lock_dir: Path) -> None:
        """ftruncate is what creates the empty window."""
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl.os, "ftruncate") as truncate:
            assert lock.acquire(timeout=5.0)
            try:
                lock._write_metadata()
            finally:
                lock.release()
        truncate.assert_not_called()

    def test_record_is_fixed_length(self, lock_dir: Path) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert lock.acquire(timeout=5.0)
        try:
            assert lock._lock_path.stat().st_size == dl._LOCK_RECORD_SIZE
        finally:
            lock.release()

    def test_a_shorter_record_does_not_leave_readable_remnants(
        self, lock_dir: Path
    ) -> None:
        """The correctness cost of dropping ftruncate, paid by padding.

        Rewriting a short record over a long one in place would leave
        the tail of the old one behind and make the file unparseable.
        """
        lock_dir.mkdir(parents=True, exist_ok=True)
        long_host = "d" * 200
        first = DeviceLock(long_host, lock_dir, heartbeat_interval=None)
        assert first.acquire(timeout=5.0)
        path = first._lock_path
        first.release()

        # Same path, much shorter device_host.
        second = DeviceLock("x", lock_dir, heartbeat_interval=None)
        second._lock_path = path
        assert second.acquire(timeout=5.0)
        try:
            info = json.loads(path.read_text())
            assert info["device_host"] == "x"
            assert path.stat().st_size == dl._LOCK_RECORD_SIZE
        finally:
            second.release()

    def test_every_prefix_read_is_either_empty_or_valid(
        self, lock_dir: Path
    ) -> None:
        """A reader capped at 4096 bytes gets a parseable record.

        ``read_info`` and ``cleanup_stale`` both read at most 4096, so
        the record has to be parseable when truncated to that length.
        """
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert lock.acquire(timeout=5.0)
        try:
            raw = lock._lock_path.read_bytes()[:4096]
            assert json.loads(raw)["pid"] == os.getpid()
            assert lock.read_info() is not None
        finally:
            lock.release()

    def test_oversized_metadata_is_not_silently_truncated(
        self, lock_dir: Path, caplog
    ) -> None:
        """Padding must not become a size cap that corrupts the record.

        The oversized host is injected after acquire rather than passed
        to the constructor: an 8 KB device_host makes a filename longer
        than NAME_MAX, so the open fails and the record is never
        reached. This exercises the guard, not the filesystem.
        """
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert lock.acquire(timeout=5.0)
        try:
            huge = "h" * (dl._LOCK_RECORD_SIZE * 2)
            lock._device_host = huge
            with caplog.at_level(logging.ERROR, logger=dl.__name__):
                lock._write_metadata()
            info = lock.read_info()
            assert info is not None, "the oversized record was written corrupt"
            assert info["device_host"] == huge
            assert any(r.levelno >= logging.ERROR for r in caplog.records), (
                f"an over-length record was written silently: {caplog.text!r}"
            )
        finally:
            lock._device_host = HOST
            lock.release()


# =========================================================================== #
# #165 -- metadata must be written before the acquire is observable           #
# =========================================================================== #
class TestMetadataOrdering:
    """The flock was granted, then the inode was rechecked, and only
    then was the metadata written -- so in between the file still named
    the *previous* holder, and unlocked readers reported that PID.

    **Not a race reproduction.** Rather than trying to land a reader in
    a microsecond window, this drives the window open deterministically:
    the inode recheck that sits between the two points is instrumented,
    and the file is read at exactly that moment.
    """

    def test_file_names_the_new_holder_by_the_inode_recheck(
        self, lock_dir: Path
    ) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        previous = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert previous.acquire(timeout=5.0)
        path = previous._lock_path
        previous.release()

        # Make the leftover record name someone else, so "still the old
        # holder" is distinguishable from "already us".
        stale = json.loads(path.read_text())
        stale["pid"] = 999999
        path.write_text(json.dumps(stale).ljust(dl._LOCK_RECORD_SIZE))

        seen: list[object] = []
        real_fstat = dl.os.fstat

        def spy(fd):
            # Runs between flock() and the return of _try_acquire_once.
            try:
                seen.append(json.loads(path.read_text()).get("pid"))
            except Exception as exc:  # noqa: BLE001
                seen.append(f"unreadable: {exc}")
            return real_fstat(fd)

        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl.os, "fstat", spy):
            assert lock.acquire(timeout=5.0)
        try:
            assert seen, "the inode recheck never ran"
            assert seen[0] == os.getpid(), (
                f"lockfile still named {seen[0]!r} after the flock was "
                f"granted -- readers in that window name the wrong holder"
            )
        finally:
            lock.release()

    def test_process_hold_registered_before_acquire_returns(
        self, lock_dir: Path
    ) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        assert lock.acquire(timeout=5.0)
        try:
            assert DeviceLock.held_by_this_process(HOST, lock_dir) is True
        finally:
            lock.release()


# =========================================================================== #
# #160 -- heartbeat must survive a transient OSError                          #
# =========================================================================== #
class TestHeartbeatResilience:
    """The loop returned on the first ``OSError``, unlogged, and nothing
    restarted it. The holder stayed healthy while its mtime froze, so
    every waiter was told it was wedged -- a confident, specific, wrong
    diagnosis that the holder itself could not see.
    """

    def test_transient_oserror_does_not_kill_the_heartbeat(
        self, lock_dir: Path, caplog
    ) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        calls: list[int] = []
        real_utime = dl.os.utime

        def flaky(path, times=None):
            calls.append(1)
            if len(calls) == 1:
                raise OSError(5, "transient")
            return real_utime(path, times)

        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
        with caplog.at_level(logging.WARNING, logger=dl.__name__):
            with patch.object(dl.os, "utime", flaky):
                assert lock.acquire(timeout=5.0)
                try:
                    deadline = time.monotonic() + 5.0
                    while len(calls) < 3 and time.monotonic() < deadline:
                        time.sleep(0.02)
                finally:
                    lock.release()

        assert len(calls) >= 3, (
            f"heartbeat stopped after {len(calls)} bump(s) -- a transient "
            f"error killed it permanently"
        )
        assert any(
            r.levelno == logging.WARNING
            and "heartbeat" in r.getMessage().lower()
            for r in caplog.records
        ), f"the failure was silent: {caplog.text!r}"

    def test_gives_up_after_repeated_failures_and_says_so(
        self, lock_dir: Path, caplog
    ) -> None:
        """Retrying forever against a genuinely broken path is its own bug."""
        lock_dir.mkdir(parents=True, exist_ok=True)

        def always_fails(path, times=None):
            raise OSError(5, "broken")

        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.02)
        with caplog.at_level(logging.ERROR, logger=dl.__name__):
            with patch.object(dl.os, "utime", always_fails):
                assert lock.acquire(timeout=5.0)
                try:
                    deadline = time.monotonic() + 5.0
                    while lock._heartbeat_healthy and time.monotonic() < deadline:
                        time.sleep(0.02)
                finally:
                    lock.release()

        assert lock._heartbeat_healthy is False
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            f"giving up was silent: {caplog.text!r}"
        )

    def test_unlinked_lockfile_stops_immediately_and_silently(
        self, lock_dir: Path, caplog
    ) -> None:
        """A FileNotFoundError is not a hiccup -- the file is gone.

        Asserts *one* utime call and no warning, not merely that the
        thread exits: an earlier version of this test only checked that
        it finished within two seconds, which the retry path also does
        (five failures at a 10 ms interval is 50 ms). It passed against
        both behaviours and so tested neither.
        """
        lock_dir.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.01)
        calls: list[int] = []

        def missing(path, times=None):
            calls.append(1)
            raise FileNotFoundError(2, "gone")

        with caplog.at_level(logging.WARNING, logger=dl.__name__):
            with patch.object(dl.os, "utime", missing):
                thread = threading.Thread(
                    target=lock._heartbeat_loop, args=(stop, 0.01), daemon=True
                )
                thread.start()
                thread.join(2.0)

        assert not thread.is_alive(), "heartbeat retried a missing lockfile"
        assert calls == [1], (
            f"expected a single attempt, got {len(calls)} -- a gone lockfile "
            f"is being retried as if it were a transient error"
        )
        assert not [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ], f"a released lockfile was reported as a failure: {caplog.text!r}"
