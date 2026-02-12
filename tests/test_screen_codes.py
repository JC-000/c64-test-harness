"""Tests for encoding/screen_codes.py — full 256-entry table verification."""

from c64_test_harness.encoding.screen_codes import (
    SCREEN_CODE_TABLE,
    GRAPHICS_PLACEHOLDER,
    screen_code_to_char,
)


def test_table_has_256_entries():
    assert len(SCREEN_CODE_TABLE) == 256


def test_at_sign():
    assert SCREEN_CODE_TABLE[0x00] == "@"


def test_uppercase_letters():
    for i in range(1, 27):
        expected = chr(ord("A") + i - 1)
        assert SCREEN_CODE_TABLE[i] == expected, f"Code {i:#x}"


def test_brackets_and_special():
    assert SCREEN_CODE_TABLE[27] == "["
    assert SCREEN_CODE_TABLE[28] == "\\"
    assert SCREEN_CODE_TABLE[29] == "]"
    assert SCREEN_CODE_TABLE[30] == "^"
    assert SCREEN_CODE_TABLE[31] == "_"


def test_space():
    assert SCREEN_CODE_TABLE[32] == " "


def test_punctuation_and_digits():
    # Digits
    for d in range(0, 10):
        code = 0x30 + d
        assert SCREEN_CODE_TABLE[code] == str(d)
    # Some punctuation
    assert SCREEN_CODE_TABLE[0x21] == "!"
    assert SCREEN_CODE_TABLE[0x2E] == "."
    assert SCREEN_CODE_TABLE[0x3F] == "?"


def test_repeat_region_40_5f():
    """Codes 0x40-0x5F should repeat 0x00-0x1F."""
    for i in range(64, 96):
        assert SCREEN_CODE_TABLE[i] == SCREEN_CODE_TABLE[i - 64], f"Code {i:#x}"


def test_graphics_region():
    """Codes 0x60-0x7F should all be graphics placeholders."""
    for i in range(0x60, 0x80):
        assert SCREEN_CODE_TABLE[i] == GRAPHICS_PLACEHOLDER, f"Code {i:#x}"


def test_reverse_video_region():
    """Codes 0x80-0xFF should mirror 0x00-0x7F."""
    for i in range(0x80, 0x100):
        assert SCREEN_CODE_TABLE[i] == SCREEN_CODE_TABLE[i - 128], f"Code {i:#x}"


def test_screen_code_to_char_wraps():
    """screen_code_to_char should mask to 0xFF."""
    assert screen_code_to_char(0) == "@"
    assert screen_code_to_char(256) == "@"  # wraps
    assert screen_code_to_char(1) == "A"
