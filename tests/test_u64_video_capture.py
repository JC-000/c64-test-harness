"""Unit tests for u64_video_capture module (VideoFrame, VideoCapture, _unpack_4bit)."""
from __future__ import annotations

import itertools
import socket
import struct
import time
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.u64_video_capture import (
    VIC_PALETTE,
    VIDEO_HEADER_SIZE,
    VideoCapture,
    VideoCaptureResult,
    VideoFrame,
    _unpack_4bit,
)


# ---------------------------------------------------------------- helpers


def _build_video_packet(
    seq: int,
    frame_num: int,
    line_num: int,
    *,
    frame_end: bool = False,
    width: int = 384,
    lines: int = 4,
    fill: int = 0,
) -> bytes:
    """Build a video capture UDP packet.

    Header (12 bytes LE):
        seq(u16), frame_num(u16), raw_line(u16), pixels_per_line(u16),
        lines_per_packet(u8), bpp(u8), encoding(u16)

    ``fill`` is the byte the 4-bit pixel payload repeats.  It is what the
    sequence tracker digests, so two packets with different fills are
    distinct datagrams and two with the same fill are indistinguishable
    from a re-send.
    """
    raw_line = line_num | (0x8000 if frame_end else 0)
    header = struct.pack("<HHHHBBH", seq, frame_num, raw_line, width, lines, 4, 0)
    # 4-bit packed: width * lines / 2 bytes
    payload = bytes([fill]) * (width * lines // 2)
    return header + payload


# ---------------------------------------------------------------- VideoFrame


class TestVideoFrame:
    """Tests for VideoFrame pixel access."""

    def test_pixel_at(self):
        # 4x2 frame: row 0 = [1,2,3,4], row 1 = [5,6,7,8]
        pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        frame = VideoFrame(frame_number=0, width=4, height=2, pixels=pixels)
        assert frame.pixel_at(0, 0) == 1
        assert frame.pixel_at(3, 0) == 4
        assert frame.pixel_at(0, 1) == 5
        assert frame.pixel_at(3, 1) == 8

    def test_pixel_at_out_of_range(self):
        pixels = bytes([0] * 8)
        frame = VideoFrame(frame_number=0, width=4, height=2, pixels=pixels)
        with pytest.raises(IndexError):
            frame.pixel_at(4, 0)
        with pytest.raises(IndexError):
            frame.pixel_at(0, 2)
        with pytest.raises(IndexError):
            frame.pixel_at(-1, 0)

    def test_row(self):
        pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        frame = VideoFrame(frame_number=0, width=4, height=2, pixels=pixels)
        assert frame.row(0) == bytes([1, 2, 3, 4])
        assert frame.row(1) == bytes([5, 6, 7, 8])

    def test_row_out_of_range(self):
        pixels = bytes([0] * 8)
        frame = VideoFrame(frame_number=0, width=4, height=2, pixels=pixels)
        with pytest.raises(IndexError):
            frame.row(2)
        with pytest.raises(IndexError):
            frame.row(-1)


# ---------------------------------------------------------------- _unpack_4bit


class TestUnpack4bit:
    """Tests for the 4-bit pixel unpacking function."""

    def test_unpack_single_byte(self):
        # 0x21 → low nibble 1, high nibble 2
        assert _unpack_4bit(b"\x21") == b"\x01\x02"

    def test_unpack_all_colors(self):
        # 0xF0 → low nibble 0, high nibble 15
        assert _unpack_4bit(b"\xF0") == b"\x00\x0F"

    def test_unpack_empty(self):
        assert _unpack_4bit(b"") == b""

    def test_unpack_multiple_bytes(self):
        # 0x12 → [2, 1], 0x34 → [4, 3]
        result = _unpack_4bit(b"\x12\x34")
        assert result == bytes([2, 1, 4, 3])


# ---------------------------------------------------------------- VIC_PALETTE


class TestVicPalette:
    """Tests for the VIC-II colour palette."""

    def test_palette_length(self):
        assert len(VIC_PALETTE) == 16

    def test_palette_black_white(self):
        assert VIC_PALETTE[0] == (0x00, 0x00, 0x00)  # Black
        assert VIC_PALETTE[1] == (0xFF, 0xFF, 0xFF)  # White


# ---------------------------------------------------------------- VideoCapture


class TestVideoCapture:
    """Tests for VideoCapture with mocked sockets."""

    def test_start_stop_empty(self):
        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.repeat(socket.timeout())
        )

        with patch(
            "c64_test_harness.backends.u64_video_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = VideoCapture()
            cap.start()
            time.sleep(0.1)
            result = cap.stop()

        assert result.frames_completed == 0
        assert result.packets_received == 0

    def test_single_complete_frame(self):
        # PAL frame: 384 x 272. Each packet carries 4 lines.
        # 272 / 4 = 68 packets needed.
        width = 384
        lines_per_pkt = 4
        height = 272
        num_packets = height // lines_per_pkt  # 68

        packets = []
        for i in range(num_packets):
            line_num = i * lines_per_pkt
            is_last = i == num_packets - 1
            pkt = _build_video_packet(
                seq=i,
                frame_num=0,
                line_num=line_num,
                frame_end=is_last,
                width=width,
                lines=lines_per_pkt,
            )
            packets.append((pkt, ("10.0.0.1", 11000)))

        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.chain(packets, itertools.repeat(socket.timeout()))
        )

        with patch(
            "c64_test_harness.backends.u64_video_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = VideoCapture()
            cap.start()
            time.sleep(0.2)
            result = cap.stop()

        assert result.frames_completed == 1
        assert result.packets_received == num_packets
        frame = result.frames[0]
        assert frame.width == width
        assert frame.height == height

    def test_frame_transition(self):
        # Two minimal frames: frame 0 (1 packet, frame_end) then frame 1 (1 packet, frame_end)
        pkt0 = _build_video_packet(seq=0, frame_num=0, line_num=0, frame_end=True, width=384, lines=4)
        pkt1 = _build_video_packet(seq=1, frame_num=1, line_num=0, frame_end=True, width=384, lines=4)

        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.chain(
                [
                    (pkt0, ("10.0.0.1", 11000)),
                    (pkt1, ("10.0.0.1", 11000)),
                ],
                itertools.repeat(socket.timeout()),
            )
        )

        with patch(
            "c64_test_harness.backends.u64_video_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = VideoCapture()
            cap.start()
            time.sleep(0.1)
            result = cap.stop()

        assert result.frames_completed == 2

    def test_already_started_raises(self):
        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.repeat(socket.timeout())
        )

        with patch(
            "c64_test_harness.backends.u64_video_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = VideoCapture()
            cap.start()
            try:
                with pytest.raises(RuntimeError):
                    cap.start()
            finally:
                cap.stop()

    def test_not_started_raises(self):
        cap = VideoCapture()
        with pytest.raises(RuntimeError):
            cap.stop()


# ------------------------------------------------- frame assembly vs reorder


def _capture(packets: list[bytes]) -> VideoCaptureResult:
    """Feed *packets* to a VideoCapture through a mocked socket."""
    mock_sock = MagicMock()
    mock_sock.recvfrom = MagicMock(
        side_effect=itertools.chain(
            [(p, ("10.0.0.1", 11000)) for p in packets],
            itertools.repeat(socket.timeout()),
        )
    )
    with patch(
        "c64_test_harness.backends.u64_video_capture.socket.socket",
        return_value=mock_sock,
    ):
        cap = VideoCapture()
        cap.start()
        deadline = time.monotonic() + 2.0
        while cap.packets_received < len(packets) and time.monotonic() < deadline:
            time.sleep(0.005)
        return cap.stop()


class TestReorderAndFrameAssembly:
    """What a late, duplicated or re-admitted datagram does to a frame (#442).

    The decision under test: **assembly is driven by each datagram's own
    ``frame_num``/``line_num`` header, and the sequence tracker governs the
    counters plus one assembly rule** -- a late datagram is applied only if
    it belongs to the frame still being assembled.  A late datagram for a
    frame already finalised cannot repair it (it has been emitted or
    counted dropped), and letting it through would finalise the frame in
    progress early and start re-assembling the old frame number.
    """

    def test_a_swap_inside_the_current_frame_still_completes_it(self) -> None:
        """Control: the lines arrive out of order but all before frame end."""
        result = _capture([
            _build_video_packet(seq=0, frame_num=0, line_num=0, fill=0x11),
            _build_video_packet(seq=2, frame_num=0, line_num=8, fill=0x33),
            _build_video_packet(seq=1, frame_num=0, line_num=4, fill=0x22),
            _build_video_packet(
                seq=3, frame_num=0, line_num=12, frame_end=True, fill=0x44
            ),
        ])
        assert result.packets_dropped == 0
        assert result.packets_reordered == 1
        assert result.frames_dropped == 0
        assert [(f.frame_number, f.height) for f in result.frames] == [(0, 16)]
        assert result.frames[0].row(4) == bytes([2]) * 384

    def test_a_late_packet_for_a_finished_frame_does_not_corrupt_the_next(
        self,
    ) -> None:
        """Frame 0 is already out when its missing line turns up.

        Applying it would finalise frame 1 after one packet (emitting a
        truncated frame) and then re-open frame 0, which is finalised
        incomplete in turn: one reordered datagram, two corrupted frames.
        """
        result = _capture([
            _build_video_packet(
                seq=0, frame_num=0, line_num=0, frame_end=True, fill=0x11
            ),
            _build_video_packet(seq=2, frame_num=1, line_num=0, fill=0x22),
            _build_video_packet(seq=1, frame_num=0, line_num=4, fill=0x33),
            _build_video_packet(
                seq=3, frame_num=1, line_num=4, frame_end=True, fill=0x44
            ),
        ])
        assert result.packets_dropped == 0
        assert result.packets_reordered == 1
        assert result.stale_packets == 1
        assert result.frames_dropped == 0
        assert [(f.frame_number, f.height) for f in result.frames] == [
            (0, 4), (1, 8)
        ]

    def test_a_duplicate_datagram_is_discarded_not_counted_as_loss(self) -> None:
        result = _capture([
            _build_video_packet(seq=0, frame_num=0, line_num=0, fill=0x11),
            _build_video_packet(seq=1, frame_num=0, line_num=4, fill=0x22),
            _build_video_packet(seq=1, frame_num=0, line_num=4, fill=0x22),
            _build_video_packet(
                seq=2, frame_num=0, line_num=8, frame_end=True, fill=0x33
            ),
        ])
        assert result.packets_dropped == 0
        assert result.packets_reordered == 1
        assert result.payloads_discarded == 1
        assert [(f.frame_number, f.height) for f in result.frames] == [(0, 12)]

    def test_a_restart_re_admits_the_lines_it_held(self) -> None:
        """A held run that turns out to be a restarted counter, not a duplicate.

        Datagram 4 repeats sequence 1 with byte-identical pixels for a
        different line, so the tracker holds it undecided; datagram 5
        continues that run with new pixels, which makes it a restart, and
        the held datagram's line is re-admitted ahead of it.  Without the
        re-admission line 12 never reaches the frame and it is finalised
        incomplete.
        """
        result = _capture([
            _build_video_packet(seq=0, frame_num=0, line_num=0, fill=0x11),
            _build_video_packet(seq=1, frame_num=0, line_num=4, fill=0x22),
            _build_video_packet(seq=2, frame_num=0, line_num=8, fill=0x33),
            _build_video_packet(seq=1, frame_num=0, line_num=12, fill=0x22),
            _build_video_packet(
                seq=2, frame_num=0, line_num=8, frame_end=True, fill=0x44
            ),
        ])
        assert result.sequence_resyncs == 1
        assert result.packets_dropped == 0
        assert result.frames_dropped == 0
        assert [(f.frame_number, f.height) for f in result.frames] == [(0, 16)]
        assert result.frames[0].row(12) == bytes([2]) * 384
        assert result.frames[0].row(8) == bytes([4]) * 384

    def test_a_duplicate_of_an_earlier_frame_does_not_reopen_it(self) -> None:
        """The held rule earns its keep here (mutation M4 of the #442 PR).

        Datagram 4 duplicates sequence 1, which belonged to frame 0 -- long
        finalised.  Applying it, rather than holding it undecided, finalises
        frame 1 after one packet and re-opens frame 0, so one duplicate
        costs two frames.  Pixel-identical payloads make the duplicate
        invisible *within* a frame, which is why the case that proves the
        rule has to cross a frame boundary.
        """
        result = _capture([
            _build_video_packet(seq=0, frame_num=0, line_num=0, fill=0x11),
            _build_video_packet(
                seq=1, frame_num=0, line_num=4, frame_end=True, fill=0x22
            ),
            _build_video_packet(seq=2, frame_num=1, line_num=0, fill=0x33),
            _build_video_packet(seq=1, frame_num=0, line_num=4, fill=0x22),
            _build_video_packet(
                seq=3, frame_num=1, line_num=4, frame_end=True, fill=0x44
            ),
        ])
        assert result.packets_dropped == 0
        assert result.packets_reordered == 1
        assert result.payloads_discarded == 1
        assert result.frames_dropped == 0
        assert [(f.frame_number, f.height) for f in result.frames] == [
            (0, 8), (1, 8)
        ]
