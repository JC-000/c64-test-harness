"""``BinaryViceTransport._resume_confirmed`` tells a stale trap from a real stop.

#516 re-verify.  VICE queues a monitor trap at every vsync that finds a
command waiting, and runs all queued traps back to back, so a command sent
while the CPU is stalled across a vsync (an REU DMA) leaves a spare trap
that re-enters the monitor the moment the next EXIT leaves it.  Measured on
VICE 3.10 (``tests/test_resume_after_monitor_vice_live.py``): a clean EXIT
is followed by a Resumed event and nothing else; a swallowed one by
Resumed, a Registers event and a Stopped event, immediately.  A checkpoint
announces itself with Checkpoint-info first and a jam with
``RESPONSE_JAM``: those are real stops and must not be resumed over.

The fake socket answers each EXIT with a scripted list of frames and then
times out, as a socket with nothing more to deliver does.
"""

from __future__ import annotations

import collections
import socket
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_binary import (
    API_VERSION,
    CMD_EXIT,
    CMD_TO_RESPONSE_TYPE,
    EVENT_REQUEST_ID,
    EVENT_RESUMED,
    EVENT_STOPPED,
    RESPONSE_CHECKPOINT_INFO,
    RESPONSE_JAM,
    STX,
    BinaryViceTransport,
)

REGISTERS_EVENT = 0x31


def _frame(rtype: int, req_id: int = EVENT_REQUEST_ID, body: bytes = b"") -> bytes:
    return struct.pack("<BBIBBI", STX, API_VERSION, len(body), rtype, 0, req_id) + body


class _ScriptedSocket:
    """Answers the n-th EXIT with ``scripts[n]`` (a list of event types)."""

    def __init__(self, scripts: list[list[int]]) -> None:
        self.scripts = list(scripts)
        self.buf = bytearray()
        self.exits = 0

    def sendall(self, data: bytes) -> None:
        req_id = struct.unpack_from("<I", data, 6)[0]
        assert data[10] == CMD_EXIT
        events = self.scripts[self.exits] if self.exits < len(self.scripts) else [EVENT_RESUMED]
        self.exits += 1
        self.buf += _frame(CMD_TO_RESPONSE_TYPE[CMD_EXIT], req_id)
        for ev in events:
            rtype, body = ev if isinstance(ev, tuple) else (ev, b"")
            self.buf += _frame(rtype, body=body)

    def recv(self, n: int) -> bytes:
        if not self.buf:
            raise socket.timeout("nothing more")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def settimeout(self, value) -> None:
        pass


def _transport(sock: _ScriptedSocket) -> BinaryViceTransport:
    with patch.object(BinaryViceTransport, "_connect"):
        t = BinaryViceTransport.__new__(BinaryViceTransport)
    t.timeout = 5.0
    t._req_id = 0
    t._recv_buf = bytearray()
    t._pending_header = None
    t._resume_generation = 0
    t._event_queue = collections.deque()
    t._lock = threading.Lock()
    t._sock = sock
    return t


STALE = [EVENT_RESUMED, REGISTERS_EVENT, EVENT_STOPPED]


def _checkpoint_info(stop_when_hit: bool) -> tuple[int, bytes]:
    # number, currently-hit, start, end, stop_when_hit, enabled, op,
    # temporary, hit count, ignore count, has condition, memspace
    body = struct.pack("<IBHHBBBBIIBB", 1, 1, 0xC000, 0xC000,
                       int(stop_when_hit), 1, 4, 0, 1, 0, 0, 0)
    return (RESPONSE_CHECKPOINT_INFO, body)


def test_a_clean_exit_is_confirmed_with_one_exit():
    sock = _ScriptedSocket([[EVENT_RESUMED]])
    assert _transport(sock)._resume_confirmed(window=0.01) is True
    assert sock.exits == 1


def test_a_swallowed_exit_is_resent_until_one_runs():
    sock = _ScriptedSocket([STALE, STALE, [EVENT_RESUMED]])
    t = _transport(sock)
    assert t._resume_confirmed(window=0.01) is True
    assert sock.exits == 3
    # The stale re-entry's Stopped is not left for wait_for_stopped.
    assert not any(e.response_type == EVENT_STOPPED for _, e in t._event_queue)


def test_a_checkpoint_stop_is_not_resumed_over():
    sock = _ScriptedSocket([[EVENT_RESUMED, _checkpoint_info(True),
                             REGISTERS_EVENT, EVENT_STOPPED]])
    t = _transport(sock)
    assert t._resume_confirmed(window=0.01) is False
    assert sock.exits == 1
    assert any(e.response_type == RESPONSE_CHECKPOINT_INFO for _, e in t._event_queue)


def test_a_tracepoint_hit_is_not_a_stop():
    # A checkpoint with stop_when_hit=False reports the hit and keeps
    # running: no Stopped follows, so the CPU is running.
    sock = _ScriptedSocket([[EVENT_RESUMED, _checkpoint_info(False)]])
    t = _transport(sock)
    assert t._resume_confirmed(window=0.01) is True
    assert sock.exits == 1
    assert any(e.response_type == RESPONSE_CHECKPOINT_INFO for _, e in t._event_queue)


def test_a_tracepoint_hit_does_not_hide_a_stale_trap():
    sock = _ScriptedSocket([[EVENT_RESUMED, _checkpoint_info(False),
                             REGISTERS_EVENT, EVENT_STOPPED], [EVENT_RESUMED]])
    assert _transport(sock)._resume_confirmed(window=0.01) is True
    assert sock.exits == 2


def test_the_default_budget_covers_a_guest_64k_swap():
    # reu_dma_swap costs 2 cycles per byte (reu.c): a 64 KB swap stalls the
    # CPU 131072 cycles, ~6.7 PAL frames of 19656 cycles, so up to 6 spare
    # traps can queue behind one command.
    sock = _ScriptedSocket([STALE] * 6 + [[EVENT_RESUMED]])
    assert _transport(sock)._resume_confirmed(window=0.01) is True
    assert sock.exits == 7


def test_a_text_monitor_command_waits_for_the_binary_lock():
    # A text-monitor command enters VICE's monitor, which the binary socket
    # reports as a bare Stopped; inside a confirmation window that reads as
    # a stale trap.  So text commands are serialised with the binary lock.
    import threading
    import time as _time

    t = _transport(_ScriptedSocket([]))
    t.timeout = 0.3
    sent = threading.Event()
    text_sock = MagicMock()
    text_sock.sendall.side_effect = lambda data: sent.set()
    # No reply at all: the worker's read times out and it ends, so the
    # thread never outlives the test.
    text_sock.recv.side_effect = socket.timeout("no reply")
    t._text_sock = text_sock
    t._text_lock = threading.Lock()

    def run() -> None:
        try:
            t._text_command("warp")
        except Exception:
            pass

    t._lock.acquire()
    try:
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        _time.sleep(0.1)
        assert not sent.is_set(), "text command ran while the binary lock was held"
    finally:
        t._lock.release()
    worker.join(timeout=5.0)
    assert sent.is_set()


def test_exit_and_window_happen_under_one_lock_hold():
    # Otherwise a text-monitor command (which takes the same lock) can slip
    # in between the EXIT and the listening window.
    log: list[str] = []

    class TrackingLock:
        def __init__(self) -> None:
            self._l = threading.Lock()

        def __enter__(self):
            self._l.acquire()
            log.append("acquire")
            return self

        def __exit__(self, *exc) -> None:
            log.append("release")
            self._l.release()

        def locked(self) -> bool:
            return self._l.locked()

    sock = _ScriptedSocket([STALE, [EVENT_RESUMED]])
    real_sendall = sock.sendall

    def sendall(data: bytes) -> None:
        log.append("exit")
        real_sendall(data)

    sock.sendall = sendall
    t = _transport(sock)
    t._lock = TrackingLock()
    real = t._after_exit

    def spy(window):
        log.append("window")
        return real(window)

    t._after_exit = spy
    assert t._resume_confirmed(window=0.01) is True
    joined = " ".join(log)
    assert joined.count("acquire exit window release") == 2, joined


def test_a_checkpoint_frame_too_short_to_read_counts_as_a_stop():
    # The stop flag is byte 9; a frame that does not reach it cannot say it
    # is a tracepoint, and resuming over a real stop is the worse mistake.
    sock = _ScriptedSocket([[EVENT_RESUMED, (RESPONSE_CHECKPOINT_INFO, b"\x01" * 9)]])
    t = _transport(sock)
    assert t._resume_confirmed(window=0.01) is False
    assert sock.exits == 1


def test_events_queued_before_the_exit_are_aged_out():
    # Like resume(), the confirmed resume bumps the generation, so a Stopped
    # left over from before it is not taken by wait_for_stopped as the
    # machine stopping now.
    from c64_test_harness.backends.vice_binary import _Response
    from c64_test_harness.transport import TimeoutError as HarnessTimeout

    sock = _ScriptedSocket([[EVENT_RESUMED]])
    t = _transport(sock)
    t._event_queue.append(
        (t._resume_generation, _Response(EVENT_STOPPED, 0, EVENT_REQUEST_ID, b""))
    )
    assert t._resume_confirmed(window=0.01) is True
    with pytest.raises(HarnessTimeout):
        t.wait_for_stopped(timeout=0.05)


def test_a_jam_is_not_resumed_over_and_stays_queued():
    sock = _ScriptedSocket([[EVENT_RESUMED, RESPONSE_JAM]])
    t = _transport(sock)
    assert t._resume_confirmed(window=0.01) is False
    assert sock.exits == 1
    assert [e.response_type for _, e in t._event_queue] == [RESPONSE_JAM]


def test_gives_up_after_the_attempt_budget():
    sock = _ScriptedSocket([STALE] * 10)
    assert _transport(sock)._resume_confirmed(window=0.01, attempts=3) is False
    assert sock.exits == 3


def test_resume_quietly_confirms_only_when_asked():
    from c64_test_harness.screen import _resume_quietly

    t = MagicMock(spec=["resume", "_resume_confirmed"])
    t._resume_confirmed.return_value = True
    assert _resume_quietly(t) is True
    t.resume.assert_called_once_with()
    t._resume_confirmed.assert_not_called()
    assert _resume_quietly(t, confirm=True) is True
    t._resume_confirmed.assert_called_once_with()
    t._resume_confirmed.return_value = False
    assert _resume_quietly(t, confirm=True) is False
