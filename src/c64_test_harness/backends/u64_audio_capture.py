"""UDP audio capture from Ultimate 64 audio stream.

The U64 streams 16-bit signed stereo PCM over UDP.  Each packet:
2-byte LE sequence number + raw PCM sample data.

The stream is **not** 48000 Hz
--------------------------------
It is clock-derived, so the true figures are exact rationals.  On NTSC
everything falls out of the colour carrier::

    Fc    = 315e6/88   = 3579545.4545... Hz
    phi2  = Fc * 2/7   = 11250000/11    = 1022727.2727... Hz
    audio = Fc * 3/224 =  2109375/44    =   47940.3409... Hz

Calling that 48000 is a **1244 ppm** error -- roughly 75 ms of slip per
minute, which is fatal inside a single measurement block rather than a
drift to correct afterwards.  ``DEFAULT_SAMPLE_RATE`` keeps its value
(and its meaning: the nominal figure the firmware documentation quotes),
but :data:`U64_NTSC_AUDIO_RATE_HZ` carries the real one and is what a
timing-sensitive capture should pass.

``Fc`` cancels out of the ratio between the two derived rates::

    phi2 : audio = (2/7) / (3/224) = 64 : 3   exactly

so three audio samples span exactly 64 phi2 cycles no matter what the
crystal actually runs at -- crystal error and drift cancel.  Two things
follow.  Sizing measurement blocks in multiples of 64 cycles makes each
block hold a whole number of samples (*coherent capture*).  And stepping
the start offset over 1..63 cycles across repeated runs places the
sampling instants at 64 distinct sub-sample phases, an effective
3068182 Hz (exactly 3 per phi2 cycle), which resolves repeatable
trajectories far below one audio sample (*equivalent-time sampling*).

Do not do that arithmetic in floats.  Rounded rates
(``1022727.14 / 47940.0``) give 21.333482 against a true 21.333333 -- a
7 ppm arithmetic error, enough to fail a lock test for reasons that have
nothing to do with the hardware.  Hence :class:`fractions.Fraction`.

**PAL does not lock, structurally.**  Its phi2 divides 17734472 while
its colour carrier is 17734475/4: different base integers, and the ratio
does not reduce.  No exact PAL constant is published here because the
U64's PAL stream rate has not been measured on this bench -- quoting the
NTSC construction for PAL would be a guess.

Dropped packets destroy the time base
-------------------------------------
Gaps are detected and counted, but **not padded**: the capture is the
concatenation of the payloads that arrived.  After a drop, sample index
no longer maps to time, and every downstream alignment is wrong by an
unknown offset.  The WAV is perfectly well-formed either way, so a run
with ``packets_dropped != 0`` must be *discarded*, not analysed --
:attr:`CaptureResult.time_base_intact` is the check.

Public API
----------
- ``AudioCapture`` — background-thread UDP receiver
- ``CaptureResult`` — result dataclass
- ``write_wav()`` — write raw PCM buffer to WAV file
- ``DEFAULT_AUDIO_PORT`` — 11001
- ``DEFAULT_SAMPLE_RATE`` — 48000 (nominal)
- ``U64_NTSC_AUDIO_RATE_HZ`` — 2109375/44 Hz (exact)
- ``NTSC_PHI2_HZ`` — 11250000/11 Hz (exact)
- ``PHI2_CYCLES_PER_AUDIO_SAMPLE`` — 64/3 (exact)
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
from fractions import Fraction
from pathlib import Path

__all__ = [
    "AudioCapture",
    "CaptureResult",
    "write_wav",
    "DEFAULT_AUDIO_PORT",
    "DEFAULT_SAMPLE_RATE",
    "CHANNELS",
    "SAMPLE_WIDTH",
    "NTSC_COLOR_CARRIER_HZ",
    "NTSC_PHI2_HZ",
    "U64_NTSC_AUDIO_RATE_HZ",
    "PHI2_CYCLES_PER_AUDIO_SAMPLE",
    "coherent_block_cycles",
]

_log = logging.getLogger(__name__)

DEFAULT_AUDIO_PORT = 11001

#: Nominal rate, kept for API stability.  It is what the U64
#: documentation quotes and what every existing caller passes; it is
#: **not** the rate the device streams at.  See the module docstring.
DEFAULT_SAMPLE_RATE = 48000

#: NTSC colour subcarrier, exactly.  Everything else divides down from it.
NTSC_COLOR_CARRIER_HZ = Fraction(315_000_000, 88)

#: NTSC phi2, exactly: ``Fc * 2/7``.  ``render_wav.NTSC_CLOCK_HZ`` is the
#: same quantity rounded to 1022727 for cycle budgeting, where 0.27 Hz
#: does not matter; here it does.
NTSC_PHI2_HZ = NTSC_COLOR_CARRIER_HZ * Fraction(2, 7)

#: The U64's NTSC audio stream rate, exactly: ``Fc * 3/224``.
#:
#: Harness-verified on the U64E (fw 3.15, NTSC, 1 MHz) on 2026-09-05
#: through the ``64:3`` identity below, which is its own instrument:
#: the 6510 ran cycle-counted windows bracketed by SID master-volume
#: edges with CIA2 chained as a 32-bit phi2 counter, ``AudioCapture``
#: counted samples between the edges, and ``samples*64 / (cycles*3)``
#: came out at +8.8 ppm over one 60.2 s window (61 565 284 cycles,
#: 2 885 898 samples) and +47..+55 ppm over 10 s windows (n=4), the
#: residual being a fixed ~25-sample edge-detection offset that cancels
#: in the slope between window lengths: **+0.4 ppm** (n=2 slopes, +0.9
#: and +0.36).  The nominal 48000 Hz sits at -1234 ppm and is rejected
#: by ~3600 samples per minute.  Runs with dropped packets were
#: discarded (they read -457 and -4382 ppm).  Method and gate in
#: ``tests/test_audio_rate_lock_live.py`` (``AUDIO_RATE_LIVE=1``);
#: issue #205.  It was originally quoted (issue #195) as the reporter's
#: measurement because the FPGA sources are not part of this repo.
U64_NTSC_AUDIO_RATE_HZ = NTSC_COLOR_CARRIER_HZ * Fraction(3, 224)

#: phi2 cycles per audio sample: exactly ``64/3``, independent of the
#: crystal.  Both NTSC rates derive from ``Fc``, which cancels.
PHI2_CYCLES_PER_AUDIO_SAMPLE = NTSC_PHI2_HZ / U64_NTSC_AUDIO_RATE_HZ

CHANNELS = 2          # stereo
SAMPLE_WIDTH = 2      # 16-bit (2 bytes per sample per channel)
_SEQ_HEADER_LEN = 2   # 2-byte LE sequence number prefix


def coherent_block_cycles(samples: int) -> int:
    """phi2 cycles spanning exactly *samples* audio samples.

    A measurement block of this length starts and ends on a sample
    boundary, so repeated blocks do not creep against the stream.  Only
    exact for a whole number of 3-sample groups; anything else raises,
    because a "nearly coherent" block is the failure this exists to
    prevent.

    :param samples: Number of audio samples the block should span.
    :returns: The phi2 cycle count (``samples * 64 / 3``).
    :raises ValueError: If *samples* is not a positive multiple of 3.
    """
    if not isinstance(samples, int) or isinstance(samples, bool):
        raise TypeError("samples must be an int")
    if samples <= 0 or samples % PHI2_CYCLES_PER_AUDIO_SAMPLE.denominator:
        raise ValueError(
            f"samples must be a positive multiple of "
            f"{PHI2_CYCLES_PER_AUDIO_SAMPLE.denominator} for a coherent "
            f"block (got {samples})"
        )
    cycles = samples * PHI2_CYCLES_PER_AUDIO_SAMPLE
    return int(cycles)


@dataclass
class CaptureResult:
    """Outcome of an audio capture session.

    ``sample_rate`` is the integer that went into the WAV header.
    ``sample_rate_exact`` is the rate the capture was actually timed
    against, as a :class:`~fractions.Fraction`, when the caller supplied
    one -- the header cannot hold ``2109375/44``, so the two differ by
    the rounding and ``duration_seconds`` is computed from the exact one.
    """
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

        Gaps are not padded, so a single drop shifts every later sample
        by an unknown amount.  Check this before analysing a capture;
        the file itself looks fine either way.
        """
        return self.packets_dropped == 0


def _wav_header_rate(sample_rate: int | float | Fraction) -> int:
    """Round *sample_rate* to the integer a WAV header can hold.

    ``wave`` writes an integer frame rate, so an exact rational has to be
    rounded somewhere; doing it here keeps the residual visible and
    bounded (7 ppm for the U64's NTSC rate) instead of leaving the caller
    to write 48000 and take 1244 ppm.

    :param sample_rate: Nominal or exact rate.
    :returns: The header value.
    :raises ValueError: If the rate is not positive.
    """
    if isinstance(sample_rate, bool) or not isinstance(
        sample_rate, (int, float, Fraction)
    ):
        raise TypeError(
            f"sample_rate must be int, float or Fraction, got "
            f"{type(sample_rate).__name__}"
        )
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive (got {sample_rate})")
    rounded = int(round(sample_rate))
    if rounded != sample_rate:
        _log.debug(
            "WAV header rate %d rounded from %s (%.1f ppm)",
            rounded,
            sample_rate,
            abs(rounded - Fraction(sample_rate)) / Fraction(sample_rate) * 1_000_000,
        )
    return rounded


def write_wav(
    path: str | Path,
    pcm_data: bytes,
    sample_rate: int | float | Fraction = DEFAULT_SAMPLE_RATE,
    channels: int = CHANNELS,
    sample_width: int = SAMPLE_WIDTH,
) -> Path:
    """Write raw PCM data to a WAV file.

    Args:
        path: Output file path.
        pcm_data: Raw PCM bytes (interleaved stereo, 16-bit signed LE).
        sample_rate: Sample rate in Hz.  Accepts a
            :class:`~fractions.Fraction` such as
            :data:`U64_NTSC_AUDIO_RATE_HZ`; the header gets the nearest
            integer (47940, 7 ppm) rather than the nominal 48000
            (1244 ppm).
        channels: Number of audio channels.
        sample_width: Bytes per sample per channel.

    Returns:
        Path to the written WAV file.
    """
    # Validate before creating the file: a rate error that surfaces as a
    # half-written WAV is much harder to read than a ValueError.
    header_rate = _wav_header_rate(sample_rate)
    path = Path(path)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(header_rate)
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
    concatenation of received PCM payloads in order. **A capture whose
    ``packets_dropped`` is non-zero has no usable time base and should be
    discarded rather than analysed**; the resulting WAV is well-formed
    either way, so nothing downstream will notice on its own. See
    :attr:`CaptureResult.time_base_intact`.

    The default *sample_rate* is the nominal 48000, which is 1244 ppm
    away from the U64's real NTSC rate. Pass
    :data:`U64_NTSC_AUDIO_RATE_HZ` for anything timing-sensitive; the
    default logs a warning saying so.
    """

    def __init__(
        self,
        port: int = DEFAULT_AUDIO_PORT,
        sample_rate: int | float | Fraction = DEFAULT_SAMPLE_RATE,
        bind_addr: str = "",
        multicast_group: str | None = None,
        recv_buf_size: int = 65536,
    ) -> None:
        """
        Args:
            port: UDP port to listen on.
            sample_rate: Rate the capture is timed against, and (rounded)
                the WAV header value. Accepts a
                :class:`~fractions.Fraction`.
            bind_addr: Address to bind to (empty = all interfaces).
            multicast_group: If set, join this multicast group (e.g. "239.0.1.65").
            recv_buf_size: SO_RCVBUF size hint.
        """
        self._port = port
        # Validates and rounds; raises here rather than at stop() time,
        # after a capture has already been thrown away.
        self._header_rate = _wav_header_rate(sample_rate)
        self._sample_rate = sample_rate
        self._exact_rate = (
            sample_rate if isinstance(sample_rate, Fraction) else None
        )
        if sample_rate == DEFAULT_SAMPLE_RATE:
            _log.warning(
                "AudioCapture using the nominal %d Hz: the U64's NTSC "
                "stream is %s Hz (2109375/44), so sample index drifts "
                "1244 ppm -- ~75 ms per minute -- against this rate. Pass "
                "sample_rate=U64_NTSC_AUDIO_RATE_HZ for timing-sensitive "
                "work.",
                DEFAULT_SAMPLE_RATE,
                float(U64_NTSC_AUDIO_RATE_HZ),
            )
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

        # Calculate actual duration from captured data.  Timed against the
        # exact rate when the caller gave one -- with 48000 assumed, this
        # figure is itself 1244 ppm long.
        bytes_per_frame = CHANNELS * SAMPLE_WIDTH  # 4 bytes per stereo frame
        total_frames = len(pcm_data) // bytes_per_frame if bytes_per_frame > 0 else 0
        duration = float(total_frames / self._sample_rate) if self._sample_rate > 0 else 0.0

        if packets_dropped:
            _log.warning(
                "Audio capture lost %d packet(s): sample index no longer "
                "maps to time, every later sample is offset by an unknown "
                "amount. Discard this capture rather than analysing it "
                "(CaptureResult.time_base_intact is False).",
                packets_dropped,
            )

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
            sample_rate=self._header_rate,
            total_samples=total_frames,
            packets_received=packets_received,
            packets_dropped=packets_dropped,
            sample_rate_exact=self._exact_rate,
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
