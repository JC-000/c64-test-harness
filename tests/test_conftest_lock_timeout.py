"""The live-test device-lock guard reads its budget through the harness (#301).

``tests/conftest.py``'s autouse ``device_lock_guard`` took its timeout from
``_lock_timeout()``, which parsed ``U64_DEVICE_LOCK_TIMEOUT`` with a bare
``float()``: a malformed value silently became 300 s, and ``0`` / ``inf`` /
``nan`` were accepted and handed to ``acquire_or_raise`` as an *explicit*
timeout, which skips the harness's validation entirely (review round 1 of
PR #308 measured ``timeout=nan`` and ``timeout=inf`` still waiting after
2.5 s).  After #233 the same variable is fatal-on-malformed everywhere else,
so the guard now goes through ``resolve_lock_timeout`` with its own 300 s
default.

Driven offline: the fixture's own function runs against a recording stand-in
for ``DeviceLock``, so no lockfile is created -- not in the real lock
directory, not anywhere -- and no device is contacted.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
from c64_test_harness.backends import device_lock as dl

ENV = "U64_DEVICE_LOCK_TIMEOUT"


@pytest.fixture(autouse=True)
def _no_ambient_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


class _RecordingLock:
    instances: list["_RecordingLock"] = []

    def __init__(self, host: str, **kwargs) -> None:
        self.host = host
        self.kwargs = kwargs
        self.timeouts: list[float] = []
        self.released = False
        _RecordingLock.instances.append(self)

    def acquire_or_raise(self, timeout=None, **_kw) -> None:
        self.timeouts.append(timeout)

    def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_lock(monkeypatch: pytest.MonkeyPatch):
    _RecordingLock.instances = []
    monkeypatch.setattr(conftest, "DeviceLock", _RecordingLock)
    return _RecordingLock


def _request(path: str) -> SimpleNamespace:
    return SimpleNamespace(
        node=SimpleNamespace(path=Path(path), name="test_x"),
        module=SimpleNamespace(_HOST="10.0.0.99"),
    )


def _guard():
    return conftest.device_lock_guard._get_wrapped_function()


def _run_guard(request) -> object:
    gen = _guard()(request)
    value = next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    return value


class TestLockTimeoutHelper:
    def test_unset_uses_the_conftest_default(self) -> None:
        """Patched default is what comes back: the constant is really used."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conftest, "_DEFAULT_LOCK_TIMEOUT", 123.0)
            assert conftest._lock_timeout() == 123.0

    def test_the_guard_default_is_still_300(self) -> None:
        """Paired with the test above; the live guard's default did not move."""
        assert conftest._DEFAULT_LOCK_TIMEOUT == 300.0

    def test_a_valid_value_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "7200")
        assert conftest._lock_timeout() == 7200.0

    @pytest.mark.parametrize("raw", ["30m", "abc", "0", "-5", "inf", "nan"])
    def test_malformed_non_positive_or_non_finite_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(dl.DeviceLockTimeoutConfigError):
            conftest._lock_timeout()

    def test_empty_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conftest, "_DEFAULT_LOCK_TIMEOUT", 123.0)
            assert conftest._lock_timeout() == 123.0


class TestTheGuardFixture:
    def test_control_a_non_live_test_takes_no_lock(self, fake_lock) -> None:
        """The fake request really selects the live branch in the tests below."""
        assert _run_guard(_request("tests/test_x.py")) is None
        assert fake_lock.instances == []

    def test_a_valid_value_reaches_the_lock(
        self, fake_lock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV, "7200")
        lock = _run_guard(_request("tests/test_x_live.py"))
        assert lock is fake_lock.instances[0]
        assert lock.host == "10.0.0.99"
        assert lock.timeouts == [7200.0]
        assert lock.released

    @pytest.mark.parametrize("raw", ["30m", "0", "inf", "nan"])
    def test_the_guard_refuses_a_bad_budget_before_acquiring(
        self, fake_lock, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(dl.DeviceLockTimeoutConfigError):
            next(_guard()(_request("tests/test_x_live.py")))
        assert all(not lk.timeouts for lk in fake_lock.instances), (
            "the guard reached acquire_or_raise with a refused budget"
        )
