"""Unit tests for BinaryViceTransport defense-in-depth fixes (issue #88).

These tests mock the socket layer to avoid needing a live VICE instance.
They cover three correctness gaps the audit identified in the binary
monitor read path:

1. ``_wait_for_response`` validates ``response_type`` so a colliding
   request_id from an unrelated response can't be silently parsed as
   the expected reply (the failure shape behind issue #88).
2. ``read_memory`` asserts ``len(data) == chunk_size`` per chunk so a
   short MEM_GET response surfaces loudly instead of compounding into
   a structured corruption.
3. ``wait_for_stopped`` re-queues unrelated events (RESUMED,
   CHECKPOINT_INFO) instead of dropping them and raises on a
   non-event response with a non-STOPPED type (a wire desync).
"""

from __future__ import annotations

import collections
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_binary import (
    API_VERSION,
    CMD_MEM_GET,
    CMD_REGISTERS_GET,
    CMD_TO_RESPONSE_TYPE,
    EVENT_REQUEST_ID,
    EVENT_RESUMED,
    EVENT_STOPPED,
    RESPONSE_CHECKPOINT_INFO,
    STX,
    BinaryViceTransport,
    _Response,
)
from c64_test_harness.transport import TransportError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport() -> BinaryViceTransport:
    """Create a BinaryViceTransport with the connection step skipped."""
    with patch.object(BinaryViceTransport, "_connect"):
        t = BinaryViceTransport.__new__(BinaryViceTransport)
        t.host = "127.0.0.1"
        t.port = 6502
        t.timeout = 5.0
        t.screen_base = 0x0400
        t.keybuf_addr = 0x0277
        t.keybuf_count_addr = 0x00C6
        t.keybuf_max = 10
        t._cols = 40
        t._rows = 25
        t._text_monitor_port = 0
        t._req_id = 0
        t._reg_map = {}
        t._event_queue = collections.deque()
        t._lock = threading.Lock()
        t._text_lock = threading.Lock()
        t._sock = MagicMock()
        t._text_sock = None
        return t


def _build_response_bytes(
    response_type: int,
    body: bytes,
    request_id: int = 0,
    error_code: int = 0x00,
) -> bytes:
    """Build raw wire bytes for a response (header + body)."""
    header = struct.pack(
        "<BBIBBI",
        STX,
        API_VERSION,
        len(body),
        response_type,
        error_code,
        request_id,
    )
    return header + body


def _queue_recvs(sock: MagicMock, payloads: list[bytes]) -> None:
    """Make ``sock.recv(n)`` deliver the concatenation of *payloads* as
    a fixed sequence of chunks.  Each ``recv`` call returns the next
    chunk regardless of the requested size — the production code's
    ``_recv_exact`` will loop and re-request as needed.
    """
    blob = b"".join(payloads)
    state = {"off": 0}

    def fake_recv(n: int) -> bytes:
        off = state["off"]
        if off >= len(blob):
            return b""
        chunk = blob[off : off + n]
        state["off"] = off + len(chunk)
        return chunk

    sock.recv.side_effect = fake_recv


# ---------------------------------------------------------------------------
# Fix 1 — _wait_for_response response-type validation
# ---------------------------------------------------------------------------


class TestWaitForResponseTypeValidation:
    def test_wait_for_response_raises_on_type_mismatch(self) -> None:
        """A response with the right req_id but wrong response_type is rejected."""
        t = _make_transport()
        # Wire delivers a CHECKPOINT_INFO (0x11) response carrying req_id=42.
        # The caller asked for a MEM_GET (0x01) expected_response_type.
        # Without validation, the CHECKPOINT_INFO body's first 2 bytes
        # would be parsed as a MEM_GET data_len.
        body = bytes([0x99] * 16)
        _queue_recvs(
            t._sock,
            [_build_response_bytes(RESPONSE_CHECKPOINT_INFO, body, request_id=42)],
        )

        with pytest.raises(TransportError, match="response_type mismatch"):
            t._wait_for_response(42, expected_response_type=0x01)

    def test_wait_for_response_error_message_names_both_types(self) -> None:
        t = _make_transport()
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x11, b"\xaa\xbb", request_id=7)],
        )
        with pytest.raises(TransportError) as exc_info:
            t._wait_for_response(7, expected_response_type=0x01)
        msg = str(exc_info.value)
        assert "0x1" in msg  # expected
        assert "0x11" in msg  # actual

    def test_wait_for_response_succeeds_on_matching_type(self) -> None:
        """Happy path: response_type matches expected → returned without error."""
        t = _make_transport()
        # MEM_GET response: data_len(2) + N bytes of data
        data = b"\xde\xad\xbe\xef"
        body = struct.pack("<H", len(data)) + data
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x01, body, request_id=99)],
        )
        resp = t._wait_for_response(99, expected_response_type=0x01)
        assert resp.response_type == 0x01
        assert resp.body == body

    def test_wait_for_response_no_expected_type_legacy_behavior(self) -> None:
        """Without expected_response_type, any response_type is accepted."""
        t = _make_transport()
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x11, b"\x00\x00", request_id=3)],
        )
        # Should not raise — legacy callers (none currently) keep old shape.
        resp = t._wait_for_response(3)
        assert resp.response_type == 0x11

    def test_event_queue_path_unaffected(self) -> None:
        """Event responses (req_id == 0xFFFFFFFF) are still buffered, not
        validated against expected_response_type."""
        t = _make_transport()
        # Wire: a STOPPED event (0x62, req_id=0xFFFFFFFF), then the real
        # MEM_GET response we asked for.
        stopped_body = struct.pack("<H", 0xC000) + b"\x00" * 4
        good_body = struct.pack("<H", 2) + b"\xab\xcd"
        _queue_recvs(
            t._sock,
            [
                _build_response_bytes(
                    EVENT_STOPPED, stopped_body, request_id=EVENT_REQUEST_ID
                ),
                _build_response_bytes(0x01, good_body, request_id=11),
            ],
        )
        resp = t._wait_for_response(11, expected_response_type=0x01)
        assert resp.body == good_body
        # The STOPPED event should now be in the queue (not dropped).
        assert len(t._event_queue) == 1
        assert t._event_queue[0].response_type == EVENT_STOPPED

    def test_cmd_to_response_type_map_covers_critical_commands(self) -> None:
        """The map exists and covers MEM_GET (the issue #88 hot path)."""
        assert CMD_TO_RESPONSE_TYPE[CMD_MEM_GET] == 0x01
        assert CMD_TO_RESPONSE_TYPE[CMD_REGISTERS_GET] == 0x31


# ---------------------------------------------------------------------------
# Fix 2 — read_memory chunk-length validation
# ---------------------------------------------------------------------------


class TestReadMemoryChunkLength:
    def test_read_memory_raises_on_short_chunk(self) -> None:
        """If MEM_GET returns fewer bytes than requested, raise loudly."""
        t = _make_transport()
        # Caller asks for 16 bytes; response advertises data_len=8 with 8 bytes.
        short_body = struct.pack("<H", 8) + b"\xaa" * 8
        # _send_and_recv() will use the next req_id, which is 0.
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x01, short_body, request_id=0)],
        )
        with pytest.raises(TransportError, match="short read"):
            t.read_memory(0x1000, 16)

    def test_read_memory_short_chunk_message_names_sizes(self) -> None:
        t = _make_transport()
        short_body = struct.pack("<H", 4) + b"\x55\x55\x55\x55"
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x01, short_body, request_id=0)],
        )
        with pytest.raises(TransportError) as exc_info:
            t.read_memory(0x2000, 32)
        msg = str(exc_info.value)
        assert "32" in msg  # requested
        assert "4" in msg   # got

    def test_read_memory_full_chunk_succeeds(self) -> None:
        """Happy path: chunk_size == data_len → no raise, bytes returned."""
        t = _make_transport()
        body = struct.pack("<H", 4) + b"\x01\x02\x03\x04"
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x01, body, request_id=0)],
        )
        result = t.read_memory(0x3000, 4)
        assert result == b"\x01\x02\x03\x04"


# ---------------------------------------------------------------------------
# Fix 3 — wait_for_stopped re-queues events, raises on desync
# ---------------------------------------------------------------------------


class TestWaitForStoppedRequeue:
    def test_wait_for_stopped_requeues_unrelated_events(self) -> None:
        """RESUMED before STOPPED should be re-queued, not dropped."""
        t = _make_transport()
        resumed_body = b""  # RESUMED has no body fields we use
        stopped_body = struct.pack("<H", 0xC000)
        _queue_recvs(
            t._sock,
            [
                _build_response_bytes(
                    EVENT_RESUMED, resumed_body, request_id=EVENT_REQUEST_ID
                ),
                _build_response_bytes(
                    EVENT_STOPPED, stopped_body, request_id=EVENT_REQUEST_ID
                ),
            ],
        )

        pc = t.wait_for_stopped(timeout=5.0)
        assert pc == 0xC000
        # RESUMED must be retained for diagnostic / later inspection.
        assert any(
            ev.response_type == EVENT_RESUMED for ev in t._event_queue
        ), "RESUMED event was dropped instead of re-queued"

    def test_wait_for_stopped_raises_on_unexpected_response(self) -> None:
        """A non-event response with a non-STOPPED type is a wire desync."""
        t = _make_transport()
        # MEM_GET response (response_type=0x01) with a real req_id, no
        # STOPPED in sight.  This shouldn't be there at all and silently
        # discarding it would corrupt the next request's reply.
        body = struct.pack("<H", 2) + b"\x00\x00"
        _queue_recvs(
            t._sock,
            [_build_response_bytes(0x01, body, request_id=42)],
        )
        with pytest.raises(TransportError, match="Unexpected non-event"):
            t.wait_for_stopped(timeout=5.0)

    def test_wait_for_stopped_clears_stale_events_at_start(self) -> None:
        """Pre-existing events from before the call must still be cleared."""
        t = _make_transport()
        # Pre-load the queue with a stale event.
        stale = _Response(
            response_type=EVENT_STOPPED,
            error_code=0x00,
            request_id=EVENT_REQUEST_ID,
            body=struct.pack("<H", 0xDEAD),
        )
        t._event_queue.append(stale)

        stopped_body = struct.pack("<H", 0xBEEF)
        _queue_recvs(
            t._sock,
            [
                _build_response_bytes(
                    EVENT_STOPPED, stopped_body, request_id=EVENT_REQUEST_ID
                ),
            ],
        )
        pc = t.wait_for_stopped(timeout=5.0)
        assert pc == 0xBEEF
        # The stale 0xDEAD event must NOT be in the queue.
        assert all(
            struct.unpack_from("<H", ev.body, 0)[0] != 0xDEAD
            for ev in t._event_queue
            if len(ev.body) >= 2
        )
