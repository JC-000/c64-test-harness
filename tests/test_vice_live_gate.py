"""The ``C64_REQUIRE_VICE`` gate in ``tests/conftest.py``, driven directly.

The gate exists to stop a mocks-only run from certifying the VICE
backend.  Two ways it could itself be vacuous:

* the report hook counting a live test whose *body skipped* as "ran" --
  a ``pytest.skip()`` inside a ``vice_live`` test produces a ``call``
  report with ``outcome == "skipped"``, and a run made entirely of those
  exercised no emulator at all;
* the session hook overwriting an exit status that was already telling
  the operator something worse (INTERRUPTED, INTERNAL_ERROR) with a
  plain 1.

These tests call the hook functions with fake report / session objects
rather than spinning up a nested pytest, matching how the rest of this
suite treats conftest helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import conftest


def _report(when: str, outcome: str, *, live: bool = True) -> SimpleNamespace:
    keywords = {conftest.VICE_LIVE_MARKER: 1} if live else {}
    return SimpleNamespace(when=when, outcome=outcome, keywords=keywords)


def _session(*, exitstatus: int, collected: int = 5, live_collected: int = 2):
    pm = SimpleNamespace(get_plugin=lambda name: None)
    return SimpleNamespace(
        exitstatus=exitstatus,
        testscollected=collected,
        _vice_live_collected=live_collected,
        config=SimpleNamespace(pluginmanager=pm),
    )


@pytest.fixture
def fresh_counter(monkeypatch):
    monkeypatch.setattr(conftest, "_vice_live_ran", 0)
    yield


@pytest.fixture
def vice_required(monkeypatch):
    monkeypatch.setenv(conftest.REQUIRE_VICE_ENV, "1")
    yield


# ------------------------------------------------------ the report hook


@pytest.mark.usefixtures("fresh_counter")
@pytest.mark.parametrize("outcome", ["passed", "failed"])
def test_a_live_body_that_ran_is_counted(outcome):
    conftest.pytest_runtest_logreport(_report("call", outcome))
    assert conftest._vice_live_ran == 1


@pytest.mark.usefixtures("fresh_counter")
def test_a_live_body_that_skipped_is_not_counted():
    """``pytest.skip()`` inside the body still yields a ``call`` report.

    Counting it says an emulator was exercised when nothing ran, which
    is the exact vacuous pass the gate is there to catch.
    """
    conftest.pytest_runtest_logreport(_report("call", "skipped"))
    assert conftest._vice_live_ran == 0


@pytest.mark.usefixtures("fresh_counter")
@pytest.mark.parametrize("when", ["setup", "teardown"])
def test_only_the_call_phase_counts(when):
    conftest.pytest_runtest_logreport(_report(when, "passed"))
    assert conftest._vice_live_ran == 0


@pytest.mark.usefixtures("fresh_counter")
def test_a_non_live_test_is_not_counted():
    conftest.pytest_runtest_logreport(_report("call", "passed", live=False))
    assert conftest._vice_live_ran == 0


# ----------------------------------------------------- the session hook


@pytest.mark.usefixtures("fresh_counter", "vice_required")
def test_a_green_run_with_no_live_test_is_failed():
    session = _session(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1


@pytest.mark.usefixtures("fresh_counter", "vice_required")
@pytest.mark.parametrize("status", [2, 3], ids=["INTERRUPTED", "INTERNAL_ERROR"])
def test_a_worse_exit_status_is_not_masked(status):
    """Ctrl-C or a crashed plugin must not be relabelled 'tests failed'."""
    session = _session(exitstatus=status)
    conftest.pytest_sessionfinish(session, status)
    assert session.exitstatus == status


@pytest.mark.usefixtures("fresh_counter", "vice_required")
def test_an_already_failed_run_keeps_its_status():
    session = _session(exitstatus=1)
    conftest.pytest_sessionfinish(session, 1)
    assert session.exitstatus == 1


@pytest.mark.usefixtures("fresh_counter")
def test_the_gate_is_inert_when_not_required(monkeypatch):
    monkeypatch.delenv(conftest.REQUIRE_VICE_ENV, raising=False)
    session = _session(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0


@pytest.mark.usefixtures("vice_required")
def test_a_run_in_which_a_live_test_ran_passes(monkeypatch):
    monkeypatch.setattr(conftest, "_vice_live_ran", 1)
    session = _session(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
