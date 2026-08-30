"""A launch must not inherit the operator's ``~/.config/vice/vicerc``.

Nothing used to pass ``-default``, so ``loadconfig`` stayed true
(S ``main.c:206,388``) and every VICE the harness started absorbed
whatever the operator happened to have configured.  Consumers of this
harness assert on machine state — turbo, REU, video standard, drive type
— so an inherited resource is a correctness problem, not untidiness.

It is also a **crash**.  Measured on both x64sc builds on this bench,
with ``HOME`` redirected at a directory containing a vicerc:

===========================================  ==================
launch                                       result
===========================================  ==================
``-console``, vicerc, no ``-default``        SIGSEGV (rc=-11)
``-console``, vicerc, with ``-default``      runs
windowed, vicerc, no ``-default``            runs
``-console``, no vicerc                      runs
===========================================  ==================

The content of the vicerc is irrelevant — a file holding nothing but
``Speed=50`` is enough.  stderr fills with ``Gtk-CRITICAL
_gtk_style_provider_private_get_settings: assertion
'GTK_IS_STYLE_PROVIDER_PRIVATE (provider)' failed`` first, so
``resources_load()`` is reaching UI state that console mode never built.

That makes the crash console-mode-specific, which is why it stayed
invisible: while ``-console`` was positionally broken every launch was
really a windowed launch, and windowed launches survive a vicerc.  Fixing
``-console`` without also passing ``-default`` would turn "silently opens
a window" into "segfaults on startup" for any operator who has ever
opened VICE's settings dialog.

These tests redirect ``HOME`` through ``ViceConfig.env`` rather than
writing to the real ``~/.config/vice/``, so they are hermetic and never
touch the operator's configuration.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket

import pytest
from conftest import connect_binary_transport

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess

pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)


#: Every value here is deliberately *not* what the harness intends, so a
#: resource reading back as one of these proves contamination.
CONTAMINANT_VICERC = """[C64SC]
SaveResourcesOnExit=1
AutostartWarp=0
AutostartPrgMode=2
Sound=1
MachineVideoStandard=1
JAMAction=0
Speed=50
SoundEmulateOnWarp=0
Drive8Type=1571
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def home_with_vicerc(tmp_path: pathlib.Path, body: str) -> dict[str, str]:
    """An environment whose ``HOME`` holds a vicerc containing *body*."""
    vice_dir = tmp_path / ".config" / "vice"
    vice_dir.mkdir(parents=True, exist_ok=True)
    (vice_dir / "vicerc").write_text(body)
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    return env


def resources_after_launch(cfg: ViceConfig, *names: str) -> dict[str, int | str]:
    proc = ViceProcess(cfg)
    proc.start()
    try:
        transport = connect_binary_transport(cfg.port, proc=proc, timeout=25.0)
        try:
            return {name: transport.resource_get(name) for name in names}
        finally:
            transport.close()
    finally:
        proc.stop()


def test_ambient_vicerc_is_read_when_the_harness_asks_for_it(tmp_path):
    """Control: prove the redirected ``HOME`` is genuinely in play.

    Without this, a redirection that VICE silently ignored — wrong path,
    wrong section name, ``HOME`` not consulted on this platform — would
    make every assertion below pass for the wrong reason.

    ``load_user_config=True`` suppresses ``-default``, so VICE loads the
    vicerc and dies on it.  A launch that survives here means the file
    was never read, and the neutralisation tests prove nothing.
    """
    cfg = ViceConfig(
        port=free_port(),
        env=home_with_vicerc(tmp_path, CONTAMINANT_VICERC),
        load_user_config=True,
    )
    proc = ViceProcess(cfg)
    proc.start()
    try:
        with pytest.raises(Exception):
            connect_binary_transport(cfg.port, proc=proc, timeout=20.0).close()
    finally:
        proc.stop()


def test_default_neutralises_an_ambient_vicerc(tmp_path):
    """The vicerc must not reach the emulator at all.

    Each expected value below is the harness's own intent, and differs
    from the contaminant, so this cannot pass by coincidence.
    """
    cfg = ViceConfig(
        port=free_port(),
        env=home_with_vicerc(tmp_path, CONTAMINANT_VICERC),
        warp=True,
        ntsc=True,
        sound=False,
    )
    got = resources_after_launch(
        cfg,
        "SaveResourcesOnExit",
        "AutostartWarp",
        "AutostartPrgMode",
        "Sound",
        "MachineVideoStandard",
        "JAMAction",
        "Speed",
        "SoundEmulateOnWarp",
        "Drive8Type",
    )
    assert got == {
        # Never rewrite the operator's vicerc on exit.
        "SaveResourcesOnExit": 0,
        # Follows cfg.warp instead of running warped regardless.
        "AutostartWarp": 1,
        # Inject, uniformly on every platform.
        "AutostartPrgMode": 1,
        # cfg.sound is False.
        "Sound": 0,
        # NTSC, from cfg.ntsc.
        "MachineVideoStandard": 2,
        # Continue on a CPU jam; never open a dialog in a headless run.
        "JAMAction": 1,
        "Speed": 100,
        # Keep emulating the SID under warp, or render_wav() records
        # silence.
        "SoundEmulateOnWarp": 1,
        "Drive8Type": 1542,
    }


def test_autostart_warp_follows_cfg_warp(tmp_path):
    """``AutostartWarp`` defaults to 1 (S ``autostart.c:413``).

    So autostart ran warped even for a caller that explicitly asked for
    ``warp=False`` — a real timing difference, not a cosmetic one.
    """
    cfg = ViceConfig(port=free_port(), warp=False)
    got = resources_after_launch(cfg, "AutostartWarp")
    assert got["AutostartWarp"] == 0


def test_pal_is_selected_when_ntsc_is_false(tmp_path):
    """``ntsc=False`` used to emit nothing and inherit the vicerc's standard.

    Cycle counts and TOD rates differ between PAL and NTSC, and
    ``tod_timer.py`` calibrates against them.  The vicerc here asks for
    NTSC so the assertion cannot be satisfied by the factory default.
    """
    cfg = ViceConfig(
        port=free_port(),
        ntsc=False,
        env=home_with_vicerc(tmp_path, "[C64SC]\nMachineVideoStandard=2\n"),
    )
    got = resources_after_launch(cfg, "MachineVideoStandard")
    assert got["MachineVideoStandard"] == 1  # PAL


def test_sound_true_actually_enables_sound(tmp_path):
    """``sound=True`` used to emit no flag at all and inherit ``Sound``.

    The vicerc disables sound, so an inherited value would lose here.
    """
    cfg = ViceConfig(
        port=free_port(),
        sound=True,
        env=home_with_vicerc(tmp_path, "[C64SC]\nSound=0\n"),
    )
    got = resources_after_launch(cfg, "Sound")
    assert got["Sound"] == 1


def test_autostart_prg_mode_is_inject_on_every_platform(tmp_path, monkeypatch):
    """The macOS-only ``-autostartprgmode 1`` made Linux take another path.

    Factory default is 2/Disk (S ``autostart-prg.h:45``); the harness
    wants 1/Inject, and wants it identically everywhere.  ``sys.platform``
    is forced to ``"linux"`` so this exercises the branch that used to
    emit nothing — on an unpatched macOS run the old code happened to be
    right, which is exactly why the divergence went unnoticed.
    """
    monkeypatch.setattr(
        "c64_test_harness.backends.vice_lifecycle.sys.platform", "linux"
    )
    prg = tmp_path / "empty.prg"
    prg.write_bytes(bytes([0x01, 0x08, 0x00, 0x00, 0x00]))
    cfg = ViceConfig(
        port=free_port(),
        prg_path=str(prg),
        env=home_with_vicerc(tmp_path, "[C64SC]\nAutostartPrgMode=2\n"),
    )
    got = resources_after_launch(cfg, "AutostartPrgMode")
    assert got["AutostartPrgMode"] == 1


def test_saveres_stops_a_vicerc_run_rewriting_the_operators_settings(tmp_path):
    """``+saveres`` must win over a vicerc that asks to save on exit.

    ``-default`` already makes this unreachable in a normal run, so the
    only way to exercise the flag is a launch that *does* read the
    vicerc.  That combination segfaults in console mode, hence
    ``console=False``: command-line options are parsed after
    ``resources_load()`` (S ``main.c:390`` then ``:421``), so the flag
    overrides the file either way.

    Without this test ``+saveres`` is unfalsifiable — removing it entirely
    leaves every other assertion in this module green, because the factory
    value is already 0.
    """
    cfg = ViceConfig(
        port=free_port(),
        console=False,
        minimize=True,
        load_user_config=True,
        env=home_with_vicerc(tmp_path, "[C64SC]\nSaveResourcesOnExit=1\n"),
    )
    got = resources_after_launch(cfg, "SaveResourcesOnExit")
    assert got["SaveResourcesOnExit"] == 0
