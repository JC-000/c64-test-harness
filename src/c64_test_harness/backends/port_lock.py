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
        """
        if self._fd is not None:
            return True  # Already held by us
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
        self._fd = fd
        self._write_metadata()
        return True

    def release(self) -> None:
        """Release the lock and remove the lockfile (best-effort)."""
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
        try:
            self._lock_path.unlink(missing_ok=True)
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
                data = json.loads(path.read_text())
                pid = data.get("pid")
                if pid is not None and not _pid_alive(pid):
                    path.unlink(missing_ok=True)
                    removed += 1
            except (OSError, json.JSONDecodeError, ValueError):
                # Can't read it — try to lock it to check if it's stale
                try:
                    fd = os.open(str(path), os.O_RDWR)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # We got the lock, so no one holds it — stale
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)
                        path.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        os.close(fd)
                except OSError:
                    pass
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
