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
import threading
import time
import urllib.error
import urllib.request
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


class DeviceLockTimeout(TimeoutError):
    """Raised when :meth:`DeviceLock.acquire_or_raise` exceeds its timeout.

    Carries structured diagnostics so callers (and the agents reading the
    error) can distinguish "queued behind a healthy holder" from
    "holder wedged" from "stale metadata" from "device unreachable" — and
    avoid the historical misdiagnosis of "device is broken, reboot it".

    Attributes
    ----------
    device_host:
        The device's host string (as passed to ``DeviceLock``).
    holder_pid:
        PID recorded in the lockfile's metadata, or ``None`` if no
        readable metadata was found.
    pid_alive:
        Whether the recorded holder PID is currently alive
        (``os.kill(pid, 0)``).  ``None`` when ``holder_pid`` is ``None``.
    lockfile_age_seconds:
        Wall-clock seconds since the lockfile mtime was last bumped, or
        ``None`` if the lockfile is missing.
    device_reachable_rest:
        ``True`` if a quick ``GET /v1/version`` against the device's
        REST API returned a 2xx response, ``False`` on connection or
        timeout failure, ``None`` if the probe was skipped or the URL
        could not be built.
    timeout:
        The ``timeout`` argument passed to ``acquire_or_raise``.
    progress_window:
        The ``progress_window`` argument used during the wait — needed
        for the message to compare ``lockfile_age_seconds`` against.
    """

    def __init__(
        self,
        *,
        device_host: str,
        holder_pid: int | None,
        pid_alive: bool | None,
        lockfile_age_seconds: float | None,
        device_reachable_rest: bool | None,
        timeout: float,
        progress_window: float | None = None,
    ) -> None:
        self.device_host = device_host
        self.holder_pid = holder_pid
        self.pid_alive = pid_alive
        self.lockfile_age_seconds = lockfile_age_seconds
        self.device_reachable_rest = device_reachable_rest
        self.timeout = timeout
        self.progress_window = progress_window
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        # Tag for device reachability — appended to the diagnosed-state
        # sentence so agents stop conflating "queued" with "broken".
        if self.device_reachable_rest is True:
            reach = "; device REST API responsive"
        elif self.device_reachable_rest is False:
            reach = "; device REST API unreachable"
        else:
            reach = ""

        host_tag = f" on {self.device_host!r}"

        # No holder metadata at all — race or fresh stale-cleanup
        if self.holder_pid is None:
            return (
                f"DeviceLock acquire timed out after {self.timeout}s{host_tag}: "
                f"no holder metadata found; acquire still failed (race?)"
                f"{reach}"
            )

        # Dead holder PID — cleanup_stale should have removed this, but
        # we hit the timeout anyway (e.g. test monkeypatched it out, or
        # a real race).
        if self.pid_alive is False:
            return (
                f"DeviceLock acquire timed out after {self.timeout}s{host_tag}: "
                f"stale lock from dead PID {self.holder_pid}; will be cleaned "
                f"on next acquire — retry"
                f"{reach}"
            )

        # From here pid is alive.
        age = self.lockfile_age_seconds
        age_text = (
            f"{age:.0f}s" if isinstance(age, (int, float)) else "unknown"
        )
        pw = self.progress_window
        wedged = (
            isinstance(age, (int, float))
            and isinstance(pw, (int, float))
            and age > pw
        )
        if wedged:
            return (
                f"DeviceLock acquire timed out after {self.timeout}s{host_tag}: "
                f"holder PID {self.holder_pid} is alive but the lockfile "
                f"hasn't been touched in {age_text}; holder may be wedged"
                f"{reach}"
            )
        return (
            f"DeviceLock acquire timed out after {self.timeout}s{host_tag}: "
            f"queued behind live, progressing PID {self.holder_pid} "
            f"(lockfile age {age_text}); retry with a larger timeout — "
            f"device is healthy"
            f"{reach}"
        )


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
        *,
        heartbeat_interval: float | None = 15.0,
    ) -> None:
        self._device_host = device_host
        self._device_id = _sanitize_device_id(device_host)
        self._lock_dir = lock_dir or _default_lock_dir()
        self._lock_path = self._lock_dir / f"device-{self._device_id}.lock"
        self._fd: int | None = None
        # Heartbeat: keep the lockfile mtime fresh so waiters using
        # queue-aware acquire() see this holder as "progressing" past
        # their progress_window.  None/0/negative disables.
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None

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
            # Already held by us; ensure heartbeat is running (idempotent).
            self._start_heartbeat()
            return True

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
                    self._start_heartbeat()
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

    def acquire_or_raise(
        self,
        timeout: float = 30.0,
        *,
        progress_window: float | None = 60.0,
    ) -> None:
        """Acquire the lock or raise :class:`DeviceLockTimeout` with diagnostics.

        Thin wrapper around :meth:`acquire` that turns the bare ``False``
        return value into a structured exception:

        * holder PID, liveness, and lockfile age (the three signals that
          tell you whether you're queued behind a healthy holder or
          something is wrong)
        * a quick reachability probe against the device's REST API
          (``GET /v1/version``) so the message disambiguates "queued"
          from "device broken"

        ``acquire()``'s contract is unchanged — this method is purely
        additive.

        :raises DeviceLockTimeout: when the underlying ``acquire`` returns
            ``False``.
        """
        if self.acquire(timeout=timeout, progress_window=progress_window):
            return
        # Acquire failed — gather diagnostics before raising.
        info = self.read_info()
        holder_pid: int | None = None
        if isinstance(info, dict):
            raw = info.get("pid")
            if isinstance(raw, int):
                holder_pid = raw
        pid_alive: bool | None
        if holder_pid is None:
            pid_alive = None
        else:
            pid_alive = _pid_alive(holder_pid)
        # Lockfile age
        age: float | None
        try:
            st = os.stat(str(self._lock_path))
            age = max(0.0, time.time() - st.st_mtime)
        except OSError:
            age = None
        # Device REST reachability — fast probe, never let it dominate.
        reachable = self._probe_rest_reachable()
        raise DeviceLockTimeout(
            device_host=self._device_host,
            holder_pid=holder_pid,
            pid_alive=pid_alive,
            lockfile_age_seconds=age,
            device_reachable_rest=reachable,
            timeout=timeout,
            progress_window=progress_window,
        )

    def _probe_rest_reachable(self) -> bool | None:
        """Best-effort ``GET /v1/version`` against the device.

        Returns ``True`` on any 2xx response, ``False`` on connection /
        timeout / HTTP error, ``None`` if a URL cannot be built (empty
        host, etc.).  Capped at a 3s budget so it never dominates the
        caller's flow.
        """
        host = (self._device_host or "").strip()
        if not host:
            return None
        try:
            url = f"http://{host}/v1/version"
        except Exception:
            return None
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                return 200 <= int(status) < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        except Exception:
            # Defensive: don't let a probe oddity hide the timeout.
            return False

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
        # Stop the heartbeat before unlinking/releasing so the thread
        # doesn't race with the final mtime bump or the fd close.
        self._stop_heartbeat()
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

    # -- Heartbeat --

    def _start_heartbeat(self) -> None:
        """Start a daemon thread that periodically bumps the lockfile mtime.

        The heartbeat keeps the lockfile mtime fresh so queue-aware waiters
        (``progress_window``) see this holder as "progressing" instead of
        falling back to the hard-timeout deadline.  Only the mtime is
        touched — the JSON metadata is preserved.

        Idempotent: a no-op if a heartbeat thread is already running, or
        if the interval is disabled (``None``, ``0``, or negative).
        """
        interval = self._heartbeat_interval
        if interval is None or interval <= 0:
            return
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop, interval),
            name=f"DeviceLock-heartbeat-{self._device_id}",
            daemon=True,
        )
        self._heartbeat_stop = stop
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        """Signal the heartbeat thread to exit and join briefly.

        Safe to call when no heartbeat is running.
        """
        stop = self._heartbeat_stop
        thread = self._heartbeat_thread
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            # Brief join — the loop wakes on the Event every interval.
            thread.join(timeout=2.0)

    def _heartbeat_loop(
        self, stop: threading.Event, interval: float
    ) -> None:
        """Thread body: bump mtime every *interval* seconds until *stop*.

        Exits quietly on ``FileNotFoundError`` (lockfile unlinked under
        us) or any other ``OSError``.  Never propagates exceptions —
        a misbehaving heartbeat must not crash the holder.
        """
        path = str(self._lock_path)
        while not stop.is_set():
            # Wait first so the very first bump (from _write_metadata)
            # isn't immediately overwritten; this also makes tests that
            # disable the heartbeat predictable.
            if stop.wait(interval):
                return
            try:
                os.utime(path, None)
            except FileNotFoundError:
                return
            except OSError:
                # Filesystem hiccup — stop quietly rather than spinning.
                return
            except Exception:  # pragma: no cover - defensive
                return


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
