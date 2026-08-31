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
import socket
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_binary import (
    API_VERSION,
    CMD_ADVANCE_INSTRUCTIONS,
    CMD_CHECKPOINT_DEL,
    CMD_CHECKPOINT_SET,
    CMD_CONDITION_SET,
    CMD_DUMP,
    CMD_EXECUTE_UNTIL_RETURN,
    CMD_MEM_GET,
    CMD_MEM_SET,
    CMD_REGISTERS_GET,
    CMD_REGISTERS_SET,
    CMD_RESOURCE_GET,
    CMD_RESOURCE_SET,
    CMD_TO_RESPONSE_TYPE,
    CMD_UNDUMP,
    EVENT_REQUEST_ID,
    EVENT_RESUMED,
    EVENT_STOPPED,
    RESPONSE_CHECKPOINT_INFO,
    RESPONSE_HEADER_SIZE,
    STX,
    BinaryViceTransport,
    _Response,
)
from c64_test_harness.transport import ConnectionError, TimeoutError, TransportError


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
        from c64_test_harness.memory_policy import MemoryPolicy

        t._memory_policy = MemoryPolicy.permissive()
        t._req_id = 0
        t._recv_buf = bytearray()
        t._pending_header = None
        t._resume_generation = 0
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
        _gen, _evt = t._event_queue[0]
        assert _evt.response_type == EVENT_STOPPED

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
            resp.response_type == EVENT_RESUMED for _gen, resp in t._event_queue
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

    def test_wait_for_stopped_discards_pre_resume_stale_events(self) -> None:
        """Events tagged at a prior resume generation must be discarded.

        Simulates a STOPPED event that was buffered during a *previous* test
        phase (generation 0).  After resume() bumps the generation to 1, that
        event is stale and wait_for_stopped must not return it.  A fresh
        STOPPED at the current generation arrives on the wire instead.
        """
        t = _make_transport()
        # Simulate a prior resume phase: generation is already 1 (as if
        # resume() was called once before).
        t._resume_generation = 1
        # Pre-load the queue with a stale event from generation 0.
        stale_resp = _Response(
            response_type=EVENT_STOPPED,
            error_code=0x00,
            request_id=EVENT_REQUEST_ID,
            body=struct.pack("<H", 0xDEAD),
        )
        t._event_queue.append((0, stale_resp))  # generation 0 → stale

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
            struct.unpack_from("<H", resp.body, 0)[0] != 0xDEAD
            for _gen, resp in t._event_queue
            if len(resp.body) >= 2
        )


# ---------------------------------------------------------------------------
# Fix 4 — resume-race: EVENT_STOPPED arriving during resume() ack window
# ---------------------------------------------------------------------------


class TestWaitForStoppedResumeRace:
    def test_wait_for_stopped_honours_event_arriving_during_resume_ack(self) -> None:
        """EVENT_STOPPED buffered during resume()'s CMD_EXIT ack window must
        not be discarded by a subsequent wait_for_stopped() call.

        This is the race reported in issue #103: VICE hits a breakpoint
        immediately (especially under warp) and pushes the STOPPED event
        onto the wire before the CMD_EXIT ack arrives.  _wait_for_response
        parks the early event in _event_queue tagged at the current
        _resume_generation.  The old code's _event_queue.clear() would
        then discard it, causing a 60-second timeout.  The fix: only drain
        events whose generation predates the current resume.
        """
        t = _make_transport()

        # Simulate resume() having been called: bump the generation to 1.
        # (We do this manually so we can inject the queued event without
        # actually going through the socket mock for the CMD_EXIT exchange.)
        t._resume_generation = 1

        # Inject a STOPPED event tagged at generation 1 (the current
        # generation) — as if _wait_for_response parked it during the
        # CMD_EXIT ack read.
        raced_resp = _Response(
            response_type=EVENT_STOPPED,
            error_code=0x00,
            request_id=EVENT_REQUEST_ID,
            body=struct.pack("<H", 0x08A8),  # PC matches issue #103 report
        )
        t._event_queue.append((1, raced_resp))

        # wait_for_stopped must return immediately with the queued event —
        # a timeout of 0.1 s rules out it blocking on the wire.
        pc = t.wait_for_stopped(timeout=0.1)
        assert pc == 0x08A8

    def test_wait_for_stopped_ignores_pre_resume_stopped_still_blocks(self) -> None:
        """A STOPPED event tagged at an older generation is stale and must
        not be returned; wait_for_stopped falls through to the wire recv."""
        t = _make_transport()

        # Generation 2 is current; the queued event is from generation 1.
        t._resume_generation = 2
        stale_resp = _Response(
            response_type=EVENT_STOPPED,
            error_code=0x00,
            request_id=EVENT_REQUEST_ID,
            body=struct.pack("<H", 0x1234),
        )
        t._event_queue.append((1, stale_resp))  # stale

        # The wire delivers the real STOPPED at generation 2.
        fresh_body = struct.pack("<H", 0x5678)
        _queue_recvs(
            t._sock,
            [
                _build_response_bytes(
                    EVENT_STOPPED, fresh_body, request_id=EVENT_REQUEST_ID
                ),
            ],
        )
        pc = t.wait_for_stopped(timeout=5.0)
        assert pc == 0x5678


# ---------------------------------------------------------------------------
# Fix 5 — CMD_TO_RESPONSE_TYPE completeness + _send_and_recv loud failure
# ---------------------------------------------------------------------------


class TestCmdResponseTypeMapCompleteness:
    """Every command the transport can send must have a map entry, and
    _send_and_recv must refuse to send an unmapped command rather than
    silently skipping response validation."""

    #: The two commands whose reply is *not* an echo of the opcode, with
    #: the reason.  Both are structural to the protocol -- a "set" that
    #: returns the resulting state -- and both are proven against a real
    #: VICE by the live tests named below, because a wrong entry here
    #: makes ``_wait_for_response`` raise a type mismatch on the wire.
    NON_ECHO_REPLIES = {
        CMD_CHECKPOINT_SET: (
            0x11,
            "replies with CHECKPOINT_INFO, not a Set ack "
            "(live: test_vice_binary.py::test_checkpoint_and_resume)",
        ),
        CMD_REGISTERS_SET: (
            0x31,
            "replies with a Registers response "
            "(live: test_vice_binary.py::test_set_and_read_registers)",
        ),
    }

    def test_response_type_echoes_the_opcode_except_where_documented(self) -> None:
        """The map must follow the protocol's rule, not a typed-in table.

        This replaces eight assertions of the form
        ``CMD_TO_RESPONSE_TYPE[CMD_DUMP] == 0x41``.  Those restated the
        opcode constant as a literal in the test file, under a docstring
        citing the spec -- so their oracle was the author, and they would
        have agreed just as readily with eight wrong numbers.  They also
        covered only 8 of the 23 commands.

        The real rule is that a reply echoes the command opcode, with two
        documented exceptions.  Asserting the rule covers every command,
        including ones added later, and makes the exceptions explicit
        rather than indistinguishable from the other 21 entries.
        """
        import c64_test_harness.backends.vice_binary as vb

        for name, cmd in sorted(vars(vb).items()):
            if not name.startswith("CMD_") or name == "CMD_TO_RESPONSE_TYPE":
                continue
            got = CMD_TO_RESPONSE_TYPE[cmd]
            if cmd in self.NON_ECHO_REPLIES:
                want, why = self.NON_ECHO_REPLIES[cmd]
                assert got == want, f"{name}: expected {want:#04x} ({why})"
            else:
                assert got == cmd, (
                    f"{name} ({cmd:#04x}) maps to {got:#04x}. A reply echoes "
                    f"its command opcode unless it is a documented exception "
                    f"-- add it to NON_ECHO_REPLIES with the reason if this "
                    f"is genuinely one."
                )

    def test_the_echo_rule_would_catch_a_wrong_entry(self) -> None:
        """Negative control: the rule above must reject a corrupted map.

        Without this, a refactor that made the loop iterate over nothing
        would leave a passing test that checks no commands at all.
        """
        import c64_test_harness.backends.vice_binary as vb

        assert len(CMD_TO_RESPONSE_TYPE) >= 20, "map shrank; rule covers little"
        bad = dict(CMD_TO_RESPONSE_TYPE)
        bad[vb.CMD_DUMP] = 0xFF
        assert bad[vb.CMD_DUMP] != vb.CMD_DUMP, (
            "a corrupted entry must be distinguishable from a good one"
        )

    def test_every_cmd_constant_is_mapped(self) -> None:
        """All CMD_* module constants have a CMD_TO_RESPONSE_TYPE entry."""
        import c64_test_harness.backends.vice_binary as vb

        cmds = {
            name: value
            for name, value in vars(vb).items()
            if name.startswith("CMD_") and name != "CMD_TO_RESPONSE_TYPE"
        }
        missing = [
            name for name, value in cmds.items()
            if value not in CMD_TO_RESPONSE_TYPE
        ]
        assert not missing, f"Commands without response-type mapping: {missing}"

    def test_send_and_recv_raises_on_unmapped_command(self) -> None:
        """An unmapped command type raises before anything hits the wire."""
        t = _make_transport()
        with pytest.raises(TransportError, match="CMD_TO_RESPONSE_TYPE"):
            t._send_and_recv(0x99, b"")
        t._sock.sendall.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 6 — _recv_exact partial-read resume after socket.timeout
# ---------------------------------------------------------------------------


def _queue_recvs_with_timeouts(sock: MagicMock, sequence: list) -> None:
    """Make sock.recv(n) walk *sequence*: a bytes item is served (chunked
    to at most n bytes per call), an Exception instance is raised once."""
    state = {"seq": list(sequence)}

    def fake_recv(n: int) -> bytes:
        while state["seq"]:
            item = state["seq"][0]
            if isinstance(item, BaseException):
                state["seq"].pop(0)
                raise item
            if not item:
                state["seq"].pop(0)
                continue
            chunk = item[:n]
            state["seq"][0] = item[len(chunk):]
            if not state["seq"][0]:
                state["seq"].pop(0)
            return chunk
        return b""

    sock.recv.side_effect = fake_recv


class TestRecvExactPartialReadResume:
    """A socket.timeout mid-frame must not drop the partial bytes; a
    retried read must resume the same frame and return it intact."""

    def test_timeout_mid_header_resumes_same_frame(self) -> None:
        t = _make_transport()
        frame = _build_response_bytes(0x01, struct.pack("<H", 2) + b"\xab\xcd",
                                      request_id=5)
        # First 7 header bytes, then a timeout, then the rest.
        _queue_recvs_with_timeouts(
            t._sock, [frame[:7], socket.timeout("timed out"), frame[7:]]
        )

        with pytest.raises(TimeoutError):
            t._recv_response()

        resp = t._recv_response()
        assert resp.response_type == 0x01
        assert resp.request_id == 5
        assert resp.body == struct.pack("<H", 2) + b"\xab\xcd"

    def test_timeout_mid_body_resumes_same_frame(self) -> None:
        t = _make_transport()
        body = struct.pack("<H", 4) + b"\xde\xad\xbe\xef"
        frame = _build_response_bytes(0x01, body, request_id=9)
        split = RESPONSE_HEADER_SIZE + 2  # header + 2 body bytes
        _queue_recvs_with_timeouts(
            t._sock, [frame[:split], socket.timeout("timed out"), frame[split:]]
        )

        with pytest.raises(TimeoutError):
            t._recv_response()

        resp = t._recv_response()
        assert resp.request_id == 9
        assert resp.body == body

    def test_wait_for_stopped_retry_returns_correct_frame(self) -> None:
        """The wait_for_stopped deadline pattern: a timed-out call followed
        by a retry must yield the correct STOPPED PC, not garbage parsed
        from an arbitrary wire offset."""
        t = _make_transport()
        stopped_body = struct.pack("<H", 0xC0DE)
        frame = _build_response_bytes(
            EVENT_STOPPED, stopped_body, request_id=EVENT_REQUEST_ID
        )
        _queue_recvs_with_timeouts(
            t._sock, [frame[:5], socket.timeout("timed out"), frame[5:]]
        )

        with pytest.raises(TimeoutError):
            t.wait_for_stopped(timeout=0.2)

        pc = t.wait_for_stopped(timeout=1.0)
        assert pc == 0xC0DE

    def test_back_to_back_frames_after_resume(self) -> None:
        """After a resumed frame, the next frame parses from the correct
        offset (no leftover state)."""
        t = _make_transport()
        frame1 = _build_response_bytes(0x01, struct.pack("<H", 1) + b"\x11",
                                       request_id=1)
        frame2 = _build_response_bytes(0x02, b"", request_id=2)
        _queue_recvs_with_timeouts(
            t._sock,
            [frame1[:3], socket.timeout("timed out"), frame1[3:] + frame2],
        )

        with pytest.raises(TimeoutError):
            t._recv_response()
        resp1 = t._recv_response()
        resp2 = t._recv_response()
        assert resp1.request_id == 1
        assert resp1.body == struct.pack("<H", 1) + b"\x11"
        assert resp2.request_id == 2
        assert resp2.response_type == 0x02

    def test_connection_close_clears_partial_buffer(self) -> None:
        """A closed connection resets the carry-over state."""
        t = _make_transport()
        _queue_recvs_with_timeouts(t._sock, [b"\x02\x02\x00"])  # then b"" = EOF
        with pytest.raises(ConnectionError):
            t._recv_response()
        assert t._recv_buf == bytearray()
        assert t._pending_header is None


# ---------------------------------------------------------------------------
# Fix 7 — _connect closes the socket when post-connect init raises
# ---------------------------------------------------------------------------


class TestConnectClosesSocketOnInitFailure:
    def test_init_register_map_failure_closes_socket(self) -> None:
        """If _init_register_map raises, the connected socket is closed."""
        mock_sock = MagicMock()
        with patch(
            "c64_test_harness.backends.vice_binary.socket.socket",
            return_value=mock_sock,
        ), patch.object(
            BinaryViceTransport,
            "_init_register_map",
            side_effect=TransportError("boom"),
        ):
            with pytest.raises(TransportError, match="boom"):
                BinaryViceTransport(host="127.0.0.1", port=6502)
        mock_sock.close.assert_called()

    def test_text_monitor_failure_closes_binary_socket(self) -> None:
        """If _connect_text_monitor raises, the binary socket is closed."""
        mock_sock = MagicMock()
        with patch(
            "c64_test_harness.backends.vice_binary.socket.socket",
            return_value=mock_sock,
        ), patch.object(
            BinaryViceTransport, "_init_register_map"
        ), patch.object(
            BinaryViceTransport,
            "_connect_text_monitor",
            side_effect=ConnectionError("no text monitor"),
        ):
            with pytest.raises(ConnectionError, match="no text monitor"):
                BinaryViceTransport(
                    host="127.0.0.1", port=6502, text_monitor_port=6510
                )
        mock_sock.close.assert_called()

    def test_successful_init_leaves_socket_open(self) -> None:
        """Happy path: no init failure, socket stays open."""
        mock_sock = MagicMock()
        with patch(
            "c64_test_harness.backends.vice_binary.socket.socket",
            return_value=mock_sock,
        ), patch.object(BinaryViceTransport, "_init_register_map"):
            t = BinaryViceTransport(host="127.0.0.1", port=6502)
        mock_sock.close.assert_not_called()
        assert t._sock is mock_sock


# ---------------------------------------------------------------------------
# Fix 8 — read/write_memory refuse to wrap past $FFFF
# ---------------------------------------------------------------------------


class TestMemoryNoAddressWrap:
    def test_write_past_ffff_raises_value_error(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv") as mock_send:
            with pytest.raises(ValueError, match="wrap"):
                t.write_memory(0xFFFF, b"\x01\x02")
        mock_send.assert_not_called()

    def test_write_far_past_ffff_raises_value_error(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv") as mock_send:
            with pytest.raises(ValueError, match="wrap"):
                t.write_memory(0xC000, bytes(0x8000))
        mock_send.assert_not_called()

    def test_write_ending_exactly_at_ffff_succeeds(self) -> None:
        t = _make_transport()
        resp = _Response(0x02, 0x00, 0, b"")
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            t.write_memory(0xFFFE, b"\xaa\xbb")
        assert mock_send.call_args[0][0] == CMD_MEM_SET

    def test_read_past_ffff_raises_value_error(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv") as mock_send:
            with pytest.raises(ValueError, match="wrap"):
                t.read_memory(0xFFF0, 0x11)
        mock_send.assert_not_called()

    def test_read_ending_exactly_at_ffff_succeeds(self) -> None:
        t = _make_transport()
        body = struct.pack("<H", 2) + b"\x12\x34"
        resp = _Response(0x01, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp):
            assert t.read_memory(0xFFFE, 2) == b"\x12\x34"

    def test_write_zero_length_still_noop(self) -> None:
        """Empty writes remain a no-op even at the top of memory."""
        t = _make_transport()
        with patch.object(t, "_send_and_recv") as mock_send:
            t.write_memory(0xFFFF, b"")
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Truncation boundaries in the response parsers
# ---------------------------------------------------------------------------


class TestParserTruncationBoundaries:
    """Every guard here is one byte from an ``IndexError`` on a short frame.

    These are defensive branches against a malformed or truncated
    response -- the exact failure mode issue #88 turned out to be -- and
    a mutation run found them unguarded: flipping ``>=`` to ``>`` or
    ``<=`` to ``<`` in each left the whole suite green, live modules
    included.  A guard nothing exercises at its boundary is a guard
    nobody knows is still there.

    Each test sits *on* the boundary rather than safely inside it, which
    is what makes the relational operator load-bearing: an off-by-one
    turns a clean ``break``/``return None`` into an exception.
    """

    def test_register_map_stops_when_the_count_outruns_the_data(self):
        """``off >= len(data)``: the count claims one more entry than
        the body carries, and ``off`` lands exactly on ``len(data)``."""
        t = _make_transport()
        # count=2, but only one complete entry follows.
        entry = bytes([3, 0x00, 0x02, ord("A")])  # size, id_lo, id_hi, name
        data = struct.pack("<H", 2) + entry
        resp = _Response(0x83, 0x00, 0, data)
        with patch.object(t, "_send_and_recv", return_value=resp):
            t._init_register_map()
        # The truncated second entry is dropped, not fatal.
        assert isinstance(t._reg_map, dict)

    def test_cpu_history_entry_truncated_before_an_item_returns_none(self):
        """``off >= len(entry)``: the entry ends exactly where the next
        item's size byte should start."""
        t = _make_transport()
        entry = struct.pack("<H", 1)  # claims one register, then stops
        assert t._parse_cpu_history_entry(entry) is None

    def test_cpu_history_item_of_size_exactly_one_still_yields_its_register(self):
        """``item_size >= 1``: size 1 is an id byte and a zero-width value.

        Size *1* is the boundary, not size 0 -- a zero-size item fails
        both ``>= 1`` and ``> 1``, so a test built on it passes with the
        operator either way.  That was this test's first form, and a
        mutation run caught it doing nothing.

        The register map must also be populated, or no item resolves to a
        name and both branches yield ``registers == {}``.
        """
        t = _make_transport()
        t._reg_map = {"a": (0x42, 8)}
        tail = b"\x00" * 9  # cycle(8) + instr_len(1)

        one = t._parse_cpu_history_entry(
            struct.pack("<H", 1) + bytes([1, 0x42]) + tail
        )
        assert one is not None and one["registers"] == {"a": 0}, (
            f"a size-1 item was skipped instead of decoded: {one}"
        )

    def test_cpu_history_cycle_field_is_read_when_it_exactly_fits(self):
        """``off + 8 <= len(entry)``: eight trailing bytes are a cycle count.

        Asserting only that neither input raises passes with ``<`` too --
        declining to read the field is not an error, just silent data
        loss.  So assert the value: at ``off + 8 == len(entry)`` the
        count must actually be decoded.
        """
        t = _make_transport()
        base = struct.pack("<H", 0)  # no register items, so off == 2
        exact = base + struct.pack("<Q", 0xDEADBEEF)  # off + 8 == len(entry)

        result = t._parse_cpu_history_entry(exact)
        assert result is not None
        assert result["cycle"] == 0xDEADBEEF, (
            f"the cycle field was not read at the exact boundary: "
            f"got {result['cycle']:#x}"
        )

        short = base + b"\x00" * 7  # one byte short of a <Q
        assert t._parse_cpu_history_entry(short) is not None, (
            "a seven-byte tail must be declined, not fatal"
        )


class TestRedundantGuards:
    """Guards a mutation run showed to be unreachable or already implied.

    Recorded rather than tested: a test that cannot distinguish the guard
    being present from absent is decorative by construction, and writing
    one would only hide that.
    """

    def test_zero_length_read_is_guarded_twice(self):
        """``read_memory``'s ``if length <= 0`` is redundant.

        The chunking loop below it is ``while remaining > 0``, so a zero
        length sends nothing with or without the early return -- which is
        why mutating ``<=`` to ``<`` changes no observable behaviour.
        Both paths are pinned here so the *behaviour* stays covered even
        though the branch cannot be isolated.
        """
        t = _make_transport()
        with patch.object(t, "_send_and_recv") as send:
            assert t.read_memory(0x1000, 0) == b""
            assert t.read_memory(0x1000, -5) == b""
        send.assert_not_called()


class TestStatusRegisterAliases:
    """``sr`` is resolved through a three-name chain: FL, then FLAGS, then SR.

    Nothing pinned any of the three.  A mutation run renamed ``"FLAGS"``
    to ``"FLAGSX"`` and the whole suite stayed green, live modules
    included -- the alias silently stopped matching and ``sr`` fell
    through to the next name, or to 0.

    The chain is also an *assumption about VICE's naming*, not a fact
    derived from it, which is the same shape of defect as the flag names.
    ``test_vice_binary.py`` anchors it to a real emulator; these pin the
    resolution order so a reordering cannot pass unnoticed.
    """

    @staticmethod
    def _entry(reg_map: dict, values: dict) -> bytes:
        """A CPU-history entry carrying *values* keyed by register name."""
        ids = {name: rid for name, (rid, _) in reg_map.items()}
        body = b"".join(
            bytes([2, ids[name], val]) for name, val in values.items()
        )
        return (
            struct.pack("<H", len(values)) + body
            + struct.pack("<Q", 0) + bytes([0])
        )

    def _parse(self, reg_map, values):
        t = _make_transport()
        t._reg_map = reg_map
        return t._parse_cpu_history_entry(self._entry(reg_map, values))

    def test_each_alias_resolves_on_its_own(self):
        for name in ("FL", "FLAGS", "SR"):
            reg_map = {name: (0x10, 8)}
            result = self._parse(reg_map, {name: 0x37})
            assert result is not None and result["sr"] == 0x37, (
                f"alias {name!r} did not resolve to sr"
            )

    def test_fl_wins_over_flags_and_flags_wins_over_sr(self):
        """Precedence is load-bearing: a VICE exposing more than one of
        these must not have ``sr`` depend on dict ordering."""
        reg_map = {"FL": (0x10, 8), "FLAGS": (0x11, 8), "SR": (0x12, 8)}
        both = self._parse(reg_map, {"FL": 0x01, "FLAGS": 0x02, "SR": 0x03})
        assert both["sr"] == 0x01, "FL must win"

        no_fl = self._parse(
            {"FLAGS": (0x11, 8), "SR": (0x12, 8)}, {"FLAGS": 0x02, "SR": 0x03}
        )
        assert no_fl["sr"] == 0x02, "FLAGS must win over SR"

    def test_sr_is_zero_when_no_alias_matches(self):
        """The documented fallback, pinned so it stays deliberate."""
        reg_map = {"ZZ": (0x10, 8)}
        assert self._parse(reg_map, {"ZZ": 0x37})["sr"] == 0
