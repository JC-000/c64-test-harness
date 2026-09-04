"""High-level SID audio capture from Ultimate 64 hardware.

Start the U64 audio stream, play a SID tune, capture UDP packets for
a given duration, then write the result to a WAV file.  This is the
U64 equivalent of :mod:`render_wav` (which drives VICE).

Which entry point
-----------------
:func:`capture_sid_u64` owns the whole run: it hands a ``.sid`` to the
firmware's player and, by default, resets the C64 afterwards to stop the
tune.  That reset makes it **unusable around a program the host is
driving itself** -- a run reached through a keyboard or handshake
sequence is destroyed by it (issue #196).

For that case use :func:`capture_u64_audio`, a context manager that
brings the UDP stream up and down around whatever the caller does and
touches nothing else, or drive
:class:`~c64_test_harness.backends.u64_audio_capture.AudioCapture`
directly.  ``capture_sid_u64(..., reset_after=False)`` is the same
escape hatch when the firmware SID player *is* what you want but the
reset is not.

Public API
----------
- ``capture_sid_u64()`` -- one-call SID capture
- ``capture_u64_audio()`` -- capture around an arbitrary run
- ``U64CaptureResult`` -- result dataclass
"""

from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator

from .u64_audio_capture import (
    AudioCapture,
    CaptureResult,
    DEFAULT_AUDIO_PORT,
    DEFAULT_SAMPLE_RATE,
)
from .ultimate64_client import Ultimate64Client

__all__ = [
    "U64CaptureResult",
    "capture_sid_u64",
    "capture_u64_audio",
]

logger = logging.getLogger(__name__)


@dataclass
class U64CaptureResult:
    """Outcome of a :func:`capture_sid_u64` call."""

    wav_path: Path
    duration_seconds: float
    sample_rate: int
    total_samples: int
    packets_received: int
    packets_dropped: int
    sample_rate_exact: Fraction | None = None

    @property
    def time_base_intact(self) -> bool:
        """False when a dropped packet broke the index-to-time mapping.

        See :attr:`c64_test_harness.backends.u64_audio_capture.
        CaptureResult.time_base_intact` -- gaps are counted, never
        padded.
        """
        return self.packets_dropped == 0


def _detect_local_ip(remote_host: str, remote_port: int = 80) -> str:
    """Determine which local IP address can reach *remote_host*.

    Uses a UDP connect (no actual traffic) to let the OS pick the
    right source interface.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]


def capture_sid_u64(
    client: Ultimate64Client,
    sid: "SidFile",  # noqa: F821 — avoid circular import
    out_wav: str | Path,
    duration_seconds: float,
    song: int = 0,
    sample_rate: int | float | Fraction = DEFAULT_SAMPLE_RATE,
    listen_port: int = DEFAULT_AUDIO_PORT,
    listen_addr: str = "",
    stream_destination: str | None = None,
    settle_time: float = 0.3,
    reset_after: bool = True,
) -> U64CaptureResult:
    """Capture SID audio from an Ultimate 64 to a WAV file.

    Parameters
    ----------
    client:
        Connected :class:`Ultimate64Client` instance.
    sid:
        Parsed :class:`SidFile` to play.
    out_wav:
        Destination WAV file path.
    duration_seconds:
        How long to record (in seconds), excluding *settle_time*.
    song:
        Sub-song number (0-based).
    sample_rate:
        Rate the capture is timed against, and (rounded) the WAV header
        value.  The default 48000 is the *nominal* rate and is 1244 ppm
        away from the device's real NTSC rate; pass
        :data:`~c64_test_harness.backends.u64_audio_capture.
        U64_NTSC_AUDIO_RATE_HZ` for timing-sensitive work.
    listen_port:
        Local UDP port to receive audio packets on.
    listen_addr:
        Local address to bind the UDP socket to (empty = all interfaces).
    stream_destination:
        ``"host:port"`` string sent to the U64 to direct the audio stream.
        If *None*, auto-detect the local IP that can reach the U64 and
        combine it with *listen_port*.
    settle_time:
        Seconds to wait after starting playback before the timed capture
        window begins.  Allows the audio stream to stabilise.
    reset_after:
        Reset the C64 on the way out, which is what stops the firmware
        SID player.  Set False when the machine is running something the
        caller set up and the reset would destroy it -- but note that the
        tune then keeps playing.  For capturing around a host-driven
        program, prefer :func:`capture_u64_audio`, which never resets and
        never starts a player.

    Returns
    -------
    U64CaptureResult
        Contains the output path and capture statistics.  Check
        ``time_base_intact`` before analysing: a dropped packet is not
        padded, and the WAV looks fine regardless.

    Raises
    ------
    RuntimeError
        If the output WAV is missing or empty after capture.
    """
    out_wav = Path(out_wav)

    # --- auto-detect stream destination ---
    if stream_destination is None:
        local_ip = _detect_local_ip(client.host)
        stream_destination = f"{local_ip}:{listen_port}"
        logger.info("Auto-detected stream destination: %s", stream_destination)

    capture = AudioCapture(
        port=listen_port,
        sample_rate=sample_rate,
        bind_addr=listen_addr,
    )

    stream_started = False
    capture_started = False

    try:
        # 1. Start the UDP receiver
        capture.start()
        capture_started = True
        logger.info("Audio capture started on port %d", listen_port)

        # 2. Tell the U64 to stream audio to us
        client.stream_audio_start(stream_destination)
        stream_started = True
        logger.info("U64 audio stream started -> %s", stream_destination)

        # 3. Play the SID
        client.sid_play(sid.raw, songnr=song)
        logger.info(
            "Playing SID '%s' song %d for %.1fs (settle %.1fs)",
            sid.name, song, duration_seconds, settle_time,
        )

        # 4. Wait for audio to settle, then capture for the requested duration
        if settle_time > 0:
            time.sleep(settle_time)
        time.sleep(duration_seconds)

    finally:
        # Always clean up in reverse order
        if stream_started:
            try:
                client.stream_audio_stop()
                logger.info("U64 audio stream stopped")
            except Exception:
                logger.warning("Failed to stop U64 audio stream", exc_info=True)

        if capture_started:
            try:
                result = capture.stop(wav_path=out_wav)
            except Exception:
                logger.warning("Failed to stop audio capture", exc_info=True)
                # Re-raise after reset attempt below
                if reset_after:
                    try:
                        client.reset()
                    except Exception:
                        logger.warning("Failed to reset C64", exc_info=True)
                raise

        if reset_after:
            try:
                client.reset()
                logger.info("C64 reset to stop SID playback")
            except Exception:
                logger.warning("Failed to reset C64", exc_info=True)
        else:
            logger.info(
                "reset_after=False: leaving the machine running; the SID "
                "player is still going."
            )

    # Validate output
    if not out_wav.exists():
        raise RuntimeError(f"WAV file was not created: {out_wav}")
    if out_wav.stat().st_size == 0:
        raise RuntimeError(f"WAV file is empty: {out_wav}")

    logger.info(
        "Capture complete: %s (%.2fs, %d packets, %d dropped)",
        out_wav,
        result.duration_seconds,
        result.packets_received,
        result.packets_dropped,
    )

    return _to_u64_result(result)


def _to_u64_result(result: CaptureResult) -> U64CaptureResult:
    """Adapt a low-level :class:`CaptureResult` to this module's type."""
    return U64CaptureResult(
        wav_path=result.wav_path,
        duration_seconds=result.duration_seconds,
        sample_rate=result.sample_rate,
        total_samples=result.total_samples,
        packets_received=result.packets_received,
        packets_dropped=result.packets_dropped,
        sample_rate_exact=result.sample_rate_exact,
    )


@contextmanager
def capture_u64_audio(
    client: Ultimate64Client,
    out_wav: str | Path | None = None,
    *,
    sample_rate: int | float | Fraction = DEFAULT_SAMPLE_RATE,
    listen_port: int = DEFAULT_AUDIO_PORT,
    listen_addr: str = "",
    stream_destination: str | None = None,
    settle_time: float = 0.3,
) -> Iterator[list[U64CaptureResult]]:
    """Capture U64 audio around an arbitrary run.

    The primitive :func:`capture_sid_u64` is not: it owns the run, starts
    the firmware's SID player, and resets the machine on the way out.
    This one starts the UDP receiver and the device's audio stream, hands
    control back, and on exit stops both and writes the WAV.  It never
    resets, never loads anything, and never touches the running program
    -- so it composes with a host-driven run (issue #196).

    The context variable is a one-element-at-exit list; the result is
    appended when the block finishes, so::

        with capture_u64_audio(client, "run.wav") as captured:
            target.jsr(0xC000)
            target.wait_for_text("DONE")
        result = captured[0]
        assert result.time_base_intact

    The stream is stopped even when the block raises, and the WAV is
    still written -- a partial capture of a failed run is usually the
    thing you want to look at.

    :param client: Connected Ultimate64 client.
    :param out_wav: Where to write the WAV.  ``None`` captures without
        writing a file (the result still carries the statistics).
    :param sample_rate: See :func:`capture_sid_u64`.
    :param listen_port: Local UDP port to receive on.
    :param listen_addr: Local bind address (empty = all interfaces).
    :param stream_destination: ``"host:port"`` for the device to stream
        to.  ``None`` auto-detects the local IP that reaches the U64.
    :param settle_time: Seconds to wait after the stream starts before
        yielding, so the block does not begin mid-handshake.
    :yields: A list that receives the :class:`U64CaptureResult` on exit.
    """
    if stream_destination is None:
        local_ip = _detect_local_ip(client.host)
        stream_destination = f"{local_ip}:{listen_port}"
        logger.info("Auto-detected stream destination: %s", stream_destination)

    capture = AudioCapture(
        port=listen_port,
        sample_rate=sample_rate,
        bind_addr=listen_addr,
    )
    results: list[U64CaptureResult] = []
    stream_started = False

    capture.start()
    try:
        client.stream_audio_start(stream_destination)
        stream_started = True
        logger.info("U64 audio stream started -> %s", stream_destination)
        if settle_time > 0:
            time.sleep(settle_time)
        yield results
    finally:
        if stream_started:
            try:
                client.stream_audio_stop()
            except Exception:
                logger.warning("Failed to stop U64 audio stream", exc_info=True)
        result = capture.stop(wav_path=out_wav)
        results.append(_to_u64_result(result))
