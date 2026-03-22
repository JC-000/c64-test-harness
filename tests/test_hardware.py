"""Tests for the hardware transport base class (backends/hardware.py)."""
from __future__ import annotations

import pytest

from c64_test_harness.backends.hardware import HardwareTransportBase


def test_default_dimensions():
    h = HardwareTransportBase()
    assert h.screen_cols == 40
    assert h.screen_rows == 25


def test_custom_dimensions():
    h = HardwareTransportBase(screen_cols=80, screen_rows=50)
    assert h.screen_cols == 80
    assert h.screen_rows == 50


def test_read_memory_raises():
    h = HardwareTransportBase()
    with pytest.raises(NotImplementedError):
        h.read_memory(0x0400, 16)


def test_write_memory_raises():
    h = HardwareTransportBase()
    with pytest.raises(NotImplementedError):
        h.write_memory(0x0400, b"\x00")


def test_all_abstract_methods_raise():
    h = HardwareTransportBase()
    with pytest.raises(NotImplementedError):
        h.read_screen_codes()
    with pytest.raises(NotImplementedError):
        h.inject_keys([0x41])
    with pytest.raises(NotImplementedError):
        h.read_registers()
    with pytest.raises(NotImplementedError):
        h.resume()


def test_close_is_noop():
    h = HardwareTransportBase()
    h.close()  # should not raise
