"""A text-monitor reply is matched to its own command, not to a stray prompt.

VICE's remote text monitor transmits a prompt every time the monitor loop
asks for input (``uimon_in``, ``monitor/mon_util.c``), and that loop also
serves the binary monitor.  So every binary-monitor command processed
while the text monitor is connected leaves a ``(C:$xxxx) `` on the text
socket.  ``_text_recv_until_prompt`` used to stop at the first prompt: a
stray one was taken for the reply, ``get_warp()`` returned False, and the
real reply was left for the next command (#516, ~18 in 65k calls under
binary traffic).

The fake answers each line the way VICE 3.10 does (``warp`` ->
``Warp mode is on.``; ``print`` -> tab, decimal; a prompt after every
command) and can inject stray prompts ahead of and inside a reply.
"""

from __future__ import annotations

import collections
import socket
import threading
from unittest.mock import patch

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport

STRAY = b"(C:$e5d4) "


class _TextMonitor:
    """Delivers output in the chunks VICE writes it: one ``recv`` returns at
    most one write, so a stray prompt can arrive on its own."""

    def __init__(self, *, stray_before: int = 0, stray_inside: int = 0,
                 warp: bool = True) -> None:
        self.chunks: collections.deque[bytes] = collections.deque(
            [STRAY] * stray_before)
        self.stray_inside = stray_inside
        self.warp = warp
        self.lines: list[str] = []
        self.pending: list[str] = []

    def stray(self) -> None:
        self.chunks.append(STRAY)

    def sendall(self, data: bytes) -> None:
        # VICE 3.10 handles one line per arrival of data: a second line in
        # the same read waits until more data comes
        # (monitor_network_get_command_line), so it is queued here too.
        self.pending.extend(data.decode("ascii").splitlines())
        if self.pending:
            self._handle(self.pending.pop(0))

    def _handle(self, line: str) -> None:
        self.lines.append(line)
        if line == "warp":
            self.chunks.append(
                f"Warp mode is {'on' if self.warp else 'off'}.\n".encode())
            self.chunks.extend([STRAY] * self.stray_inside)
        elif line.startswith(("p ", "print ")):
            value = int(line.split()[1].lstrip("$"), 16)
            self.chunks.append(f"\t{value}\n".encode())
        self.chunks.append(b"(C:$e5cf) ")

    def recv(self, n: int) -> bytes:
        if not self.chunks:
            raise socket.timeout("nothing more")
        chunk = self.chunks.popleft()
        if len(chunk) > n:
            self.chunks.appendleft(chunk[n:])
            chunk = chunk[:n]
        return chunk

    @property
    def buf(self) -> bytes:
        return b"".join(self.chunks)


def _transport(text: _TextMonitor) -> BinaryViceTransport:
    with patch.object(BinaryViceTransport, "_connect"):
        t = BinaryViceTransport.__new__(BinaryViceTransport)
    t.timeout = 1.0
    t._lock = threading.Lock()
    t._text_lock = threading.Lock()
    t._text_sock = text
    t._event_queue = collections.deque()
    return t


@pytest.mark.parametrize("before,inside", [(0, 0), (1, 0), (3, 0), (0, 2), (2, 2)])
def test_get_warp_reads_its_own_reply(before, inside):
    t = _transport(_TextMonitor(stray_before=before, stray_inside=inside))
    assert t.get_warp() is True


def test_consecutive_commands_do_not_read_each_others_replies():
    text = _TextMonitor(stray_before=2, stray_inside=1)
    t = _transport(text)
    assert t.get_warp() is True
    text.warp = False
    text.stray()  # binary traffic in between
    assert t.get_warp() is False
    text.warp = True
    assert t.get_warp() is True


def test_a_silent_command_returns_without_waiting_for_the_timeout():
    text = _TextMonitor(stray_before=1)
    t = _transport(text)
    t.detach_drive(8)
    assert text.lines[-1] == "detach 8"
    assert not text.buf, "reply left behind for the next command"


def test_endless_stray_prompts_do_not_wait_for_ever():
    # A reply that never comes, with binary traffic still producing prompts:
    # the overall timeout ends it, not only a quiet socket.
    import time as _time

    class Chatty(_TextMonitor):
        def sendall(self, data: bytes) -> None:
            self.lines.append(data.decode("ascii"))

        def recv(self, n: int) -> bytes:
            _time.sleep(0.01)
            return STRAY

    t = _transport(Chatty())
    t.timeout = 0.2
    start = _time.monotonic()
    with pytest.raises(Exception, match="never answered"):
        t.get_warp()
    assert _time.monotonic() - start < 2.0
