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

import ast
import inspect
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


def _resolve_fixture_function(fixture):
    """The generator function behind a ``@pytest.fixture``, without private API (#314).

    pytest 9's ``FixtureFunctionDefinition`` is built with
    ``functools.update_wrapper``, so the standard ``__wrapped__`` reaches the
    original function. A pytest whose decorator returns the function itself
    is used directly. Anything else skips *naming the pytest version*, so a
    pytest refactor reads as "this harness needs updating", not as a lock
    timeout regression. :class:`TestTheGuardIsResolvedPublicly` asserts the
    skip path is not taken on the pytest this repo installs.
    """
    wrapped = getattr(fixture, "__wrapped__", None)
    if wrapped is not None:
        return wrapped
    if inspect.isgeneratorfunction(fixture):
        return fixture
    pytest.skip(
        f"pytest {pytest.__version__}: cannot reach the function behind "
        f"{type(fixture).__name__} (no __wrapped__); update "
        f"_resolve_fixture_function"
    )


def _guard():
    return _resolve_fixture_function(conftest.device_lock_guard)


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


def _resolve_or_fail(fixture):
    """For the controls below: a skip here would hide the defect, so fail."""
    try:
        return _resolve_fixture_function(fixture)
    except pytest.skip.Exception as exc:
        pytest.fail(f"resolution fell through to the skip path: {exc}")


class TestTheGuardIsResolvedPublicly:
    """#314: no dependency on pytest's private ``_get_wrapped_function``.

    The positive controls resolve through :func:`_resolve_or_fail`: a broken
    resolver otherwise *skips*, the run exits 0, and the mutation survives.
    """

    def test_this_module_never_names_the_private_method(self) -> None:
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        private = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "_get_wrapped_function"
        ]
        assert private == [], f"private pytest API used at lines {private}"

    def test_a_fixture_object_with_only_wrapped_is_resolved(self, monkeypatch) -> None:
        """What a pytest without ``_get_wrapped_function`` looks like."""
        fn = conftest.device_lock_guard.__wrapped__
        stand_in = SimpleNamespace(__wrapped__=fn)
        assert not hasattr(stand_in, "_get_wrapped_function")
        monkeypatch.setattr(conftest, "device_lock_guard", stand_in)
        assert _resolve_or_fail(conftest.device_lock_guard) is fn

    def test_a_bare_generator_function_is_used_directly(self) -> None:
        def fixture_fn(request):
            yield None

        assert _resolve_or_fail(fixture_fn) is fixture_fn

    def test_an_unreachable_fixture_skips_naming_the_pytest_version(self) -> None:
        with pytest.raises(pytest.skip.Exception, match=rf"pytest {pytest.__version__}:"):
            _resolve_fixture_function(SimpleNamespace())

    def test_vacuity_guard_the_skip_path_is_not_taken_here(self) -> None:
        """Every TestTheGuardFixture case would skip, not fail, otherwise."""
        try:
            fn = _guard()
        except pytest.skip.Exception as exc:
            pytest.fail(f"_guard() fell through to the skip path: {exc}")
        assert inspect.isgeneratorfunction(fn)
        assert fn.__name__ == "device_lock_guard"
