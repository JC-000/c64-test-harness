"""Batch WAV audio capture from VICE.

Launch x64sc, autostart a .prg, record audio to a WAV file for a
specified duration via ``-limitcycles``, then cleanly shut down.

Public API
----------
- ``render_wav()`` — high-level one-call render
- ``RenderResult`` — result dataclass
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .vice_lifecycle import ViceConfig, ViceProcess
from .vice_manager import PortAllocator

logger = logging.getLogger(__name__)

PAL_CLOCK_HZ = 985248
NTSC_CLOCK_HZ = 1022727


@dataclass
class RenderResult:
    """Outcome of a ``render_wav()`` call."""

    wav_path: Path
    pid: int | None
    exit_code: int
    duration_seconds: float
    cycles: int
    sample_rate: int


def render_wav(
    prg_path: str | Path,
    out_wav: str | Path,
    duration_seconds: float,
    sample_rate: int = 44100,
    mono: bool = True,
    pal: bool = True,
    config: ViceConfig | None = None,
    timeout: float | None = None,
) -> RenderResult:
    """Record audio from a C64 .prg to a WAV file via VICE.

    Parameters
    ----------
    prg_path:
        Path to the .prg file to autostart.
    out_wav:
        Destination WAV file path.
    duration_seconds:
        How long to record (in seconds).
    sample_rate:
        WAV sample rate (default 44100).
    mono:
        If True, record mono (1 channel); else stereo (2 channels).
    pal:
        If True, use PAL clock rate; else NTSC.
    config:
        Optional base ``ViceConfig`` to inherit executable/extra_args from.
        Sound, cycle, and port fields are overridden.
    timeout:
        Max wall-clock seconds to wait for VICE to finish.
        Default: ``max(30.0, duration_seconds * 1.5 + 20.0)``.

    Returns
    -------
    RenderResult
        Contains the output path, VICE PID, exit code, and timing info.

    Raises
    ------
    FileNotFoundError
        If *prg_path* does not exist.
    RuntimeError
        If the output WAV is missing or empty after VICE exits.
    subprocess.TimeoutExpired
        If VICE does not exit within *timeout*.
    """
    prg_path = Path(prg_path)
    out_wav = Path(out_wav)

    if not prg_path.exists():
        raise FileNotFoundError(f"PRG file not found: {prg_path}")

    clock_hz = PAL_CLOCK_HZ if pal else NTSC_CLOCK_HZ
    cycles = int(round(duration_seconds * clock_hz))

    if timeout is None:
        timeout = max(30.0, duration_seconds * 1.5 + 20.0)

    base = config or ViceConfig()

    # Build headless environment
    env = os.environ.copy()
    env["SDL_VIDEODRIVER"] = "dummy"

    cfg = ViceConfig(
        executable=base.executable,
        prg_path=str(prg_path),
        port=0,  # placeholder, will be set from allocator
        warp=False,
        ntsc=not pal,
        sound=True,
        minimize=True,
        extra_args=[
            "+autostart-warp",
            "+binarymonitor",
            "+remotemonitor",
            "+saveres",
        ] + list(base.extra_args),
        sounddev="wav",
        soundarg=str(out_wav),
        soundrate=sample_rate,
        soundoutput=1 if mono else 2,
        limit_cycles=cycles,
        env=env,
    )

    allocator = PortAllocator()
    port = allocator.allocate()
    cfg.port = port

    # Take the file lock BEFORE closing the reservation socket.
    # The file lock bridges the gap between socket close and VICE
    # binding, preventing other processes from stealing the port.
    port_lock = allocator.take_lock(port)

    # Release reservation socket so VICE can bind if needed
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    proc = ViceProcess(cfg)
    pid: int | None = None
    exit_code = -1

    try:
        proc.start()
        pid = proc.pid

        logger.info(
            "VICE PID %s rendering %s -> %s (%d cycles, %.1fs)",
            pid, prg_path.name, out_wav, cycles, duration_seconds,
        )

        exit_code = proc.wait_for_exit(timeout=timeout)

        # x64sc returns 1 when -limitcycles is hit — this is normal
        if exit_code not in (0, 1):
            logger.warning("VICE exited with unexpected code %d", exit_code)

        # Validate output
        if not out_wav.exists():
            raise RuntimeError(
                f"VICE exited but WAV file was not created: {out_wav}"
            )
        if out_wav.stat().st_size == 0:
            raise RuntimeError(
                f"VICE created an empty WAV file: {out_wav}"
            )

    except Exception:
        proc.stop()
        raise
    finally:
        if port_lock is not None:
            port_lock.release()
        allocator.release(port)

    return RenderResult(
        wav_path=out_wav,
        pid=pid,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        cycles=cycles,
        sample_rate=sample_rate,
    )
