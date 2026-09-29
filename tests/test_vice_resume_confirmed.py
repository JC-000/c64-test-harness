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
        for rtype in events:
            self.buf += _frame(rtype)

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
    sock = _ScriptedSocket([[EVENT_RESUMED, RESPONSE_CHECKPOINT_INFO,
                             REGISTERS_EVENT, EVENT_STOPPED]])
    t = _transport(sock)
    assert t._resume_confirmed(window=0.01) is False
    assert sock.exits == 1
    assert any(e.response_type == RESPONSE_CHECKPOINT_INFO for _, e in t._event_queue)


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
