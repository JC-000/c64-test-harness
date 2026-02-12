"""Tests for encoding/petscii.py — PETSCII conversion and extensions."""

import pytest

from c64_test_harness.encoding.petscii import (
    char_to_petscii,
    register_petscii,
    PETSCII_RETURN,
    PETSCII_HOME,
    PETSCII_CLR,
    PETSCII_DEL,
    PETSCII_F1,
    PETSCII_F3,
    PETSCII_F5,
    PETSCII_F7,
    PETSCII_CRSR_DOWN,
    PETSCII_CRSR_RIGHT,
    PETSCII_CRSR_UP,
    PETSCII_CRSR_LEFT,
    PETSCII_RUN_STOP,
)


class TestBasicMappings:
    def test_uppercase_letters(self):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            code = char_to_petscii(ch)
            assert code == ord(ch), f"{ch} -> {code:#x}"

    def test_lowercase_maps_to_uppercase(self):
        for ch in "abcdefghijklmnopqrstuvwxyz":
            code = char_to_petscii(ch)
            assert code == ord(ch) - 32, f"{ch} -> {code:#x}"

    def test_digits(self):
        for ch in "0123456789":
            assert char_to_petscii(ch) == ord(ch)

    def test_space(self):
        assert char_to_petscii(" ") == 0x20

    def test_return(self):
        assert char_to_petscii("\r") == 0x0D
        assert char_to_petscii("\n") == 0x0D

    def test_common_punctuation(self):
        assert char_to_petscii("!") == 0x21
        assert char_to_petscii(".") == 0x2E
        assert char_to_petscii("/") == 0x2F
        assert char_to_petscii(":") == 0x3A
        assert char_to_petscii("=") == 0x3D


class TestExtendedMappings:
    """These were missing from vicemon.py and caused test failures (bug #3)."""

    def test_at_sign(self):
        assert char_to_petscii("@") == 0x40

    def test_angle_brackets(self):
        assert char_to_petscii("<") == 0x3C
        assert char_to_petscii(">") == 0x3E

    def test_square_brackets(self):
        assert char_to_petscii("[") == 0x5B
        assert char_to_petscii("]") == 0x5D

    def test_underscore(self):
        assert char_to_petscii("_") == 0xA4


class TestSpecialKeys:
    def test_constants_are_correct(self):
        assert PETSCII_RETURN == 0x0D
        assert PETSCII_HOME == 0x13
        assert PETSCII_CLR == 0x93
        assert PETSCII_DEL == 0x14
        assert PETSCII_F1 == 0x85
        assert PETSCII_F3 == 0x86
        assert PETSCII_F5 == 0x87
        assert PETSCII_F7 == 0x88
        assert PETSCII_CRSR_DOWN == 0x11
        assert PETSCII_CRSR_UP == 0x91
        assert PETSCII_CRSR_RIGHT == 0x1D
        assert PETSCII_CRSR_LEFT == 0x9D
        assert PETSCII_RUN_STOP == 0x03


class TestRoundTrip:
    """All mappable ASCII chars should survive char_to_petscii without error."""

    MAPPABLE = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
        " !\"#$%&'()*+,-./:;=?@<>[]_"
    )

    def test_all_mappable_chars(self):
        for ch in self.MAPPABLE:
            code = char_to_petscii(ch)
            assert 0 <= code <= 255, f"{ch} -> {code}"


class TestErrors:
    def test_unmapped_char_raises(self):
        with pytest.raises(ValueError, match="No PETSCII mapping"):
            char_to_petscii("\x00")

    def test_emoji_raises(self):
        with pytest.raises(ValueError):
            char_to_petscii("\U0001f600")


class TestRegister:
    def test_register_custom(self):
        register_petscii("\x80", 0xFE)
        assert char_to_petscii("\x80") == 0xFE

    def test_register_out_of_range(self):
        with pytest.raises(ValueError, match="0-255"):
            register_petscii("X", 256)
