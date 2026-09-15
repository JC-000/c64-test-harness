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

Dropped packets are filled with silence (#410)
----------------------------------------------
Every audio datagram carries exactly :data:`AUDIO_PCM_BYTES_PER_PACKET`
(768) bytes of PCM, and the 16-bit sequence number says exactly how many
datagrams went missing.  So each lost packet is replaced by 768 zero bytes
at its own position, and sample index stays a clock across the loss.  The
contract, for anyone reading samples:

* ``packets_filled`` lost packets are zeros in the WAV, at the frame
  ranges listed in ``filled_frame_ranges`` (``(start_frame, frame_count)``,
  merged, ascending).  ``fill_fraction`` is the share of samples that are
  fill.  Zeros are not the signal: analysis that must not see them should
  skip those ranges or bound ``fill_fraction``.
* A packet that arrives late (#430) overwrites its own zeros, so it is
  neither dropped nor filled.  A duplicate is discarded.
* ``time_base_intact`` is True when every dropped packet was filled at a
  trusted length: no unfilled drop, no sequence resync (a restarted
  counter hides an unknown number of packets), and no datagram whose PCM
  was not 768 bytes if anything was filled.  It no longer means "nothing
  was lost"; ``packets_dropped == 0`` still does.

**Older behaviour, for readers of older captures.**  Before #410 gaps were
not padded: the capture was the concatenation of the payloads that arrived,
so any drop shifted every later sample, and ``time_base_intact`` was
``packets_dropped == 0``.  #430 alone (the sequence fix without this fill)
left a *zero-length* placeholder per missing packet, which is the same
concatenation.  A result without a ``packets_filled`` attribute comes from
a version that never padded.

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
- ``AUDIO_FRAMES_PER_PACKET`` — 192 stereo frames per datagram
- ``AUDIO_PCM_BYTES_PER_PACKET`` — 768 PCM bytes per datagram
"""
from __future__ import annotations

import errno
import logging
import socket
import struct
import subprocess
import threading
import time
import wave
import zlib
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import _stream_seq

__all__ = [
    "AudioCapture",
    "CaptureResult",
    "write_wav",
    "DEFAULT_AUDIO_PORT",
    "EPHEMERAL_AUDIO_PORT",
    "AudioCapturePortInUseError",
    "DEFAULT_SAMPLE_RATE",
    "CHANNELS",
    "SAMPLE_WIDTH",
    "AUDIO_FRAMES_PER_PACKET",
    "AUDIO_PCM_BYTES_PER_PACKET",
    "NTSC_COLOR_CARRIER_HZ",
    "NTSC_PHI2_HZ",
    "U64_NTSC_AUDIO_RATE_HZ",
    "PHI2_CYCLES_PER_AUDIO_SAMPLE",
    "coherent_block_cycles",
]

_log = logging.getLogger(__name__)

DEFAULT_AUDIO_PORT = 11001

#: Bind an ephemeral port instead of a fixed one.  Pass as ``port`` and
#: read the port the OS chose from :attr:`AudioCapture.port` after
#: :meth:`AudioCapture.start`.  This is the only collision-free way to
#: listen on a shared host: reserving a port by binding it, closing the
#: socket and re-binding later loses the race with any other process
#: (issue #230).
EPHEMERAL_AUDIO_PORT = 0


class AudioCapturePortInUseError(OSError):
    """The requested UDP port is already bound by something else.

    Raised by :meth:`AudioCapture.start` instead of a bare
    ``OSError: [Errno 48] Address already in use``, which says nothing
    about *which* port or who has it.  Subclasses ``OSError`` so callers
    that already handle bind failures keep working.

    ``errno`` is :data:`errno.EADDRINUSE`, so the idiomatic
    ``except OSError as e: if e.errno == errno.EADDRINUSE`` keeps
    matching.

    Attributes:
        port: The port that could not be bound.
        holder: Best-effort description of the holding process(es), or
            ``None`` when the host offers no way to ask.
    """

    def __init__(self, port: int, holder: str | None = None) -> None:
        self.port = port
        self.holder = holder
        detail = f" (held by {holder})" if holder else ""
        # Set after super().__init__ rather than through it: passing the
        # errno positionally would prepend "[Errno 48] " to str(self).
        # Without it, `except OSError as e: e.errno == EADDRINUSE` -- the
        # idiomatic handler, and the exact shape of #230's own failure --
        # silently stops matching, so the OSError-subclass promise below
        # would be words only.
        super().__init__(
            f"UDP port {port} is already in use{detail}; pass "
            f"port=EPHEMERAL_AUDIO_PORT (0) and read AudioCapture.port after "
            f"start() to listen on a port nobody else can take"
        )
        self.errno = errno.EADDRINUSE


def _port_holder(port: int) -> str | None:
    """Best-effort name of whatever holds *port*, for the error message.

    Never raises and never blocks for long: a diagnostic that can fail
    the call it is diagnosing is worse than no diagnostic.
    """
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iUDP:{port}"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [ln for ln in proc.stdout.splitlines()[1:] if ln.strip()]
    if not lines:
        return None
    return "; ".join(
        " ".join(ln.split()[:2]) for ln in lines[:3]
    )

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

#: The U64's NTSC audio stream rate: ``Fc * 3/224``.  Exactness is the
#: design claim from issue #195 (both rates divide down from one
#: crystal); the measurement below bounds it.
#:
#: Harness-verified on the U64E (fw 3.15, NTSC, 1 MHz) on 2026-09-05
#: through the ``64:3`` identity below, which is its own instrument:
#: the 6510 ran cycle-counted windows bracketed by SID master-volume
#: edges with CIA2 chained as a 32-bit phi2 counter, ``AudioCapture``
#: counted samples between the edges, and ``samples*64 / (cycles*3)``
#: came out at +8.8 ppm over a 60.2 s window (61 565 284 cycles,
#: 2 885 898 samples) and +47..+55 ppm over 10 s windows (n=4), the
#: residual being a fixed ~25-sample edge-detection offset that cancels
#: in the slope between window lengths: +0.36 and +0.9 ppm.  So the
#: ratio is **consistent with exactly 64:3 to ~1 ppm**; it is not
#: proven exact.  (A second 60.2 s run returned the identical sample
#: count, which supports the lock but adds almost nothing over n=1:
#: with a phase-locked ratio and a fixed cycle count only two adjacent
#: counts are possible.)  The nominal 48000 Hz sits at -1234 ppm and is
#: rejected by ~3600 samples per minute.  Runs with dropped packets
#: were discarded (they read -457 and -4382 ppm).  Method and gate in
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

#: Stereo frames in one audio datagram.  Ultimate documentation, "Data
#: Streams": "192 stereo samples in 16-bit signed, little endian format ...
#: Thus, the total UDP packet size is 770 bytes"; every datagram in the 28
#: U64E captures of #410 was 770 B.  The gap fill relies on it, and a
#: datagram of another size is counted in ``nonstandard_payloads``.
AUDIO_FRAMES_PER_PACKET = 192

#: PCM bytes in one audio datagram (768): the length of one packet's fill.
AUDIO_PCM_BYTES_PER_PACKET = AUDIO_FRAMES_PER_PACKET * CHANNELS * SAMPLE_WIDTH


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
    #: Packets that arrived behind the highest sequence number seen (#205,
    #: #430), **one per packet**.  Before #430 this counted backward *steps*:
    #: ``[0, 5, 1, 2, 3, 4, 6]`` read 1 then and reads 4 now.
    #:
    #: - A late packet un-counts the drop its gap charged, and its PCM goes
    #:   into its own slot.
    #: - A duplicate (number already received, identical PCM, not followed by
    #:   a continuation of the re-sent run) is discarded.
    #:
    #: Neither shifts the sample index, and neither is in ``packets_dropped``.
    #: Everything else behind the highest number is a resync, also counted in
    #: :attr:`sequence_resyncs`: a restarted counter, a forward loss of 32,768 or more, or a
    #: packet ``_stream_seq.SEQ_REORDER_WINDOW`` (1024) or more positions late.
    #: A resync's PCM is appended where it arrived (a restart over identical
    #: PCM is recognised when the re-sent run continues, and its held packets
    #: are kept).  An over-late packet also overcounts ``packets_dropped`` by
    #: about its lateness, because the next in-order packet is charged a gap
    #: back up to the stream's real position.  Residuals: see
    #: ``backends/_stream_seq.py``.
    packets_reordered: int = 0
    #: Backward steps taken as a new stream position (see
    #: :attr:`packets_reordered`).  The number of packets lost across one is
    #: unknown, so the sample index is not a clock across it.
    sequence_resyncs: int = 0
    #: Lost packets replaced by :data:`AUDIO_PCM_BYTES_PER_PACKET` zero
    #: bytes at their own position (#410).  0 on a result constructed
    #: without it, which then reads as unfilled.
    packets_filled: int = 0
    #: Datagrams whose PCM was not :data:`AUDIO_PCM_BYTES_PER_PACKET` bytes.
    #: A fill's length is only trusted while this is 0.
    nonstandard_payloads: int = 0
    #: ``(start_frame, frame_count)`` of every run of fill in the capture,
    #: ascending and merged.
    filled_frame_ranges: tuple[tuple[int, int], ...] = ()

    @property
    def time_base_intact(self) -> bool:
        """True when sample index maps exactly to time.

        See the module docstring, "Dropped packets are filled with silence".
        """
        return _time_base_intact(self)

    @property
    def fill_fraction(self) -> float:
        """Share of :attr:`total_samples` that is fill, 0.0 to 1.0."""
        return _fill_fraction(self)


def _time_base_intact(result) -> bool:
    """Shared by :class:`CaptureResult` and ``render_wav_u64.U64CaptureResult``."""
    filled = getattr(result, "packets_filled", 0)
    return (
        result.packets_dropped == filled
        and getattr(result, "sequence_resyncs", 0) == 0
        and (filled == 0 or getattr(result, "nonstandard_payloads", 0) == 0)
    )


def _fill_fraction(result) -> float:
    total = result.total_samples
    if total <= 0:
        return 0.0
    return min(1.0, getattr(result, "packets_filled", 0) * AUDIO_FRAMES_PER_PACKET / total)


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


def _filled_ranges(
    chunks: list[bytes], fill_chunks: dict[int, int]
) -> tuple[tuple[int, int], ...]:
    """``(start_frame, frame_count)`` of each run of fill, merged."""
    if not fill_chunks:
        return ()
    bytes_per_frame = CHANNELS * SAMPLE_WIDTH
    ranges: list[list[int]] = []
    offset = 0
    for i, chunk in enumerate(chunks):
        if i in fill_chunks:
            start = offset // bytes_per_frame
            count = len(chunk) // bytes_per_frame
            if ranges and ranges[-1][0] + ranges[-1][1] == start:
                ranges[-1][1] += count
            else:
                ranges.append([start, count])
        offset += len(chunk)
    return tuple((s, c) for s, c in ranges)


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

    Sequence numbers are tracked for gap detection. Each lost packet is
    logged and replaced by one packet of silence at its own position, so
    the sample index stays a clock (#410); late packets overwrite their
    fill and duplicates are discarded (#430). **Zeros are not signal**:
    check :attr:`CaptureResult.time_base_intact`, and bound
    :attr:`CaptureResult.fill_fraction` or skip
    :attr:`CaptureResult.filled_frame_ranges` before analysing. A capture
    whose time base is not intact should be discarded; the WAV is
    well-formed either way.

    A busy port fails loudly: :meth:`start` raises
    :class:`AudioCapturePortInUseError` rather than binding alongside the
    holder.  ``SO_REUSEADDR`` is set only for a multicast capture, where
    sharing the port is the point -- so two *multicast* captures on one
    group and port still coexist by design.

    **Behaviour change (issue #230).**  Before this, ``SO_REUSEADDR`` was
    set unconditionally, and a unicast capture could bind a port another
    process already held -- silently, receiving nothing, because the
    kernel delivers to the more specific socket.  Nothing in this repo
    relied on that, but a downstream caller that deliberately shares a
    unicast port now gets ``AudioCapturePortInUseError`` where it used to
    get a working-looking capture.  Multicast is unaffected.

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
            port: UDP port to listen on.  :data:`EPHEMERAL_AUDIO_PORT` (0)
                binds a free port chosen by the OS; read it back from
                :attr:`port` after :meth:`start` (issue #230).
            sample_rate: Rate the capture is timed against, and (rounded)
                the WAV header value. Accepts a
                :class:`~fractions.Fraction`.
            bind_addr: Address to bind to (empty = all interfaces).
            multicast_group: If set, join this multicast group (e.g. "239.0.1.65").
            recv_buf_size: SO_RCVBUF size hint.
        """
        self._requested_port = port
        #: The port actually bound.  Equal to the requested one until
        #: ``start()`` resolves an ephemeral request.
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
        self._packets_reordered = 0
        self._sequence_resyncs = 0
        self._last_seq: int | None = None
        self._seq = _stream_seq.SequenceTracker()
        self._fill_chunks: dict[int, int] = {}  # chunk index -> packets
        self._packets_filled = 0
        self._nonstandard_payloads = 0
        self._started = False

    def start(self) -> None:
        """Begin capturing audio packets in a background thread."""
        if self._started:
            raise RuntimeError("AudioCapture already started")

        self._stop_event.clear()
        self._pcm_chunks = []
        self._packets_received = 0
        self._packets_dropped = 0
        self._packets_reordered = 0
        self._sequence_resyncs = 0
        self._last_seq = None
        self._seq = _stream_seq.SequenceTracker()
        self._fill_chunks = {}
        self._packets_filled = 0
        self._nonstandard_payloads = 0

        # Create and bind UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if self._multicast_group:
            # Several listeners sharing one multicast group and port is
            # the normal case, and needs SO_REUSEADDR.  For a unicast
            # capture it is the opposite: a second binder is the bug, and
            # SO_REUSEADDR asks the kernel to hide exactly the collision
            # issue #230 is about.  Measured on this host, squatter vs
            # capture bind address, with the flag set:
            #   127.0.0.1 vs 127.0.0.1 -> EADDRINUSE
            #   127.0.0.1 vs ""        -> BOUND, no error   <- the default
            #   0.0.0.0   vs 127.0.0.1 -> BOUND, no error
            #   0.0.0.0   vs ""        -> EADDRINUSE
            # With it clear, all four raise EADDRINUSE.  So the flag,
            # not the address, is what made a busy port bind silently:
            # the two BOUND rows are one asymmetry read both ways --
            # detection failed whenever the capture bound something
            # *broader* than the holder (wildcard over 127.0.0.1), and
            # the kernel then delivered to the more specific socket, so
            # the neighbour's capture was a silent zero-packet run.
            # Both addresses are AF_INET throughout; it is specificity,
            # not address family.
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # BSD (so macOS) needs SO_REUSEPORT as well before two
            # wildcard binds may share a port; SO_REUSEADDR alone still
            # gives EADDRINUSE there.  Absent on some platforms, hence
            # the guard.  Only one of the two flags is under test on this
            # bench: removing SO_REUSEADDR here passes the whole suite on
            # macOS, because SO_REUSEPORT alone carries the share. It is
            # kept because on Linux the roles are the other way round and
            # SO_REUSEADDR is the load-bearing one -- which nothing here
            # can catch (rev-232 finding 6).
            if hasattr(socket, "SO_REUSEPORT"):
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._recv_buf_size)
        except OSError:
            pass  # best-effort buffer size
        # Always bind what the *caller* asked for: a second start() after
        # a stop() must re-draw an ephemeral port, not re-bind the one the
        # OS happened to give us last time (which may since have gone).
        try:
            self._sock.bind((self._bind_addr, self._requested_port))
        except OSError as exc:
            self._sock.close()
            self._sock = None
            # EADDRINUSE only.  EACCES is a privilege failure (a
            # low-numbered port as an ordinary user), and reporting it as
            # "already in use" sends the reader after a holder that does
            # not exist, with a remedy that cannot help.  Let it through
            # with its own strerror.
            if exc.errno == errno.EADDRINUSE:
                raise AudioCapturePortInUseError(
                    self._requested_port, _port_holder(self._requested_port)
                ) from exc
            raise
        self._port = self._sock.getsockname()[1]

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
                self._packets_received += 1
                if len(pcm_payload) != AUDIO_PCM_BYTES_PER_PACKET:
                    if not self._nonstandard_payloads:
                        _log.warning(
                            "Audio datagram carries %d PCM bytes, not the "
                            "documented %d: gap fill length is not trusted "
                            "for this capture",
                            len(pcm_payload), AUDIO_PCM_BYTES_PER_PACKET,
                        )
                    self._nonstandard_payloads += 1
                ev = self._seq.observe(seq, zlib.crc32(pcm_payload), pcm_payload)
                if ev.discarded_held:
                    _log.warning(
                        "Audio stream: %d re-sent packet(s) were duplicates; "
                        "payload discarded", ev.discarded_held,
                    )
                if ev.kind == _stream_seq.GAP:
                    gap = len(ev.tracked_missing) + ev.untracked
                    _log.warning(
                        "Audio stream gap: expected seq %d, got %d (%d packets dropped)",
                        ev.expected, seq, gap,
                    )
                    # Silence for every lost packet (#410).  Packets older
                    # than the reorder window get one block with no slot;
                    # each remembered one gets its own, which a late
                    # arrival overwrites (#430).
                    if ev.untracked:
                        self._fill_chunks[len(self._pcm_chunks)] = ev.untracked
                        self._pcm_chunks.append(
                            bytes(AUDIO_PCM_BYTES_PER_PACKET * ev.untracked)
                        )
                    for missing in ev.tracked_missing:
                        self._seq.bind(missing, len(self._pcm_chunks))
                        self._fill_chunks[len(self._pcm_chunks)] = 1
                        self._pcm_chunks.append(bytes(AUDIO_PCM_BYTES_PER_PACKET))
                    self._packets_filled += gap
                elif ev.kind == _stream_seq.LATE:
                    _log.warning(
                        "Audio stream late packet: seq %d arrived after %d; "
                        "placed in its own slot",
                        seq, self._seq.highest,
                    )
                    self._pcm_chunks[ev.slot] = pcm_payload
                    del self._fill_chunks[ev.slot]
                    self._packets_filled -= 1
                    self._sync_counters()
                    continue
                elif ev.kind == _stream_seq.HELD:
                    # Duplicate or restart over identical PCM: the next
                    # packet decides (see _stream_seq).
                    self._sync_counters()
                    continue
                elif ev.kind == _stream_seq.RESYNC:
                    _log.warning(
                        "Audio stream backward step: expected seq %d, got %d; "
                        "resynchronising on the new sequence",
                        ev.expected, seq,
                    )
                    self._pcm_chunks.extend(ev.readmit)
                self._pcm_chunks.append(pcm_payload)
                self._sync_counters()

    def _sync_counters(self) -> None:
        """Mirror the tracker's counts into the historical attributes."""
        self._packets_dropped = self._seq.dropped
        self._packets_reordered = self._seq.reordered
        self._sequence_resyncs = self._seq.resyncs
        self._last_seq = self._seq.highest

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
            held = self._seq.flush_held()
            if held:
                _log.warning(
                    "Audio capture stopped with %d re-sent packet(s) "
                    "undecided; discarded as duplicates", held,
                )
            pcm_data = b"".join(self._pcm_chunks)
            packets_received = self._packets_received
            packets_dropped = self._packets_dropped
            packets_reordered = self._packets_reordered
            packets_filled = self._packets_filled
            sequence_resyncs = self._sequence_resyncs
            nonstandard_payloads = self._nonstandard_payloads
            filled_frame_ranges = _filled_ranges(self._pcm_chunks, self._fill_chunks)

        # Calculate actual duration from captured data.  Timed against the
        # exact rate when the caller gave one -- with 48000 assumed, this
        # figure is itself 1244 ppm long.
        bytes_per_frame = CHANNELS * SAMPLE_WIDTH  # 4 bytes per stereo frame
        total_frames = len(pcm_data) // bytes_per_frame if bytes_per_frame > 0 else 0
        duration = float(total_frames / self._sample_rate) if self._sample_rate > 0 else 0.0

        intact = _time_base_intact(CaptureResult(
            wav_path=Path("/dev/null"), duration_seconds=0.0, sample_rate=0,
            total_samples=total_frames, packets_received=packets_received,
            packets_dropped=packets_dropped, packets_filled=packets_filled,
            sequence_resyncs=sequence_resyncs,
            nonstandard_payloads=nonstandard_payloads,
        ))
        if packets_dropped and intact:
            _log.warning(
                "Audio capture lost %d packet(s), filled with silence "
                "(%.2f%% of samples; CaptureResult.filled_frame_ranges). The "
                "time base is intact, but the zeros are not signal.",
                packets_dropped,
                100.0 * packets_filled * AUDIO_FRAMES_PER_PACKET / total_frames
                if total_frames else 0.0,
            )
        elif not intact:
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
            packets_reordered=packets_reordered,
            packets_filled=packets_filled,
            sequence_resyncs=sequence_resyncs,
            nonstandard_payloads=nonstandard_payloads,
            filled_frame_ranges=filled_frame_ranges,
        )

    @property
    def port(self) -> int:
        """The UDP port this capture listens on.

        Before :meth:`start`, the port that was requested.  After it, the
        port actually bound -- which is the only meaningful value when
        :data:`EPHEMERAL_AUDIO_PORT` was requested, and the one to hand to
        ``stream_audio_start`` as the stream destination.
        """
        return self._port

    @property
    def is_capturing(self) -> bool:
        """True if the capture thread is running."""
        return self._started and not self._stop_event.is_set()

    @property
    def packets_received(self) -> int:
        """Number of packets received so far (thread-safe)."""
        with self._lock:
            return self._packets_received
