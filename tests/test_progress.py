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
from c64_test_harness.backends.vice_binary import BinaryViceTransport


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


def _make_transport() -> MagicMock:
    """Mock that quacks like the slice of :class:`C64Transport` we need.

    ``watch_progress`` only ever touches ``transport.read_memory``; the
    rest of the protocol is irrelevant. We use a bare :class:`MagicMock`
    so individual tests script the ``read_memory`` side effects
    declaratively.
    """
    return MagicMock(spec=BinaryViceTransport)


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
