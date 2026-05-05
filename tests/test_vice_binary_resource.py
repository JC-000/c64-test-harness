"""Unit tests for BinaryViceTransport resource_get / resource_set / warp.

These tests mock the socket layer to avoid needing a live VICE instance.
They verify the wire-format encoding and decoding of the binary monitor
resource commands (0x51, 0x52) and the warp convenience methods.
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_binary import (
    API_VERSION,
    CMD_RESOURCE_GET,
    CMD_RESOURCE_SET,
    RESPONSE_HEADER_SIZE,
    STX,
    BinaryViceTransport,
    _Response,
)
from c64_test_harness.transport import TransportError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transport(*, text_monitor: bool = False) -> BinaryViceTransport:
    """Create a BinaryViceTransport with mocked connection."""
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
        t._event_queue = __import__("collections").deque()
        t._lock = __import__("threading").Lock()
        t._text_lock = __import__("threading").Lock()
        t._sock = MagicMock()
        t._text_sock = MagicMock() if text_monitor else None
        return t


def _build_response(response_type: int, body: bytes, request_id: int = 0) -> bytes:
    """Build a raw binary monitor response (header + body) as bytes."""
    header = struct.pack(
        "<BBIBBI",
        STX,
        API_VERSION,
        len(body),       # body_length (4 bytes)
        response_type,   # 1 byte
        0x00,            # error_code (1 byte)
        request_id,      # 4 bytes
    )
    return header + body


# ---------------------------------------------------------------------------
# resource_get tests
# ---------------------------------------------------------------------------

class TestResourceGet:
    """Tests for resource_get wire format and decoding."""

    def test_resource_get_integer(self) -> None:
        """resource_get returns int for type 0x01 (integer resource)."""
        t = _make_transport()
        # Response body: type=0x01, value_length=4, value=1 (little-endian)
        resp_body = bytes([0x01, 0x04]) + struct.pack("<i", 1)
        resp = _Response(
            response_type=CMD_RESOURCE_GET,
            error_code=0x00,
            request_id=0,
            body=resp_body,
        )
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            result = t.resource_get("WarpMode")

        assert result == 1
        assert isinstance(result, int)

        # Verify sent body: name_length(1) + name bytes
        sent_body = mock_send.call_args[0][1]
        name_bytes = b"WarpMode"
        assert sent_body == bytes([len(name_bytes)]) + name_bytes

    def test_resource_get_string(self) -> None:
        """resource_get returns str for type 0x00 (string resource)."""
        t = _make_transport()
        string_value = "SomeStringValue"
        value_bytes = string_value.encode("ascii")
        resp_body = bytes([0x00, len(value_bytes)]) + value_bytes
        resp = _Response(
            response_type=CMD_RESOURCE_GET,
            error_code=0x00,
            request_id=0,
            body=resp_body,
        )
        with patch.object(t, "_send_and_recv", return_value=resp):
            result = t.resource_get("SomeResource")

        assert result == "SomeStringValue"
        assert isinstance(result, str)

    def test_resource_get_short_response(self) -> None:
        """resource_get raises TransportError when body < 2 bytes."""
        t = _make_transport()
        resp = _Response(
            response_type=CMD_RESOURCE_GET,
            error_code=0x00,
            request_id=0,
            body=bytes([0x01]),  # only 1 byte
        )
        with patch.object(t, "_send_and_recv", return_value=resp):
            with pytest.raises(TransportError, match="too short"):
                t.resource_get("WarpMode")


# ---------------------------------------------------------------------------
# resource_set tests
# ---------------------------------------------------------------------------

class TestResourceSet:
    """Tests for resource_set wire format encoding."""

    def test_resource_set_integer(self) -> None:
        """resource_set sends correct wire format for an integer value."""
        t = _make_transport()
        resp = _Response(
            response_type=CMD_RESOURCE_SET,
            error_code=0x00,
            request_id=0,
            body=b"",
        )
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            t.resource_set("WarpMode", 1)

        # Verify command type
        assert mock_send.call_args[0][0] == CMD_RESOURCE_SET

        # Verify body: type(1) + name_length(1) + name + value_length(1) + value(4)
        sent_body = mock_send.call_args[0][1]
        name_bytes = b"WarpMode"
        expected = (
            bytes([0x01, len(name_bytes)])
            + name_bytes
            + bytes([4])
            + struct.pack("<i", 1)
        )
        assert sent_body == expected

    def test_resource_set_string(self) -> None:
        """resource_set sends correct wire format for a string value."""
        t = _make_transport()
        resp = _Response(
            response_type=CMD_RESOURCE_SET,
            error_code=0x00,
            request_id=0,
            body=b"",
        )
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            t.resource_set("SomeName", "hello")

        sent_body = mock_send.call_args[0][1]
        name_bytes = b"SomeName"
        value_bytes = b"hello"
        expected = (
            bytes([0x00, len(name_bytes)])
            + name_bytes
            + bytes([len(value_bytes)])
            + value_bytes
        )
        assert sent_body == expected


# ---------------------------------------------------------------------------
# Warp convenience methods
# ---------------------------------------------------------------------------

class TestWarp:
    """Tests for set_warp / get_warp via text monitor."""

    def test_set_warp_on(self) -> None:
        """set_warp(True) sends 'warp on' to text monitor."""
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.set_warp(True)
        mock_cmd.assert_called_once_with("warp on")

    def test_set_warp_off(self) -> None:
        """set_warp(False) sends 'warp off' to text monitor."""
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.set_warp(False)
        mock_cmd.assert_called_once_with("warp off")

    def test_get_warp_true(self) -> None:
        """get_warp() returns True when text monitor says 'is on'."""
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command", return_value="Warp mode is on.\n(C:$e5d4) "):
            assert t.get_warp() is True

    def test_get_warp_false(self) -> None:
        """get_warp() returns False when text monitor says 'is off'."""
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command", return_value="Warp mode is off.\n(C:$e5d4) "):
            assert t.get_warp() is False

    def test_set_warp_without_text_monitor_raises(self) -> None:
        """set_warp raises TransportError without text monitor connection."""
        t = _make_transport(text_monitor=False)
        with pytest.raises(TransportError, match="text monitor"):
            t.set_warp(True)

    def test_get_warp_without_text_monitor_raises(self) -> None:
        """get_warp raises TransportError without text monitor connection."""
        t = _make_transport(text_monitor=False)
        with pytest.raises(TransportError, match="text monitor"):
            t.get_warp()
