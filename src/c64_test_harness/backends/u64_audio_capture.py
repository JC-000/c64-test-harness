"""UDP audio capture from Ultimate 64 audio stream.

The U64 streams 16-bit signed stereo PCM at ~48 kHz over UDP.
Each packet: 2-byte LE sequence number + raw PCM sample data.

Public API
----------
- ``AudioCapture`` — background-thread UDP receiver
- ``CaptureResult`` — result dataclass
- ``write_wav()`` — write raw PCM buffer to WAV file
- ``DEFAULT_AUDIO_PORT`` — 11001
- ``DEFAULT_SAMPLE_RATE`` — 48000
- ``CHANNELS`` — 2 (stereo)
- ``SAMPLE_WIDTH`` — 2 (16-bit)
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "AudioCapture",
    "CaptureResult",
    "write_wav",
    "DEFAULT_AUDIO_PORT",
    "DEFAULT_SAMPLE_RATE",
    "CHANNELS",
    "SAMPLE_WIDTH",
]

_log = logging.getLogger(__name__)

DEFAULT_AUDIO_PORT = 11001
DEFAULT_SAMPLE_RATE = 48000
CHANNELS = 2          # stereo
SAMPLE_WIDTH = 2      # 16-bit (2 bytes per sample per channel)
_SEQ_HEADER_LEN = 2   # 2-byte LE sequence number prefix


@dataclass
class CaptureResult:
    """Outcome of an audio capture session."""
    wav_path: Path
    duration_seconds: float
    sample_rate: int
    total_samples: int
    packets_received: int
    packets_dropped: int


def write_wav(
    path: str | Path,
    pcm_data: bytes,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = CHANNELS,
    sample_width: int = SAMPLE_WIDTH,
) -> Path:
    """Write raw PCM data to a WAV file.

    Args:
        path: Output file path.
        pcm_data: Raw PCM bytes (interleaved stereo, 16-bit signed LE).
        sample_rate: Sample rate in Hz.
        channels: Number of audio channels.
        sample_width: Bytes per sample per channel.

    Returns:
        Path to the written WAV file.
    """
    path = Path(path)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return path


class AudioCapture:
    """Background-thread UDP receiver for U64 audio streams.

    Usage::

        cap = AudioCapture(port=11001)
        cap.start()
        # ... play SID, wait ...
        result = cap.stop(wav_path="output.wav")

    The receiver runs in a daemon thread. ``start()`` begins capturing
    packets into an internal buffer. ``stop()`` halts capture and
    optionally writes a WAV file.

    Sequence numbers are tracked for gap detection. Gaps are logged
    but do NOT insert silence — the captured audio is simply the
    concatenation of received PCM payloads in order.
    """

    def __init__(
        self,
        port: int = DEFAULT_AUDIO_PORT,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        bind_addr: str = "",
        multicast_group: str | None = None,
        recv_buf_size: int = 65536,
    ) -> None:
        """
        Args:
            port: UDP port to listen on.
            sample_rate: Expected sample rate (for WAV header).
            bind_addr: Address to bind to (empty = all interfaces).
            multicast_group: If set, join this multicast group (e.g. "239.0.1.65").
            recv_buf_size: SO_RCVBUF size hint.
        """
        self._port = port
        self._sample_rate = sample_rate
        self._bind_addr = bind_addr
        self._multicast_group = multicast_group
        self._recv_buf_size = recv_buf_size

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Accumulated PCM data (no sequence headers)
        self._pcm_chunks: list[bytes] = []
        self._packets_received = 0
        self._packets_dropped = 0
        self._last_seq: int | None = None
        self._started = False

    def start(self) -> None:
        """Begin capturing audio packets in a background thread."""
        if self._started:
            raise RuntimeError("AudioCapture already started")

        self._stop_event.clear()
        self._pcm_chunks = []
        self._packets_received = 0
        self._packets_dropped = 0
        self._last_seq = None

        # Create and bind UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._recv_buf_size)
        except OSError:
            pass  # best-effort buffer size
        self._sock.bind((self._bind_addr, self._port))

        # Join multicast group if requested
        if self._multicast_group:
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(self._multicast_group),
                socket.inet_aton("0.0.0.0"),
            )
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        self._sock.settimeout(0.5)  # so recv loop can check stop_event

        self._thread = threading.Thread(
            target=self._recv_loop,
            name="u64-audio-capture",
            daemon=True,
        )
        self._started = True
        self._thread.start()
        _log.info("AudioCapture started on port %d", self._port)

    def _recv_loop(self) -> None:
        """Receive UDP packets until stop_event is set."""
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                raise

            if len(data) <= _SEQ_HEADER_LEN:
                continue  # runt packet

            seq = struct.unpack_from("<H", data, 0)[0]
            pcm_payload = data[_SEQ_HEADER_LEN:]

            with self._lock:
                # Gap detection
                if self._last_seq is not None:
                    expected = (self._last_seq + 1) & 0xFFFF
                    if seq != expected:
                        gap = (seq - expected) & 0xFFFF
                        if gap < 0x8000:  # forward gap (not reorder)
                            self._packets_dropped += gap
                            _log.warning(
                                "Audio stream gap: expected seq %d, got %d (%d packets dropped)",
                                expected, seq, gap,
                            )

                self._last_seq = seq
                self._pcm_chunks.append(pcm_payload)
                self._packets_received += 1

    def stop(self, wav_path: str | Path | None = None) -> CaptureResult:
        """Stop capturing and optionally write a WAV file.

        Args:
            wav_path: If provided, write captured audio to this path.

        Returns:
            CaptureResult with capture statistics.
        """
        if not self._started:
            raise RuntimeError("AudioCapture not started")

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        with self._lock:
            pcm_data = b"".join(self._pcm_chunks)
            packets_received = self._packets_received
            packets_dropped = self._packets_dropped

        # Calculate actual duration from captured data
        bytes_per_frame = CHANNELS * SAMPLE_WIDTH  # 4 bytes per stereo frame
        total_frames = len(pcm_data) // bytes_per_frame if bytes_per_frame > 0 else 0
        duration = total_frames / self._sample_rate if self._sample_rate > 0 else 0.0

        out_path = Path(wav_path) if wav_path else Path("/dev/null")
        if wav_path:
            write_wav(out_path, pcm_data, sample_rate=self._sample_rate)
            _log.info(
                "Wrote %s (%.2fs, %d packets, %d dropped)",
                out_path, duration, packets_received, packets_dropped,
            )

        self._started = False

        return CaptureResult(
            wav_path=out_path,
            duration_seconds=duration,
            sample_rate=self._sample_rate,
            total_samples=total_frames,
            packets_received=packets_received,
            packets_dropped=packets_dropped,
        )

    @property
    def is_capturing(self) -> bool:
        """True if the capture thread is running."""
        return self._started and not self._stop_event.is_set()

    @property
    def packets_received(self) -> int:
        """Number of packets received so far (thread-safe)."""
        with self._lock:
            return self._packets_received
