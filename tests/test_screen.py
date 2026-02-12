"""Tests for screen.py — ScreenGrid, wrap-aware search, extract_between."""

import pytest

from c64_test_harness.screen import ScreenGrid, wait_for_text, wait_for_stable
from conftest import MockTransport


def _text_to_screen_codes(text: str, cols: int = 40, rows: int = 25) -> list[int]:
    """Convert a simple ASCII string to C64 screen codes for testing.

    Uppercase A-Z → 1-26, digits → 0x30-0x39, space → 32.
    Only handles the chars needed for testing.
    """
    total = cols * rows
    # Pad to fill screen
    text = text.ljust(total)[:total]
    codes = []
    for ch in text:
        if "A" <= ch <= "Z":
            codes.append(ord(ch) - ord("A") + 1)
        elif "0" <= ch <= "9":
            codes.append(ord(ch))
        elif ch == " ":
            codes.append(32)
        elif ch == ".":
            codes.append(0x2E)
        elif ch == ":":
            codes.append(0x3A)
        elif ch == "/":
            codes.append(0x2F)
        elif ch == "=":
            codes.append(0x3D)
        elif ch == "-":
            codes.append(0x2D)
        elif ch == "@":
            codes.append(0)
        elif ch == "\n":
            codes.append(32)
        else:
            codes.append(32)
    return codes


class TestScreenGrid:
    def test_from_codes(self):
        codes = [32] * 1000
        grid = ScreenGrid.from_codes(codes)
        assert grid.cols == 40
        assert grid.rows == 25
        assert len(grid.codes) == 1000

    def test_text_lines(self):
        codes = [32] * 1000
        codes[0] = 8  # 'H'
        codes[1] = 9  # 'I'
        grid = ScreenGrid.from_codes(codes)
        lines = grid.text_lines()
        assert len(lines) == 25
        assert lines[0].startswith("HI")

    def test_continuous_text_no_newlines(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        ct = grid.continuous_text()
        assert "\n" not in ct
        assert len(ct) == 1000

    def test_wrap_aware_search(self):
        """Text spanning two 40-col rows should be findable (bug fix #2)."""
        codes = [32] * 1000
        # Place "EMAIL AD" at end of row 2 (positions 72-79)
        # and "DRESS:" at start of row 3 (positions 80-85)
        text_before = "EMAIL AD"
        text_after = "DRESS:"
        for i, ch in enumerate(text_before):
            codes[72 + i] = ord(ch) - ord("A") + 1 if ch.isalpha() else 32
        for i, ch in enumerate(text_after):
            codes[80 + i] = ord(ch) - ord("A") + 1 if ch.isalpha() else 0x3A
        grid = ScreenGrid.from_codes(codes)

        # Should fail with line-by-line text (the original bug)
        assert "EMAIL ADDRESS:" not in grid.text()
        # Should succeed with continuous text
        assert grid.has_text("EMAIL ADDRESS:")

    def test_has_text_case_insensitive(self):
        codes = _text_to_screen_codes("HELLO WORLD" + " " * 989)
        grid = ScreenGrid.from_codes(codes)
        assert grid.has_text("hello world")
        assert grid.has_text("HELLO WORLD")

    def test_find_text(self):
        codes = _text_to_screen_codes("  READY." + " " * 992)
        grid = ScreenGrid.from_codes(codes)
        pos = grid.find_text("READY.")
        assert pos == 2

    def test_find_text_not_found(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        assert grid.find_text("ABSENT") == -1

    def test_extract_between(self):
        text = "KEY: ABCDEF1234 SUBJECT: /CN=TEST"
        codes = _text_to_screen_codes(text + " " * (1000 - len(text)))
        grid = ScreenGrid.from_codes(codes)
        result = grid.extract_between("KEY: ", " SUBJECT")
        assert result is not None
        assert "ABCDEF1234" in result

    def test_extract_between_not_found(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        assert grid.extract_between("KEY:", "END") is None

    def test_extract_between_no_end_marker(self):
        text = "START:HELLO WORLD"
        codes = _text_to_screen_codes(text + " " * (1000 - len(text)))
        grid = ScreenGrid.from_codes(codes)
        result = grid.extract_between("START:", "ZZZZZ")
        assert result is not None
        assert "HELLO" in result

    def test_dump_format(self):
        grid = ScreenGrid.from_codes([32] * 1000)
        dump = grid.dump("test")
        assert "[test]" in dump
        assert "0|" in dump
        assert "24|" in dump

    def test_from_transport(self):
        transport = MockTransport()
        grid = ScreenGrid.from_transport(transport)
        assert grid.cols == 40
        assert grid.rows == 25
        assert len(grid.codes) == 1000


class TestWaitForText:
    def test_immediate_match(self):
        codes = _text_to_screen_codes("READY." + " " * 994)
        transport = MockTransport(screen_codes=codes)
        grid = wait_for_text(transport, "READY.", timeout=1, poll_interval=0.1, verbose=False)
        assert grid is not None
        assert grid.has_text("READY.")

    def test_timeout_returns_none(self):
        transport = MockTransport()  # blank screen
        grid = wait_for_text(transport, "NEVER", timeout=0.3, poll_interval=0.1, verbose=False)
        assert grid is None


class TestWaitForStable:
    def test_stable_returns_grid(self):
        codes = _text_to_screen_codes("STABLE" + " " * 994)
        transport = MockTransport(screen_codes=codes)
        grid = wait_for_stable(transport, timeout=2, poll_interval=0.1, stable_count=2)
        assert grid is not None

    def test_changing_screen_eventually_stabilizes(self):
        transport = MockTransport()
        # Screen changes once then stays stable
        call_count = 0
        original_read = transport.read_screen_codes

        def changing_read():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return [call_count] * 1000
            return [99] * 1000

        transport.read_screen_codes = changing_read
        grid = wait_for_stable(transport, timeout=3, poll_interval=0.1, stable_count=2)
        assert grid is not None
