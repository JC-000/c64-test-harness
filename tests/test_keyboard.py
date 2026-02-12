"""Tests for keyboard.py — batching logic and single key send."""

from c64_test_harness.keyboard import send_text, send_key, KEYBUF_MAX
from c64_test_harness.encoding.petscii import char_to_petscii
from conftest import MockTransport


class TestSendText:
    def test_short_string_single_batch(self):
        transport = MockTransport()
        send_text(transport, "HELLO")
        assert len(transport.injected_keys) == 1
        assert len(transport.injected_keys[0]) == 5

    def test_exact_batch_size(self):
        transport = MockTransport()
        send_text(transport, "A" * KEYBUF_MAX)
        assert len(transport.injected_keys) == 1
        assert len(transport.injected_keys[0]) == KEYBUF_MAX

    def test_exceeds_batch_size(self):
        transport = MockTransport()
        text = "A" * (KEYBUF_MAX + 3)
        send_text(transport, text)
        assert len(transport.injected_keys) == 2
        assert len(transport.injected_keys[0]) == KEYBUF_MAX
        assert len(transport.injected_keys[1]) == 3

    def test_two_full_batches(self):
        transport = MockTransport()
        send_text(transport, "A" * (KEYBUF_MAX * 2))
        assert len(transport.injected_keys) == 2
        assert all(len(b) == KEYBUF_MAX for b in transport.injected_keys)

    def test_petscii_conversion(self):
        transport = MockTransport()
        send_text(transport, "Hi")
        codes = transport.injected_keys[0]
        assert codes[0] == char_to_petscii("H")
        assert codes[1] == char_to_petscii("i")

    def test_empty_string(self):
        transport = MockTransport()
        send_text(transport, "")
        assert len(transport.injected_keys) == 0

    def test_return_key(self):
        transport = MockTransport()
        send_text(transport, "\r")
        assert transport.injected_keys[0] == [0x0D]


class TestSendKey:
    def test_single_char(self):
        transport = MockTransport()
        send_key(transport, "A")
        assert transport.injected_keys == [[char_to_petscii("A")]]

    def test_raw_petscii_code(self):
        transport = MockTransport()
        send_key(transport, 0x85)  # F1
        assert transport.injected_keys == [[0x85]]
