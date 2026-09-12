"""A self-held acquire must give up promptly, not serve the full timeout.

``_held_by_this_thread()`` names the one wait that cannot be resolved by
the waiter: the thread blocked in :meth:`DeviceLock.acquire` is the
thread that holds the flock, so nothing it does can release it.
``tests/test_device_lock_self_deadlock.py`` already stops that wait from
extending its deadline for ever; what it does not do is stop it from
sitting out the caller's ``timeout``, and those timeouts are large
(600 s in two of the live UCI modules).  A 13-test diagnostic run spent
22 minutes discovering this (issue #273 class 2).

The bound here is a short grace rather than an immediate ``False``
because the wait is *not* unconditionally stuck in practice: a second
thread holding a reference to the holder can release it, which
``test_device_lock.py::TestBlockingTimeout::test_acquire_succeeds_after_release``
and ``test_device_lock_self_deadlock.py::...::test_helper_thread_can_still_rescue_a_self_held_wait``
both pin.  ``_SELF_HELD_WAIT_GRACE`` keeps that rescue window open while
turning 600 s of certain failure into a couple of seconds.

Every acquire below runs on a bounded thread, so a regression fails the
test instead of hanging the suite.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.device_lock import (
    _SELF_HELD_WAIT_GRACE,
    DeviceLock,
)

HOST = "10.0.0.73"

#: Comfortably larger than the grace, so "gave up early" and "served the
#: caller's timeout" cannot be confused for one another.
CALLER_TIMEOUT = 20.0

#: A caller timeout well *under* the grace, for the cap-is-not-a-floor
#: direction.
SHORT_TIMEOUT = 0.3
#: Scheduling slack on top of it.  ``acquire`` polls at 0.1 s, so this
#: is several poll intervals -- loose enough not to flake on a loaded
#: bench, far tighter than the grace it must not be confused with.
SHORT_TIMEOUT_SLACK = 0.5


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "locks"


def _run_bounded(fn, *, join_timeout: float = 10.0):
    """Run *fn* on a daemon thread with a deadline (see sibling module)."""
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- reported, not swallowed
            box["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(join_timeout)
    if thread.is_alive():
        pytest.fail(
            f"did not return within {join_timeout}s -- the self-held waiter is "
            f"still serving its caller's timeout"
        )
    if "exc" in box:
        return "raise", box["exc"]
    return "return", box["value"]


class TestSelfHeldWaitIsBounded:
    def test_self_held_acquire_gives_up_within_the_grace(
        self, lock_dir: Path
    ) -> None:
        """The whole scenario runs on one thread: hold, then wait."""

        def scenario():
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                started = time.monotonic()
                got = inner.acquire(timeout=CALLER_TIMEOUT)
                return got, time.monotonic() - started
            finally:
                outer.release()

        kind, result = _run_bounded(scenario)
        assert kind == "return", f"unexpected raise: {result!r}"
        got, elapsed = result
        assert got is False
        assert elapsed < _SELF_HELD_WAIT_GRACE + 2.0, (
            f"waited {elapsed:.1f}s for a lock this thread holds -- the grace "
            f"is {_SELF_HELD_WAIT_GRACE}s and the caller asked for "
            f"{CALLER_TIMEOUT}s"
        )

    def test_self_held_wait_says_so_at_warning(
        self, lock_dir: Path, caplog
    ) -> None:
        """The give-up must be diagnosable, not a bare ``False``.

        This is the test the constant's own note leans on.  Choosing a
        lossy cap over a fail-fast is justified there by the claim that
        a rescue which needs longer "fails where it used to succeed --
        the WARNING in :meth:`acquire` names the cap so that failure is
        diagnosable rather than mysterious".  So the assertion has to
        cover everything that sentence promises, not merely that some
        WARNING was emitted: which device, what the caller asked for,
        what it was cut to, and how to opt out.  Asserting less lets the
        message decay to a bare "this thread problem" while the
        docstring keeps promising a diagnosis.
        """

        def scenario():
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                return inner.acquire(timeout=CALLER_TIMEOUT)
            finally:
                outer.release()

        with caplog.at_level(
            logging.WARNING, logger="c64_test_harness.backends.device_lock"
        ):
            assert _run_bounded(scenario) == ("return", False)

        messages = [r.getMessage() for r in caplog.records]
        required = {
            "the device it is about": HOST,
            "the condition": "this thread",
            "the caller's timeout": f"{CALLER_TIMEOUT:.0f}s",
            "the cap actually applied": f"{_SELF_HELD_WAIT_GRACE:.1f}s",
            "the remedy": "allow_nested=True",
        }
        for label, needle in required.items():
            assert any(needle in m for m in messages), (
                f"the self-held WARNING does not name {label} ({needle!r}); "
                f"records={messages}"
            )

    def test_a_shorter_caller_timeout_still_wins(self, lock_dir: Path) -> None:
        """The grace is a cap, never an extension of a shorter timeout.

        Measured against *the caller's own timeout*, not against the
        grace.  "Faster than 2 s" is satisfied by anything that inflates
        a 0.3 s caller to 1.5 s, which is a floor wearing a cap's name;
        only the caller's own number can tell the two apart.
        """

        def scenario():
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                started = time.monotonic()
                got = inner.acquire(timeout=SHORT_TIMEOUT)
                return got, time.monotonic() - started
            finally:
                outer.release()

        kind, result = _run_bounded(scenario)
        assert kind == "return"
        got, elapsed = result
        assert got is False
        assert elapsed < SHORT_TIMEOUT + SHORT_TIMEOUT_SLACK, (
            f"a {SHORT_TIMEOUT}s caller waited {elapsed:.2f}s -- the grace is "
            f"being applied as a floor, not a cap"
        )


class TestTheBoundIsScopedToSelfHeldWaits:
    def test_allow_nested_joins_instead_of_waiting(self, lock_dir: Path) -> None:
        def scenario() -> bool:
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            try:
                inner = DeviceLock(
                    HOST, lock_dir, heartbeat_interval=0.05, allow_nested=True
                )
                got = inner.acquire(timeout=CALLER_TIMEOUT)
                if got:
                    inner.release()
                return got
            finally:
                outer.release()

        assert _run_bounded(scenario) == ("return", True)

    def test_a_hold_in_another_thread_is_not_capped(
        self, lock_dir: Path
    ) -> None:
        """A normal queue must keep serving the caller's whole timeout.

        The holding thread releases well after the grace would have
        expired; a bound applied to *any* in-process hold would return
        ``False`` before that.
        """
        acquired = threading.Event()
        release_at = _SELF_HELD_WAIT_GRACE + 1.5

        def hold() -> None:
            lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            if lock.acquire(timeout=5.0):
                acquired.set()
                time.sleep(release_at)
                lock.release()

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        try:
            assert acquired.wait(10.0), "holder thread never acquired"

            def scenario() -> bool:
                waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                got = waiter.acquire(timeout=CALLER_TIMEOUT)
                if got:
                    waiter.release()
                return got

            assert _run_bounded(
                scenario, join_timeout=release_at + 8.0
            ) == ("return", True)
        finally:
            thread.join(15.0)

    def test_a_foreign_hold_still_serves_the_whole_timeout(
        self, lock_dir: Path
    ) -> None:
        """The cap must not leak onto ordinary contention.

        The sibling test above cannot see that on its own: a live holder
        in another thread keeps *extending* the deadline, so a wrongly
        capped ``timeout`` is invisible there.  ``progress_window=None``
        turns extension off, which is the only configuration in which
        the caller's timeout is the thing actually being served -- and
        it must be served in full.
        """
        acquired = threading.Event()
        release = threading.Event()
        caller_timeout = 4.0

        def hold() -> None:
            lock = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            if lock.acquire(timeout=5.0):
                acquired.set()
                release.wait(30.0)
                lock.release()

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        try:
            assert acquired.wait(10.0), "holder thread never acquired"

            def scenario():
                waiter = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                started = time.monotonic()
                got = waiter.acquire(
                    timeout=caller_timeout, progress_window=None
                )
                return got, time.monotonic() - started

            kind, result = _run_bounded(scenario, join_timeout=15.0)
            assert kind == "return", f"unexpected raise: {result!r}"
            got, elapsed = result
            assert got is False
            assert elapsed >= caller_timeout - 0.5, (
                f"gave up after {elapsed:.1f}s of a {caller_timeout}s timeout "
                f"against a lock held by another thread -- the self-held cap "
                f"is being applied to ordinary contention"
            )
        finally:
            release.set()
            thread.join(15.0)


class TestTheRescueWindow:
    """Where the rescue window closes, pinned so the trade stays visible.

    The two rescue tests this cap was designed around release after
    0.2 s, so they pass at any grace above roughly a quarter of a
    second -- they cannot tell 0.5 s from 2 s from 60 s.  These do:
    they put the cliff at the constant, in both directions, so that
    changing ``_SELF_HELD_WAIT_GRACE`` is a deliberate act with a
    visible consequence rather than a silent one.

    Nothing here claims the value is *right*.  See the constant's own
    note: it is a chosen trade, and the rescue latency that would
    justify a derived value does not exist in this repo.
    """

    def _rescue_after(self, lock_dir: Path, delay: float):
        def scenario():
            outer = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
            assert outer.acquire(timeout=5.0)
            timer = threading.Timer(delay, outer.release)
            timer.start()
            try:
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                got = inner.acquire(timeout=CALLER_TIMEOUT)
                if got:
                    inner.release()
                return got
            finally:
                timer.join(delay + 5.0)
                if outer.held:
                    outer.release()

        return _run_bounded(scenario, join_timeout=delay + 15.0)

    def test_a_rescue_inside_the_grace_still_succeeds(
        self, lock_dir: Path
    ) -> None:
        assert self._rescue_after(lock_dir, _SELF_HELD_WAIT_GRACE * 0.5) == (
            "return",
            True,
        )

    def test_a_rescue_past_the_grace_no_longer_does(
        self, lock_dir: Path
    ) -> None:
        """The cost side of the trade, stated rather than discovered."""
        assert self._rescue_after(lock_dir, _SELF_HELD_WAIT_GRACE * 1.5) == (
            "return",
            False,
        )


class TestNestingIsTheAcquirersProperty:
    """The other half of the sentence in ``tests/conftest.py``.

    That paragraph now says nesting is a property of the **acquirer**,
    never of the holder.  The first half is pinned in two places already
    -- ``test_allow_nested_joins_instead_of_waiting`` above and
    ``test_device_lock_self_deadlock.py::TestSelfHeldDoesNotExtendDeadline::
    test_allow_nested_still_joins`` -- but both set the flag on the
    *inner* lock, as does every other ``allow_nested=True`` in the suite.
    Nothing set it on the holder while the acquirer went without, which
    is the direction the sentence is actually about, and the direction
    the four broken live modules got wrong.

    **This is a consistency test, not a hazard control, and the
    distinction matters enough to spell out.**  If the gate in
    :meth:`DeviceLock.acquire` ever did start consulting the holder's
    flag, the effect would be **fail-safe**: a plain acquirer under
    ``conftest``'s guard (which holds with ``allow_nested=True``) would
    join, and the self-deadlock of issue #273 would quietly *disappear*.
    Nobody would deadlock.  What would actually go wrong is one step
    further out -- ``tests/test_live_device_lock_nesting.py`` would begin
    flagging call sites that were no longer broken, somebody would read
    a screenful of false positives as obsolete noise, and the guard
    would be deleted.  That is the failure this closes, and it is a
    minor one.

    It earns its place on the commit's own logic rather than on risk:
    the thesis here is that an unpinned paragraph in ``conftest.py``
    about this exact semantic produced a two-week-class defect.
    Shipping the corrected paragraph with precisely the protection the
    wrong one had would contradict that reasoning.
    """

    def test_the_holders_flag_does_not_nest_the_acquirer(
        self, lock_dir: Path
    ) -> None:
        """Holder opts in, acquirer does not: the acquirer must not join.

        Nesting is a property of the acquirer.  A holder that set
        ``allow_nested=True`` does not confer it on anyone else -- which
        is the proposition ``tests/conftest.py``'s ``device_lock_guard``
        docstring asserts, and the **inverse** of that belief is what
        produced #273 class 2: four live modules built a plain
        ``DeviceLock`` in the test body on the understanding that the
        guard's flag covered them.

        Three conditions, and dropping any one turns this into a test
        that already exists: the holder carries the flag, the acquirer
        does not, and both are on the same thread.

        The whole scenario runs on one thread, so the acquirer is asking
        for a lock its own thread holds -- the arrangement
        ``conftest``'s autouse guard creates for every live test body.

        Asserted on the outcome, not on the clock: a joined hold returns
        ``True`` immediately, a refused one returns ``False``.  The wall
        time it takes to say ``False`` is ``_SELF_HELD_WAIT_GRACE``
        expiring, which is this module's own constant and would restate
        the implementation rather than test it.
        """

        def scenario():
            outer = DeviceLock(
                HOST, lock_dir, heartbeat_interval=0.05, allow_nested=True
            )
            # Condition 1, asserted rather than assumed.  Drop the
            # holder's flag and this test still passes -- the inner
            # acquire is self-held and capped either way, so ``got`` is
            # ``False`` for a reason with nothing to do with nesting.
            # Unasserted, a refactor that dropped it would leave this
            # green while silently demoting it to a duplicate of
            # ``test_self_held_acquire_gives_up_within_the_grace``.  A
            # scaffolding precondition that can rot unnoticed is the
            # same silent-decay shape this test was added to close.
            assert outer._allow_nested is True, (
                "the holder must opt in, or this stops being a test about "
                "nesting at all"
            )
            assert outer.acquire(timeout=5.0)
            try:
                # No allow_nested here -- that is the point.
                inner = DeviceLock(HOST, lock_dir, heartbeat_interval=0.05)
                got = inner.acquire(timeout=CALLER_TIMEOUT)
                if got:
                    inner.release()
                return got, inner.held
            finally:
                outer.release()

        kind, result = _run_bounded(scenario)
        assert kind == "return", f"unexpected raise: {result!r}"
        got, held = result
        assert got is False, (
            "a plain DeviceLock joined a hold whose *holder* set "
            "allow_nested=True -- nesting is the acquirer's property, and "
            "the holder's flag must not stand in for it (see the paragraph "
            "in tests/conftest.py)"
        )
        assert held is False, "refused acquire must not leave the lock held"
