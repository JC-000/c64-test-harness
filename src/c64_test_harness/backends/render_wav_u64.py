"""High-level SID audio capture from Ultimate 64 hardware.

Start the U64 audio stream, play a SID tune, capture UDP packets for
a given duration, then write the result to a WAV file.  This is the
U64 equivalent of :mod:`render_wav` (which drives VICE).

Public API
----------
- ``capture_sid_u64()`` -- one-call SID capture
- ``U64CaptureResult`` -- result dataclass
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .u64_audio_capture import AudioCapture, DEFAULT_AUDIO_PORT, DEFAULT_SAMPLE_RATE
from .ultimate64_client import Ultimate64Client

__all__ = [
    "U64CaptureResult",
    "capture_sid_u64",
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
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    listen_port: int = DEFAULT_AUDIO_PORT,
    listen_addr: str = "",
    stream_destination: str | None = None,
    settle_time: float = 0.3,
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
        Expected sample rate from the U64 stream (default 48000).
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

    Returns
    -------
    U64CaptureResult
        Contains the output path and capture statistics.

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
                try:
                    client.reset()
                except Exception:
                    logger.warning("Failed to reset C64", exc_info=True)
                raise

        try:
            client.reset()
            logger.info("C64 reset to stop SID playback")
        except Exception:
            logger.warning("Failed to reset C64", exc_info=True)

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

    return U64CaptureResult(
        wav_path=result.wav_path,
        duration_seconds=result.duration_seconds,
        sample_rate=result.sample_rate,
        total_samples=result.total_samples,
        packets_received=result.packets_received,
        packets_dropped=result.packets_dropped,
    )
