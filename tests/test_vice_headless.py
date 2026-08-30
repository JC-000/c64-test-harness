"""``-console`` must actually suppress the GUI, not merely appear in argv.

These tests measure the *effect* of the flag — whether the launched
emulator registers with LaunchServices as a GUI application — rather
than asserting on the argv string.  That distinction is the whole point
of the module: the previous generation of these tests asserted
``"-console" in args`` against a mocked ``Popen`` and stayed green for
months while every launch on macOS still opened a window.

Why the position of the flag matters (VICE 3.10 source):

``main.c:267-303`` scans argv *before* the UI is initialised, looking for
``-console``, ``-default``, ``-config``, ``-seed`` and the logging
flags.  That scan ``break``\\ s at the first argument it does not
recognise.  ``ui_init_with_args`` (``main.c:385``) is gated on the
``console_mode`` flag that only this early scan sets.  There is a second,
late handler registered through ``initcmdline.c:307`` and fired at
``main.c:421``, but by then the UI is already up.

So ``-console`` suppresses the window only when nothing unrecognised
precedes it.  The harness used to emit it after ``-autostart`` / ``-warp``,
which meant the scan broke on ``-autostart`` and the flag was handled far
too late.  On native macOS that does not crash — it silently opens the
window, which is exactly what the flag exists to prevent.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import time

import pytest

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess

pytestmark = [
    pytest.mark.skipif(
        shutil.which("x64sc") is None, reason="x64sc not found on PATH"
    ),
    pytest.mark.skipif(
        platform.system() != "Darwin",
        reason="the LaunchServices GUI probe is macOS-only",
    ),
]


def gui_app_count(pid: int) -> int:
    """How many GUI applications LaunchServices knows *pid* as (0 or 1).

    A GTK3 process that creates its window registers with LaunchServices
    and acquires a display name; a ``-console`` process never builds the
    UI and stays unregistered, so ``lsappinfo`` reports the name as
    ``[ NULL ]``.  This is the observable difference between a windowed
    and a headless launch, and it needs no extra Python dependency and no
    accessibility permission.

    Measured on this bench across repeated launches: windowed →
    ``"LSDisplayName"="x64sc"``, headless → ``"LSDisplayName"=[ NULL ]``.
    """
    try:
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "name", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - bench-dependent
        pytest.skip("lsappinfo unavailable")
    text = out.stdout.strip()
    if not text or "[ NULL ]" in text:
        return 0
    return 1


def free_port() -> int:
    """An unused localhost TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_monitor(port: int, timeout: float = 30.0) -> None:
    """Block until the binary monitor accepts a connection on *port*.

    The GUI, if one is going to appear, is up by the time the monitor
    listens — ``ui_init_with_args`` runs before the monitor is opened —
    so this is a sound point at which to probe for a window.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise AssertionError(f"binary monitor never came up on port {port}")


# The settle window after the monitor answers.  LaunchServices registration
# is asynchronous, so a windowed launch needs a moment to become visible to
# lsappinfo.  Generous on purpose: a too-short wait would make the windowed
# case look headless and turn this into a vacuous pass.
_LS_SETTLE = 3.0


def _gui_count_for(cfg: ViceConfig) -> int:
    """Launch *cfg*, wait for it to be fully up, return its GUI app count."""
    proc = ViceProcess(cfg)
    proc.start()
    try:
        wait_for_monitor(cfg.port)
        time.sleep(_LS_SETTLE)
        pid = proc.resolve_vice_pid()
        assert pid is not None, "VICE exited before it could be probed"
        return gui_app_count(pid)
    finally:
        proc.stop()


def test_console_launch_creates_no_gui_app():
    """A default ``console=True`` launch must open no window.

    This is the regression test for the defect: with ``-console`` emitted
    after ``-warp``, VICE's early scan breaks on ``-warp`` and the window
    is created anyway.
    """
    cfg = ViceConfig(port=free_port(), console=True)
    assert _gui_count_for(cfg) == 0


def test_console_launch_with_autostart_creates_no_gui_app(tmp_path):
    """Same, with ``-autostart`` in play — the harness's real-world shape.

    ``prg_path`` puts ``-autostart <path>`` at the very front of argv,
    which is what broke the early scan in the field.
    """
    prg = tmp_path / "empty.prg"
    # $0801 BASIC start address, then an immediate end-of-program marker.
    prg.write_bytes(bytes([0x01, 0x08, 0x00, 0x00, 0x00]))
    cfg = ViceConfig(port=free_port(), console=True, prg_path=str(prg))
    assert _gui_count_for(cfg) == 0


def test_windowed_launch_does_create_a_gui_app():
    """The probe's positive control.

    Without this, ``gui_app_count`` returning a constant 0 — a broken
    probe, a missing ``lsappinfo``, a changed output format — would make
    both tests above pass vacuously.  ``console=False`` must be seen as a
    GUI app, or the measurement itself is worthless.
    """
    cfg = ViceConfig(port=free_port(), console=False, minimize=True)
    assert _gui_count_for(cfg) == 1
