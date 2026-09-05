"""The unlocked-client notice on a real lane, and under ``parallel.py``.

Issue #206, following up #194 / PR #202.  The once-per-process dedupe and
the thread-scoped suppression are covered by mocked tests
(``tests/test_device_lock_visibility.py``); what those cannot show is
that the *real* lane has no construction site the mocks do not model.
There is exactly one: ``Ultimate64InstanceManager.acquire()`` builds the
``Ultimate64Transport`` -- and with it the ``Ultimate64Client`` -- before
``_LockedU64Manager.acquire()`` can know which host to lock, and wraps
that in ``suppress_unlocked_warning()``.  A mis-scoped suppression would
put a WARNING on every correctly-locked lane.

What is measured, in one process against the live device:

1. A ``create_manager(backend="u64")`` lane doing a memory write and
   read-back emits the notice **zero** times.  The capture is proven
   live by asserting the lane's own DEBUG line ("Acquired U64 ... with
   cross-process lock") was seen, so an empty record list cannot pass by
   accident.
2. After that lane has released, a bare ``Ultimate64Client(host)`` in the
   same process emits the notice **once**, naming the lockfile, and a
   second construction is silent.
3. ``run_parallel`` with two workers over three lanes emits it **zero**
   times, while a foreign thread is parked inside
   ``suppress_unlocked_warning()`` the whole time (a process-wide
   suppression would be indistinguishable here, so the next step settles
   the scope); then, with no lock held, a client built *inside* that
   parked thread's suppression is silent and one built on a fresh worker
   thread emits **once**, a second on the same thread silent.

Why the "deliberately unlocked client on a worker thread" is built after
the lanes rather than during them: the notice's contract is "this
*process* holds no lock for the host", so a bare client built on any
thread while a lane holds the lock is, by definition, not unlocked and is
correctly silent.  The thread-scoping question is about the suppression,
and step 3 asks it directly.

Gate: ``U64_NOTICE_LIVE=1``.  Host: ``U64_NOTICE_HOST`` (default
``10.43.23.81``).  The host is deliberately **not** stored in ``_HOST``:
``conftest.device_lock_guard`` keys on that attribute (and on
``U64_HOST``) and would hold the device lock around the whole test, which
would make every "unlocked" phase here measure the guard instead of the
client.  Run with ``U64_HOST`` unset; the fixture refuses to proceed
otherwise.

Mutation record (2026-09-05, U64E fw 3.15, all three tests):
``suppress_unlocked_warning`` made a no-op -> tests 1 and 3 fail (notice
on the locked lanes); dedupe removed -> tests 2 and 3 fail (second
construction warns); suppression made process-global instead of
thread-local -> test 3 fails (fresh worker thread silent).
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from c64_test_harness import (
    DeviceLock,
    create_manager,
    device_lock_path,
    run_parallel,
    suppress_unlocked_warning,
)
from c64_test_harness.backends import device_lock as lock_mod
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

NOTICE_HOST = os.environ.get("U64_NOTICE_HOST", "10.43.23.81")

pytestmark = pytest.mark.skipif(
    os.environ.get("U64_NOTICE_LIVE") != "1",
    reason="U64_NOTICE_LIVE=1 not set -- live unlocked-notice test disabled",
)

#: The phrase every unlocked-client notice carries (device_lock.py,
#: ``warn_unlocked_client``).
_NOTICE_PHRASE = "built without holding this device's lock in this process"
#: The DEBUG line ``_LockedU64Manager.acquire`` logs once it holds the lock.
_LANE_LOCKED_PHRASE = "with cross-process lock"

#: Scratch byte well clear of every HARNESS_SCRATCH span.
_SCRATCH = 0xC9F8
_LOCK_TIMEOUT = 600.0


def _notices(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and _NOTICE_PHRASE in r.getMessage()
    ]


def _forget_host_warned() -> None:
    """Drop only this host's dedupe entry -- never the hold registry.

    ``_reset_advisory_state`` also clears ``_PROCESS_HELD``, which would
    corrupt lock accounting for anything this process still holds.
    """
    with lock_mod._PROCESS_HELD_GUARD:
        lock_mod._UNLOCKED_WARNED.discard(NOTICE_HOST)


@pytest.fixture(autouse=True)
def _fresh_notice_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(lock_mod.UNLOCKED_WARNING_ENV, raising=False)
    if DeviceLock.held_by_this_process(NOTICE_HOST):
        pytest.fail(
            f"this process already holds the DeviceLock for {NOTICE_HOST} "
            f"before the test started (conftest's device_lock_guard engages "
            f"when U64_HOST is set).  Run with U64_HOST unset and pass the "
            f"device as U64_NOTICE_HOST; otherwise the unlocked phases "
            f"measure the guard, not the client."
        )
    _forget_host_warned()
    yield
    _forget_host_warned()


def _lane_round_trip(target) -> tuple[bool, str]:
    """One locked lane's work: a write and its read-back."""
    marker = bytes([threading.get_ident() & 0xFF])
    target.transport.write_memory(_SCRATCH, marker)
    got = target.transport.read_memory(_SCRATCH, 1)
    return got == marker, f"wrote {marker.hex()} read {got.hex()}"


def test_locked_lane_emits_no_notice(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="c64_test_harness"):
        with create_manager(
            backend="u64", u64_hosts=NOTICE_HOST, lock_timeout=_LOCK_TIMEOUT
        ) as mgr:
            with mgr.instance() as target:
                assert DeviceLock.held_by_this_process(NOTICE_HOST)
                ok, detail = _lane_round_trip(target)
                assert ok, detail
                assert target.client.get_info()["product"]
    # The capture is live: the lane's own lock line was seen.
    assert any(
        _LANE_LOCKED_PHRASE in r.getMessage() for r in caplog.records
    ), "caplog saw no lane activity at all; the assertion below is vacuous"
    assert _notices(caplog) == [], (
        "the correctly-locked lane was warned -- suppression is mis-scoped"
    )
    assert not DeviceLock.held_by_this_process(NOTICE_HOST)


def test_bare_client_warns_once_per_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same shape as the previous test's lane, in the same process, so the
    # bare client that follows is measured after a locked lane released.
    with caplog.at_level(logging.WARNING, logger="c64_test_harness"):
        with create_manager(
            backend="u64", u64_hosts=NOTICE_HOST, lock_timeout=_LOCK_TIMEOUT
        ) as mgr:
            with mgr.instance() as target:
                ok, detail = _lane_round_trip(target)
                assert ok, detail
        assert _notices(caplog) == []
        assert not DeviceLock.held_by_this_process(NOTICE_HOST)

        first = Ultimate64Client(NOTICE_HOST)
        after_first = _notices(caplog)
        second = Ultimate64Client(NOTICE_HOST)
        after_second = _notices(caplog)
    try:
        assert len(after_first) == 1, after_first
        assert str(device_lock_path(NOTICE_HOST)) in after_first[0]
        assert NOTICE_HOST in after_first[0]
        assert after_second == after_first, (
            "second construction in the same process warned again; the "
            "once-per-process dedupe is broken"
        )
    finally:
        first.close()
        second.close()


def test_parallel_lanes_silent_and_suppression_is_thread_scoped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    park = threading.Event()
    parked = threading.Event()
    parked_client: list[Ultimate64Client] = []
    build_inside = threading.Event()
    built_inside = threading.Event()

    def _parked_suppressor() -> None:
        # Holds a suppression open on THIS thread for the whole test.  If
        # the scope leaked process-wide, the fresh worker below would be
        # silent; if it were absent, the lanes would warn.
        with suppress_unlocked_warning():
            parked.set()
            build_inside.wait(timeout=60.0)
            parked_client.append(Ultimate64Client(NOTICE_HOST))
            built_inside.set()
            park.wait(timeout=600.0)

    suppressor = threading.Thread(
        target=_parked_suppressor, name="parked-suppressor", daemon=True
    )
    fresh: list[Ultimate64Client] = []
    try:
        with caplog.at_level(logging.DEBUG, logger="c64_test_harness"):
            suppressor.start()
            assert parked.wait(timeout=10.0)

            lanes = [
                (f"lane-{i}", _lane_round_trip) for i in range(3)
            ]
            with create_manager(
                backend="u64", u64_hosts=NOTICE_HOST, lock_timeout=_LOCK_TIMEOUT
            ) as mgr:
                result = run_parallel(mgr, lanes, max_workers=2)
            assert result.all_passed, [
                (r.name, r.message) for r in result.results if not r.passed
            ]
            assert len(result.results) == 3
            lane_lines = [
                r.getMessage()
                for r in caplog.records
                if _LANE_LOCKED_PHRASE in r.getMessage()
            ]
            assert len(lane_lines) == 3, lane_lines
            assert _notices(caplog) == [], (
                "a correctly-locked parallel lane was warned"
            )
            assert not DeviceLock.held_by_this_process(NOTICE_HOST)

            # No lock held now.  A client built inside the parked thread's
            # suppression must be silent and must NOT consume the
            # once-per-process slot ...
            build_inside.set()
            assert built_inside.wait(timeout=10.0)
            assert _notices(caplog) == [], (
                "a client built inside suppress_unlocked_warning() warned"
            )

            # ... so a client built on a fresh worker thread is the first
            # unlocked one this process has seen, and says so exactly once.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="fresh") as pool:
                fresh.append(pool.submit(Ultimate64Client, NOTICE_HOST).result())
                after_first = _notices(caplog)
                fresh.append(pool.submit(Ultimate64Client, NOTICE_HOST).result())
                after_second = _notices(caplog)
        assert len(after_first) == 1, (
            after_first or "fresh worker thread was silent: the suppression "
            "leaked out of the thread that entered it"
        )
        assert after_second == after_first, "dedupe broken across threads"
    finally:
        park.set()
        suppressor.join(timeout=5.0)
        for c in fresh + parked_client:
            c.close()
