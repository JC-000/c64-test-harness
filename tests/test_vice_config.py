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
    assert cfg.port == 6502
    assert cfg.warp is True
    assert cfg.ntsc is True
    assert cfg.sound is False
    assert cfg.console is True
    assert cfg.minimize is True
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




def _launch_args(cfg: ViceConfig) -> list[str]:
    """Build the x64sc argv for *cfg* without spawning anything."""
    import subprocess
    from unittest import mock
    from c64_test_harness.backends.vice_lifecycle import ViceProcess

    captured: list[list[str]] = []

    def fake_popen(args, **kw):
        captured.append(list(args))
        raise RuntimeError("stop before spawn")

    with mock.patch.object(subprocess, "Popen", fake_popen):
        try:
            ViceProcess(cfg).start()
        except RuntimeError:
            pass
    assert captured, "Popen was not reached"
    return captured[0]


def test_console_default_emits_console_not_minimized():
    args = _launch_args(ViceConfig(executable="x64sc"))
    assert "-console" in args
    assert "-minimized" not in args


def test_console_false_falls_back_to_minimized():
    args = _launch_args(ViceConfig(executable="x64sc", console=False))
    assert "-console" not in args
    assert "-minimized" in args


def test_console_false_minimize_false_emits_neither():
    args = _launch_args(ViceConfig(executable="x64sc", console=False, minimize=False))
    assert "-console" not in args
    assert "-minimized" not in args
