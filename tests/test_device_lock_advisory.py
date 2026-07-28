"""Tests for device-lock adoption + advisory enforcement (issue #136).

Covers the three pieces that turn DeviceLock from an opt-in primitive
into an enforced contract:

* in-process hold tracking (``held_by_this_process``) and the nested
  acquire that lets a lock holder re-enter the library,
* live-holder detection (``foreign_holder``) that ignores the leftover
  lockfiles ``release()`` deliberately keeps,
* the warn / raise policy in ``advisory_lock_check`` and its wiring into
  the destructive paths of ``Ultimate64Client``.

No test here touches a real device: holders are fake lockfiles or
short-lived subprocesses, and every HTTP call is a patched urlopen.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import ultimate64_client as client_mod
from c64_test_harness.backends.device_lock import (
    REQUIRE_DEVICE_LOCK_ENV,
    DeviceLock,
    DeviceLockContentionError,
    _reset_advisory_state,
    advisory_lock_check,
    require_device_lock,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

HOST = "10.0.0.64"


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Provide a temporary lock directory for each test."""
    d = tmp_path / "locks"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def clean_advisory_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the warn-once cache, hold registry, and the env flag."""
    monkeypatch.delenv(REQUIRE_DEVICE_LOCK_ENV, raising=False)
    _reset_advisory_state()
    yield
    _reset_advisory_state()


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
        stop.wait(timeout=10.0)
    finally:
        lock.release()


@pytest.fixture
def foreign_holder_process(lock_dir: Path):
    """Run a subprocess that holds HOST's lock for the test's duration."""
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    stop = ctx.Event()
    proc = ctx.Process(
        target=_hold_device_lock_worker,
        args=(str(lock_dir), HOST, started, stop),
    )
    proc.start()
    try:
        if not started.wait(timeout=10.0):
            pytest.fail("holder subprocess never acquired the lock")
        yield proc
    finally:
        stop.set()
        proc.join(timeout=10.0)
        if proc.is_alive():  # pragma: no cover - defensive
            proc.terminate()
            proc.join(timeout=5.0)


# -- In-process hold tracking --


class TestHeldByThisProcess:
    def test_false_when_nothing_held(self, lock_dir: Path) -> None:
        assert not DeviceLock.held_by_this_process(HOST, lock_dir=lock_dir)

    def test_true_while_held_false_after_release(self, lock_dir: Path) -> None:
        lock = DeviceLock(HOST, lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            assert DeviceLock.held_by_this_process(HOST, lock_dir=lock_dir)
        finally:
            lock.release()
        assert not DeviceLock.held_by_this_process(HOST, lock_dir=lock_dir)

    def test_scoped_per_device(self, lock_dir: Path) -> None:
        lock = DeviceLock(HOST, lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            assert not DeviceLock.held_by_this_process("10.0.0.99", lock_dir=lock_dir)
        finally:
            lock.release()

    def test_no_lock_dir_side_effect(self, tmp_path: Path) -> None:
        """The read-only check must not create the lock directory."""
        missing = tmp_path / "never-created"
        assert not DeviceLock.held_by_this_process(HOST, lock_dir=missing)
        assert not missing.exists()


# -- Nested acquire --


class TestNestedAcquire:
    def test_second_instance_conflicts_by_default(self, lock_dir: Path) -> None:
        """Default behaviour is unchanged: no implicit re-entrancy."""
        outer = DeviceLock(HOST, lock_dir=lock_dir)
        assert outer.acquire(timeout=1.0)
        try:
            inner = DeviceLock(HOST, lock_dir=lock_dir)
            assert not inner.acquire(timeout=0.2, progress_window=None)
        finally:
            outer.release()

    def test_nested_joins_existing_hold(self, lock_dir: Path) -> None:
        outer = DeviceLock(HOST, lock_dir=lock_dir)
        assert outer.acquire(timeout=1.0)
        try:
            inner = DeviceLock(HOST, lock_dir=lock_dir, allow_nested=True)
            start = time.monotonic()
            assert inner.acquire(timeout=5.0)
            assert time.monotonic() - start < 1.0  # joined, did not queue
            assert inner.held
            inner.release()
            assert not inner.held
            # The outer holder is untouched by the nested release.
            assert outer.held
            assert DeviceLock.held_by_this_process(HOST, lock_dir=lock_dir)
        finally:
            outer.release()

    def test_nested_acquire_without_holder_takes_real_lock(
        self, lock_dir: Path
    ) -> None:
        """allow_nested only short-circuits when there IS a hold to join."""
        lock = DeviceLock(HOST, lock_dir=lock_dir, allow_nested=True)
        assert lock.acquire(timeout=1.0)
        try:
            assert lock.read_info() is not None  # it wrote metadata: real hold
        finally:
            lock.release()

    def test_outer_release_ends_process_hold(self, lock_dir: Path) -> None:
        """Once the flock is gone the process no longer counts as a holder."""
        outer = DeviceLock(HOST, lock_dir=lock_dir)
        assert outer.acquire(timeout=1.0)
        inner = DeviceLock(HOST, lock_dir=lock_dir, allow_nested=True)
        assert inner.acquire(timeout=1.0)
        outer.release()
        assert not DeviceLock.held_by_this_process(HOST, lock_dir=lock_dir)
        inner.release()  # no error


# -- Live-holder detection --


class TestForeignHolder:
    def test_none_when_no_lockfile(self, lock_dir: Path) -> None:
        assert DeviceLock.foreign_holder(HOST, lock_dir=lock_dir) is None

    def test_none_for_leftover_lockfile(self, lock_dir: Path) -> None:
        """release() leaves the lockfile behind — that is not a holder."""
        path = lock_dir / f"device-{HOST}.lock"
        path.write_text(
            json.dumps({"pid": 999999999, "ts": time.time(), "device_host": HOST})
        )
        assert DeviceLock.foreign_holder(HOST, lock_dir=lock_dir) is None

    def test_none_when_held_by_us(self, lock_dir: Path) -> None:
        lock = DeviceLock(HOST, lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            assert DeviceLock.foreign_holder(HOST, lock_dir=lock_dir) is None
        finally:
            lock.release()

    def test_reports_live_holder(
        self, lock_dir: Path, foreign_holder_process
    ) -> None:
        holder = DeviceLock.foreign_holder(HOST, lock_dir=lock_dir)
        assert holder is not None
        assert holder["pid"] == foreign_holder_process.pid
        assert holder["device_host"] == HOST

    def test_probe_does_not_take_the_lock(
        self, lock_dir: Path, foreign_holder_process
    ) -> None:
        """The shared-flock probe must leave the holder undisturbed."""
        assert DeviceLock.foreign_holder(HOST, lock_dir=lock_dir) is not None
        assert DeviceLock.foreign_holder(HOST, lock_dir=lock_dir) is not None
        contender = DeviceLock(HOST, lock_dir=lock_dir)
        assert not contender.acquire(timeout=0.2, progress_window=None)


# -- Warn / raise policy --


class TestAdvisoryLockCheck:
    def test_silent_when_we_hold_the_lock(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lock = DeviceLock(HOST, lock_dir=lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            with caplog.at_level(logging.DEBUG):
                advisory_lock_check(HOST, "PUT /v1/machine:reboot", lock_dir=lock_dir)
        finally:
            lock.release()
        assert caplog.records == []

    def test_no_warning_for_single_user_flow(
        self, lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nobody else on the device: at most a debug line, never a warning."""
        with caplog.at_level(logging.DEBUG):
            advisory_lock_check(HOST, "PUT /v1/machine:reboot", lock_dir=lock_dir)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert any(r.levelno == logging.DEBUG for r in caplog.records)

    def test_warns_when_another_process_holds(
        self,
        lock_dir: Path,
        foreign_holder_process,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            advisory_lock_check(HOST, "PUT /v1/machine:reboot", lock_dir=lock_dir)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "PUT /v1/machine:reboot" in message
        assert str(foreign_holder_process.pid) in message
        assert REQUIRE_DEVICE_LOCK_ENV in message

    def test_warns_once_per_holder(
        self,
        lock_dir: Path,
        foreign_holder_process,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A chunked write must not produce one warning per request."""
        with caplog.at_level(logging.DEBUG):
            for _ in range(5):
                advisory_lock_check(HOST, "PUT /v1/machine:writemem", lock_dir=lock_dir)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_raises_under_env_flag(
        self,
        lock_dir: Path,
        foreign_holder_process,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(REQUIRE_DEVICE_LOCK_ENV, "1")
        with pytest.raises(DeviceLockContentionError) as excinfo:
            advisory_lock_check(HOST, "PUT /v1/machine:reboot", lock_dir=lock_dir)
        assert excinfo.value.device_host == HOST
        assert excinfo.value.holder_pid == foreign_holder_process.pid

    def test_env_flag_does_not_fire_without_a_holder(
        self, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict mode still allows the uncontended single-user case."""
        monkeypatch.setenv(REQUIRE_DEVICE_LOCK_ENV, "1")
        advisory_lock_check(HOST, "PUT /v1/machine:reboot", lock_dir=lock_dir)

    def test_env_flag_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", " 1 "):
            monkeypatch.setenv(REQUIRE_DEVICE_LOCK_ENV, value)
            assert require_device_lock(), value
        for value in ("", "0", "false", "no", "off", "maybe"):
            monkeypatch.setenv(REQUIRE_DEVICE_LOCK_ENV, value)
            assert not require_device_lock(), value

    def test_internal_error_never_breaks_the_caller(
        self, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object, **kwargs: object) -> bool:
            raise OSError("lock dir on fire")

        monkeypatch.setattr(DeviceLock, "held_by_this_process", boom)
        advisory_lock_check(HOST, "PUT /v1/machine:reboot", lock_dir=lock_dir)


# -- Client wiring --


class _FakeResponse:
    """Context-manager mock of a urlopen response."""

    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def recorded_checks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the advisory check with a recorder of (host, operation)."""
    calls: list[tuple[str, str]] = []

    def _record(host: str, operation: str, **kwargs: object) -> None:
        calls.append((host, operation))

    monkeypatch.setattr(client_mod, "_advisory_lock_check", _record)
    return calls


def _client() -> Ultimate64Client:
    """Build a client without letting the firmware probe hit the network."""
    with patch("urllib.request.urlopen", MagicMock(side_effect=OSError("no net"))):
        return Ultimate64Client("10.0.0.64")


class TestClientAdvisoryWiring:
    def test_reads_are_not_checked(self, recorded_checks: list) -> None:
        c = _client()
        with patch(
            "urllib.request.urlopen", MagicMock(return_value=_FakeResponse())
        ):
            c.get_version()
        assert recorded_checks == []

    def test_destructive_calls_are_checked(self, recorded_checks: list) -> None:
        c = _client()
        with patch(
            "urllib.request.urlopen", MagicMock(return_value=_FakeResponse())
        ):
            c.reboot()
            c.set_config_item("Clock Settings", "CPU Speed", "16")
            c.run_prg(b"\x01\x08rest")
        assert [op for _, op in recorded_checks] == [
            "PUT /v1/machine:reboot",
            "PUT /v1/configs/Clock%20Settings/CPU%20Speed",
            "POST /v1/runners:run_prg",
        ]
        assert {host for host, _ in recorded_checks} == {"10.0.0.64"}

    def test_check_runs_before_the_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under the env flag, a refused call must not reach the device."""

        def _raise(host: str, operation: str, **kwargs: object) -> None:
            raise DeviceLockContentionError(
                "nope", device_host=host, holder_pid=4242
            )

        monkeypatch.setattr(client_mod, "_advisory_lock_check", _raise)
        c = _client()
        urlopen = MagicMock(return_value=_FakeResponse())
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(DeviceLockContentionError):
                c.reboot()
        urlopen.assert_not_called()

    def test_no_check_when_device_lock_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, recorded_checks: list
    ) -> None:
        monkeypatch.setattr(client_mod, "_HAS_DEVICE_LOCK", False)
        c = _client()
        with patch(
            "urllib.request.urlopen", MagicMock(return_value=_FakeResponse())
        ):
            c.reboot()
        assert recorded_checks == []


# -- conftest fixture helpers --


class TestLiveFixtureHelpers:
    def test_live_file_detection(self) -> None:
        from conftest import is_live_test_file

        assert is_live_test_file("/repo/tests/test_socketdma_live.py")
        assert not is_live_test_file("/repo/tests/test_device_lock.py")
        assert not is_live_test_file("/repo/tests/test_live_helpers.py")

    def test_host_from_module_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conftest import live_device_host

        monkeypatch.setenv("U64_HOST", "10.0.0.1")

        class _Module:
            _HOST = "10.0.0.2"

        assert live_device_host(_Module) == "10.0.0.2"

    def test_host_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conftest import live_device_host

        monkeypatch.setenv("U64_HOST", "10.0.0.1")
        assert live_device_host(object()) == "10.0.0.1"

    def test_host_takes_first_of_a_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conftest import live_device_host

        monkeypatch.setenv("U64_HOST", "10.0.0.1, 10.0.0.2")
        assert live_device_host(object()) == "10.0.0.1"

    def test_no_host_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conftest import live_device_host

        monkeypatch.delenv("U64_HOST", raising=False)
        assert live_device_host(object()) is None

    def test_guard_is_a_noop_for_unit_tests(self, request) -> None:
        """This very test proves the autouse guard skips non-live files."""
        assert request.getfixturevalue("device_lock_guard") is None
        assert not DeviceLock.held_by_this_process(
            os.environ.get("U64_HOST", "unset-host")
        )
