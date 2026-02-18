"""Tests for debug utilities (debug.py)."""
from __future__ import annotations

from c64_test_harness.debug import dump_screen
from c64_test_harness.transport import TransportError
from conftest import MockTransport


def test_dump_screen_returns_formatted_string():
    t = MockTransport()
    # Put some recognisable screen codes on row 0
    codes = list(t.screen_codes)
    codes[0] = 8   # 'H' in screen codes
    codes[1] = 5   # 'E'
    codes[2] = 12  # 'L'
    codes[3] = 12  # 'L'
    codes[4] = 15  # 'O'
    t.screen_codes = codes

    result = dump_screen(t, label="test")
    assert "--- Screen dump [test] ---" in result
    assert "hello" in result.lower()
    assert "---" in result


def test_dump_screen_prints_to_stdout(capsys):
    t = MockTransport()
    dump_screen(t, label="cap")
    captured = capsys.readouterr()
    assert "--- Screen dump [cap] ---" in captured.out


def test_dump_screen_empty_label():
    t = MockTransport()
    result = dump_screen(t, label="")
    assert "--- Screen dump ---" in result
    # No bracket artifacts
    assert "[]" not in result


def test_dump_screen_transport_error():
    """When transport raises, dump_screen returns an error string instead of crashing."""

    class FailTransport(MockTransport):
        def read_screen_codes(self):
            raise TransportError("connection lost")

    t = FailTransport()
    result = dump_screen(t)
    assert "screen read failed" in result
