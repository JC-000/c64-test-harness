"""Tests for ViceConfig and ViceProcess (backends/vice_lifecycle.py)."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess


def test_default_values():
    cfg = ViceConfig()
    assert cfg.executable == "x64sc"
    assert cfg.prg_path == ""
    assert cfg.port == 6510
    assert cfg.warp is True
    assert cfg.ntsc is True
    assert cfg.sound is False
    assert cfg.extra_args == []


def test_custom_values():
    cfg = ViceConfig(
        executable="x128",
        prg_path="game.prg",
        port=7000,
        warp=False,
        ntsc=False,
        sound=True,
        extra_args=["-VICIIfilter", "0"],
    )
    assert cfg.executable == "x128"
    assert cfg.prg_path == "game.prg"
    assert cfg.port == 7000
    assert cfg.warp is False
    assert cfg.ntsc is False
    assert cfg.sound is True
    assert cfg.extra_args == ["-VICIIfilter", "0"]


def test_disk_image_default_none():
    cfg = ViceConfig()
    assert cfg.disk_image is None


def test_drive_unit_default():
    cfg = ViceConfig()
    assert cfg.drive_unit == 8


def test_not_frozen():
    """ViceConfig is a regular (non-frozen) dataclass — fields are mutable."""
    cfg = ViceConfig()
    cfg.port = 9999
    assert cfg.port == 9999


class TestWaitForMonitor:
    def test_early_exit_returns_false_fast(self):
        """wait_for_monitor fails fast when VICE process has already exited."""
        proc = ViceProcess(ViceConfig(port=19900))
        # Simulate a process that already exited with rc=1
        mock_popen = MagicMock()
        mock_popen.poll.return_value = 1  # already exited
        proc._proc = mock_popen
        # Should return False almost immediately, not wait 30s
        import time
        start = time.monotonic()
        result = proc.wait_for_monitor(timeout=30.0)
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 2.0  # Should be near-instant, not 30s

    def test_no_proc_still_polls(self):
        """wait_for_monitor polls normally when _proc is None."""
        proc = ViceProcess(ViceConfig(port=19901))
        proc._proc = None
        import time
        start = time.monotonic()
        result = proc.wait_for_monitor(timeout=2.0)
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed >= 1.5  # Should wait near full timeout
