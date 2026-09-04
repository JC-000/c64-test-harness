"""Batch WAV audio capture from VICE.

Launch x64sc, autostart a .prg, record audio to a WAV file for a
specified duration via ``-limitcycles``, then cleanly shut down.

Two VICE settings silently produce a well-formed, empty capture rather
than an error, so both are forced/refused here (issue #196):

**Warp must be off.**  ``sound_flush()`` throws the sample buffer away
when warp is on and no record device is configured (S ``sound.c:1528``:
``snddata.bufptr = 0``), and configuring one does *not* rescue it --
the loop that writes to the play and record devices is
``while (!warp_mode_enabled)`` (S ``sound.c:1573-1613``), so under warp
it never runs and ``snddata.bufptr -= nr`` drops the samples anyway.
``-soundwarpmode 1`` only keeps the SID *emulated* under warp; it does
not make the audio reach a device.  ``render_wav`` therefore sets
``warp=False`` and refuses a ``-warp`` smuggled in through
``extra_args``.

**The volume must not be zero.**  At ``-soundvolume 0`` VICE ``memset``s
the sample buffer before it reaches any device (S ``sound.c:1441-1449``),
so the WAV is silence.  (Volume 0 is still safe for *register*-domain
measurement -- see :attr:`ViceConfig.soundvolume` and
:func:`~c64_test_harness.backends.vice_lifecycle.headless_sid_config`.)

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


#: ``extra_args`` entries that would re-enable warp behind
#: ``render_wav``'s ``warp=False``.  VICE's cmdline parser takes the last
#: setting of a resource, and ``extra_args`` is appended after the flags
#: this module emits, so one of these wins.
_WARP_ENABLING_ARGS = frozenset({"-warp", "-warp=1", "+warp=0"})


def _reject_silent_capture(config: ViceConfig | None) -> None:
    """Refuse a base config that would yield a valid, empty WAV.

    Both failures are silent in VICE: warp discards every sample, and
    volume 0 zeroes the buffer.  Either way the render "succeeds", the
    file is a well-formed WAV, and the analysis downstream fits a model
    to silence (issue #196).

    :param config: The caller's base ``ViceConfig``, or ``None``.
    :raises ValueError: When the config would silence the capture.
    """
    if config is None:
        return
    smuggled = [a for a in config.extra_args if a in _WARP_ENABLING_ARGS]
    if smuggled:
        raise ValueError(
            f"render_wav() cannot record under warp: extra_args contains "
            f"{smuggled!r}, which re-enables warp after render_wav sets "
            f"warp=False. VICE discards the sample buffer under warp "
            f"(S sound.c:1528 / 1573), so the WAV would be well-formed "
            f"and empty. Remove it, or capture on hardware."
        )
    if config.soundvolume == 0:
        raise ValueError(
            "render_wav() cannot record at soundvolume=0: VICE memsets the "
            "sample buffer to zero before it reaches the sound device "
            "(S sound.c:1441-1449), so the WAV would be silence. Volume 0 "
            "is only safe for register-domain measurement "
            "(headless_sid_config)."
        )


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
    ValueError
        If *config* would silently produce an empty capture: a ``-warp``
        in ``extra_args``, or ``soundvolume=0``.  See the module
        docstring.
    RuntimeError
        If the output WAV is missing or empty after VICE exits.
    subprocess.TimeoutExpired
        If VICE does not exit within *timeout*.
    """
    prg_path = Path(prg_path)
    out_wav = Path(out_wav)

    if not prg_path.exists():
        raise FileNotFoundError(f"PRG file not found: {prg_path}")

    _reject_silent_capture(config)

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
        warp=False,
        ntsc=not pal,
        sound=True,
        monitor=False,
        minimize=True,
        extra_args=[
            "+autostart-warp",
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

    return RenderResult(
        wav_path=out_wav,
        pid=pid,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        cycles=cycles,
        sample_rate=sample_rate,
    )
