"""Live integration tests for U64 debug and video stream capture.

Gated by ``U64_HOST`` env var. These tests use the real hardware.
DeviceLock is used for cross-process safety.

Example::

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_u64_streams_live.py -v
"""
from __future__ import annotations

import logging
import os
import socket
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.u64_debug_capture import DebugCapture
from c64_test_harness.backends.u64_video_capture import VideoCapture
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    get_data_streams_config,
    get_debug_stream_mode,
    set_debug_stream_mode,
    set_stream_destination,
    DEBUG_MODE_6510,
    DEBUG_MODE_6510_VIC,
)

logger = logging.getLogger(__name__)

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set -- live Ultimate device tests disabled",
)


def _local_ip() -> str:
    """Detect the local IP address that can reach the U64."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((_HOST, 80))
        return s.getsockname()[0]
    finally:
        s.close()


@pytest.fixture(scope="module")
def client():
    """Acquire device lock and return client for the module."""
    host = os.environ.get("U64_HOST")
    pw = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(host)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {host}")
    c = Ultimate64Client(host=host, password=pw, timeout=8.0)
    yield c
    lock.release()


# ======================================================================
# Debug stream tests
# ======================================================================


def test_debug_stream_captures_cycles(client: Ultimate64Client) -> None:
    """Start debug stream, capture for 1 second, verify cycle count."""
    local = _local_ip()
    cap = DebugCapture(port=11002)
    cap.start()
    try:
        client.stream_debug_start(f"{local}:11002")
        time.sleep(1.0)
    finally:
        try:
            client.stream_debug_stop()
        except Exception:
            pass
        time.sleep(0.3)
        result = cap.stop()

    logger.info(
        "Debug capture: %d cycles, %d packets, %d dropped, %.2fs",
        result.total_cycles, result.packets_received,
        result.packets_dropped, result.duration_seconds,
    )
    assert result.total_cycles > 10000, (
        f"Expected >10000 cycles, got {result.total_cycles}"
    )
    assert len(result.trace) > 0, "Trace is empty"
    assert result.packets_dropped < 50, (
        f"Too many drops: {result.packets_dropped}"
    )


def test_debug_bus_cycle_fields(client: Ultimate64Client) -> None:
    """Capture briefly, verify BusCycle field ranges on a CPU read cycle."""
    local = _local_ip()
    # Ensure 6510-only mode so all cycles have is_cpu=True
    orig_mode = get_debug_stream_mode(client)
    try:
        set_debug_stream_mode(client, DEBUG_MODE_6510)

        cap = DebugCapture(port=11002)
        cap.start()
        try:
            client.stream_debug_start(f"{local}:11002")
            time.sleep(0.5)
        finally:
            try:
                client.stream_debug_stop()
            except Exception:
                pass
            time.sleep(0.3)
            result = cap.stop()
    finally:
        set_debug_stream_mode(client, orig_mode)

    assert result.total_cycles > 0, "No cycles captured"

    # Find a CPU read cycle
    cpu_read = None
    for cycle in result.trace[:1000]:
        if cycle.is_cpu and cycle.is_read:
            cpu_read = cycle
            break

    assert cpu_read is not None, "No CPU read cycle found in first 1000 entries"
    assert 0 <= cpu_read.address <= 0xFFFF, (
        f"Address out of 16-bit range: {cpu_read.address:#06x}"
    )
    assert 0 <= cpu_read.data <= 0xFF, (
        f"Data out of 8-bit range: {cpu_read.data:#04x}"
    )
    assert cpu_read.is_cpu is True, "Expected is_cpu=True in 6510-only mode"
    logger.info(
        "CPU read cycle: addr=$%04X data=$%02X",
        cpu_read.address, cpu_read.data,
    )


def test_debug_stream_irq_detection(client: Ultimate64Client) -> None:
    """Capture for 1 second, verify IRQ# is asserted during KERNAL IRQ handler."""
    local = _local_ip()
    cap = DebugCapture(port=11002)
    cap.start()
    try:
        client.stream_debug_start(f"{local}:11002")
        time.sleep(1.0)
    finally:
        try:
            client.stream_debug_stop()
        except Exception:
            pass
        time.sleep(0.3)
        result = cap.stop()

    irq_count = sum(1 for c in result.trace if c.irq)
    logger.info(
        "IRQ asserted in %d / %d cycles (%.2f%%)",
        irq_count, result.total_cycles,
        100.0 * irq_count / max(result.total_cycles, 1),
    )
    # KERNAL IRQ fires ~60 times/sec; each handler runs many cycles
    # with IRQ# still asserted. We should see at least some.
    assert irq_count > 0, (
        f"Expected some IRQ-asserted cycles, got 0 out of {result.total_cycles}"
    )


def test_debug_stream_mode_config(client: Ultimate64Client) -> None:
    """Read current mode, set to '6510 & VIC', verify, restore original."""
    orig_mode = get_debug_stream_mode(client)
    logger.info("Original debug stream mode: %s", orig_mode)
    try:
        set_debug_stream_mode(client, DEBUG_MODE_6510_VIC)
        new_mode = get_debug_stream_mode(client)
        assert new_mode == DEBUG_MODE_6510_VIC, (
            f"Expected '{DEBUG_MODE_6510_VIC}', got '{new_mode}'"
        )
        logger.info("Successfully set mode to: %s", new_mode)
    finally:
        set_debug_stream_mode(client, orig_mode)
        restored = get_debug_stream_mode(client)
        logger.info("Restored mode to: %s", restored)


# ======================================================================
# Video stream tests
# ======================================================================


def test_video_stream_captures_frames(client: Ultimate64Client) -> None:
    """Start video stream, capture for 1 second, verify frame count."""
    local = _local_ip()
    cap = VideoCapture(port=11000)
    cap.start()
    try:
        client.stream_video_start(f"{local}:11000")
        time.sleep(1.0)
    finally:
        try:
            client.stream_video_stop()
        except Exception:
            pass
        time.sleep(0.3)
        result = cap.stop()

    logger.info(
        "Video capture: %d frames, %d packets, %d dropped, %.2fs",
        result.frames_completed, result.packets_received,
        result.packets_dropped, result.duration_seconds,
    )
    # At 50fps PAL, 1 second should yield ~50 frames; allow some slack
    assert result.frames_completed >= 10, (
        f"Expected >=10 frames, got {result.frames_completed}"
    )
    # Check first frame has expected width
    if result.frames:
        assert result.frames[0].width == 384, (
            f"Expected width=384, got {result.frames[0].width}"
        )


def test_video_frame_dimensions(client: Ultimate64Client) -> None:
    """Verify captured frame dimensions: 384 wide, reasonable height."""
    local = _local_ip()
    cap = VideoCapture(port=11000)
    cap.start()
    try:
        client.stream_video_start(f"{local}:11000")
        time.sleep(0.5)
    finally:
        try:
            client.stream_video_stop()
        except Exception:
            pass
        time.sleep(0.3)
        result = cap.stop()

    assert result.frames_completed > 0, "No frames captured"
    frame = result.frames[0]
    assert frame.width == 384, f"Expected width=384, got {frame.width}"
    assert 240 <= frame.height <= 272, (
        f"Height {frame.height} outside expected PAL/NTSC range 240-272"
    )
    logger.info("Frame dimensions: %dx%d", frame.width, frame.height)


def test_video_frame_pixel_colors(client: Ultimate64Client) -> None:
    """Verify pixel values are valid VIC-II colour indices (0-15)."""
    local = _local_ip()
    cap = VideoCapture(port=11000)
    cap.start()
    try:
        client.stream_video_start(f"{local}:11000")
        time.sleep(0.5)
    finally:
        try:
            client.stream_video_stop()
        except Exception:
            pass
        time.sleep(0.3)
        result = cap.stop()

    assert result.frames_completed > 0, "No frames captured"
    frame = result.frames[0]

    # All pixels must be in 0-15 range
    pixel_set = set(frame.pixels)
    for p in pixel_set:
        assert 0 <= p <= 15, f"Invalid pixel colour index: {p}"

    # Screen should have some content -- not all same colour
    assert len(pixel_set) > 1, (
        f"All pixels are the same colour ({pixel_set.pop()}); "
        "expected some variation on screen"
    )
    logger.info("Unique pixel colours: %s", sorted(pixel_set))


# ======================================================================
# Data streams config tests
# ======================================================================


def test_data_streams_config_readable(client: Ultimate64Client) -> None:
    """Verify get_data_streams_config returns a dict with expected keys."""
    config = get_data_streams_config(client)
    assert isinstance(config, dict), f"Expected dict, got {type(config).__name__}"
    # Expect at least these items
    expected_keys = {
        "Stream VIC to",
        "Stream Audio to",
        "Stream Debug to",
        "Debug Stream Mode",
    }
    missing = expected_keys - set(config.keys())
    assert not missing, f"Missing expected config keys: {missing}"
    for key, value in config.items():
        logger.info("Data Streams: %s = %s", key, value)


def test_set_stream_destination_roundtrip(client: Ultimate64Client) -> None:
    """Save original debug destination, set test value, read back, restore."""
    config = get_data_streams_config(client)
    orig_dest = config.get("Stream Debug to", "")
    logger.info("Original debug stream destination: %s", orig_dest)

    test_dest = "192.168.99.99:11002"
    try:
        set_stream_destination(client, "debug", test_dest)
        updated = get_data_streams_config(client)
        new_dest = updated.get("Stream Debug to", "")
        assert new_dest == test_dest, (
            f"Expected '{test_dest}', got '{new_dest}'"
        )
        logger.info("Successfully set debug destination to: %s", new_dest)
    finally:
        set_stream_destination(client, "debug", orig_dest)
        restored = get_data_streams_config(client)
        logger.info(
            "Restored debug destination to: %s",
            restored.get("Stream Debug to", ""),
        )
