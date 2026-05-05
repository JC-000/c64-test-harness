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
from dataclasses import dataclass, field

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

    Sequence numbers are tracked for gap detection.  Gaps are logged
    but frames with missing packets are still assembled (with gaps in
    line data tracked as dropped frames).
    """

    def __init__(
        self,
        port: int = DEFAULT_VIDEO_PORT,
        bind_addr: str = "",
        multicast_group: str | None = None,
        recv_buf_size: int = 262144,
    ) -> None:
        """
        Args:
            port: UDP port to listen on.
            bind_addr: Address to bind to (empty = all interfaces).
            multicast_group: If set, join this multicast group.
            recv_buf_size: SO_RCVBUF size hint.
        """
        self._port = port
        self._bind_addr = bind_addr
        self._multicast_group = multicast_group
        self._recv_buf_size = recv_buf_size

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Frame assembly state
        self._frames: list[VideoFrame] = []
        self._packets_received = 0
        self._packets_dropped = 0
        self._frames_dropped = 0
        self._last_seq: int | None = None
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
        self._frames_dropped = 0
        self._last_seq = None
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
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(self._multicast_group),
                socket.inet_aton("0.0.0.0"),
            )
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

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

            with self._lock:
                # Sequence gap detection
                if self._last_seq is not None:
                    expected = (self._last_seq + 1) & 0xFFFF
                    if seq != expected:
                        gap = (seq - expected) & 0xFFFF
                        if gap < 0x8000:  # forward gap (not reorder)
                            self._packets_dropped += gap
                            _log.warning(
                                "Video stream gap: expected seq %d, got %d (%d packets dropped)",
                                expected, seq, gap,
                            )
                self._last_seq = seq
                self._packets_received += 1

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
            # Finalize any in-progress frame
            self._finalize_frame()

            frames = list(self._frames)
            packets_received = self._packets_received
            packets_dropped = self._packets_dropped
            frames_dropped = self._frames_dropped

        _log.info(
            "VideoCapture stopped: %.2fs, %d frames, %d packets (%d dropped), %d frames dropped",
            duration, len(frames), packets_received, packets_dropped, frames_dropped,
        )

        self._started = False

        return VideoCaptureResult(
            frames=frames,
            duration_seconds=duration,
            packets_received=packets_received,
            packets_dropped=packets_dropped,
            frames_completed=len(frames),
            frames_dropped=frames_dropped,
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
