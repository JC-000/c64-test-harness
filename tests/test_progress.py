"""Unit tests for the backend-agnostic ``watch_progress`` (issue #108).

These tests exercise :func:`c64_test_harness.progress.watch_progress`
at its canonical entry point — a :class:`C64Transport` ``read_memory``
caller — rather than the legacy ``Ultimate64Client.read_mem`` shim in
:mod:`c64_test_harness.backends.ultimate64_helpers`. The full
``Ultimate64Client``-driven battery still lives in
``test_ultimate64_helpers.py`` and exercises the shim.
"""
from __future__ import annotations

from typing import Callable
from unittest.mock import MagicMock

import pytest

from c64_test_harness import ProgressEvent, watch_progress
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.vice_binary import BinaryViceTransport

# Both concrete C64Transport implementations are exercised through the
# canonical entry point so the cross-backend protocol claim is asserted
# (PR #123 lifted ``watch_progress`` off of ``Ultimate64Client`` onto
# ``C64Transport.read_memory``).
_TRANSPORT_SPECS = [BinaryViceTransport, Ultimate64Transport]


# --------------------------------------------------------------------------- #
# Fakes / helpers                                                             #
# --------------------------------------------------------------------------- #


class _FakeClock:
    """Deterministic monotonic clock with explicit tick list.

    Once the explicit list is exhausted the clock keeps incrementing by
    ``step`` so generators bounded by ``overall_timeout`` always
    eventually terminate.
    """

    def __init__(self, ticks: list[float], step: float = 1.0) -> None:
        if not ticks:
            raise ValueError("ticks must be non-empty")
        self._ticks = list(ticks)
        self._idx = 0
        self._step = step
        self._tail = ticks[-1]

    def __call__(self) -> float:
        if self._idx < len(self._ticks):
            value = self._ticks[self._idx]
            self._idx += 1
            self._tail = value
        else:
            self._tail += self._step
            value = self._tail
        return value


def _record_sleep() -> tuple[list[float], "Callable[[float], None]"]:
    calls: list[float] = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, _sleep


def _make_transport(spec=BinaryViceTransport) -> MagicMock:
    """Mock that quacks like the slice of :class:`C64Transport` we need.

    ``watch_progress`` only ever touches ``transport.read_memory``; the
    rest of the protocol is irrelevant. We use a bare :class:`MagicMock`
    so individual tests script the ``read_memory`` side effects
    declaratively. ``spec`` is parametrised over both concrete transport
    types in the suites below so a regression that ties ``watch_progress``
    back to one specific transport surfaces here.
    """
    return MagicMock(spec=spec)


# --------------------------------------------------------------------------- #
# Canonical-entry-point coverage: Advanced / Stalled / Finished               #
# --------------------------------------------------------------------------- #


class TestWatchProgressCanonical:
    """Smoke-test the three primary event kinds against a mocked transport."""

    def test_advanced_on_change(self) -> None:
        """Memory changing between polls yields an Advanced event with the diff."""
        transport = _make_transport()
        transport.read_memory.side_effect = [b"\x00", b"\x01"]
        clock = _FakeClock([0.0, 0.1, 0.2, 1.0, 1.1, 1.2])
        _, sleep = _record_sleep()

        gen = watch_progress(
            transport,
            addresses={"sentinel": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=120.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)   # baseline Advanced (b"" -> 0x00)
        second = next(gen)  # actual change Advanced (0x00 -> 0x01)
        gen.close()

        assert first.kind == "Advanced"
        assert first.changed == {"sentinel": (b"", b"\x00")}
        assert second.kind == "Advanced"
        assert second.changed == {"sentinel": (b"\x00", b"\x01")}
        assert second.values == {"sentinel": b"\x01"}
        # And the mock confirms we went through the protocol's read_memory,
        # NOT some backend-specific call.
        assert transport.read_memory.call_count == 2
        transport.read_memory.assert_any_call(0x0400, 1)

    def test_stalled_after_idle_timeout(self) -> None:
        """No-change for idle_timeout seconds yields a Stalled event."""
        transport = _make_transport()
        # Same byte every poll => no diff => stall.
        transport.read_memory.return_value = b"\x42"
        # Make the second poll's elapsed >= idle_timeout=5.0.
        clock = _FakeClock([0.0, 0.1, 0.2, 6.0, 6.1, 6.2, 6.3])
        _, sleep = _record_sleep()

        gen = watch_progress(
            transport,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=5.0,
            overall_timeout=60.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)   # baseline Advanced
        second = next(gen)  # second poll: no change, idle threshold tripped
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "Stalled"
        assert second.changed == {}
        assert second.values == {"x": b"\x42"}

    def test_finished_via_stop_when(self) -> None:
        """stop_when returning truthy yields Finished and ends the generator."""
        transport = _make_transport()
        transport.read_memory.side_effect = [b"\x00", b"\xFF"]
        clock = _FakeClock([0.0, 0.1, 0.2, 1.0, 1.1, 1.2])
        _, sleep = _record_sleep()

        def stop_at_ff(values: dict) -> bool:
            return values.get("x") == b"\xFF"

        gen = watch_progress(
            transport,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=600.0,
            overall_timeout=600.0,
            stop_when=stop_at_ff,
            _clock=clock,
            _sleep=sleep,
        )
        events = list(gen)
        # baseline Advanced (sentinel=0x00), then change Advanced to 0xFF,
        # then Finished.
        assert [e.kind for e in events] == ["Advanced", "Advanced", "Finished"]
        assert events[-1].values == {"x": b"\xFF"}


# --------------------------------------------------------------------------- #
# Cross-backend smoke: watch_progress only touches the protocol's read_memory #
# --------------------------------------------------------------------------- #


class TestWatchProgressProtocolAgnostic:
    """Same Advanced-event smoke against both concrete transports.

    Guards against ``watch_progress`` regressing to a backend-specific call
    (e.g. ``client.read_mem`` on U64 or any other shape). PR #123 lifted
    the implementation onto ``C64Transport.read_memory``; the test proves
    that with no transport-typed fork in the canonical path.
    """

    @pytest.mark.parametrize("spec", _TRANSPORT_SPECS)
    def test_advanced_event_drives_protocol_read_memory(self, spec) -> None:
        transport = _make_transport(spec=spec)
        transport.read_memory.side_effect = [b"\x00", b"\x01"]
        clock = _FakeClock([0.0, 0.1, 0.2, 1.0, 1.1, 1.2])
        _, sleep = _record_sleep()

        gen = watch_progress(
            transport,
            addresses={"sentinel": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=120.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)
        second = next(gen)
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "Advanced"
        assert second.values == {"sentinel": b"\x01"}
        # Critical: it drives the protocol, not any backend-specific path.
        assert transport.read_memory.call_count == 2
        transport.read_memory.assert_any_call(0x0400, 1)


# --------------------------------------------------------------------------- #
# ProgressEvent dataclass shape                                               #
# --------------------------------------------------------------------------- #


class TestProgressEvent:
    """``ProgressEvent`` is exposed at the package root with sensible defaults."""

    def test_progress_event_defaults(self) -> None:
        e = ProgressEvent(kind="Stalled", elapsed=42.0)
        assert e.kind == "Stalled"
        assert e.changed == {}
        assert e.values == {}
        assert e.error is None


# --------------------------------------------------------------------------- #
# Shim parity: legacy ultimate64_helpers entry point still works              #
# --------------------------------------------------------------------------- #


class TestShimParity:
    """The shim in ``ultimate64_helpers`` is identity-bound to canonical names."""

    def test_progress_event_is_same_class(self) -> None:
        from c64_test_harness.backends.ultimate64_helpers import (
            ProgressEvent as ShimEvent,
        )
        assert ShimEvent is ProgressEvent

    def test_shim_watch_progress_drives_client_read_mem(self) -> None:
        """The legacy ``watch_progress(client, …)`` calls ``client.read_mem``."""
        from c64_test_harness.backends.ultimate64_helpers import (
            watch_progress as shim_watch_progress,
        )
        client = MagicMock()
        client.read_mem.return_value = b"\x00"
        clock = _FakeClock([0.0, 0.1, 0.2])
        _, sleep = _record_sleep()

        gen = shim_watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=120.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        event = next(gen)
        gen.close()

        assert event.kind == "Advanced"
        # Critical: the shim must drive the legacy ``read_mem`` method,
        # not the protocol's ``read_memory`` — that's the whole point
        # of the backwards-compat adapter.
        client.read_mem.assert_called_once_with(0x0400, 1)


# --------------------------------------------------------------------------- #
# Smoke: addresses validation still happens at the canonical entry point     #
# --------------------------------------------------------------------------- #


class TestCanonicalValidation:
    """Argument validation is unchanged from the original implementation."""

    def test_empty_addresses_rejected(self) -> None:
        transport = _make_transport()
        with pytest.raises(ValueError, match="non-empty"):
            list(watch_progress(transport, addresses={}))

    def test_non_positive_poll_interval_rejected(self) -> None:
        transport = _make_transport()
        with pytest.raises(ValueError, match="poll_interval"):
            list(watch_progress(
                transport, addresses={"x": (0x0400, 1)}, poll_interval=0,
            ))


# --------------------------------------------------------------------------- #
# Issue #514: the watched program must keep running between polls            #
# --------------------------------------------------------------------------- #


class _HaltingCounter:
    """VICE-shaped: every read halts the guest; it counts only while running.

    Each ``resume`` advances the counter by one, so a watcher that never
    resumes sees one value for ever -- the ``Stalled`` report #514 found.
    """

    def __init__(self, *, fail_read: int | None = None) -> None:
        self.counter = 0
        self.halted = False
        self.reads = 0
        self.resumes = 0
        self.fail_read = fail_read

    def read_memory(self, addr: int, length: int) -> bytes:
        self.reads += 1
        self.halted = True
        if self.reads == self.fail_read:
            raise OSError("wire dropped")
        return bytes([self.counter & 0xFF]) * length

    def resume(self) -> None:
        self.halted = False
        self.resumes += 1
        self.counter += 1


class _DmaCounter(_HaltingCounter):
    """U64-shaped: reads do not halt; a resume would clear a real pause."""

    halts_cpu_on_access = False

    def read_memory(self, addr: int, length: int) -> bytes:
        self.counter += 1  # the machine runs on its own
        data = super().read_memory(addr, length)
        self.halted = False
        return data


def _watch(transport, *, ticks: int = 6, **kw):
    clock = _FakeClock([0.0], step=1.0)
    _, sleep = _record_sleep()
    return watch_progress(
        transport,
        addresses={"a": (0x0400, 1), "b": (0x0401, 1)},
        poll_interval=1.0,
        idle_timeout=2.5,
        overall_timeout=float(ticks),
        _clock=clock,
        _sleep=sleep,
        **kw,
    )


class TestResumeBetweenPolls:
    def test_halting_backend_advances_and_is_running_at_every_event(self) -> None:
        t = _HaltingCounter()
        kinds = []
        for event in _watch(t):
            assert not t.halted, f"yielded {event.kind} with the CPU halted"
            kinds.append(event.kind)
        assert "Stalled" not in kinds, kinds
        assert kinds.count("Advanced") >= 2, kinds
        # One resume per poll (two reads each), none for the Timeout.
        assert t.resumes == t.reads // 2

    def test_halting_backend_resumed_after_a_failed_read(self) -> None:
        t = _HaltingCounter(fail_read=1)
        gen = _watch(t)
        assert next(gen).kind == "PollError"
        assert not t.halted
        gen.close()

    def test_halting_backend_resumed_before_finished(self) -> None:
        t = _HaltingCounter()
        events = list(_watch(t, stop_when=lambda v: True))
        assert [e.kind for e in events] == ["Advanced", "Finished"]
        assert not t.halted and t.resumes == 1

    def test_non_halting_backend_is_never_resumed(self) -> None:
        t = _DmaCounter()
        kinds = [e.kind for e in _watch(t)]
        assert kinds.count("Advanced") >= 2, kinds
        assert t.reads > 0 and t.resumes == 0

    def test_legacy_client_shim_never_resumes(self) -> None:
        from c64_test_harness.backends.ultimate64_helpers import (
            watch_progress as shim_watch_progress,
        )

        client = MagicMock()
        client.read_mem.side_effect = lambda a, n: b"\x00" * n
        clock = _FakeClock([0.0], step=1.0)
        _, sleep = _record_sleep()
        list(shim_watch_progress(
            client, {"a": (0x0400, 1)}, poll_interval=1.0, idle_timeout=10.0,
            overall_timeout=3.0, _clock=clock, _sleep=sleep,
        ))
        assert client.read_mem.called
        client.resume.assert_not_called()
