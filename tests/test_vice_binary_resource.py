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
    CMD_ADVANCE_INSTRUCTIONS,
    CMD_BANKS_AVAILABLE,
    CMD_CONDITION_SET,
    CMD_CPUHISTORY_GET,
    CMD_DISPLAY_GET,
    CMD_DUMP,
    CMD_EXECUTE_UNTIL_RETURN,
    CMD_JOYPORT_SET,
    CMD_PALETTE_GET,
    CMD_REGS_AVAILABLE,
    CMD_RESET,
    CMD_RESOURCE_GET,
    CMD_RESOURCE_SET,
    CMD_UNDUMP,
    CMD_USERPORT_SET,
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
        from c64_test_harness.memory_policy import MemoryPolicy

        t._memory_policy = MemoryPolicy.permissive()
        t._req_id = 0
        t._recv_buf = bytearray()
        t._pending_header = None
        t._resume_generation = 0
        t._reg_map = {}
        t._event_queue = __import__("collections").deque()
        t._lock = __import__("threading").Lock()
        t._text_lock = __import__("threading").Lock()
        t._sock = MagicMock()
        t._text_sock = MagicMock() if text_monitor else None
        return t


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

def _resource_int_resp(value: int) -> _Response:
    """Build a Resource Get response carrying an integer resource."""
    return _Response(
        response_type=CMD_RESOURCE_GET,
        error_code=0x00,
        request_id=0,
        body=bytes([0x01, 0x04]) + struct.pack("<i", value),
    )


class TestWarp:
    """Tests for the hybrid set_warp / get_warp.

    Warp control must NOT require the text monitor — text_monitor_port
    is 0 on every default construction path. VICE 3.10 has no warp
    control on the binary monitor (no ``WarpMode`` resource; ``Speed``
    rejects 0), so the fallback drives the ``Speed`` resource with a
    pseudo-warp percentage. When a text monitor IS connected, the real
    ``warp on``/``warp off`` toggle is used instead.
    """

    def test_set_warp_on_uses_speed_resource(self) -> None:
        """Without a text monitor, set_warp(True) sets Speed to pseudo-warp."""
        t = _make_transport(text_monitor=False)
        resp = _Response(CMD_RESOURCE_SET, 0x00, 0, b"")
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            t.set_warp(True)
        assert mock_send.call_args[0][0] == CMD_RESOURCE_SET
        name_bytes = b"Speed"
        expected = (
            bytes([0x01, len(name_bytes)])
            + name_bytes
            + bytes([4])
            + struct.pack("<i", BinaryViceTransport._WARP_SPEED_PERCENT)
        )
        assert mock_send.call_args[0][1] == expected

    def test_set_warp_off_restores_native_speed(self) -> None:
        """Without a text monitor, set_warp(False) sets Speed back to 100."""
        t = _make_transport(text_monitor=False)
        resp = _Response(CMD_RESOURCE_SET, 0x00, 0, b"")
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            t.set_warp(False)
        assert mock_send.call_args[0][0] == CMD_RESOURCE_SET
        name_bytes = b"Speed"
        expected = (
            bytes([0x01, len(name_bytes)])
            + name_bytes
            + bytes([4])
            + struct.pack("<i", 100)
        )
        assert mock_send.call_args[0][1] == expected

    def test_get_warp_true(self) -> None:
        """get_warp() is True when Speed reads the pseudo-warp percentage."""
        t = _make_transport(text_monitor=False)
        with patch.object(
            t,
            "_send_and_recv",
            return_value=_resource_int_resp(
                BinaryViceTransport._WARP_SPEED_PERCENT
            ),
        ) as mock_send:
            assert t.get_warp() is True
        assert mock_send.call_args[0][0] == CMD_RESOURCE_GET
        name_bytes = b"Speed"
        assert mock_send.call_args[0][1] == bytes([len(name_bytes)]) + name_bytes

    def test_get_warp_false(self) -> None:
        """get_warp() is False at native speed."""
        t = _make_transport(text_monitor=False)
        with patch.object(t, "_send_and_recv", return_value=_resource_int_resp(100)):
            assert t.get_warp() is False

    def test_warp_prefers_text_monitor_when_connected(self) -> None:
        """With a text monitor connected, warp uses VICE's real toggle."""
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_send_and_recv") as mock_send, \
             patch.object(t, "_text_command", return_value="") as mock_cmd:
            t.set_warp(True)
        mock_cmd.assert_called_once_with("warp on")
        mock_send.assert_not_called()

    def test_get_warp_prefers_text_monitor_when_connected(self) -> None:
        """With a text monitor connected, get_warp reads the real state."""
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_send_and_recv") as mock_send, \
             patch.object(
                 t, "_text_command", return_value="Warp mode is on"
             ) as mock_cmd:
            assert t.get_warp() is True
        mock_cmd.assert_called_once_with("warp")
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Code-flow / inspection
# ---------------------------------------------------------------------------

def _empty_resp(rt: int) -> _Response:
    return _Response(response_type=rt, error_code=0x00, request_id=0, body=b"")


class TestSingleStep:
    def test_single_step_default(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_ADVANCE_INSTRUCTIONS)) as mock_send, \
             patch.object(t, "wait_for_stopped") as mock_wait:
            t.single_step()
        assert mock_send.call_args[0][0] == CMD_ADVANCE_INSTRUCTIONS
        assert mock_send.call_args[0][1] == struct.pack("<BH", 0x00, 1)
        mock_wait.assert_called_once()

    def test_single_step_step_over(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_ADVANCE_INSTRUCTIONS)), \
             patch.object(t, "wait_for_stopped"):
            t.single_step(count=42, step_over_subroutines=True)
        # Re-issue to inspect arguments
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_ADVANCE_INSTRUCTIONS)) as mock_send, \
             patch.object(t, "wait_for_stopped"):
            t.single_step(count=42, step_over_subroutines=True)
        assert mock_send.call_args[0][1] == struct.pack("<BH", 0x01, 42)

    def test_single_step_count_overflow(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="u16"):
            t.single_step(count=0x10000)


class TestStepOut:
    def test_step_out(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_EXECUTE_UNTIL_RETURN)) as mock_send, \
             patch.object(t, "wait_for_stopped") as mock_wait:
            t.step_out()
        assert mock_send.call_args[0][0] == CMD_EXECUTE_UNTIL_RETURN
        assert mock_send.call_args[0][1] == b""
        mock_wait.assert_called_once()


class TestSetCondition:
    def test_set_condition(self) -> None:
        t = _make_transport()
        expr = "A == $42"
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_CONDITION_SET)) as mock_send:
            t.set_condition(7, expr)
        assert mock_send.call_args[0][0] == CMD_CONDITION_SET
        body = mock_send.call_args[0][1]
        expected = struct.pack("<IB", 7, len(expr)) + expr.encode("ascii")
        assert body == expected

    def test_set_condition_bad_checkpoint(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="u32"):
            t.set_condition(0x1_0000_0000, "x")

    def test_set_condition_expression_too_long(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="too long"):
            t.set_condition(1, "x" * 256)


class TestCpuHistory:
    def test_cpu_history_empty(self) -> None:
        t = _make_transport()
        resp = _Response(CMD_CPUHISTORY_GET, 0x00, 0, struct.pack("<I", 0))
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            result = t.cpu_history(count=4)
        assert result == []
        body = mock_send.call_args[0][1]
        assert body == struct.pack("<BI", 0x00, 4)

    def test_cpu_history_truncated_returns_partial(self) -> None:
        t = _make_transport()
        # Claim 1 record but provide a body too short to parse fully.
        body = struct.pack("<I", 1) + b"\x05\x00"
        resp = _Response(CMD_CPUHISTORY_GET, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp):
            result = t.cpu_history(count=1)
        # Defensive parse: should not raise; returns whatever it could decode.
        assert isinstance(result, list)

    def test_cpu_history_one_record(self) -> None:
        t = _make_transport()
        # Build one entry: no registers (count=0) + cycle=0x1234 + instr_len=1 + opcode=0xEA
        entry = struct.pack("<HQB", 0, 0x1234, 1) + bytes([0xEA])
        body = struct.pack("<I", 1) + bytes([len(entry)]) + entry
        resp = _Response(CMD_CPUHISTORY_GET, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp):
            result = t.cpu_history(count=1)
        assert len(result) == 1
        assert result[0]["cycle"] == 0x1234
        assert result[0]["instruction"] == bytes([0xEA])


class TestBanksAvailable:
    def test_banks_available_empty(self) -> None:
        t = _make_transport()
        resp = _Response(CMD_BANKS_AVAILABLE, 0x00, 0, struct.pack("<H", 0))
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            result = t.banks_available()
        assert result == []
        assert mock_send.call_args[0][0] == CMD_BANKS_AVAILABLE
        assert mock_send.call_args[0][1] == b""

    def test_banks_available_two_banks(self) -> None:
        t = _make_transport()
        # entry: bank_id(2) + name_len(1) + name
        e1 = struct.pack("<HB", 0x0000, 3) + b"cpu"
        e2 = struct.pack("<HB", 0x0001, 3) + b"ram"
        body = struct.pack("<H", 2) + bytes([len(e1)]) + e1 + bytes([len(e2)]) + e2
        resp = _Response(CMD_BANKS_AVAILABLE, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp):
            result = t.banks_available()
        assert result == [(0x0000, "cpu"), (0x0001, "ram")]


class TestRegistersAvailable:
    def test_registers_available(self) -> None:
        t = _make_transport()
        # Two register descriptors
        # entry: reg_id(1) + size_bits(1) + name_len(1) + name
        e1 = bytes([0x00, 16, 2]) + b"PC"
        e2 = bytes([0x01, 8, 1]) + b"A"
        body = struct.pack("<H", 2) + bytes([len(e1)]) + e1 + bytes([len(e2)]) + e2
        resp = _Response(CMD_REGS_AVAILABLE, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            result = t.registers_available()
        assert result == [
            {"id": 0x00, "size_bits": 16, "name": "PC"},
            {"id": 0x01, "size_bits": 8, "name": "A"},
        ]
        assert mock_send.call_args[0][1] == bytes([0x00])


# ---------------------------------------------------------------------------
# I/O injection
# ---------------------------------------------------------------------------

class TestInjectJoystick:
    def test_inject_joystick_port1(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_JOYPORT_SET)) as mock_send:
            t.inject_joystick(1, 0x10)
        assert mock_send.call_args[0][0] == CMD_JOYPORT_SET
        assert mock_send.call_args[0][1] == struct.pack("<HH", 1, 0x10)

    def test_inject_joystick_port2(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_JOYPORT_SET)) as mock_send:
            t.inject_joystick(2, 0xABCD)
        assert mock_send.call_args[0][1] == struct.pack("<HH", 2, 0xABCD)

    def test_inject_joystick_invalid_port(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="port"):
            t.inject_joystick(3, 0)

    def test_inject_joystick_invalid_value(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="u16"):
            t.inject_joystick(1, 0x10000)


class TestInjectUserport:
    def test_inject_userport(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_USERPORT_SET)) as mock_send:
            t.inject_userport(0xBEEF)
        assert mock_send.call_args[0][0] == CMD_USERPORT_SET
        assert mock_send.call_args[0][1] == struct.pack("<H", 0xBEEF)

    def test_inject_userport_overflow(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="u16"):
            t.inject_userport(0x10000)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

class TestReadFramebuffer:
    def test_read_framebuffer(self) -> None:
        t = _make_transport()
        # info: debug_w=384, debug_h=272, inner_x=32, inner_y=35, inner_w=320, inner_h=200, bpp=8
        # info_len = 13 (4-byte info_len excluded? No, info_len is the count of
        # bytes that follow up to and including bpp). The implementation expects
        # 4-byte info_len at offset 0, then debug_w/h at 4/6, inner_x/y/w/h at
        # 8/10/12/14, bpp at 16. So info_len = 13.
        info_len = 13
        info = struct.pack("<IHHHHHHB", info_len, 384, 272, 32, 35, 320, 200, 8)
        pixels = bytes([0xAA, 0xBB, 0xCC])
        body = info + struct.pack("<I", len(pixels)) + pixels
        resp = _Response(CMD_DISPLAY_GET, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            result = t.read_framebuffer(use_vic=True, format=0)
        assert mock_send.call_args[0][0] == CMD_DISPLAY_GET
        assert mock_send.call_args[0][1] == struct.pack("<BB", 0x01, 0)
        assert result["debug_rect"] == (0, 0, 384, 272)
        assert result["inner_rect"] == (32, 35, 320, 200)
        assert result["bpp"] == 8
        assert result["bytes"] == pixels

    def test_read_framebuffer_short(self) -> None:
        t = _make_transport()
        resp = _Response(CMD_DISPLAY_GET, 0x00, 0, b"\x00")
        with patch.object(t, "_send_and_recv", return_value=resp):
            with pytest.raises(TransportError, match="too short"):
                t.read_framebuffer()

    # ---- the declared-vs-delivered length check (upstream bug 1) ----

    @staticmethod
    def _display_body(declared: int, delivered: int) -> bytes:
        """A DISPLAY_GET body claiming *declared* bytes and carrying
        *delivered*."""
        info = struct.pack("<IHHHHHHB", 13, 384, 272, 32, 35, 320, 200, 8)
        return info + struct.pack("<I", declared) + bytes(delivered)

    def test_a_full_length_buffer_reports_no_shortfall(self) -> None:
        """The healthy case still reports the declared length."""
        t = _make_transport()
        resp = _Response(CMD_DISPLAY_GET, 0x00, 0, self._display_body(16, 16))
        with patch.object(t, "_send_and_recv", return_value=resp):
            result = t.read_framebuffer()
        assert result["short_by"] == 0
        assert result["declared_length"] == 16
        assert len(result["bytes"]) == 16

    def test_the_known_four_byte_shortfall_is_surfaced_not_hidden(
        self, caplog
    ) -> None:
        """VICE 3.10's under-allocation must reach the caller as data.

        Slicing alone stops early and hands back a buffer that does not
        match its own geometry, with no error and no log line -- which is
        exactly how this went unnoticed.  It must not raise either: the
        bug fires on every call and ``read_framebuffer`` is on the
        cross-backend protocol, so raising would make VICE fail where the
        U64 backend succeeds.
        """
        t = _make_transport()
        resp = _Response(CMD_DISPLAY_GET, 0x00, 0, self._display_body(20, 16))
        with caplog.at_level("WARNING"):
            with patch.object(t, "_send_and_recv", return_value=resp):
                result = t.read_framebuffer()
        assert result["declared_length"] == 20
        assert result["short_by"] == 4
        assert len(result["bytes"]) == 16
        assert "under-allocates" in caplog.text, "the shortfall was silent"

    def test_an_unexpected_shortfall_is_refused(self) -> None:
        """Anything but the documented 4 bytes is real corruption.

        Pinning the exact number is the point: a desynchronised or
        truncated response is not this bug, and must not be waved through
        as though it were.
        """
        t = _make_transport()
        resp = _Response(CMD_DISPLAY_GET, 0x00, 0, self._display_body(24, 16))
        with patch.object(t, "_send_and_recv", return_value=resp):
            with pytest.raises(TransportError, match="different fault"):
                t.read_framebuffer()


class TestReadPalette:
    def test_read_palette(self) -> None:
        t = _make_transport()
        # 2 palette entries: each 3 RGB bytes prefixed with item_size=3
        e1 = bytes([0x00, 0x00, 0x00])
        e2 = bytes([0xFF, 0xFF, 0xFF])
        body = struct.pack("<H", 2) + bytes([3]) + e1 + bytes([3]) + e2
        resp = _Response(CMD_PALETTE_GET, 0x00, 0, body)
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            result = t.read_palette(use_vic=False)
        assert result == [(0, 0, 0), (0xFF, 0xFF, 0xFF)]
        assert mock_send.call_args[0][1] == bytes([0x00])

    def test_read_palette_short(self) -> None:
        t = _make_transport()
        resp = _Response(CMD_PALETTE_GET, 0x00, 0, b"\x01")
        with patch.object(t, "_send_and_recv", return_value=resp):
            with pytest.raises(TransportError, match="too short"):
                t.read_palette()


# ---------------------------------------------------------------------------
# Snapshots / reset
# ---------------------------------------------------------------------------

class TestSnapshots:
    def test_dump_snapshot(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_DUMP)) as mock_send:
            t.dump_snapshot("/tmp/state.vsf", save_roms=True, save_disks=False)
        body = mock_send.call_args[0][1]
        name = b"/tmp/state.vsf"
        expected = struct.pack("<BBB", 0x01, 0x00, len(name)) + name
        assert body == expected

    def test_undump_snapshot(self) -> None:
        t = _make_transport()
        resp = _Response(CMD_UNDUMP, 0x00, 0, struct.pack("<H", 0xC000))
        with patch.object(t, "_send_and_recv", return_value=resp) as mock_send:
            new_pc = t.undump_snapshot("snap.vsf")
        assert new_pc == 0xC000
        body = mock_send.call_args[0][1]
        name = b"snap.vsf"
        assert body == bytes([len(name)]) + name

    def test_dump_filename_too_long(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="too long"):
            t.dump_snapshot("a" * 256)

    def test_undump_short_response(self) -> None:
        t = _make_transport()
        resp = _Response(CMD_UNDUMP, 0x00, 0, b"")
        with patch.object(t, "_send_and_recv", return_value=resp):
            with pytest.raises(TransportError, match="too short"):
                t.undump_snapshot("snap.vsf")


class TestReset:
    def test_reset_soft(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset(0)
        assert mock_send.call_args[0][0] == CMD_RESET
        assert mock_send.call_args[0][1] == bytes([0])

    def test_reset_drive(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset(8)
        assert mock_send.call_args[0][1] == bytes([8])

    def test_reset_invalid_type(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="reset_type"):
            t.reset(2)


# ---------------------------------------------------------------------------
# Text-monitor extras
# ---------------------------------------------------------------------------

class TestDetachDrive:
    def test_detach_drive(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.detach_drive(8)
        mock_cmd.assert_called_once_with("detach 8")

    def test_detach_drive_invalid(self) -> None:
        t = _make_transport(text_monitor=True)
        with pytest.raises(ValueError, match="device"):
            t.detach_drive(7)

    def test_detach_drive_no_text_monitor(self) -> None:
        t = _make_transport(text_monitor=False)
        with pytest.raises(TransportError, match="text monitor"):
            t.detach_drive(8)


class TestAttachDrive:
    def test_attach_drive(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.attach_drive(8, "/tmp/disk.d64")
        mock_cmd.assert_called_once_with('attach "/tmp/disk.d64" 8')

    def test_attach_drive_invalid_device(self) -> None:
        t = _make_transport(text_monitor=True)
        with pytest.raises(ValueError, match="device"):
            t.attach_drive(2, "x.d64")

    def test_attach_drive_no_text_monitor(self) -> None:
        t = _make_transport(text_monitor=False)
        with pytest.raises(TransportError, match="text monitor"):
            t.attach_drive(8, "x.d64")


class TestScreenshot:
    def test_screenshot_default_png(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.screenshot_to_file("/tmp/shot.png")
        mock_cmd.assert_called_once_with('screenshot "/tmp/shot.png" png')

    def test_screenshot_format_passthrough(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.screenshot_to_file("/tmp/shot.bmp", format="bmp")
        mock_cmd.assert_called_once_with('screenshot "/tmp/shot.bmp" bmp')

    def test_screenshot_no_text_monitor(self) -> None:
        t = _make_transport(text_monitor=False)
        with pytest.raises(TransportError, match="text monitor"):
            t.screenshot_to_file("x.png")


class TestProfile:
    def test_profile_start_default(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.profile_start()
        mock_cmd.assert_called_once_with("profile on")

    def test_profile_start_invalid_mode(self) -> None:
        t = _make_transport(text_monitor=True)
        with pytest.raises(ValueError, match="mode"):
            t.profile_start("nope")

    def test_profile_stop(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command") as mock_cmd:
            t.profile_stop()
        mock_cmd.assert_called_once_with("profile off")

    def test_profile_dump(self) -> None:
        t = _make_transport(text_monitor=True)
        with patch.object(t, "_text_command", return_value="some output\n(C:$0000) ") as mock_cmd:
            out = t.profile_dump("flat")
        assert "some output" in out
        mock_cmd.assert_called_once_with("profile flat")

    def test_profile_dump_invalid(self) -> None:
        t = _make_transport(text_monitor=True)
        with pytest.raises(ValueError, match="mode"):
            t.profile_dump("on")

    def test_profile_no_text_monitor(self) -> None:
        t = _make_transport(text_monitor=False)
        with pytest.raises(TransportError, match="text monitor"):
            t.profile_start()


# ---------------------------------------------------------------------------
# set_speed / get_speed (protocol-level CPU speed control)
# ---------------------------------------------------------------------------


def _resource_answering_mock(t, speed_value: int = 100):
    """Patch t._send_and_recv with a side effect that answers the
    resource commands the way a live binary monitor would: RESOURCE_GET
    returns the integer *speed_value* (the Speed resource), RESOURCE_SET
    acks with an empty body.  Anything else is a test bug."""
    def answer(cmd_type: int, body: bytes = b"") -> _Response:
        if cmd_type == CMD_RESOURCE_GET:
            return _resource_int_resp(speed_value)
        if cmd_type == CMD_RESOURCE_SET:
            return _Response(CMD_RESOURCE_SET, 0x00, 0, b"")
        raise AssertionError(f"unexpected command {cmd_type:#x}")
    return patch.object(t, "_send_and_recv", side_effect=answer)


class TestSpeedProtocol:
    """set_speed / get_speed delegate correctly to VICE warp.

    These run against a default-shaped transport (no text monitor) — the
    regression behind the resource-API reimplementation was that
    set_speed/get_speed raised TransportError on every default
    construction path because warp needed the text monitor.
    """

    def test_set_speed_1_disables_warp(self) -> None:
        t = _make_transport(text_monitor=False)
        with patch.object(t, "set_warp") as mock_warp:
            t.set_speed(1)
        mock_warp.assert_called_once_with(False)

    def test_set_speed_none_enables_warp(self) -> None:
        t = _make_transport(text_monitor=False)
        with patch.object(t, "set_warp") as mock_warp:
            t.set_speed(None)
        mock_warp.assert_called_once_with(True)

    @pytest.mark.parametrize("mult", [2, 4, 8, 48])
    def test_set_speed_other_int_raises_notimplemented(self, mult: int) -> None:
        t = _make_transport(text_monitor=False)
        with pytest.raises(NotImplementedError, match="VICE"):
            t.set_speed(mult)

    def test_get_speed_native(self) -> None:
        t = _make_transport(text_monitor=False)
        with patch.object(t, "get_warp", return_value=False):
            assert t.get_speed() == 1

    def test_get_speed_warp(self) -> None:
        t = _make_transport(text_monitor=False)
        with patch.object(t, "get_warp", return_value=True):
            assert t.get_speed() is None

    # End-to-end (down to the command layer) without a text monitor —
    # the regression test for the resource-API warp reimplementation.

    def test_set_speed_1_no_text_monitor_end_to_end(self) -> None:
        t = _make_transport(text_monitor=False)
        with _resource_answering_mock(t) as mock_send:
            t.set_speed(1)
        assert mock_send.call_args[0][0] == CMD_RESOURCE_SET

    def test_set_speed_none_no_text_monitor_end_to_end(self) -> None:
        t = _make_transport(text_monitor=False)
        with _resource_answering_mock(t) as mock_send:
            t.set_speed(None)
        assert mock_send.call_args[0][0] == CMD_RESOURCE_SET

    def test_get_speed_no_text_monitor_end_to_end(self) -> None:
        t = _make_transport(text_monitor=False)
        with _resource_answering_mock(t, speed_value=100):
            assert t.get_speed() == 1
        with _resource_answering_mock(
            t, speed_value=BinaryViceTransport._WARP_SPEED_PERCENT
        ):
            assert t.get_speed() is None


# ---------------------------------------------------------------------------
# reset(scope=...) — protocol-level dispatch
# ---------------------------------------------------------------------------


class TestResetScope:
    """reset(scope=...) sends the right CMD_RESET type byte."""

    def test_reset_scope_cpu(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset("cpu")
        assert mock_send.call_args[0][0] == CMD_RESET
        assert mock_send.call_args[0][1] == bytes([0])

    def test_reset_scope_machine(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset("machine")
        assert mock_send.call_args[0][1] == bytes([1])

    @pytest.mark.parametrize("idx,expected", [(0, 8), (1, 9), (2, 10), (3, 11)])
    def test_reset_scope_drive(self, idx: int, expected: int) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset("drive", drive=idx)
        assert mock_send.call_args[0][1] == bytes([expected])

    def test_reset_scope_default_is_cpu(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset()
        assert mock_send.call_args[0][1] == bytes([0])

    def test_reset_scope_drive_missing_drive(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="drive"):
            t.reset("drive")

    def test_reset_scope_drive_index_out_of_range(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="0..3"):
            t.reset("drive", drive=4)

    def test_reset_scope_drive_string_value(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="drive"):
            t.reset("drive", drive="bogus")

    def test_reset_scope_unknown(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="scope"):
            t.reset("everything")

    def test_reset_bool_scope_refused(self) -> None:
        # bool is an int subclass — make sure we don't silently coerce.
        t = _make_transport()
        with pytest.raises(ValueError, match="bool"):
            t.reset(True)

    # Backwards-compat: the legacy reset_type kwarg / positional int still works.

    def test_reset_legacy_kwarg(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset(reset_type=1)
        assert mock_send.call_args[0][1] == bytes([1])

    def test_reset_legacy_positional_int(self) -> None:
        t = _make_transport()
        with patch.object(t, "_send_and_recv", return_value=_empty_resp(CMD_RESET)) as mock_send:
            t.reset(9)
        assert mock_send.call_args[0][1] == bytes([9])

    def test_reset_legacy_kwarg_invalid_still_raises(self) -> None:
        t = _make_transport()
        with pytest.raises(ValueError, match="reset_type"):
            t.reset(reset_type=2)
