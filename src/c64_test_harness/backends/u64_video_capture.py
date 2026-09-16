"""UDP video capture from Ultimate 64 VIC-II stream.

The U64 streams 4-bit VIC-II video frames over UDP.
Each packet: 12-byte LE header + 768-byte pixel data (4 lines × 384 pixels).

Public API
----------
- ``VideoCapture`` — background-thread UDP receiver
- ``VideoCaptureResult`` — result dataclass
- ``VideoFrame`` — single assembled frame
- ``DEFAULT_VIDEO_PORT`` — 11000
- ``VIC_PALETTE`` — 16 standard VIC-II RGB colours
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

from . import _stream_iface, _stream_seq

__all__ = [
    "VideoCapture",
    "VideoCaptureResult",
    "VideoFrame",
    "DEFAULT_VIDEO_PORT",
    "VIC_PALETTE",
]

_log = logging.getLogger(__name__)

DEFAULT_VIDEO_PORT = 11000
VIDEO_HEADER_SIZE = 12
VIDEO_PAYLOAD_SIZE = 768

# Standard VIC-II colour palette (RGB tuples).
VIC_PALETTE = (
    (0x00, 0x00, 0x00),  # 0  Black
    (0xFF, 0xFF, 0xFF),  # 1  White
    (0x88, 0x39, 0x32),  # 2  Red
    (0x67, 0xB6, 0xBD),  # 3  Cyan
    (0x8B, 0x3F, 0x96),  # 4  Purple
    (0x55, 0xA0, 0x49),  # 5  Green
    (0x40, 0x31, 0x8D),  # 6  Blue
    (0xBF, 0xCE, 0x72),  # 7  Yellow
    (0x8B, 0x54, 0x29),  # 8  Orange
    (0x57, 0x42, 0x00),  # 9  Brown
    (0xB8, 0x69, 0x62),  # 10 Light Red
    (0x50, 0x50, 0x50),  # 11 Dark Grey
    (0x78, 0x78, 0x78),  # 12 Grey
    (0x94, 0xE0, 0x89),  # 13 Light Green
    (0x78, 0x69, 0xC4),  # 14 Light Blue
    (0x9F, 0x9F, 0x9F),  # 15 Light Grey
)


def _unpack_4bit(data: bytes) -> bytes:
    """Unpack 4-bit packed pixels to 1 byte per pixel.

    Each input byte holds two pixels: low nibble = first pixel,
    high nibble = second pixel.
    """
    result = bytearray(len(data) * 2)
    for i, byte in enumerate(data):
        result[i * 2] = byte & 0x0F
        result[i * 2 + 1] = (byte >> 4) & 0x0F
    return bytes(result)


@dataclass(frozen=True)
class VideoFrame:
    """A single assembled VIC-II video frame."""

    frame_number: int
    width: int
    height: int
    pixels: bytes  # 1 byte per pixel, colour indices 0-15

    def pixel_at(self, x: int, y: int) -> int:
        """Return the colour index at (x, y)."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"pixel ({x}, {y}) out of range ({self.width}x{self.height})")
        return self.pixels[y * self.width + x]

    def row(self, y: int) -> bytes:
        """Return one row of pixel data."""
        if not 0 <= y < self.height:
            raise IndexError(f"row {y} out of range (0-{self.height - 1})")
        start = y * self.width
        return self.pixels[start : start + self.width]


@dataclass
class VideoCaptureResult:
    """Outcome of a video capture session."""

    frames: list[VideoFrame]
    duration_seconds: float
    packets_received: int
    packets_dropped: int
    frames_completed: int
    frames_dropped: int
    #: Datagrams that arrived behind the highest sequence number seen --
    #: late, held or resync -- one per datagram (#442).  Before that fix a
    #: backward step was not counted at all and moved the last-seen number
    #: backwards, so the next in-order datagram was charged as a gap too.
    packets_reordered: int = 0
    #: Backward steps the tracker could not repair (a restarted counter, a
    #: forward loss of 32,768 or more, a datagram past the reorder window).
    #: See ``backends/_stream_seq.py`` for the classification.
    sequence_resyncs: int = 0
    #: Datagrams held as possible duplicates and then decided to be
    #: duplicates, so their lines were never applied.  A true duplicate is
    #: a correct discard, so a non-zero count is not by itself a fault.
    payloads_discarded: int = 0
    #: Late datagrams belonging to a frame that had already been finalised.
    #: Their lines are dropped: the frame they belong to is gone, and
    #: applying them would finalise the frame in progress early and re-open
    #: the old frame number (#442).
    stale_packets: int = 0


class VideoCapture:
    """Background-thread UDP receiver for U64 VIC-II video streams.

    Usage::

        cap = VideoCapture(port=11000)
        cap.start()
        # ... wait for frames ...
        result = cap.stop()

    The receiver runs in a daemon thread.  ``start()`` begins capturing
    packets into frame buffers.  ``stop()`` halts capture and returns
    assembled frames.

    Sequence numbers go through the shared
    :class:`~._stream_seq.SequenceTracker`, so only true loss is counted as
    a drop and a late or duplicated datagram is a reorder (#442).  Gaps are
    logged; frames with missing lines are still assembled and counted in
    ``frames_dropped``.

    **Frame assembly stays header-driven.**  Every datagram carries its own
    ``frame_num`` and line number, so assembly does not depend on the
    sequence number, and the tracker governs the counters plus one assembly
    rule: a *late* datagram is applied only when it belongs to the frame
    still being assembled.  A late datagram for a frame already finalised
    cannot repair it -- the frame has been emitted or counted dropped -- and
    applying it would finalise the frame in progress early and re-open the
    old frame number, turning one reordered datagram into two corrupted
    frames.  Those are counted in ``stale_packets`` instead.  A datagram the
    tracker holds as a possible duplicate is applied only if a restart later
    re-admits it.
    """

    def __init__(
        self,
        port: int = DEFAULT_VIDEO_PORT,
        bind_addr: str = "",
        multicast_group: str | None = None,
        recv_buf_size: int = 262144,
        *,
        multicast_interface: str | None = None,
        device_host: str | None = None,
    ) -> None:
        """
        Args:
            port: UDP port to listen on.
            bind_addr: Address to bind to (empty = all interfaces).
            multicast_group: If set, join this multicast group.
            recv_buf_size: SO_RCVBUF size hint.
            multicast_interface: Local interface address to join the group
                on.  Default (and the behaviour before #399) is INADDR_ANY,
                which lets the kernel route the group -- via the VPN on this
                bench.  Ignored without ``multicast_group``: naming one
                without the other raises ``ValueError``.
            device_host: Resolve ``multicast_interface`` as the local
                address that reaches this host (a UDP connect, no traffic).
                An explicit ``multicast_interface`` wins over it.
        """
        _stream_iface.validate_request(
            multicast_group, multicast_interface, device_host
        )
        self._port = port
        self._bind_addr = bind_addr
        self._multicast_group = multicast_group
        self._multicast_interface = multicast_interface
        self._device_host = device_host
        self._recv_buf_size = recv_buf_size

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Frame assembly state
        self._frames: list[VideoFrame] = []
        self._packets_received = 0
        self._packets_dropped = 0
        self._packets_reordered = 0
        self._sequence_resyncs = 0
        self._payloads_discarded = 0
        self._stale_packets = 0
        self._frames_dropped = 0
        self._last_seq: int | None = None
        self._seq = _stream_seq.SequenceTracker()
        self._started = False

        # Current frame being assembled: {line_number: unpacked pixel bytes}
        self._cur_frame_num: int | None = None
        self._cur_frame_width: int = 0
        self._cur_frame_lines: dict[int, bytes] = {}

    def start(self) -> None:
        """Begin capturing video packets in a background thread."""
        if self._started:
            raise RuntimeError("VideoCapture already started")

        self._stop_event.clear()
        self._frames = []
        self._packets_received = 0
        self._packets_dropped = 0
        self._packets_reordered = 0
        self._sequence_resyncs = 0
        self._payloads_discarded = 0
        self._stale_packets = 0
        self._frames_dropped = 0
        self._last_seq = None
        self._seq = _stream_seq.SequenceTracker()
        self._cur_frame_num = None
        self._cur_frame_width = 0
        self._cur_frame_lines = {}

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
            _stream_iface.join_group(
                self._sock, self._multicast_group,
                self._multicast_interface, self._device_host,
            )

        self._sock.settimeout(0.5)  # so recv loop can check stop_event

        self._thread = threading.Thread(
            target=self._recv_loop,
            name="u64-video-capture",
            daemon=True,
        )
        self._started = True
        self._capture_start = time.monotonic()
        self._thread.start()
        _log.info("VideoCapture started on port %d", self._port)

    def _finalize_frame(self) -> None:
        """Assemble the current line buffer into a VideoFrame."""
        if self._cur_frame_num is None or not self._cur_frame_lines:
            return

        max_line = max(self._cur_frame_lines)
        width = self._cur_frame_width or 384
        height = max_line + 1
        expected_row_len = width

        # Check for missing lines
        missing = 0
        rows: list[bytes] = []
        for line in range(height):
            row = self._cur_frame_lines.get(line)
            if row is not None and len(row) >= expected_row_len:
                rows.append(row[:expected_row_len])
            else:
                missing += 1
                rows.append(b"\x00" * expected_row_len)

        if missing > 0:
            self._frames_dropped += 1
            _log.debug(
                "Frame %d incomplete: %d/%d lines missing",
                self._cur_frame_num, missing, height,
            )
        else:
            frame = VideoFrame(
                frame_number=self._cur_frame_num,
                width=width,
                height=height,
                pixels=b"".join(rows),
            )
            self._frames.append(frame)

        self._cur_frame_lines = {}

    def _recv_loop(self) -> None:
        """Receive UDP packets until stop_event is set."""
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                raise

            if len(data) < VIDEO_HEADER_SIZE:
                continue  # runt packet

            # Parse 12-byte header
            seq, frame_num, raw_line, pixels_per_line, lines_per_packet, bpp, encoding = (
                struct.unpack_from("<HHHHBBH", data, 0)
            )
            frame_end = bool(raw_line & 0x8000)
            line_num = raw_line & 0x7FFF

            payload = data[VIDEO_HEADER_SIZE:]

            # Unpack 4-bit pixel data
            unpacked = _unpack_4bit(payload)
            pixels_per_packet = lines_per_packet * pixels_per_line

            lines = (
                frame_num, line_num, frame_end, pixels_per_line,
                lines_per_packet, unpacked,
            )

            with self._lock:
                self._packets_received += 1
                ev = self._seq.observe(seq, zlib.crc32(payload), lines)
                self._sync_counters()

                if ev.discarded_held:
                    _log.warning(
                        "Video stream: %d re-sent datagram(s) were "
                        "duplicates; lines discarded", ev.discarded_held,
                    )
                if ev.kind == _stream_seq.GAP:
                    _log.warning(
                        "Video stream gap: expected seq %d, got %d (%d packets dropped)",
                        ev.expected, seq,
                        len(ev.tracked_missing) + ev.untracked,
                    )
                elif ev.kind == _stream_seq.HELD:
                    # Duplicate, or a restart over identical pixels: the
                    # next datagram decides (see _stream_seq).  Applying it
                    # now would write a line the restart may not own.
                    continue
                elif ev.kind == _stream_seq.LATE:
                    if frame_num != self._cur_frame_num:
                        # Its frame is already finalised; see the class
                        # docstring for why it is not applied anyway.
                        self._stale_packets += 1
                        _log.warning(
                            "Video stream late packet: seq %d belongs to "
                            "frame %d, which is already finalised; %d "
                            "line(s) dropped",
                            seq, frame_num, lines_per_packet,
                        )
                        continue
                    _log.warning(
                        "Video stream late packet: seq %d arrived after %d; "
                        "applied to the frame still being assembled",
                        seq, self._seq.highest,
                    )
                elif ev.kind == _stream_seq.RESYNC:
                    _log.warning(
                        "Video stream backward step: expected seq %d, got "
                        "%d; resynchronising on the new sequence",
                        ev.expected, seq,
                    )
                    for held_lines in ev.readmit:
                        self._apply_packet(*held_lines)

                self._apply_packet(*lines)

    def _apply_packet(
        self,
        frame_num: int,
        line_num: int,
        frame_end: bool,
        pixels_per_line: int,
        lines_per_packet: int,
        unpacked: bytes,
    ) -> None:
        """Place one datagram's lines into the frame under assembly.

        Caller holds ``self._lock``.
        """
        # Frame transition: new frame_number means previous frame is done
        if self._cur_frame_num is not None and frame_num != self._cur_frame_num:
            self._finalize_frame()

        self._cur_frame_num = frame_num
        self._cur_frame_width = pixels_per_line

        # Store each line from this packet
        for i in range(lines_per_packet):
            row_start = i * pixels_per_line
            row_end = row_start + pixels_per_line
            if row_end <= len(unpacked):
                self._cur_frame_lines[line_num + i] = unpacked[row_start:row_end]

        # Frame-end marker: finalize immediately
        if frame_end:
            self._finalize_frame()
            self._cur_frame_num = None

    def _sync_counters(self) -> None:
        """Mirror the tracker's counts into this capture's attributes."""
        self._packets_dropped = self._seq.dropped
        self._packets_reordered = self._seq.reordered
        self._sequence_resyncs = self._seq.resyncs
        self._payloads_discarded = self._seq.discarded
        self._last_seq = self._seq.highest

    def stop(self) -> VideoCaptureResult:
        """Stop capturing and return assembled frames.

        Returns:
            VideoCaptureResult with frames and capture statistics.
        """
        if not self._started:
            raise RuntimeError("VideoCapture not started")

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        duration = time.monotonic() - self._capture_start

        with self._lock:
            # Whatever is still held was never decided: take it as
            # duplicates, as the audio and debug receivers do.
            held = self._seq.flush_held()
            if held:
                _log.warning(
                    "Video stream: %d datagram(s) were still undecided; "
                    "discarded as duplicates", held,
                )
            self._sync_counters()

            # Finalize any in-progress frame
            self._finalize_frame()

            frames = list(self._frames)
            packets_received = self._packets_received
            packets_dropped = self._packets_dropped
            packets_reordered = self._packets_reordered
            sequence_resyncs = self._sequence_resyncs
            payloads_discarded = self._payloads_discarded
            stale_packets = self._stale_packets
            frames_dropped = self._frames_dropped

        _log.info(
            "VideoCapture stopped: %.2fs, %d frames, %d packets (%d dropped, "
            "%d reordered, %d resyncs, %d discarded, %d stale), %d frames dropped",
            duration, len(frames), packets_received, packets_dropped,
            packets_reordered, sequence_resyncs, payloads_discarded,
            stale_packets, frames_dropped,
        )

        self._started = False

        return VideoCaptureResult(
            frames=frames,
            duration_seconds=duration,
            packets_received=packets_received,
            packets_dropped=packets_dropped,
            frames_completed=len(frames),
            frames_dropped=frames_dropped,
            packets_reordered=packets_reordered,
            sequence_resyncs=sequence_resyncs,
            payloads_discarded=payloads_discarded,
            stale_packets=stale_packets,
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

    @property
    def frames_completed(self) -> int:
        """Number of complete frames assembled so far (thread-safe)."""
        with self._lock:
            return len(self._frames)
