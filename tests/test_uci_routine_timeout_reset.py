"""A timed-out UCI routine must not be left running under the next upload (#313).

``_execute_uci_routine`` dispatches its routine with a typed ``SYS`` and
polls a sentinel.  The routine's wait fragments are unbounded busy-waits
(``_build_wait_idle``, ``_build_push_and_wait`` and the turbo
``JMP busy_loop`` form), so when the sentinel never arrives the 6510 can
still be spinning inside the routine.  The next call writes its own
routine to the same address -- since #294 as several chunked PUTs -- with
the old code still executing in between.

The fix: on the timeout path, reset the 6510 (``transport.reset(scope=
"cpu")``: U64 ``PUT machine:reset``, bodyless, no ``/Temp`` cost) and give
the KERNAL time to come back to ``READY.`` before raising.  ``scope=
"machine"`` would be wrong on a U64: that is ``machine:reboot``, which
returns the device with the Command-Interface slot disabled.

All of this is offline, against a mock transport.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.transport import TimeoutError as HarnessTimeout
from c64_test_harness.transport import TransportError
from c64_test_harness.uci_network import (
    UCIError,
    _CODE_ADDR,
    _ERROR_ADDR,
    _SENTINEL_ADDR,
    _SENTINEL_DONE,
    _execute_uci_routine,
)

CODE = bytes([0xEA] * 20 + [0x60])


class _Recorder:
    """A transport whose every call lands, in order, in one event list."""

    def __init__(self, *, completes: bool, error: int = 0,
                 reset_raises: Exception | None = None) -> None:
        self.events: list[tuple] = []
        self.completes = completes
        self.error = error
        self.reset_raises = reset_raises
        self.mock = MagicMock()
        self.mock.read_memory.side_effect = self._read
        self.mock.write_memory.side_effect = self._write
        self.mock.reset.side_effect = self._reset

    def _read(self, addr: int, length: int) -> bytes:
        self.events.append(("read", addr, length))
        if addr == _SENTINEL_ADDR:
            return bytes([_SENTINEL_DONE if self.completes else 0])
        if addr == _ERROR_ADDR:
            return bytes([self.error])
        return bytes(length)

    def _write(self, addr: int, data: bytes) -> None:
        self.events.append(("write", addr, bytes(data)))

    def _reset(self, *args, **kwargs) -> None:
        self.events.append(("reset", args, kwargs))
        if self.reset_raises is not None:
            raise self.reset_raises

    def names(self) -> list[str]:
        return [e[0] for e in self.events]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record host sleeps instead of taking them; also time-stamps them
    into the transport event stream of whichever recorder is active."""
    recorded: list[float] = []
    monkeypatch.setattr(u.time, "sleep", recorded.append)
    return recorded


def _timeout(t: _Recorder) -> HarnessTimeout:
    with pytest.raises(HarnessTimeout) as info:
        _execute_uci_routine(t.mock, CODE, timeout=0.0)
    return info.value


class TestTheTimeoutPath:
    def test_resets_the_machine(self, sleeps: list[float]) -> None:
        t = _Recorder(completes=False)
        _timeout(t)
        assert t.names().count("reset") == 1

    def test_reset_is_a_cpu_reset_not_a_reboot(self, sleeps: list[float]) -> None:
        """``scope="machine"`` on a U64 is ``machine:reboot``, which leaves
        the Command-Interface slot disabled: every later routine would time
        out too."""
        t = _Recorder(completes=False)
        _timeout(t)
        assert t.mock.reset.call_args_list == [call(scope="cpu")]

    def test_reset_happens_before_the_exception_reaches_the_caller(
        self, sleeps: list[float]
    ) -> None:
        t = _Recorder(completes=False)
        try:
            _execute_uci_routine(t.mock, CODE, timeout=0.0)
        except HarnessTimeout:
            t.events.append(("caller-saw-timeout",))
        assert t.names()[-2:] == ["reset", "caller-saw-timeout"]

    def test_nothing_touches_the_machine_after_the_reset(
        self, sleeps: list[float]
    ) -> None:
        t = _Recorder(completes=False)
        _timeout(t)
        assert t.names()[-1] == "reset"

    def test_waits_for_the_kernal_after_the_reset(self, sleeps: list[float]) -> None:
        """The KERNAL reset clears ``$0200-$03FF``, so a ``SYS`` typed into
        the keyboard buffer before ``READY.`` is lost and the next routine
        would time out as well."""
        t = _Recorder(completes=False)
        before = len(sleeps)
        _timeout(t)
        assert sleeps[before:][-1] == u._TIMEOUT_RESET_SETTLE
        assert u._TIMEOUT_RESET_SETTLE >= 3.0

    def test_message_says_the_machine_was_reset(self, sleeps: list[float]) -> None:
        exc = _timeout(_Recorder(completes=False))
        assert "reset" in str(exc)
        assert f"${_SENTINEL_ADDR:04X}" in str(exc)


class TestAFailedReset:
    """A reset that fails must not replace the timeout the caller asked about."""

    def test_still_raises_the_timeout(self, sleeps: list[float]) -> None:
        boom = TransportError("REST down")
        t = _Recorder(completes=False, reset_raises=boom)
        exc = _timeout(t)
        assert exc.__cause__ is boom

    def test_message_says_the_routine_may_still_be_running(
        self, sleeps: list[float]
    ) -> None:
        t = _Recorder(completes=False, reset_raises=TransportError("REST down"))
        exc = _timeout(t)
        assert "may still be running" in str(exc)

    def test_does_not_pretend_to_settle(self, sleeps: list[float]) -> None:
        t = _Recorder(completes=False, reset_raises=TransportError("REST down"))
        _timeout(t)
        assert u._TIMEOUT_RESET_SETTLE not in sleeps


class TestTheNextCall:
    def test_its_upload_comes_after_the_reset(self, sleeps: list[float]) -> None:
        """The #313 hazard: the next routine's code write must never land
        while the timed-out routine is still the one executing."""
        t = _Recorder(completes=False)
        with pytest.raises(HarnessTimeout):
            _execute_uci_routine(t.mock, CODE, timeout=0.0)
        t.completes = True
        _execute_uci_routine(t.mock, CODE, timeout=5.0)

        code_writes = [i for i, e in enumerate(t.events)
                       if e[0] == "write" and e[1] == _CODE_ADDR]
        resets = [i for i, e in enumerate(t.events) if e[0] == "reset"]
        assert len(code_writes) == 2 and len(resets) == 1
        assert code_writes[0] < resets[0] < code_writes[1]


class TestPathsThatCompleted:
    """The routine returned: nothing is left running, so no reset."""

    def test_success_does_not_reset(self, sleeps: list[float]) -> None:
        t = _Recorder(completes=True)
        _execute_uci_routine(t.mock, CODE, timeout=5.0)
        assert "reset" not in t.names()

    def test_uci_error_does_not_reset(self, sleeps: list[float]) -> None:
        t = _Recorder(completes=True, error=0xFF)
        with pytest.raises(UCIError):
            _execute_uci_routine(t.mock, CODE, timeout=5.0)
        assert "reset" not in t.names()
