"""#233: ``U64_DEVICE_LOCK_TIMEOUT`` budget and progress while blocked.

Owner decision 2026-09-14 (option 1c):

* ``DeviceLock.acquire``/``acquire_or_raise``'s ``timeout`` and
  ``UnifiedManager``/``create_manager``'s ``lock_timeout`` read
  ``U64_DEVICE_LOCK_TIMEOUT`` **at call time** when not given; an explicit
  argument always wins.  The defaults do not move.
* Malformed, non-positive or non-finite -> fatal, before any device
  contact.  Empty -> unset, with a logged notice.
* While blocked: an optional ``on_wait(elapsed, holder_pid, lockfile_age,
  queue_depth)`` callback **and** a built-in periodic log line with the
  same fields -- including behind a holder whose deadline is *not* being
  extended, which was silent before.  Staleness is judged against
  ``progress_window`` and never contradicts acquire's own extend decision.

No test here asserts a default by equality alone: each default test
either patches the default and observes the patched value take effect,
or is paired with a test in which the environment demonstrably moves the
value away from it.

The queue scenarios use the #233 technique -- a real flock held by
another thread keeps the waiter blocked, and ``_holder_progress`` is
scripted where the extend decision has to be controlled -- so nothing
races and nothing touches a device.  Every lockfile lives under
``tmp_path``.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import device_lock as dl
from c64_test_harness.backends import unified_manager as um
from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout

HOST = "10.0.0.233"
ENV = "U64_DEVICE_LOCK_TIMEOUT"


@pytest.fixture(autouse=True)
def _no_ambient_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell that exports the budget must not decide these tests."""
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    d = tmp_path / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def held_elsewhere(lock_dir: Path):
    """Hold the device lock from another thread until the test ends."""
    acquired, release = threading.Event(), threading.Event()
    holder = DeviceLock(HOST, lock_dir, heartbeat_interval=None)

    def hold() -> None:
        assert holder.acquire(timeout=5.0)
        acquired.set()
        release.wait(60.0)
        holder.release()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert acquired.wait(5.0), "holder thread never acquired"
    try:
        yield holder
    finally:
        release.set()
        t.join(10.0)


def _bounded(fn, join_timeout: float = 10.0):
    box: dict[str, object] = {}

    def run() -> None:
        started = time.monotonic()
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- reported below
            box["exc"] = exc
        box["elapsed"] = time.monotonic() - started

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(join_timeout)
    box["alive"] = t.is_alive()
    return box


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

class TestResolveLockTimeout:
    def test_env_name_is_the_documented_one(self) -> None:
        assert dl.LOCK_TIMEOUT_ENV == ENV

    def test_unset_returns_the_given_default(self) -> None:
        # A non-default default: proves the parameter, not a literal, is used.
        assert dl.resolve_lock_timeout(None, default=12.5) == 12.5

    def test_env_value_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "7200")
        assert dl.resolve_lock_timeout(None, default=12.5) == 7200.0

    def test_read_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, "11")
        first = dl.resolve_lock_timeout(None, default=1.0)
        monkeypatch.setenv(ENV, "22")
        second = dl.resolve_lock_timeout(None, default=1.0)
        assert (first, second) == (11.0, 22.0)

    def test_explicit_argument_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "7200")
        assert dl.resolve_lock_timeout(5.0, default=12.5) == 5.0

    def test_explicit_argument_wins_over_a_malformed_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit budget never consults the variable at all."""
        monkeypatch.setenv(ENV, "30m")
        assert dl.resolve_lock_timeout(5.0, default=12.5) == 5.0

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_is_unset_with_a_notice(
        self, monkeypatch: pytest.MonkeyPatch, caplog, raw: str
    ) -> None:
        monkeypatch.setenv(ENV, raw)
        with caplog.at_level(logging.DEBUG, logger=dl.__name__):
            assert dl.resolve_lock_timeout(None, default=12.5) == 12.5
        notices = [r for r in caplog.records if ENV in r.getMessage()]
        assert notices, f"empty {ENV} fell back silently: {caplog.text!r}"
        assert notices[0].levelno >= logging.WARNING

    def test_unset_emits_no_notice(self, caplog) -> None:
        """The notice is for the empty spelling, not for the ordinary case."""
        with caplog.at_level(logging.DEBUG, logger=dl.__name__):
            dl.resolve_lock_timeout(None, default=12.5)
        assert not [r for r in caplog.records if ENV in r.getMessage()]

    @pytest.mark.parametrize(
        "raw", ["30m", "2 min", "'60'", "abc", "0", "-5", "-0.0", "inf", "-inf", "nan", "1e999"]
    )
    def test_malformed_non_positive_or_non_finite_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(dl.DeviceLockTimeoutConfigError) as ei:
            dl.resolve_lock_timeout(None, default=12.5)
        assert ENV in str(ei.value)
        assert repr(raw) in str(ei.value) or raw in str(ei.value)

    def test_config_error_is_not_a_lock_timeout(self) -> None:
        """A typo must never be caught by an ``except DeviceLockTimeout`` retry arm."""
        assert not issubclass(dl.DeviceLockTimeoutConfigError, TimeoutError)
        assert issubclass(dl.DeviceLockTimeoutConfigError, ValueError)

    def test_an_explicit_nan_is_refused(self) -> None:
        """NaN never compares <= 0, so it is an unbounded wait that looks bounded."""
        with pytest.raises(ValueError, match="NaN"):
            dl.resolve_lock_timeout(float("nan"), default=12.5)

    @pytest.mark.parametrize("value", [math.inf, 0.0, -1.0])
    def test_other_explicit_values_keep_their_literal_meaning(
        self, monkeypatch: pytest.MonkeyPatch, value: float
    ) -> None:
        """Documented choice: only NaN is refused on the explicit path.

        ``inf`` is a deliberate wait-forever and ``<= 0`` a single attempt;
        neither is a typo in an environment variable.  Set against a malformed
        env to prove the explicit value never consults it.
        """
        monkeypatch.setenv(ENV, "30m")
        assert dl.resolve_lock_timeout(value, default=12.5) == value

    def test_fractional_values_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, " 0.25 ")
        assert dl.resolve_lock_timeout(None, default=12.5) == 0.25


# ---------------------------------------------------------------------------
# DeviceLock.acquire / acquire_or_raise
# ---------------------------------------------------------------------------

class TestAcquireReadsTheBudget:
    def test_acquire_without_timeout_uses_the_env_budget(
        self, lock_dir: Path, held_elsewhere, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Against a non-extending wait, the env value is the time served."""
        monkeypatch.setenv(ENV, "0.3")
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        box = _bounded(lambda: waiter.acquire(progress_window=None), 8.0)
        assert not box["alive"], (
            f"acquire() without a timeout ignored {ENV}=0.3 and is still waiting"
        )
        assert box["value"] is False
        assert box["elapsed"] < 0.3 + 1.5

    def test_acquire_default_is_the_module_default_when_unset(
        self, lock_dir: Path, held_elsewhere
    ) -> None:
        """Patching the default moves the wait: the constant is what is used."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl, "DEFAULT_ACQUIRE_TIMEOUT", 0.3):
            box = _bounded(lambda: waiter.acquire(progress_window=None), 8.0)
        assert not box["alive"] and box["value"] is False

    def test_the_acquire_default_did_not_move(self) -> None:
        """Existing callers keep 30 s; paired with the two tests above."""
        assert dl.DEFAULT_ACQUIRE_TIMEOUT == 30.0

    def test_explicit_timeout_beats_the_env(
        self, lock_dir: Path, held_elsewhere, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, "600")
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        box = _bounded(lambda: waiter.acquire(timeout=0.3, progress_window=None), 8.0)
        assert not box["alive"] and box["value"] is False

    def test_malformed_env_is_fatal_before_touching_the_lock(
        self, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raised even when the lock is free -- before the flock is tried."""
        monkeypatch.setenv(ENV, "30m")
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(waiter, "_try_acquire_once", wraps=waiter._try_acquire_once) as spy:
            with pytest.raises(dl.DeviceLockTimeoutConfigError):
                waiter.acquire()
        assert spy.call_count == 0
        assert not waiter.held

    def test_an_explicit_nan_timeout_fails_instead_of_waiting_for_ever(
        self, lock_dir: Path, held_elsewhere
    ) -> None:
        """Review round 1: ``acquire(timeout=nan)`` was still waiting after 2.5 s."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        box = _bounded(
            lambda: waiter.acquire(timeout=float("nan"), progress_window=None), 3.0
        )
        assert not box["alive"], "acquire(timeout=nan) is an unbounded wait"
        assert isinstance(box.get("exc"), ValueError), box
        assert not waiter.held

    def test_acquire_or_raise_is_fatal_before_any_device_contact(
        self, lock_dir: Path, held_elsewhere, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REST reachability probe is the only device contact in this module."""
        monkeypatch.setenv(ENV, "-1")
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(waiter, "_probe_rest_reachable") as probe:
            with pytest.raises(dl.DeviceLockTimeoutConfigError):
                waiter.acquire_or_raise(progress_window=None)
        probe.assert_not_called()

    def test_acquire_or_raise_reports_the_resolved_budget(
        self, lock_dir: Path, held_elsewhere, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, "0.3")
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(waiter, "_probe_rest_reachable", return_value=None):
            box = _bounded(lambda: waiter.acquire_or_raise(progress_window=None), 8.0)
        assert not box["alive"]
        exc = box.get("exc")
        assert isinstance(exc, DeviceLockTimeout), box
        assert exc.timeout == 0.3


# ---------------------------------------------------------------------------
# UnifiedManager / _LockedU64Manager / create_manager
# ---------------------------------------------------------------------------

def _inst(host: str = "10.0.0.1") -> MagicMock:
    inst = MagicMock()
    inst.device.host = host
    return inst


class TestManagerReadsTheBudget:
    def _mgr(self, **kw):
        inner = MagicMock()
        inner.acquire.return_value = _inst()
        return inner, um._LockedU64Manager(inner, baseline_on_entry=False, **kw)

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_env_budget_reaches_the_lock(
        self, MockLock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, "7200")
        _, mgr = self._mgr()
        mgr.acquire()
        MockLock.return_value.acquire_or_raise.assert_called_once_with(timeout=7200.0)

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_unset_uses_the_manager_default(self, MockLock: MagicMock) -> None:
        _, mgr = self._mgr()
        with patch.object(um, "DEFAULT_LOCK_TIMEOUT", 42.0):
            mgr.acquire()
        MockLock.return_value.acquire_or_raise.assert_called_once_with(timeout=42.0)

    def test_the_manager_default_did_not_move(self) -> None:
        assert um.DEFAULT_LOCK_TIMEOUT == 60.0

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_explicit_lock_timeout_wins(
        self, MockLock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, "7200")
        _, mgr = self._mgr(lock_timeout=5.0)
        mgr.acquire()
        MockLock.return_value.acquire_or_raise.assert_called_once_with(timeout=5.0)

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_read_at_each_acquire(
        self, MockLock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner = MagicMock()
        inner.acquire.side_effect = [_inst("10.0.0.1"), _inst("10.0.0.2")]
        mgr = um._LockedU64Manager(inner, baseline_on_entry=False)
        monkeypatch.setenv(ENV, "100")
        mgr.acquire()
        monkeypatch.setenv(ENV, "200")
        mgr.acquire()
        timeouts = [c.kwargs["timeout"] for c in MockLock.return_value.acquire_or_raise.call_args_list]
        assert timeouts == [100.0, 200.0]

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_malformed_env_fails_before_the_device_is_touched(
        self, MockLock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``inner.acquire()`` probes the device; it must not run on a typo."""
        monkeypatch.setenv(ENV, "inf")
        inner, mgr = self._mgr()
        with pytest.raises(dl.DeviceLockTimeoutConfigError):
            mgr.acquire()
        inner.acquire.assert_not_called()
        MockLock.assert_not_called()

    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_unified_manager_carries_unset_through(self, mock_build: MagicMock) -> None:
        um.UnifiedManager(backend="u64", u64_hosts="10.0.0.1")
        assert mock_build.call_args.kwargs["lock_timeout"] is None

    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_create_manager_carries_unset_through(self, mock_build: MagicMock) -> None:
        um.create_manager(backend="u64", u64_hosts="10.0.0.1")
        assert mock_build.call_args.kwargs["lock_timeout"] is None


# ---------------------------------------------------------------------------
# Progress while blocked
# ---------------------------------------------------------------------------

def _progress_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "still waiting" in r.getMessage()]


class TestOnWaitCallback:
    def test_called_with_the_four_fields_behind_a_non_extending_holder(
        self, lock_dir: Path, held_elsewhere
    ) -> None:
        calls: list[tuple] = []
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl, "_PROGRESS_LOG_INTERVAL", 0.1):
            box = _bounded(
                lambda: waiter.acquire(
                    timeout=0.6, progress_window=None,
                    on_wait=lambda *a: calls.append(a),
                ),
                8.0,
            )
        assert box["value"] is False
        assert len(calls) >= 2, f"on_wait called {len(calls)} time(s)"
        for elapsed, holder_pid, age, depth in calls:
            assert isinstance(elapsed, float) and elapsed > 0
            assert holder_pid == os.getpid()
            assert isinstance(age, float) and age >= 0
            assert isinstance(depth, int) and depth >= 1
        elapsed = [c[0] for c in calls]
        assert elapsed == sorted(elapsed)

    def test_not_called_for_an_uncontended_acquire(self, lock_dir: Path) -> None:
        cb = MagicMock()
        lock = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl, "_PROGRESS_LOG_INTERVAL", 0.0):
            assert lock.acquire(timeout=1.0, on_wait=cb)
        lock.release()
        cb.assert_not_called()

    def test_acquire_or_raise_passes_it_through(
        self, lock_dir: Path, held_elsewhere
    ) -> None:
        cb = MagicMock()
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl, "_PROGRESS_LOG_INTERVAL", 0.1), patch.object(
            waiter, "_probe_rest_reachable", return_value=None
        ):
            box = _bounded(
                lambda: waiter.acquire_or_raise(
                    timeout=0.5, progress_window=None, on_wait=cb
                ),
                8.0,
            )
        assert isinstance(box.get("exc"), DeviceLockTimeout)
        assert cb.call_count >= 1

    def test_a_raising_callback_aborts_the_wait_and_leaves_the_queue_clean(
        self, lock_dir: Path, held_elsewhere
    ) -> None:
        class Stop(Exception):
            pass

        def cb(*_a):
            raise Stop

        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with patch.object(dl, "_PROGRESS_LOG_INTERVAL", 0.1):
            box = _bounded(
                lambda: waiter.acquire(timeout=30.0, progress_window=None, on_wait=cb),
                8.0,
            )
        assert isinstance(box.get("exc"), Stop)
        assert not waiter.held
        assert waiter.queue_depth == 0


class TestProgressLogLine:
    def test_logged_at_warning_behind_a_non_extending_holder(
        self, lock_dir: Path, held_elsewhere, caplog
    ) -> None:
        """The case that was silent: no extension, so no extension WARNING."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.WARNING, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.1
        ):
            box = _bounded(lambda: waiter.acquire(timeout=0.5, progress_window=None), 8.0)
        assert box["value"] is False
        lines = [r for r in _progress_records(caplog) if r.levelno == logging.WARNING]
        assert lines, f"a non-extending wait was silent: {caplog.text!r}"
        msg = lines[-1].getMessage()
        for needle in (HOST, f"pid={os.getpid()}", "lockfile age=", "queue depth="):
            assert needle in msg, f"{needle!r} missing from {msg!r}"

    def test_interval_governs_the_line(
        self, lock_dir: Path, held_elsewhere, caplog
    ) -> None:
        """Control for the test above: at the real 30 s interval, a 0.5 s wait says nothing."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.DEBUG, logger=dl.__name__):
            _bounded(lambda: waiter.acquire(timeout=0.5, progress_window=None), 8.0)
        assert not _progress_records(caplog)

    def _age_lockfile(self, lock_dir: Path, seconds: float) -> None:
        path = dl.device_lock_path(HOST, lock_dir)
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_never_calls_a_holder_wedged_while_acquire_is_extending(
        self, lock_dir: Path, held_elsewhere, caplog
    ) -> None:
        """Scripted: acquire extends, but the file on disk is an hour old.

        A line that judged staleness from the age alone would say STALE
        here while the deadline is being re-armed behind the holder --
        exactly the disagreement #233 rules out.

        **No assertion depends on how fast the waiter runs (#385).**  An
        earlier revision joined the waiter for 1.5 s, then left the ``with``
        while the waiter was still queued.  The stacked patches exit in
        reverse, so ``_holder_progress`` was un-scripted while the 0.1 s
        interval was still patched, and a waiter that woke in that gap (main
        thread descheduled under host load) ran the *real* check against the
        hour-old file and logged a genuine STALE line into the records under
        test.  Separately, its ``timeout=0.3`` expired if the waiter lost the
        CPU for longer than that, failing ``still_extending``.  Now:

        * the wait is observed through ``on_wait`` (two reports), not a
          wall-clock join, and ``_PROGRESS_LOG_INTERVAL`` is 0 so a report is
          due on every iteration;
        * ``timeout`` is far longer than any stall; the wait ends because the
          holder releases, so acquire must *take the lock*, not time out;
        * the holder is released and the waiter joined *inside* the patches,
          so nothing is un-scripted while the waiter can still run;
        * on a failing run the waiter is stopped before the patches exit: a
          ``finally`` sets ``abort``, and ``on_wait`` raises on its next report
          (a raising callback aborts the wait cleanly, pinned by
          ``TestOnWaitCallback``), so the thread cannot outlive the test and
          log into the next test's records (#389 review).

        This test does **not** guard the deadline re-arm: with ``timeout=30``
        a waiter that never re-arms its deadline still reports and still takes
        the lock when the holder goes.  That property is pinned by
        ``tests/test_device_lock.py::TestProgressWindow::test_acquire_extends_on_live_progressing_holder``.
        """
        self._age_lockfile(lock_dir, 3600)
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        reports: list[tuple[int | None, float | None]] = []
        two_reports = threading.Event()
        abort = threading.Event()

        def on_wait(elapsed, holder_pid, age, depth) -> None:
            if abort.is_set():
                raise RuntimeError("test finished: abort the wait (#389)")
            reports.append((holder_pid, age))
            if len(reports) >= 2:
                two_reports.set()

        box: dict[str, object] = {}

        def run() -> None:
            try:
                box["value"] = waiter.acquire(
                    timeout=30.0, progress_window=60.0, on_wait=on_wait
                )
            except BaseException as exc:  # noqa: BLE001 -- reported below
                box["exc"] = exc

        with caplog.at_level(logging.DEBUG, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.0
        ), patch.object(waiter, "_holder_progress", lambda pw: (True, 4242)):
            t = threading.Thread(target=run, daemon=True)
            saw_two_reports = still_queued = finished = False
            try:
                t.start()
                saw_two_reports = two_reports.wait(10.0)
                # Nothing can have ended the wait yet: the holder still holds
                # the flock and the deadline is 30 s past the latest extension.
                still_queued = not box
                held_elsewhere.release()
                t.join(10.0)
                finished = not t.is_alive()
            finally:
                # A waiter still queued here (a failing run) is stopped while
                # its script is in force: its next report raises in on_wait.
                abort.set()
                if t.is_alive():
                    t.join(5.0)
        if box.get("value") is True:
            waiter.release()
        assert finished, "the waiter did not finish after the holder released"
        assert "exc" not in box, box.get("exc")
        assert saw_two_reports, f"fewer than two progress reports: {caplog.text!r}"
        assert still_queued, f"acquire ended while it should have been extending: {box!r}"
        assert box.get("value") is True, "acquire should take the lock once the holder goes"
        # The property is exercised: every report was made behind the scripted
        # holder, with the file on disk older than progress_window.
        assert reports and all(pid == 4242 for pid, _ in reports), reports
        assert all(age is not None and age > 60.0 for _, age in reports), reports
        lines = [r.getMessage() for r in _progress_records(caplog)]
        assert len(lines) >= 2, f"no progress lines while extending: {caplog.text!r}"
        for m in lines:
            assert "STALE" not in m and "wedged" not in m, m
            assert "extended" in m and "not extended" not in m, m
            assert "pid=4242" in m, m

    def test_says_stale_when_acquire_is_not_extending_and_the_file_is_old(
        self, lock_dir: Path, held_elsewhere, caplog
    ) -> None:
        """The control: same hour-old file, acquire not extending -> STALE."""
        self._age_lockfile(lock_dir, 3600)
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.WARNING, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.1
        ):
            box = _bounded(lambda: waiter.acquire(timeout=0.5, progress_window=60.0), 8.0)
        assert box["value"] is False
        msgs = [r.getMessage() for r in _progress_records(caplog)]
        assert any("STALE" in m and "progress_window=60" in m for m in msgs), msgs

    @pytest.mark.parametrize("window, stale", [(5000.0, False), (600.0, True)])
    def test_staleness_threshold_is_progress_window_not_a_fixed_number(
        self, lock_dir: Path, held_elsewhere, caplog, window: float, stale: bool
    ) -> None:
        """Same hour-old file, not extending; only ``progress_window`` differs.

        The hour-old tests above cannot tell ``progress_window`` from any
        threshold below an hour (mutation N10, a fixed 30 s, survived
        them).  Here the age sits between the two windows, so the verdict
        must follow the window.
        """
        self._age_lockfile(lock_dir, 3600)
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.WARNING, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.1
        ), patch.object(waiter, "_holder_progress", lambda pw: (False, None)):
            box = _bounded(lambda: waiter.acquire(timeout=0.5, progress_window=window), 8.0)
        assert box["value"] is False
        msgs = [r.getMessage() for r in _progress_records(caplog)]
        assert msgs, caplog.text
        if stale:
            assert all("STALE" in m and f"progress_window={window:g}" in m for m in msgs), msgs
        else:
            assert all("STALE" not in m for m in msgs), msgs

    def test_a_fresh_non_extending_holder_is_not_called_stale(
        self, lock_dir: Path, held_elsewhere, caplog
    ) -> None:
        """Handoff-chain shape: fresh file, extension stopped -> not STALE."""
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        seq = iter(range(1000, 100000))
        with caplog.at_level(logging.WARNING, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.1
        ), patch.object(waiter, "_holder_progress", lambda pw: (True, next(seq))):
            box = _bounded(lambda: waiter.acquire(timeout=0.6, progress_window=60.0), 8.0)
        assert box["value"] is False
        msgs = [r.getMessage() for r in _progress_records(caplog)]
        assert msgs and all("STALE" not in m for m in msgs), msgs

    def test_no_staleness_verdict_without_a_progress_window(
        self, lock_dir: Path, held_elsewhere, caplog
    ) -> None:
        self._age_lockfile(lock_dir, 3600)
        waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=None)
        with caplog.at_level(logging.WARNING, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.1
        ):
            _bounded(lambda: waiter.acquire(timeout=0.5, progress_window=None), 8.0)
        msgs = [r.getMessage() for r in _progress_records(caplog)]
        assert msgs and all("STALE" not in m for m in msgs), msgs

    def test_self_held_cap_still_warns_and_the_line_names_the_cause(
        self, lock_dir: Path, caplog
    ) -> None:
        """#277 x #233: the grace WARNING survives, and the line is not STALE."""

        def scenario():
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                return inner.acquire(timeout=20.0)
            finally:
                outer.release()

        with caplog.at_level(logging.WARNING, logger=dl.__name__), patch.object(
            dl, "_PROGRESS_LOG_INTERVAL", 0.2
        ):
            box = _bounded(scenario, dl._SELF_HELD_WAIT_GRACE + 6.0)
        assert box["value"] is False
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "this thread" in m and f"{dl._SELF_HELD_WAIT_GRACE:.1f}s" in m
            and "still waiting" not in m
            for m in msgs
        ), msgs
        lines = [r.getMessage() for r in _progress_records(caplog)]
        assert lines, msgs
        assert all("this thread" in m and "STALE" not in m for m in lines), lines


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
LOCK_DOC = _REPO / "docs" / "device_locking.md"
DEV_DOC = _REPO / "docs" / "development.md"


def _paragraphs(text: str) -> list[str]:
    return [" ".join(p.split()) for p in text.split("\n\n") if p.strip()]


def budget_doc_problems(lock_doc: str, dev_doc: str) -> list[str]:
    """What the two docs fail to say about the budget (empty = fine)."""
    problems = []
    for label, text in (("device_locking.md", lock_doc), ("development.md", dev_doc)):
        if ENV not in text:
            problems.append(f"{label} never names {ENV}")
    for needle in (
        f"{dl.DEFAULT_ACQUIRE_TIMEOUT:g} s",
        f"{um.DEFAULT_LOCK_TIMEOUT:g} s",
        "on_wait",
        "progress_window",
    ):
        if needle not in lock_doc:
            problems.append(f"device_locking.md does not state {needle!r}")
    # Review round 1: the refusal list must be scoped to the variable, and
    # the explicit path's rule stated where the budget is documented.
    if not any(
        "explicit" in p.lower() and "NaN" in p and "not checked" in p.lower()
        for p in _paragraphs(lock_doc)
    ):
        problems.append(
            "device_locking.md does not say an explicit timeout is not checked "
            "(except NaN)"
        )
    # #301: the live guard now goes through the resolver; the old caveat is
    # retired and its own default is stated.
    if "falls back silently" in dev_doc:
        problems.append("development.md still describes the conftest silent fallback")
    # "300 s default", not "300 s": the same paragraph's history sentence
    # ("silently became 300 s") kept a looser pin green when the default
    # itself was deleted (review-round mutation R1g).
    if not any(
        "conftest.py" in p and "300 s default" in p and ENV in p
        for p in _paragraphs(dev_doc)
    ):
        problems.append("development.md does not state the live guard's 300 s default")
    # development.md lists gates; the budget must say it is not one, in the
    # same body paragraph that names it.  Headings do not count: mutation
    # N17 deleted the sentence and survived on the section title alone.
    if not any(
        ENV in p and "budget" in p.lower() and "not a gate" in p.lower()
        for p in _paragraphs(dev_doc)
        if not p.lstrip().startswith("#")
    ):
        problems.append(
            f"development.md has no paragraph naming {ENV} as a budget, not a gate"
        )
    return problems


class TestBudgetDocs:
    def test_docs_state_the_budget(self) -> None:
        problems = budget_doc_problems(
            LOCK_DOC.read_text(encoding="utf-8"), DEV_DOC.read_text(encoding="utf-8")
        )
        assert not problems, "\n".join(problems)

    @pytest.mark.parametrize(
        "which, old, expect",
        [
            ("dev", "not a gate", "budget, not a gate"),
            ("lock", "on_wait", "'on_wait'"),
            ("lock", f"{dl.DEFAULT_ACQUIRE_TIMEOUT:g} s", repr(f"{dl.DEFAULT_ACQUIRE_TIMEOUT:g} s")),
            ("dev", ENV, "never names"),
        ],
    )
    def test_positive_control_each_mangle_is_caught(
        self, which: str, old: str, expect: str
    ) -> None:
        lock = LOCK_DOC.read_text(encoding="utf-8")
        dev = DEV_DOC.read_text(encoding="utf-8")
        if which == "dev":
            mangled = dev.replace(old, "XXXX")
            assert mangled != dev, f"mangle {old!r} changed nothing"
            problems = budget_doc_problems(lock, mangled)
        else:
            mangled = lock.replace(old, "XXXX")
            assert mangled != lock, f"mangle {old!r} changed nothing"
            problems = budget_doc_problems(mangled, dev)
        assert any(expect in p for p in problems), problems
