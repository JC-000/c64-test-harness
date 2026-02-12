"""Tests for VICE memory response parsing — including the prepended-prompt bug."""

from c64_test_harness.backends.vice import _parse_mem_response


class TestParseMemResponse:
    def test_normal_response(self):
        """Standard VICE response with >C: at start of line."""
        resp = ">C:0400  05 18 10 20  0b 05 19 3a  20 37 03 20  06 04 20 03"
        result = _parse_mem_response(resp, 16)
        assert len(result) == 16
        assert result[0] == 0x05
        assert result[3] == 0x20

    def test_prepended_prompt_bug(self):
        """Bug fix #1: VICE sometimes prepends (C:$XXXX) on same line."""
        resp = "(C:$0400) >C:40ab  05 18 10 20  0b 05 19 3a  20 37 03 20  06 04 20 03"
        result = _parse_mem_response(resp, 16)
        assert len(result) == 16
        assert result[0] == 0x05

    def test_multiple_lines(self):
        resp = (
            ">C:0400  05 18 10 20  0b 05 19 3a  20 37 03 20  06 04 20 03\n"
            ">C:0410  01 02 03 04  05 06 07 08  09 0a 0b 0c  0d 0e 0f 10"
        )
        result = _parse_mem_response(resp, 32)
        assert len(result) == 32
        assert result[16] == 0x01

    def test_mixed_prompt_and_normal(self):
        resp = (
            "(C:$0400) >C:0400  ff ee dd cc\n"
            ">C:0404  aa bb cc dd"
        )
        result = _parse_mem_response(resp, 8)
        assert len(result) == 8
        assert result[0] == 0xFF
        assert result[4] == 0xAA

    def test_max_bytes_truncation(self):
        resp = ">C:0400  01 02 03 04  05 06 07 08"
        result = _parse_mem_response(resp, 4)
        assert len(result) == 4
        assert result == [0x01, 0x02, 0x03, 0x04]

    def test_empty_response(self):
        result = _parse_mem_response("", 16)
        assert result == []

    def test_non_data_lines_ignored(self):
        resp = (
            "(C:$0400)\n"
            "Some other output\n"
            ">C:0400  ab cd ef 12"
        )
        result = _parse_mem_response(resp, 4)
        assert result == [0xAB, 0xCD, 0xEF, 0x12]

    def test_single_byte_read(self):
        """Single-byte reads were the most affected by bug #1."""
        resp = "(C:$00c6) >C:00c6  05"
        result = _parse_mem_response(resp, 1)
        assert result == [0x05]

    def test_ascii_dump_ignored(self):
        """The ASCII dump at end of line should not be parsed as hex."""
        resp = ">C:0400  48 45 4c 4c  4f 20 20 20  20 20 20 20  20 20 20 20   HELLO           "
        result = _parse_mem_response(resp, 16)
        assert len(result) == 16
        assert result[0] == 0x48
