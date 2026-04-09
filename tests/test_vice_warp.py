"""Integration tests for VICE warp mode runtime toggle.

Requires x64sc to be available on PATH.
"""

from __future__ import annotations

import shutil

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.transport import TransportError

from conftest import connect_binary_transport

# Skip entire module if x64sc is not installed
pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)


def _start_vice(warp: bool = False):
    """Start a VICE instance with dual monitors.

    Returns (ViceProcess, BinaryViceTransport, PortAllocator, port, text_port).
    The transport has text_monitor_port set for warp control.
    """
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    text_port = allocator.allocate()

    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()
    text_reservation = allocator.take_socket(text_port)
    if text_reservation is not None:
        text_reservation.close()

    config = ViceConfig(
        port=port, text_monitor_port=text_port, warp=warp, sound=False,
    )

    proc = ViceProcess(config)
    proc.start()
    transport = connect_binary_transport(
        port, proc=proc, text_monitor_port=text_port,
    )
    return proc, transport, allocator, port, text_port


class TestWarpDefault:
    """Test warp mode defaults and startup states."""

    def test_get_warp_default(self) -> None:
        """Start VICE without -warp, verify get_warp() returns False."""
        proc, transport, allocator, port, text_port = _start_vice(warp=False)
        try:
            assert transport.get_warp() is False
        finally:
            transport.close()
            proc.stop()
            allocator.release(port)
            allocator.release(text_port)

    def test_get_warp_started_with_warp(self) -> None:
        """Start VICE with warp=True in ViceConfig, verify get_warp() returns True."""
        proc, transport, allocator, port, text_port = _start_vice(warp=True)
        try:
            assert transport.get_warp() is True
        finally:
            transport.close()
            proc.stop()
            allocator.release(port)
            allocator.release(text_port)


class TestWarpToggle:
    """Test runtime warp mode toggling."""

    def test_set_warp_on_off(self) -> None:
        """Start VICE without warp, toggle on then off, verify each state."""
        proc, transport, allocator, port, text_port = _start_vice(warp=False)
        try:
            assert transport.get_warp() is False

            transport.set_warp(True)
            assert transport.get_warp() is True

            transport.set_warp(False)
            assert transport.get_warp() is False
        finally:
            transport.close()
            proc.stop()
            allocator.release(port)
            allocator.release(text_port)


class TestResourceGetSet:
    """Test resource_get and resource_set methods."""

    def test_resource_get_speed(self) -> None:
        """resource_get('Speed') returns an integer (100 for normal speed)."""
        proc, transport, allocator, port, text_port = _start_vice(warp=False)
        try:
            speed = transport.resource_get("Speed")
            assert isinstance(speed, int)
            assert speed == 100
        finally:
            transport.close()
            proc.stop()
            allocator.release(port)
            allocator.release(text_port)

    def test_resource_set_speed(self) -> None:
        """resource_set('Speed', 200), verify roundtrip, then restore."""
        proc, transport, allocator, port, text_port = _start_vice(warp=False)
        try:
            transport.resource_set("Speed", 200)
            assert transport.resource_get("Speed") == 200

            # Restore original value
            transport.resource_set("Speed", 100)
            assert transport.resource_get("Speed") == 100
        finally:
            transport.close()
            proc.stop()
            allocator.release(port)
            allocator.release(text_port)

    def test_resource_get_nonexistent(self) -> None:
        """resource_get with an invalid resource name raises TransportError."""
        proc, transport, allocator, port, text_port = _start_vice(warp=False)
        try:
            with pytest.raises(TransportError):
                transport.resource_get("NonExistentResource12345")
        finally:
            transport.close()
            proc.stop()
            allocator.release(port)
            allocator.release(text_port)
