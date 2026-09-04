"""Tests for device-lock visibility to an unlocked lane (issue #194).

``advisory_lock_check`` (issue #136) only fires when the *other* lane
took the lock.  A lane that never touches this package takes no lock and
is invisible to it, so the careful lane gets no protection and no way to
detect the collision.  This file covers the three things that narrow
that gap:

* the once-per-process "you hold no lock" notice at client construction,
  and every way it is suppressed — including the one the harness's own
  locked manager needs, because it builds the client a moment *before*
  it acquires the lock;
* ``device_lock_holder`` / ``device_lock_path``, the cheap public query
  a non-harness runner can call;
* the documented divergence between ``read_info()`` (who held it *last*)
  and ``device_lock_holder()`` (who holds it *now*), which is the trap
  both lanes in #194 fell into.

No test here touches a real device: holders are short-lived
subprocesses, and every HTTP call is a patched ``urlopen``.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import device_lock as lock_mod
from c64_test_harness.backends import ultimate64_client as client_mod
from c64_test_harness.backends.device_lock import (
    UNLOCKED_WARNING_ENV,
    DeviceLock,
    _reset_advisory_state,
    device_lock_holder,
    device_lock_path,
    suppress_unlocked_warning,
    unlocked_warning_enabled,
    warn_unlocked_client,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

HOST = "10.0.0.64"
OTHER_HOST = "10.0.0.65"

_LOGGER = "c64_test_harness.backends.device_lock"
_CLIENT_LOGGER = "c64_test_harness.backends.ultimate64_client"


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    d = tmp_path / "locks"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the warn-once caches, hold registry and env flags."""
    monkeypatch.delenv(UNLOCKED_WARNING_ENV, raising=False)
    _reset_advisory_state()
    yield
    _reset_advisory_state()


@pytest.fixture
def default_lock_dir(monkeypatch: pytest.MonkeyPatch, lock_dir: Path) -> Path:
    """Point every ``lock_dir=None`` default at the tmp directory.

    Needed by the paths that don't take a ``lock_dir`` argument at all —
    the client constructor and ``_LockedU64Manager``.
    """

    def _fake(create: bool = True) -> Path:
        return lock_dir

    monkeypatch.setattr(lock_mod, "_default_lock_dir", _fake)
    return lock_dir


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
        stop.wait(timeout=30.0)
    finally:
        lock.release()


@pytest.fixture
def foreign_holder_process(lock_dir: Path):
    """A separate OS process holding HOST's lock for the test's duration."""
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    stop = ctx.Event()
    proc = ctx.Process(
        target=_hold_device_lock_worker,
        args=(str(lock_dir), HOST, started, stop),
    )
    proc.start()
    try:
        assert started.wait(timeout=20.0), "holder process never acquired"
        yield proc
    finally:
        stop.set()
        proc.join(timeout=10.0)
        if proc.is_alive():  # pragma: no cover - defensive
            proc.terminate()
            proc.join(timeout=5.0)


# -- The public query a non-harness runner can call --------------------


class TestDeviceLockPath:
    def test_names_the_sanitized_lockfile(self, lock_dir: Path) -> None:
        assert device_lock_path("host:8080", lock_dir=lock_dir) == (
            lock_dir / "device-host_8080.lock"
        )

    def test_no_filesystem_side_effect(self, tmp_path: Path) -> None:
        """Asking for the path must not create the directory or file."""
        missing = tmp_path / "not-created"
        path = device_lock_path(HOST, lock_dir=missing)
        assert not missing.exists()
        assert not path.exists()

    def test_default_dir_is_not_created_by_asking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[bool] = []

        def _fake(create: bool = True) -> Path:
            seen.append(create)
            return tmp_path

        monkeypatch.setattr(lock_mod, "_default_lock_dir", _fake)
        device_lock_path(HOST)
        assert seen == [False]


class TestDeviceLockHolder:
    def test_none_when_nobody_ever_locked(self, lock_dir: Path) -> None:
        assert device_lock_holder(HOST, lock_dir=lock_dir) is None

    def test_reports_a_live_foreign_holder(
        self, lock_dir: Path, foreign_holder_process
    ) -> None:
        holder = device_lock_holder(HOST, lock_dir=lock_dir)
        assert holder is not None
        assert holder["pid"] == foreign_holder_process.pid
        assert holder["device_host"] == HOST

    def test_does_not_steal_the_lock(
        self, lock_dir: Path, foreign_holder_process
    ) -> None:
        assert device_lock_holder(HOST, lock_dir=lock_dir) is not None
        contender = DeviceLock(HOST, lock_dir=lock_dir)
        assert not contender.acquire(timeout=0.2, progress_window=None)


class TestReadInfoIsNotTheCurrentHolder:
    """The trap both lanes in #194 fell into, pinned as behaviour.

    ``release()`` deliberately leaves the lockfile behind, so a wrapper
    built on ``read_info()`` announces a finished run as the current
    holder.  These tests assert the divergence rather than the fix,
    because there is no fix: the two methods answer different questions
    and the documentation has to carry the difference.
    """

    def test_read_info_still_names_a_released_holder(
        self, lock_dir: Path
    ) -> None:
        lock = DeviceLock(HOST, lock_dir=lock_dir, heartbeat_interval=None)
        assert lock.acquire(timeout=1.0)
        lock.release()

        # The file survives the release, on purpose.
        assert device_lock_path(HOST, lock_dir=lock_dir).exists()
        info = lock.read_info()
        assert info is not None and info["pid"] == os.getpid()

        # ...and yet nobody holds the device.
        assert device_lock_holder(HOST, lock_dir=lock_dir) is None

    def test_dead_pid_lockfile_is_not_a_holder(self, lock_dir: Path) -> None:
        """The normal post-run state: a file naming a PID that is gone."""
        path = device_lock_path(HOST, lock_dir=lock_dir)
        path.write_text(
            json.dumps(
                {"pid": 999999999, "ts": time.time(), "device_host": HOST}
            )
        )
        lock = DeviceLock(HOST, lock_dir=lock_dir)
        assert lock.read_info()["pid"] == 999999999  # the wrong answer
        assert device_lock_holder(HOST, lock_dir=lock_dir) is None  # the right one


# -- The unlocked-client notice ----------------------------------------


class TestWarnUnlockedClient:
    def test_warns_when_this_process_holds_nothing(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert HOST in message
        # It has to name the lockfile: that is the line that would have
        # saved the reporter an evening.
        assert str(device_lock_path(HOST, lock_dir=lock_dir)) in message
        assert UNLOCKED_WARNING_ENV in message

    def test_fires_with_no_foreign_holder_at_all(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole point: it does not need the other lane to be visible."""
        assert device_lock_holder(HOST, lock_dir=lock_dir) is None
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True
        assert "invisible" in caplog.records[0].getMessage()

    def test_names_a_live_foreign_holder_when_there_is_one(
        self, lock_dir: Path, foreign_holder_process,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True
        assert str(foreign_holder_process.pid) in caplog.records[0].getMessage()

    def test_silent_when_this_process_holds_the_lock(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lock = DeviceLock(HOST, lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            with caplog.at_level(logging.DEBUG, logger=_LOGGER):
                assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
            assert caplog.records == []
        finally:
            lock.release()

    def test_once_per_process_per_host(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
            # A different device is a different collision.
            assert warn_unlocked_client(OTHER_HOST, lock_dir=lock_dir) is True
        assert len(caplog.records) == 2

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_env_var_silences(
        self,
        lock_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        value: str,
    ) -> None:
        monkeypatch.setenv(UNLOCKED_WARNING_ENV, value)
        assert unlocked_warning_enabled() is False
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
        assert caplog.records == []

    @pytest.mark.parametrize("value", ["1", "true", "on", ""])
    def test_env_var_other_values_leave_it_on(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(UNLOCKED_WARNING_ENV, value)
        assert unlocked_warning_enabled() is True

    def test_silenced_env_var_does_not_consume_the_once_token(
        self,
        lock_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Turning the notice off and on again must still produce it."""
        monkeypatch.setenv(UNLOCKED_WARNING_ENV, "0")
        assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
        monkeypatch.delenv(UNLOCKED_WARNING_ENV)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True
        assert len(caplog.records) == 1


class TestSuppressUnlockedWarning:
    def test_suppresses_inside_and_restores_after(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            with suppress_unlocked_warning():
                assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True
        assert len(caplog.records) == 1

    def test_nesting_does_not_re_enable_early(self, lock_dir: Path) -> None:
        with suppress_unlocked_warning():
            with suppress_unlocked_warning():
                assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
            # Inner exit must not lift the outer suppression.
            assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
        assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True

    def test_restores_on_exception(self, lock_dir: Path) -> None:
        with pytest.raises(RuntimeError):
            with suppress_unlocked_warning():
                raise RuntimeError("boom")
        assert warn_unlocked_client(HOST, lock_dir=lock_dir) is True

    def test_scoped_to_the_calling_thread(self, lock_dir: Path) -> None:
        """A suppressed manager thread must not silence an ad-hoc lane."""
        other_result: list[bool] = []
        release = threading.Event()
        done = threading.Event()

        def _other() -> None:
            release.wait(timeout=5.0)
            other_result.append(
                warn_unlocked_client(OTHER_HOST, lock_dir=lock_dir)
            )
            done.set()

        thread = threading.Thread(target=_other)
        thread.start()
        try:
            with suppress_unlocked_warning():
                assert warn_unlocked_client(HOST, lock_dir=lock_dir) is False
                release.set()
                assert done.wait(timeout=5.0)
        finally:
            thread.join(timeout=5.0)
        assert other_result == [True]


# -- Client wiring ------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _client(**kwargs: object) -> Ultimate64Client:
    """Build a client without letting the firmware probe hit the network."""
    with patch("urllib.request.urlopen", MagicMock(side_effect=OSError("no net"))):
        return Ultimate64Client(HOST, **kwargs)  # type: ignore[arg-type]


class TestClientConstructionNotice:
    def test_warns_once_on_construction(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
            _client()
            _client()
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "Ultimate64Client" in message
        assert "run_prg" in message  # the load-and-run point, in the line itself
        assert str(default_lock_dir) in message

    def test_warn_unlocked_false_silences(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
            _client(warn_unlocked=False)
        assert caplog.records == []

    def test_silent_when_the_lock_is_held(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lock = DeviceLock(HOST, lock_dir=default_lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
                _client()
            assert caplog.records == []
        finally:
            lock.release()

    def test_no_notice_when_device_lock_unavailable(
        self,
        default_lock_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(client_mod, "_HAS_DEVICE_LOCK", False)
        with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
            _client()
        assert caplog.records == []

    def test_notice_precedes_any_network_traffic(
        self, default_lock_dir: Path
    ) -> None:
        """The line must land even if the device is unreachable."""
        order: list[str] = []

        def _warn(*args: object, **kwargs: object) -> bool:
            order.append("warn")
            return True

        def _urlopen(*args: object, **kwargs: object):
            order.append("http")
            raise OSError("no net")

        with patch.object(client_mod, "_warn_unlocked_client", _warn):
            with patch("urllib.request.urlopen", _urlopen):
                Ultimate64Client(HOST)
        assert order and order[0] == "warn"


# -- The careful lane must not be the one warned ------------------------


class _FakeDevice:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeInstance:
    def __init__(self, host: str) -> None:
        self.device = _FakeDevice(host)


class _FakeInnerManager:
    """Stands in for Ultimate64InstanceManager.

    Reproduces the ordering that matters: the client is constructed
    while choosing a device, i.e. *before* the caller can know which
    host to lock.
    """

    def __init__(self, host: str) -> None:
        self._host = host
        self.released: list[object] = []

    def acquire(self) -> _FakeInstance:
        _client()
        return _FakeInstance(self._host)

    def release(self, instance: object) -> None:
        self.released.append(instance)

    def shutdown(self) -> None:
        return None


class TestLockedManagerDoesNotWarnItself:
    def test_locked_acquire_is_silent_but_still_locks(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from c64_test_harness.backends.unified_manager import _LockedU64Manager

        mgr = _LockedU64Manager(_FakeInnerManager(HOST), lock_timeout=5.0)
        with caplog.at_level(logging.WARNING):
            instance = mgr.acquire()
            try:
                # Suppression must not have skipped the acquire itself.
                assert DeviceLock.held_by_this_process(
                    HOST, lock_dir=default_lock_dir
                )
            finally:
                mgr.release(instance)
        assert [r.getMessage() for r in caplog.records] == []
        assert not DeviceLock.held_by_this_process(
            HOST, lock_dir=default_lock_dir
        )

    def test_suppression_does_not_leak_past_acquire(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An ad-hoc client built after the manager returns still warns."""
        from c64_test_harness.backends.unified_manager import _LockedU64Manager

        mgr = _LockedU64Manager(_FakeInnerManager(HOST), lock_timeout=5.0)
        instance = mgr.acquire()
        mgr.release(instance)
        with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
            _client()
        assert len(caplog.records) == 1
