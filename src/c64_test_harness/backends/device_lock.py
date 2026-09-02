"""File-based cross-process device locking using fcntl.flock.

Provides ``DeviceLock`` for kernel-enforced exclusive access to hardware
devices (e.g. Ultimate 64 units) across independent OS processes.  The
kernel automatically releases flocks when the holding process exits
(even on crash), making this crash-safe without manual cleanup.

Unlike :class:`PortLock` which uses non-blocking acquire, ``DeviceLock``
supports a blocking ``acquire(timeout=...)`` so multiple agents queue up
waiting for a single physical device.

**Queue-depth observability.**  The wait queue is observable without
touching the lock: each waiter registers an intent file in a
``<lockfile>.queue/`` sidecar directory before blocking and removes it
after acquiring (or on timeout/error).  ``lock.queue_depth`` (instance
property) and ``DeviceLock.peek_queue_depth(device_host)`` (classmethod,
no instance or lock required) both return the number of *live* waiters —
entries whose recorded PID is dead are treated as stale, excluded from
the count, and garbage-collected on the spot, so crashed waiters never
inflate the count.  Both return ``None`` when the queue is unobservable
(e.g. the sidecar path is unreadable).  Introspection is strictly
read-only with respect to locking: ``acquire(timeout=...)`` semantics
are unchanged.  The mechanism is plain files, so it is portable across
Linux and macOS; it counts cooperating ``DeviceLock`` waiters (which is
every harness consumer), not arbitrary foreign ``flock()`` callers.

If ``watchdog`` is installed (``pip install c64-test-harness[notify]``),
DeviceLock acquire wakes on filesystem events instead of polling.  The
100ms poll cadence remains active as a backstop for kernel-released
flocks (kill -9 holders) where ``release()`` never runs and therefore
no fs-event is emitted.

**Advisory enforcement.**  Holding the lock is a cooperative contract:
nothing stops a process from driving a device it hasn't locked, and
that is exactly how a locked bench run got rebooted underneath itself
(issue #136).  :func:`advisory_lock_check` is the cheap, network-free
check the client layers call before every destructive operation — it
warns (or raises under ``U64_REQUIRE_DEVICE_LOCK=1``) when *this*
process doesn't hold the device lock and another live process does.
Single-user flows never see it: with no live foreign holder the check
emits at most a debug line.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_log = logging.getLogger(__name__)

try:  # Optional dependency — see [project.optional-dependencies] notify
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except Exception:  # pragma: no cover - exercised only when watchdog absent
    _HAS_WATCHDOG = False


def _default_lock_dir(create: bool = True) -> Path:
    """Return the lock directory, creating it if needed.

    Uses the same directory as :func:`port_lock._default_lock_dir` so
    all harness locks live together.

    Pass ``create=False`` for read-only callers (the advisory check) that
    must not have a filesystem side effect just by looking.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        d = Path(runtime) / "c64-test-harness"
    else:
        d = Path(f"/tmp/c64-test-harness-{os.getuid()}")
    if create:
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


# Waiter intent files live in ``<lockfile>.queue/`` and are named
# ``waiter-<pid>-<token>.json`` so the owning PID is recoverable from the
# filename alone (no JSON parse needed for liveness checks).
_WAITER_NAME_RE = re.compile(r"^waiter-(\d+)-[0-9a-f]+\.json$")


#: Lockfile paths this process currently holds, mapped to a reference
#: count.  The count is 1 for the real (flock-owning) holder plus one per
#: live nested holder (see ``allow_nested``).  Consulted by
#: :meth:`DeviceLock.held_by_this_process`, which is how the advisory
#: check tells "we own this device" from "somebody else does".
#: Size of the lockfile's metadata record, in bytes.  The record is
#: padded to this length and rewritten in place so that a reader -- and
#: every reader but ``cleanup_stale`` reads with no lock at all -- sees
#: either the whole previous record or the whole new one.  4096 is
#: chosen to match the ``os.read(fd, 4096)`` cap in ``foreign_holder``
#: and ``cleanup_stale``, and to stay under the ~8 KB threshold above
#: which a reader-side second ``read()`` syscall can land mid-write
#: (issue #161).
_LOCK_RECORD_SIZE = 4096

#: Consecutive ``os.utime`` failures the heartbeat tolerates before it
#: gives up.  A transient filesystem hiccup must not permanently freeze
#: the mtime -- that is the only liveness signal a waiter has, and its
#: absence is reported as "holder may be wedged" (issue #160).
_HEARTBEAT_MAX_CONSECUTIVE_FAILURES = 5

#: How often a waiter that keeps having its deadline extended says so.
#: Extension is silent by default, which is how a starved waiter can sit
#: for hours with nobody able to tell what it is behind (issue #162).
_EXTENSION_LOG_INTERVAL = 30.0

#: How many times the holder's identity may change under a waiter before
#: the deadline stops being extended.  This is a count of *overtakes*,
#: not a duration: a single holder, however long it holds, never changes
#: identity and is never bounded by it.  See :meth:`acquire` for why the
#: two cases need separating and why the bound sits here.
_MAX_HOLDER_HANDOFFS = 3

_PROCESS_HELD: dict[str, int] = {}
#: Thread idents that took a flock for each lockfile path, one entry per
#: outstanding hold.  Only used to spot a thread waiting on a lock it
#: holds itself, which can never be released; guarded by the same lock
#: as :data:`_PROCESS_HELD`.
_PROCESS_HELD_THREADS: dict[str, list[int]] = {}
_PROCESS_HELD_GUARD = threading.Lock()

#: Environment variable that upgrades the advisory warning to a raise.
REQUIRE_DEVICE_LOCK_ENV = "U64_REQUIRE_DEVICE_LOCK"

#: (device_host, holder_pid) pairs already warned about, so a chatty
#: caller (write_mem chunking, a config sweep) warns once per holder
#: instead of once per request.
_WARNED_HOLDERS: set[tuple[str, int | None]] = set()


class DeviceLockContentionError(RuntimeError):
    """Raised by :func:`advisory_lock_check` under ``U64_REQUIRE_DEVICE_LOCK=1``.

    Signals that a destructive device operation was attempted while
    another live process holds the device lock and this process does
    not.  Deliberately not an ``Ultimate64Error`` subclass: callers that
    blanket-catch device errors to retry should NOT swallow this — the
    fix is to acquire the lock, not to retry.
    """

    def __init__(self, message: str, *, device_host: str, holder_pid: int | None = None) -> None:
        super().__init__(message)
        self.device_host = device_host
        self.holder_pid = holder_pid


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
    multiple agents to queue for the same physical device.  The queue
    is observable read-only via :attr:`queue_depth` and
    :meth:`peek_queue_depth` (see module docstring).

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
        allow_nested: bool = False,
    ) -> None:
        """Construct a device lock.

        :param allow_nested: when ``True``, an :meth:`acquire` on a
            device this *same process* already holds joins the existing
            hold (refcounted) instead of blocking on the flock.  Default
            ``False`` keeps the historical behaviour — two ``DeviceLock``
            instances in one process contend exactly like two processes.

            Opt in only where the caller genuinely owns the device
            already and is re-entering the library (e.g. a pytest
            fixture holds the lock for the whole test and the test body
            then goes through ``create_manager``, which locks again).
            Two independent worker *threads* sharing a device must not
            use it — they are concurrent users, not one nested user.
        """
        self._device_host = device_host
        self._device_id = _sanitize_device_id(device_host)
        self._lock_dir = lock_dir or _default_lock_dir()
        self._lock_path = self._lock_dir / f"device-{self._device_id}.lock"
        self._queue_dir_path = Path(str(self._lock_path) + ".queue")
        self._allow_nested = allow_nested
        self._nested = False
        self._owner_thread: int | None = None
        self._fd: int | None = None
        # Heartbeat: keep the lockfile mtime fresh so waiters using
        # queue-aware acquire() see this holder as "progressing" past
        # their progress_window.  None/0/negative disables.
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_stop: threading.Event | None = None
        #: False once the heartbeat has given up (issue #160).  Local
        #: to the holder: a waiter in another process cannot see it,
        #: so it distinguishes the two cases in *this* process's logs
        #: only.  Surfacing it cross-process would need a field in the
        #: lockfile record, which is deliberately not done here.
        self._heartbeat_healthy: bool = True
        self._heartbeat_thread: threading.Thread | None = None

    @property
    def device_host(self) -> str:
        """The original device host string."""
        return self._device_host

    @property
    def held(self) -> bool:
        """Whether this instance currently holds the lock.

        ``True`` for a nested holder too (see ``allow_nested``) — it has
        the same right to use the device, it just isn't the instance
        that owns the underlying flock.
        """
        return self._fd is not None or self._nested

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

        **Starvation by a handoff chain.**  The extension above is tied
        to the holder's *identity*, not just to its freshness.  A single
        holder, however long it holds, never changes identity and so
        waits indefinitely -- that is the documented behaviour and the
        reason a real REU drain or a full capture does not fail the
        lanes behind it.  A *chain* of short holders is different: each
        release bumps the mtime, and each new holder's metadata write
        bumps it again, so two lanes trading the device read as one
        continuously-progressing holder while a third is starved by
        someone who is never the same process twice.

        The bound is therefore a count of *overtakes*, not a duration:
        after ``_MAX_HOLDER_HANDOFFS`` identity changes the deadline
        stops being re-armed and the caller's ``timeout`` runs out
        normally.  Being overtaken once or twice is ordinary -- arriving
        just as a hold ends is a coincidence, not a pathology -- so the
        first few handoffs still extend, and a holder that settles in
        afterwards gets the same indefinite wait it would have got had
        the caller arrived a moment later.  Repeatedly losing the race
        is the signal that no single holder is accountable.  Nothing
        collapses on the first handoff: when extension does stop, the
        full remaining ``timeout`` is still served.

        A holder whose identity cannot be read is not counted as a
        change; a flaky read must not become a starvation verdict.

        While extending, a waiter logs at WARNING every
        ``_EXTENSION_LOG_INTERVAL`` seconds naming how long it has been
        queued, which PID it believes holds the lock, and whether that
        identity has been changing -- so a starved waiter can say what
        it is behind instead of sitting silently.

        **A lock held by this same thread never extends the deadline.**
        Its heartbeat keeps the mtime fresh and its PID is alive by
        definition, so it would otherwise read as a perfectly healthy
        holder for ever and *timeout would be unreachable* -- a permanent
        hang that looks exactly like a healthy queue.  The wait is still
        allowed (another thread may hold a reference to the holder and
        release it in time, which is a supported pattern); it is simply
        bounded by *timeout* again.  Holders in other threads or other
        processes extend the deadline as before.

        :param timeout: maximum wall time (seconds) to wait against
            stuck/dead holders.  With queue-aware semantics, total
            wall time may exceed *timeout* if the holder keeps making
            progress.
        :param progress_window: how recently the holder must have touched
            the lockfile (seconds) for it to count as "progressing".
            ``None`` disables queue-aware behavior (legacy mode: hard
            timeout).
        :raises DeviceLockContentionError: never from this method; see
            :meth:`acquire_or_raise` for the raising variant.

        While blocked, the waiter registers an intent file in the
        ``<lockfile>.queue/`` sidecar directory (removed on exit from
        this method, success or not) so :attr:`queue_depth` /
        :meth:`peek_queue_depth` observers can see it.  Registration is
        best-effort and never changes acquire semantics; an uncontended
        acquire takes the fast path and registers nothing.
        """
        if self._fd is not None:
            # Already held by us; ensure heartbeat is running (idempotent).
            self._start_heartbeat()
            return True
        if self._nested:
            return True

        # This process may already hold the device via another instance —
        # join that hold instead of deadlocking against ourselves.
        if self._allow_nested and self._join_process_hold():
            return True

        # Best-effort hygiene: a corrupt file shouldn't block legitimate acquirers.
        try:
            self.cleanup_stale(lock_dir=self._lock_dir)
        except Exception:
            pass

        # Fast path: an uncontended acquire never becomes a waiter, so it
        # registers no queue intent.
        if self._try_acquire_once():
            self._start_heartbeat()
            return True

        deadline = time.monotonic() + timeout
        queued_since = time.monotonic()
        # Identity of the holder we are queued behind, and how many times
        # it has changed under us.  See the "Starvation" note in the
        # docstring for why a count of overtakes is the right bound.
        observed_pid: int | None = None
        handoffs = 0
        stopped_extending = False
        last_extension_log = time.monotonic()
        notifier = _LockNotifier(self._lock_path) if _HAS_WATCHDOG else None
        # We are about to block: register wait intent so queue_depth /
        # peek_queue_depth observers see this waiter.  Best-effort — a
        # failure to register must never affect acquire semantics.
        intent = self._register_wait_intent()
        try:
            while True:
                # Queue-aware: if the current holder is live and recently
                # progressing, extend the deadline -- but only while it is
                # the *same* holder.  Freshness alone cannot tell one long
                # hold from a chain of short ones, because a handoff
                # refreshes the mtime exactly as a live holder does.
                if progress_window is not None:
                    progressing, holder_pid = self._holder_progress(
                        progress_window
                    )
                    if progressing:
                        # An unreadable identity is not evidence of a new
                        # holder; treating it as one would turn a flaky
                        # read into a starvation verdict.
                        if holder_pid is not None:
                            if (
                                observed_pid is not None
                                and holder_pid != observed_pid
                            ):
                                handoffs += 1
                            observed_pid = holder_pid
                        if handoffs <= _MAX_HOLDER_HANDOFFS:
                            deadline = time.monotonic() + timeout
                            now = time.monotonic()
                            if (
                                now - last_extension_log
                                >= _EXTENSION_LOG_INTERVAL
                            ):
                                last_extension_log = now
                                _log.warning(
                                    "DeviceLock %s: still queued after %.0fs, "
                                    "deadline extended behind live holder "
                                    "pid=%s%s",
                                    self._device_host,
                                    now - queued_since,
                                    observed_pid,
                                    (
                                        f"; holder identity has changed "
                                        f"{handoffs} time(s) (handoff chain)"
                                        if handoffs
                                        else ""
                                    ),
                                )
                        elif not stopped_extending:
                            stopped_extending = True
                            _log.warning(
                                "DeviceLock %s: no longer extending the "
                                "deadline -- the holder changed %d times in "
                                "%.0fs (handoff chain, currently pid=%s). "
                                "Waiting out the remaining timeout; nobody "
                                "holds this device long enough to blame.",
                                self._device_host,
                                handoffs,
                                time.monotonic() - queued_since,
                                observed_pid,
                            )
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
                if self._try_acquire_once():
                    self._start_heartbeat()
                    return True
        finally:
            self._deregister_wait_intent(intent)
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

    def _held_by_this_thread(self) -> bool:
        """Whether the flock we just failed to take is held by *this thread*.

        Scoped to the thread, not the process, and both halves of that
        matter:

        * Not the lockfile's ``pid`` field, because :meth:`release`
          leaves the lockfile behind on purpose -- a file naming this PID
          routinely outlives the hold that wrote it, so reading it would
          make every acquire/release/acquire cycle look self-held.
        * Not the process-wide count either.  Two worker *threads*
          sharing a device are concurrent users, and one waiting on the
          other is legitimate and terminates: the holding thread can
          still reach :meth:`release`.  Only a thread waiting on a lock
          it holds itself is unconditionally stuck, because the one
          thread that could release is the one blocked in ``acquire``.
        """
        key = str(self._lock_path)
        me = threading.get_ident()
        with _PROCESS_HELD_GUARD:
            return me in _PROCESS_HELD_THREADS.get(key, ())

    def _holder_is_progressing(self, progress_window: float) -> bool:
        """Whether the holder looks alive and recently active.

        Thin boolean wrapper over :meth:`_holder_progress`, kept because
        callers and tests use this form.
        """
        return self._holder_progress(progress_window)[0]

    def _holder_progress(
        self, progress_window: float
    ) -> tuple[bool, int | None]:
        """``(progressing, holder_pid)`` for the current lockfile holder.

        The PID half is what lets :meth:`acquire` tell one long holder
        from a chain of short ones; freshness alone cannot, because a
        handoff refreshes the mtime just as a live holder does.

        ``holder_pid`` is ``None`` when the record could not be read or
        names no PID -- which is *not* the same as "a different holder",
        and callers must not treat it as one.

        True iff the lockfile holder is alive AND mtime is recent.

        "Alive" uses the same PID-liveness check as :meth:`cleanup_stale`.
        "Recent" means lockfile mtime is within *progress_window* seconds.
        Returns False on any IO/JSON error or missing pid (those are
        treated as stuck so they count against *timeout*), and False for
        a lock held by this same thread -- see the comment below.
        """
        # A lock held by this very thread is the one case where the
        # freshness signal is worthless: our own heartbeat bumps the
        # mtime and our own PID is alive by definition, so the holder
        # looks maximally healthy while the only thread that could
        # release it is the one blocked here.  Left as "progressing",
        # the deadline resets on every poll and *timeout is unreachable*
        # -- a permanent hang indistinguishable from a healthy queue.
        # Refusing to extend makes the wait bounded again.  It does not
        # forbid the wait: another thread holding a reference to the
        # holder can still release it in time, which is a tested and
        # supported pattern.  A hold owned by a *different* thread is a
        # normal queue and still extends the deadline.
        if self._held_by_this_thread():
            return False, None
        try:
            st = os.stat(str(self._lock_path))
        except OSError:
            return False, None
        if (time.time() - st.st_mtime) > progress_window:
            return False, None
        try:
            data = json.loads(self._lock_path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return False, None
        # Valid JSON that is not an object -- "null", "[]", "42" -- parses
        # fine and then raises AttributeError on .get, which is not in the
        # guard above and would escape a routine acquire() (issue #163).
        if not isinstance(data, dict):
            return False, None
        pid = data.get("pid")
        if not isinstance(pid, int):
            return False, None
        return _pid_alive(pid), pid

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
            # Claim our identity in the file *immediately*.  The flock
            # is already ours, so from here on any unlocked reader --
            # foreign_holder, _holder_is_progressing -- is entitled to
            # believe whatever the body says.  Writing it after the
            # inode recheck left the previous holder's PID in place for
            # the length of two stat() calls, and readers in that window
            # named a process that had released the device some time
            # ago: a confident wrong answer rather than a missing one
            # (issue #165).
            self._fd = fd
            self._write_metadata()
            # Verify our fd still points to the file on disk.
            try:
                fd_stat = os.fstat(fd)
                path_stat = os.stat(str(self._lock_path))
                if (
                    fd_stat.st_ino == path_stat.st_ino
                    and fd_stat.st_dev == path_stat.st_dev
                ):
                    self._register_process_hold()
                    return True
            except OSError:
                pass
            # Inode mismatch or path gone — lock is on a dead inode.
            # The metadata we just wrote went to that dead inode, which
            # is about to be unreachable, so there is nothing to undo
            # beyond dropping our reference to it.
            self._fd = None
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

        A nested holder (see ``allow_nested``) only drops its reference —
        the flock stays with the outermost holder until that one
        releases.
        """
        if self._nested:
            self._nested = False
            self._drop_process_hold()
            return
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        # The flock is going away, so the process no longer holds the
        # device even if a nested holder outlived its parent.
        self._drop_process_hold(purge=True)
        # Stop the heartbeat before the mtime bump and the fd close so
        # the thread doesn't race with them.  (The lockfile is
        # deliberately NOT unlinked here — see the docstring above.  An
        # earlier version of this comment said "before unlinking",
        # which sent two sessions hunting a leak that does not exist:
        # a lockfile holding a dead PID is the normal post-run state.)
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

    # -- Process-hold registry (issue #136) --

    def _register_process_hold(self) -> None:
        """Record that this process owns the flock for this lockfile."""
        key = str(self._lock_path)
        self._owner_thread = threading.get_ident()
        with _PROCESS_HELD_GUARD:
            _PROCESS_HELD[key] = _PROCESS_HELD.get(key, 0) + 1
            _PROCESS_HELD_THREADS.setdefault(key, []).append(
                self._owner_thread
            )

    def _join_process_hold(self) -> bool:
        """Join an existing in-process hold; ``True`` if there was one."""
        key = str(self._lock_path)
        with _PROCESS_HELD_GUARD:
            count = _PROCESS_HELD.get(key, 0)
            if count <= 0:
                return False
            _PROCESS_HELD[key] = count + 1
        self._nested = True
        _log.debug(
            "DeviceLock %s already held by this process (pid=%d) — nested acquire",
            self._device_host,
            os.getpid(),
        )
        return True

    def _drop_process_hold(self, *, purge: bool = False) -> None:
        """Drop this instance's reference in the process-hold registry."""
        key = str(self._lock_path)
        with _PROCESS_HELD_GUARD:
            owners = _PROCESS_HELD_THREADS.get(key)
            if owners and self._owner_thread in owners:
                owners.remove(self._owner_thread)
            if purge:
                _PROCESS_HELD.pop(key, None)
                _PROCESS_HELD_THREADS.pop(key, None)
                return
            if owners is not None and not owners:
                _PROCESS_HELD_THREADS.pop(key, None)
            count = _PROCESS_HELD.get(key, 0) - 1
            if count > 0:
                _PROCESS_HELD[key] = count
            else:
                _PROCESS_HELD.pop(key, None)

    @classmethod
    def held_by_this_process(
        cls, device_host: str, lock_dir: Path | None = None
    ) -> bool:
        """Whether *this OS process* currently holds *device_host*'s lock.

        Answers from the in-process registry, so it costs a dict lookup:
        no filesystem access, no network.  Any ``DeviceLock`` instance
        that acquired successfully and hasn't released registers here,
        which makes this the check destructive-operation callers use to
        decide whether they are a good citizen or a squatter.
        """
        d = lock_dir or _default_lock_dir(create=False)
        key = str(d / f"device-{_sanitize_device_id(device_host)}.lock")
        with _PROCESS_HELD_GUARD:
            return _PROCESS_HELD.get(key, 0) > 0

    @classmethod
    def foreign_holder(
        cls, device_host: str, lock_dir: Path | None = None
    ) -> dict | None:
        """Return metadata for *another live process* holding this device.

        ``None`` when the device is unlocked, when the only holder is
        this process, or when the lock state can't be read.  Never
        blocks and never touches the network.

        Liveness is decided by the flock itself, not by the lockfile
        contents: ``release()`` deliberately leaves the lockfile behind,
        so trusting the metadata alone would report a holder long after
        the run finished.  We probe with a *shared*, non-blocking flock —
        if it fails, someone holds the exclusive lock right now.  The
        probe is released immediately and never blocks a real acquirer
        (a waiter that collides just retries on its next 100 ms poll).
        """
        d = lock_dir or _default_lock_dir(create=False)
        path = d / f"device-{_sanitize_device_id(device_host)}.lock"
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return None  # No lockfile at all — nobody has ever locked it.
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                pass  # Held right now — fall through and describe the holder.
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return None  # Nobody holds it; stale lockfile.
            try:
                raw = os.read(fd, 4096)
            except OSError:
                raw = b""
        finally:
            os.close(fd)

        info: dict = {}
        try:
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                info = parsed
        except (json.JSONDecodeError, ValueError):
            info = {}
        pid = info.get("pid")
        if isinstance(pid, int) and pid == os.getpid():
            # Held by us through some other fd (e.g. a DeviceLock that
            # never registered).  Not a foreign holder.
            return None
        holder = dict(info)
        holder["pid"] = pid if isinstance(pid, int) else None
        holder["device_host"] = info.get("device_host", device_host)
        return holder

    def read_info(self) -> dict | None:
        """Read metadata from the lockfile without acquiring the lock.

        Returns the parsed JSON dict, or ``None`` if the file doesn't
        exist or can't be read.  This is for diagnostics only.
        """
        try:
            data = self._lock_path.read_text()
            parsed = json.loads(data)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        # Only an object is metadata.  Returning a bare list or int here
        # would push the AttributeError into every caller instead
        # (issue #163).
        return parsed if isinstance(parsed, dict) else None

    # -- Queue-depth introspection (issue #130) --

    @property
    def queue_depth(self) -> int | None:
        """Number of live waiters currently queued for this device.

        Lazily computed on each access from the ``<lockfile>.queue/``
        intent directory.  Entries whose recorded PID is dead are
        treated as stale, excluded, and garbage-collected.  Returns
        ``0`` when nobody is waiting (including when the sidecar
        directory doesn't exist yet) and ``None`` when the queue is
        unobservable (sidecar path unreadable or not a directory).

        Read-only: never touches the flock, never blocks.  Note that
        the count reflects *waiters*, not the holder — a held lock with
        no queue reads ``0``.  If this instance is itself blocked in
        :meth:`acquire` (e.g. observed from another thread), its own
        intent entry is included.
        """
        return self._count_live_waiters(self._queue_dir_path)

    @classmethod
    def peek_queue_depth(
        cls, device_host: str, lock_dir: Path | None = None
    ) -> int | None:
        """Pre-acquire peek at the wait-queue depth for *device_host*.

        Classmethod so callers (e.g. a CI bot deciding whether to queue
        or yield) can probe without constructing a lock or holding
        anything.  Same semantics as :attr:`queue_depth`: live waiters
        only, ``0`` for an empty/absent queue, ``None`` when
        unobservable.
        """
        d = lock_dir or _default_lock_dir()
        device_id = _sanitize_device_id(device_host)
        queue_dir = Path(str(d / f"device-{device_id}.lock") + ".queue")
        return cls._count_live_waiters(queue_dir)

    @staticmethod
    def _count_live_waiters(queue_dir: Path) -> int | None:
        """Count live-PID intent files in *queue_dir*, pruning stale ones.

        Stale-entry hygiene mirrors :meth:`cleanup_stale`: an entry
        whose PID (parsed from the filename, falling back to the JSON
        body) is dead or unparseable is unlinked best-effort and not
        counted, so crashed waiters don't inflate the count forever.
        """
        try:
            entries = list(queue_dir.iterdir())
        except FileNotFoundError:
            return 0
        except OSError:
            # Exists but unreadable / not a directory — unobservable.
            return None
        count = 0
        for entry in entries:
            pid: int | None = None
            m = _WAITER_NAME_RE.match(entry.name)
            if m:
                pid = int(m.group(1))
            else:
                # Foreign filename — try the JSON body before giving up.
                try:
                    data = json.loads(entry.read_text())
                    raw = data.get("pid")
                    if isinstance(raw, int):
                        pid = raw
                except (OSError, json.JSONDecodeError, ValueError):
                    pid = None
            if pid is not None and _pid_alive(pid):
                count += 1
                continue
            # Stale (dead PID) or unparseable — prune best-effort.
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
        return count

    def _register_wait_intent(self) -> Path | None:
        """Create this waiter's intent file; return its path or ``None``.

        Best-effort: any failure returns ``None`` and the caller
        proceeds without queue visibility (acquire semantics are never
        affected).
        """
        try:
            self._queue_dir_path.mkdir(parents=True, exist_ok=True)
            name = f"waiter-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
            path = self._queue_dir_path / name
            meta = {
                "pid": os.getpid(),
                "ts": time.time(),
                "device_host": self._device_host,
            }
            path.write_text(json.dumps(meta))
            return path
        except OSError:
            return None

    @staticmethod
    def _deregister_wait_intent(path: Path | None) -> None:
        """Remove this waiter's intent file (best-effort, ``None``-safe)."""
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def cleanup_stale(cls, lock_dir: Path | None = None) -> int:
        """Remove every ``device-*.lock`` in *lock_dir* that nobody holds.

        The rule is **not** "whose holding PID is dead", which is what
        this docstring used to claim.  The recorded PID is read but not
        acted on: a file whose PID is alive is unlinked just the same,
        because being unheld is what makes it removable.  Nor is the
        sweep scoped to this instance's device -- it globs
        ``device-*.lock``, so acquiring one device clears every other
        device's unheld lockfile in the machine-global lock dir.

        Safety comes from the flock, not from the PID: a file is only
        unlinked after this call takes ``LOCK_EX|LOCK_NB`` on it, which
        proves nobody holds the device.  A racing acquirer that opened
        the inode before the unlink and flocks it after is caught by the
        ``st_ino``/``st_dev`` recheck in :meth:`_try_acquire_once`,
        which sees the mismatch and retries.  This is why the sweep is
        safe in a way a manual ``rm`` is not -- a shell has no way to
        prove nobody holds the lock, and unlinking the path while
        another process is blocked on the inode leaves the next acquirer
        locking a *new* inode that excludes nobody.

        Consequence worth knowing: because unheld lockfiles are swept by
        unrelated acquires, and because the record is a single
        last-writer-wins slot, the JSON body is **live status only**.  It
        cannot answer "which lane held this device an hour ago"; there
        is no durable record of a hold anywhere in this package.

        Returns the number of lockfiles removed.
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
            # We hold the flock, which is the whole justification for
            # unlinking: nobody can be using this device.  The recorded
            # PID is read only to log what we are sweeping — alive or
            # dead, the file goes, because an unheld lockfile is debris
            # either way.
            try:
                raw = os.read(fd, 4096)
                data = json.loads(raw) if raw else {}
                pid = data.get("pid") if isinstance(data, dict) else None
                if pid is not None and _pid_alive(pid):
                    _log.debug(
                        "cleanup_stale: %s names live pid %s but is unheld; "
                        "removing (the process released without cleaning up)",
                        path.name,
                        pid,
                    )
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
        """Write the metadata record to the lockfile, in place.

        Padded to :data:`_LOCK_RECORD_SIZE` and written at offset 0 with
        no truncation, because the flock excludes other *writers* and
        nothing at all excludes readers: ``read_info`` and
        ``_holder_is_progressing`` both read unlocked, and
        ``_holder_is_progressing`` has to, since it reads while the
        holder holds ``LOCK_EX``.

        The previous form was ``lseek`` + ``ftruncate(0)`` + ``write``,
        which left the file zero bytes between the second and third
        call.  A reader landing there degraded quietly -- ``read_info``
        returned ``None``, and ``acquire_or_raise`` then reported "no
        holder metadata found; acquire still failed (race?)" instead of
        naming the holder.  Rewriting a fixed-length record in place
        removes the window rather than narrowing it: the bytes on disk
        are always one complete record.

        ``json.loads`` ignores the trailing padding, so readers are
        unchanged.  Not write-to-temp-and-rename: the lockfile's
        identity *is* the flock inode, and a rename would orphan the
        holder's flock on the old inode while the next acquirer locks
        the new one -- two simultaneous holders, the hazard
        :meth:`release` documents.
        """
        if self._fd is None:
            return
        meta = {
            "pid": os.getpid(),
            "ts": time.time(),
            "device_host": self._device_host,
        }
        data = json.dumps(meta).encode()
        try:
            if len(data) > _LOCK_RECORD_SIZE:
                # Should not happen: the record is three small fields.
                # Truncate-and-write rather than silently cutting the
                # record short -- correctness over atomicity, loudly.
                # Readers capped at 4096 bytes will fail to parse this
                # and degrade to "no metadata", which is honest.
                _log.error(
                    "DeviceLock metadata for %s is %d bytes, over the %d-byte "
                    "record; falling back to a truncating write, which is "
                    "briefly observable as empty",
                    self._device_host,
                    len(data),
                    _LOCK_RECORD_SIZE,
                )
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, data)
                return
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, data.ljust(_LOCK_RECORD_SIZE))
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
        self._heartbeat_healthy = True
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

        The mtime bump is the **only** liveness signal a waiter has:
        ``_holder_is_progressing`` reads it, and
        ``DeviceLockTimeout._build_message`` turns its absence into
        "holder may be wedged".  So a heartbeat that dies takes a
        perfectly healthy holder and has every other lane told it is
        stuck -- a diagnosis that is confident, specific and wrong, and
        invisible to the holder itself.

        This used to ``return`` on the first ``OSError``, unlogged, with
        nothing to restart it (``_start_heartbeat`` is only reached from
        ``acquire``).  A single tmpfs hiccup or fd-pressure moment was
        therefore permanent.  Transient errors are now logged and
        retried; only ``_HEARTBEAT_MAX_CONSECUTIVE_FAILURES`` in a row
        give up, loudly, because retrying forever against a genuinely
        broken path is its own bug (issue #160).

        ``FileNotFoundError`` still stops immediately and quietly: the
        lockfile being gone is the documented steady state after a
        release, not a hiccup.

        Never propagates: a misbehaving heartbeat must not crash the
        holder.
        """
        path = str(self._lock_path)
        failures = 0
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
            except OSError as exc:
                failures += 1
                _log.warning(
                    "DeviceLock heartbeat for %s failed to bump %s "
                    "(%d consecutive): %s",
                    self._device_host,
                    path,
                    failures,
                    exc,
                )
                if failures >= _HEARTBEAT_MAX_CONSECUTIVE_FAILURES:
                    self._heartbeat_healthy = False
                    _log.error(
                        "DeviceLock heartbeat for %s giving up after %d "
                        "consecutive failures; this holder's mtime will now "
                        "freeze and waiters will report it as wedged even "
                        "though it is alive",
                        self._device_host,
                        failures,
                    )
                    return
                continue
            except Exception:  # pragma: no cover - defensive
                self._heartbeat_healthy = False
                return
            if failures:
                _log.info(
                    "DeviceLock heartbeat for %s recovered after %d "
                    "consecutive failure(s)",
                    self._device_host,
                    failures,
                )
                failures = 0


def require_device_lock() -> bool:
    """Whether ``U64_REQUIRE_DEVICE_LOCK`` asks for hard enforcement.

    Read at call time (not import time) so tests and long-lived
    processes can flip it.  Accepts ``1`` / ``true`` / ``yes`` / ``on``,
    case-insensitive.
    """
    raw = os.environ.get(REQUIRE_DEVICE_LOCK_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def advisory_lock_check(
    device_host: str,
    operation: str,
    *,
    lock_dir: Path | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Warn (or raise) before *operation* mutates an unlocked device.

    Called by the client layers at destructive-call time.  Three
    outcomes, in order of how much noise they make:

    * this process holds the device lock → silent, the contract is met;
    * nobody else holds it → a DEBUG line, nothing more.  This is the
      single-user case and it must stay quiet;
    * another live process holds it → a WARNING naming the holder PID,
      or :class:`DeviceLockContentionError` when
      ``U64_REQUIRE_DEVICE_LOCK=1``.

    The check is filesystem-only (one ``open`` + one non-blocking
    ``flock`` + one small read) and swallows its own errors: an advisory
    check must never be the reason a device call fails.

    :param operation: short description used in the message, e.g.
        ``"PUT /v1/machine:reboot"``.
    """
    log = logger or _log
    try:
        if DeviceLock.held_by_this_process(device_host, lock_dir=lock_dir):
            return
        holder = DeviceLock.foreign_holder(device_host, lock_dir=lock_dir)
    except Exception:  # pragma: no cover - defensive
        return
    if holder is None:
        log.debug(
            "%s on %s: no device lock held (no other holder either)",
            operation,
            device_host,
        )
        return

    holder_pid = holder.get("pid")
    message = (
        f"{operation} on {device_host} without holding the device lock — "
        f"PID {holder_pid if holder_pid is not None else '?'} holds it right "
        f"now.  Destructive calls against a device another job has locked are "
        f"how measurements get silently discarded (issue #136).  Acquire the "
        f"lock first: DeviceLock({device_host!r}).acquire_or_raise(...)."
    )
    if require_device_lock():
        raise DeviceLockContentionError(
            message + f"  ({REQUIRE_DEVICE_LOCK_ENV}=1 turns this warning into an error.)",
            device_host=device_host,
            holder_pid=holder_pid if isinstance(holder_pid, int) else None,
        )
    key = (device_host, holder_pid if isinstance(holder_pid, int) else None)
    with _PROCESS_HELD_GUARD:
        already_warned = key in _WARNED_HOLDERS
        _WARNED_HOLDERS.add(key)
    if already_warned:
        log.debug("%s (repeat, warned once already)", message)
        return
    log.warning(
        "%s  Set %s=1 to make this an error.", message, REQUIRE_DEVICE_LOCK_ENV
    )


def _reset_advisory_state() -> None:
    """Clear the warn-once cache and the hold registry (tests only)."""
    with _PROCESS_HELD_GUARD:
        _WARNED_HOLDERS.clear()
        _PROCESS_HELD.clear()
        _PROCESS_HELD_THREADS.clear()


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
