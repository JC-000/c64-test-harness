"""File-based cross-process device locking using fcntl.flock.

Provides ``DeviceLock`` for kernel-enforced exclusive access to hardware
devices (e.g. Ultimate 64 units) across independent OS processes.  The
kernel automatically releases flocks when the holding process exits
(even on crash), making this crash-safe without manual cleanup.

Unlike :class:`PortLock` which uses non-blocking acquire, ``DeviceLock``
supports a blocking ``acquire(timeout=...)`` so multiple agents queue up
waiting for a single physical device.

If ``watchdog`` is installed (``pip install c64-test-harness[notify]``),
DeviceLock acquire wakes on filesystem events instead of polling.  The
100ms poll cadence remains active as a backstop for kernel-released
flocks (kill -9 holders) where ``release()`` never runs and therefore
no fs-event is emitted.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path

try:  # Optional dependency — see [project.optional-dependencies] notify
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except Exception:  # pragma: no cover - exercised only when watchdog absent
    _HAS_WATCHDOG = False


def _default_lock_dir() -> Path:
    """Return the lock directory, creating it if needed.

    Uses the same directory as :func:`port_lock._default_lock_dir` so
    all harness locks live together.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        d = Path(runtime) / "c64-test-harness"
    else:
        d = Path(f"/tmp/c64-test-harness-{os.getuid()}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_device_id(host: str) -> str:
    """Sanitize a hostname/IP into a safe filename component.

    Replaces any character that isn't alphanumeric, dash, or dot with
    an underscore.  Collapses runs of underscores.
    """
    s = re.sub(r"[^a-zA-Z0-9.\-]", "_", host)
    s = re.sub(r"_+", "_", s)
    return s.strip("_") or "unknown"


class DeviceLock:
    """Cross-process exclusive lock for a hardware device.

    Uses ``fcntl.flock(LOCK_EX)`` on a per-device lockfile keyed by a
    sanitized device identifier (hostname or IP).  The kernel releases
    the lock automatically when the process exits or the file descriptor
    is closed, so this is crash-safe.

    The key difference from :class:`PortLock`: :meth:`acquire` polls
    with ``LOCK_NB`` in a loop up to *timeout* seconds, allowing
    multiple agents to queue for the same physical device.

    Usage::

        lock = DeviceLock("192.168.1.81")
        if lock.acquire(timeout=30.0):
            try:
                # device is exclusively ours
                ...
            finally:
                lock.release()

    Or as a context manager (acquires on enter, releases on exit)::

        with DeviceLock("192.168.1.81") as lock:
            ...
    """

    def __init__(
        self,
        device_host: str,
        lock_dir: Path | None = None,
    ) -> None:
        self._device_host = device_host
        self._device_id = _sanitize_device_id(device_host)
        self._lock_dir = lock_dir or _default_lock_dir()
        self._lock_path = self._lock_dir / f"device-{self._device_id}.lock"
        self._fd: int | None = None

    @property
    def device_host(self) -> str:
        """The original device host string."""
        return self._device_host

    @property
    def held(self) -> bool:
        """Whether this instance currently holds the lock."""
        return self._fd is not None

    def acquire(
        self,
        timeout: float = 30.0,
        *,
        progress_window: float | None = 60.0,
    ) -> bool:
        """Acquire the lock, blocking up to *timeout* seconds.

        Polls with ``LOCK_EX | LOCK_NB`` every 0.1 seconds.  Returns
        ``True`` on success, ``False`` if the timeout expired.

        Writes JSON metadata (PID, timestamp, device host) to the
        lockfile on success.

        After acquiring the flock, we verify the fd's inode still
        matches the path on disk.  If ``cleanup_stale()`` unlinked the
        file between our ``open()`` and ``flock()``, we'd hold a lock
        on an orphaned inode — another process could create a new file
        at the same path and get an independent lock.  The inode check
        detects this and retries with the new file.

        **Queue-aware semantics (default).**  By default, *timeout* is
        the time spent waiting on **stuck** holders.  A live, progressing
        holder extends the deadline indefinitely: if the holder PID is
        alive AND the lockfile mtime is within *progress_window* seconds,
        the deadline is reset on every poll iteration.  Only time spent
        waiting on dead/stuck holders (dead PID, or mtime older than
        *progress_window*) counts against *timeout*.  Pass
        ``progress_window=None`` for the legacy hard-timeout behavior.

        :param timeout: maximum wall time (seconds) to wait against
            stuck/dead holders.  With queue-aware semantics, total
            wall time may exceed *timeout* if the holder keeps making
            progress.
        :param progress_window: how recently the holder must have touched
            the lockfile (seconds) for it to count as "progressing".
            ``None`` disables queue-aware behavior (legacy mode: hard
            timeout).
        """
        if self._fd is not None:
            return True  # Already held by us

        # Best-effort hygiene: a corrupt file shouldn't block legitimate acquirers.
        try:
            self.cleanup_stale(lock_dir=self._lock_dir)
        except Exception:
            pass

        deadline = time.monotonic() + timeout
        notifier = _LockNotifier(self._lock_path) if _HAS_WATCHDOG else None
        try:
            while True:
                result = self._try_acquire_once()
                if result:
                    return True
                # Queue-aware: if the current holder is live and recently
                # progressing, extend the deadline.
                if progress_window is not None and self._holder_is_progressing(
                    progress_window
                ):
                    deadline = time.monotonic() + timeout
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(0.1, remaining)
                if notifier is not None:
                    # Wake on fs-event OR after the poll interval (backstop
                    # for kernel-released flocks where release() never ran).
                    notifier.wait(wait)
                else:
                    time.sleep(wait)
        finally:
            if notifier is not None:
                notifier.stop()

    def _holder_is_progressing(self, progress_window: float) -> bool:
        """True iff the lockfile holder is alive AND mtime is recent.

        "Alive" uses the same PID-liveness check as :meth:`cleanup_stale`.
        "Recent" means lockfile mtime is within *progress_window* seconds.
        Returns False on any IO/JSON error or missing pid (those are
        treated as stuck so they count against *timeout*).
        """
        try:
            st = os.stat(str(self._lock_path))
        except OSError:
            return False
        if (time.time() - st.st_mtime) > progress_window:
            return False
        try:
            data = json.loads(self._lock_path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return False
        pid = data.get("pid")
        if not isinstance(pid, int):
            return False
        return _pid_alive(pid)

    def _try_acquire_once(self) -> bool:
        """Single non-blocking acquire attempt with inode verification.

        Retries once internally in case ``cleanup_stale()`` deletes the
        file between ``open()`` and ``flock()``.
        """
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
            try:
                fd_stat = os.fstat(fd)
                path_stat = os.stat(str(self._lock_path))
                if (
                    fd_stat.st_ino == path_stat.st_ino
                    and fd_stat.st_dev == path_stat.st_dev
                ):
                    self._fd = fd
                    self._write_metadata()
                    return True
            except OSError:
                pass
            # Inode mismatch or path gone — lock is on a dead inode
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

        Bumps the lockfile's mtime as a cooperative wake-up signal for
        watchdog-based notifiers in queued acquirers (best-effort).
        """
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        # Cooperative wake-up: bump mtime BEFORE releasing the flock so
        # the fs-event is observed by waiters that immediately retry.
        try:
            os.utime(str(self._lock_path))
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def read_info(self) -> dict | None:
        """Read metadata from the lockfile without acquiring the lock.

        Returns the parsed JSON dict, or ``None`` if the file doesn't
        exist or can't be read.  This is for diagnostics only.
        """
        try:
            data = self._lock_path.read_text()
            return json.loads(data)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @classmethod
    def cleanup_stale(cls, lock_dir: Path | None = None) -> int:
        """Remove device lockfiles whose holding PID is dead.

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
            entries = list(d.glob("device-*.lock"))
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
            # We hold the flock.  Check if the recorded PID is dead.
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

    def __enter__(self) -> DeviceLock:
        if not self.acquire():
            raise RuntimeError(
                f"Could not acquire lock for device {self._device_host!r}"
            )
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- Internal --

    def _write_metadata(self) -> None:
        """Write JSON metadata to the lockfile."""
        if self._fd is None:
            return
        meta = {
            "pid": os.getpid(),
            "ts": time.time(),
            "device_host": self._device_host,
        }
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


# -- Optional watchdog-backed notifier --


if _HAS_WATCHDOG:

    class _LockNotifier:  # type: ignore[no-redef]
        """Wake on filesystem events targeting *lock_path*.

        Watches the parent directory (the lockfile may not yet exist)
        and signals whenever an event references the exact path.  The
        notifier is single-shot per :meth:`wait` call: after firing it
        re-arms automatically.

        This is a responsiveness optimization, not a correctness
        primitive.  Polling remains the backstop in :meth:`acquire`'s
        loop so kill -9 holders (kernel-released flock, no ``release()``
        cooperative mtime bump) still get noticed.
        """

        def __init__(self, lock_path: Path) -> None:
            import threading

            self._lock_path = str(lock_path)
            self._event = threading.Event()
            self._observer = Observer()
            handler = _LockEventHandler(self._lock_path, self._event)
            try:
                self._observer.schedule(
                    handler, str(lock_path.parent), recursive=False
                )
                self._observer.start()
                self._started = True
            except Exception:
                self._started = False

        def wait(self, timeout: float) -> bool:
            """Block up to *timeout* seconds for an event; return True if signaled."""
            if not self._started:
                time.sleep(timeout)
                return False
            fired = self._event.wait(timeout)
            self._event.clear()
            return fired

        def stop(self) -> None:
            if not self._started:
                return
            try:
                self._observer.stop()
                self._observer.join(timeout=1.0)
            except Exception:
                pass

    class _LockEventHandler(FileSystemEventHandler):  # type: ignore[no-redef,misc]
        def __init__(self, lock_path: str, event) -> None:  # type: ignore[no-untyped-def]
            super().__init__()
            self._lock_path = lock_path
            self._event = event

        def on_any_event(self, event) -> None:  # type: ignore[no-untyped-def]
            # src_path may be bytes on some backends — coerce.
            try:
                src = os.fsdecode(event.src_path)
            except Exception:
                return
            if src == self._lock_path:
                self._event.set()

else:

    class _LockNotifier:  # type: ignore[no-redef]
        """Polling stub used when watchdog is unavailable."""

        def __init__(self, lock_path: Path) -> None:  # pragma: no cover
            self._lock_path = lock_path

        def wait(self, timeout: float) -> bool:  # pragma: no cover
            time.sleep(timeout)
            return False

        def stop(self) -> None:  # pragma: no cover
            return None
