"""File-based cross-process port locking using fcntl.flock.

Provides ``PortLock`` for kernel-enforced exclusive access to TCP ports
across independent OS processes.  The kernel automatically releases
flocks when the holding process exits (even on crash), making this
crash-safe without manual cleanup.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


def _default_lock_dir() -> Path:
    """Return the lock directory, creating it if needed."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        d = Path(runtime) / "c64-test-harness"
    else:
        d = Path(f"/tmp/c64-test-harness-{os.getuid()}")
    d.mkdir(parents=True, exist_ok=True)
    return d


class PortLock:
    """Cross-process exclusive lock for a single TCP port.

    Uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a per-port lockfile.
    The kernel releases the lock automatically when the process exits
    or the file descriptor is closed, so this is crash-safe.

    Usage::

        lock = PortLock(6510)
        if lock.acquire():
            try:
                # port 6510 is exclusively ours
                ...
            finally:
                lock.release()

    Or as a context manager (acquires on enter, releases on exit)::

        lock = PortLock(6510)
        with lock:
            ...
    """

    def __init__(self, port: int, lock_dir: Path | None = None) -> None:
        self._port = port
        self._lock_dir = lock_dir or _default_lock_dir()
        self._lock_path = self._lock_dir / f"port-{port}.lock"
        self._fd: int | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Try to acquire the lock (non-blocking).

        Returns True on success, False if another process holds it.
        Writes metadata (PID, timestamp) to the lockfile on success.

        After acquiring the flock, we verify the fd's inode still
        matches the path on disk.  If ``cleanup_stale()`` unlinked the
        file between our ``open()`` and ``flock()``, we'd hold a lock
        on an orphaned inode — another process could create a new file
        at the same path and get an independent lock.  The inode check
        detects this and retries with the new file.
        """
        if self._fd is not None:
            return True  # Already held by us
        # Retry once in case cleanup_stale() deletes our file
        # between open() and flock().
        for _attempt in range(2):
            try:
                fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_RDWR,
                    0o600,
                )
            except OSError:
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            # Verify our fd still points to the file on disk.
            # If cleanup_stale() unlinked it, the path either doesn't
            # exist or points to a new inode.
            try:
                fd_stat = os.fstat(fd)
                path_stat = os.stat(str(self._lock_path))
                if fd_stat.st_ino == path_stat.st_ino and fd_stat.st_dev == path_stat.st_dev:
                    self._fd = fd
                    self._write_metadata()
                    return True
            except OSError:
                pass
            # Inode mismatch or path gone — our lock is on a dead inode
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return False

    def release(self) -> None:
        """Release the lock (best-effort).

        The lockfile is intentionally **not** deleted.  Deleting it would
        race with another process that has already opened the same path
        and is about to ``flock()`` it — the delete would destroy the
        new holder's lock (flocks are per-inode, and re-creating the
        file yields a new inode).  Leftover lockfiles are tiny, live on
        tmpfs, and are harmlessly reused by the next ``acquire()``.
        """
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def update_vice_pid(self, pid: int) -> None:
        """Update the metadata with the VICE process PID."""
        if self._fd is None:
            return
        self._write_metadata(vice_pid=pid)

    def read_info(self) -> dict | None:
        """Read metadata from the lockfile without acquiring the lock.

        Returns the parsed JSON dict, or None if the file doesn't exist
        or can't be read.  This is for diagnostics only.
        """
        try:
            data = self._lock_path.read_text()
            return json.loads(data)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @classmethod
    def cleanup_stale(cls, lock_dir: Path | None = None) -> int:
        """Remove lockfiles whose holding PID is dead.

        Safety: we only delete a lockfile while holding its flock, so
        concurrent processes that have opened the same path but not yet
        called ``flock()`` will see the flock fail (not succeed on a
        new inode).  After unlinking we keep the fd open briefly so the
        inode stays alive until we close — any racing ``open()`` on the
        same path will create a new inode.

        Returns the number of stale lockfiles removed.
        """
        d = lock_dir or _default_lock_dir()
        removed = 0
        try:
            entries = list(d.glob("port-*.lock"))
        except OSError:
            return 0
        for path in entries:
            try:
                fd = os.open(str(path), os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Someone holds it — not stale
                os.close(fd)
                continue
            # We hold the flock. Check if the recorded PID is dead.
            try:
                raw = os.read(fd, 4096)
                data = json.loads(raw) if raw else {}
                pid = data.get("pid")
                if pid is not None and _pid_alive(pid):
                    # PID is alive but nobody holds the flock — the
                    # metadata is stale (process forgot to clean up).
                    # Still safe to remove since we hold the lock.
                    pass  # fall through to unlink
                # Either dead PID or no/corrupt metadata — remove it
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
            except (json.JSONDecodeError, ValueError, OSError):
                # Corrupt metadata — remove while we hold the lock
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
        return removed

    # -- Context manager --

    def __enter__(self) -> PortLock:
        if not self.acquire():
            raise RuntimeError(
                f"Could not acquire lock for port {self._port}"
            )
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- Internal --

    def _write_metadata(self, vice_pid: int | None = None) -> None:
        """Write JSON metadata to the lockfile."""
        if self._fd is None:
            return
        meta = {
            "pid": os.getpid(),
            "ts": time.time(),
        }
        if vice_pid is not None:
            meta["vice_pid"] = vice_pid
        data = json.dumps(meta).encode()
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, data)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive using os.kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it
