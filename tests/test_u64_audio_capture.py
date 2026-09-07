"""Unit tests for u64_audio_capture module (AudioCapture, write_wav, CaptureResult)."""
from __future__ import annotations

import errno
import os
import socket
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest

from c64_test_harness.backends import u64_audio_capture as uac
from c64_test_harness.backends.u64_audio_capture import (
    CHANNELS,
    DEFAULT_AUDIO_PORT,
    DEFAULT_SAMPLE_RATE,
    SAMPLE_WIDTH,
    EPHEMERAL_AUDIO_PORT,
    AudioCapture,
    AudioCapturePortInUseError,
    CaptureResult,
    write_wav,
)


# ---------------------------------------------------------------- helpers


def _started_capture(**kwargs) -> AudioCapture:
    """An ``AudioCapture`` listening on a port nobody else can take.

    Binding port 0 and asking the socket which port it got is the only
    collision-free way to do this on a shared host. The previous helper
    reserved a port by binding it, handed back the number and let the
    caller close the placeholder before its own ``bind()`` -- which is a
    TOCTOU window, and is how ``test_audio_capture_gap_detection`` died
    with ``[Errno 48] Address already in use`` during the PR #229 full
    suite while another lane was running (issue #230).
    """
    cap = AudioCapture(port=EPHEMERAL_AUDIO_PORT, **kwargs)
    cap.start()
    return cap


def _send_test_packets(port: int, packets: list[tuple[int, bytes]]) -> None:
    """Send test packets [(seq, pcm_bytes), ...] to localhost:port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for seq, pcm in packets:
            header = struct.pack("<H", seq)
            sock.sendto(header + pcm, ("127.0.0.1", port))
            time.sleep(0.01)
    finally:
        sock.close()


def _make_pcm(n_frames: int = 100) -> bytes:
    """Create fake stereo 16-bit PCM data (n_frames * 4 bytes)."""
    return b"\x00\x01\x00\x02" * n_frames


# ---------------------------------------------------------------- write_wav


def test_write_wav_creates_valid_file(tmp_path: Path) -> None:
    pcm = _make_pcm(200)
    out = tmp_path / "test.wav"
    result = write_wav(out, pcm)
    assert result == out
    assert out.exists()

    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == CHANNELS
        assert wf.getsampwidth() == SAMPLE_WIDTH
        assert wf.getframerate() == DEFAULT_SAMPLE_RATE
        assert wf.getnframes() == 200


def test_write_wav_empty_data(tmp_path: Path) -> None:
    out = tmp_path / "empty.wav"
    write_wav(out, b"")
    assert out.exists()

    with wave.open(str(out), "rb") as wf:
        assert wf.getnframes() == 0
        assert wf.getnchannels() == CHANNELS
        assert wf.getsampwidth() == SAMPLE_WIDTH


# ---------------------------------------------------------------- CaptureResult


def test_capture_result_fields() -> None:
    cr = CaptureResult(
        wav_path=Path("/tmp/test.wav"),
        duration_seconds=2.5,
        sample_rate=48000,
        total_samples=120000,
        packets_received=500,
        packets_dropped=3,
    )
    assert cr.wav_path == Path("/tmp/test.wav")
    assert cr.duration_seconds == 2.5
    assert cr.sample_rate == 48000
    assert cr.total_samples == 120000
    assert cr.packets_received == 500
    assert cr.packets_dropped == 3


# ---------------------------------------------------------------- AudioCapture lifecycle


def test_audio_capture_start_stop_no_packets() -> None:
    cap = _started_capture()
    port = cap.port
    result = cap.stop()
    assert result.packets_received == 0
    assert result.packets_dropped == 0
    assert result.duration_seconds == 0.0
    assert result.total_samples == 0


def test_audio_capture_double_start_raises() -> None:
    cap = _started_capture()
    port = cap.port
    try:
        with pytest.raises(RuntimeError, match="already started"):
            cap.start()
    finally:
        cap.stop()


def test_audio_capture_stop_not_started_raises() -> None:
    cap = AudioCapture(port=EPHEMERAL_AUDIO_PORT)
    with pytest.raises(RuntimeError, match="not started"):
        cap.stop()


# ---------------------------------------------------------------- packet reception


def test_audio_capture_receives_packets() -> None:
    cap = _started_capture()
    port = cap.port
    try:
        pcm = _make_pcm(10)
        _send_test_packets(port, [(0, pcm), (1, pcm), (2, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 3
    assert result.packets_dropped == 0


def test_audio_capture_gap_detection() -> None:
    cap = _started_capture()
    port = cap.port
    try:
        pcm = _make_pcm(10)
        # seq 0, 1, then skip to 5 -> gap of 3
        _send_test_packets(port, [(0, pcm), (1, pcm), (5, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 3
    assert result.packets_dropped == 3


def test_audio_capture_backward_step_is_counted_not_dropped() -> None:
    """A reordered or duplicated packet is a backward step, not a gap.

    ``time_base_intact`` counts forward gaps only; a caller that needs
    the sample index to be a clock must also check ``packets_reordered``.
    """
    cap = _started_capture()
    port = cap.port
    try:
        pcm = _make_pcm(10)
        # seq 0, 1, 2, then 1 again (duplicate), then 3 (a forward gap
        # of 1 relative to the duplicate's successor, 2)
        _send_test_packets(port, [(0, pcm), (1, pcm), (2, pcm), (1, pcm), (3, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 5
    assert result.packets_reordered == 1
    assert result.packets_dropped == 1
    assert result.time_base_intact is False


def test_audio_capture_sequence_wrap() -> None:
    cap = _started_capture()
    port = cap.port
    try:
        pcm = _make_pcm(10)
        _send_test_packets(port, [(0xFFFE, pcm), (0xFFFF, pcm), (0x0000, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 3
    assert result.packets_dropped == 0


def test_audio_capture_writes_wav(tmp_path: Path) -> None:
    cap = _started_capture()
    port = cap.port
    try:
        pcm = _make_pcm(50)
        _send_test_packets(port, [(0, pcm), (1, pcm)])
        time.sleep(0.1)
    finally:
        wav_path = tmp_path / "capture.wav"
        result = cap.stop(wav_path=wav_path)
    assert wav_path.exists()
    assert result.wav_path == wav_path

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() > 0


# ---------------------------------------------------------------- properties


def test_audio_capture_is_capturing_property() -> None:
    cap = AudioCapture(port=EPHEMERAL_AUDIO_PORT)
    assert cap.is_capturing is False
    cap.start()
    assert cap.is_capturing is True
    cap.stop()
    assert cap.is_capturing is False


def test_audio_capture_packets_received_property() -> None:
    cap = _started_capture()
    port = cap.port
    try:
        assert cap.packets_received == 0
        pcm = _make_pcm(10)
        _send_test_packets(port, [(0, pcm), (1, pcm)])
        time.sleep(0.1)
        assert cap.packets_received == 2
    finally:
        cap.stop()


# ---------------------------------------------------------------- port binding (#230)


class TestPortBinding:
    """The suite must not be able to fail because the host is shared.

    #230: a fixed or reserved-then-released port races every other
    process on the machine. Three properties matter -- an ephemeral
    request must report the port it actually got, two captures must
    never be able to collide, and a genuinely busy port must fail with
    something a reader can act on.
    """

    def test_ephemeral_request_reports_the_bound_port(self) -> None:
        cap = AudioCapture(port=EPHEMERAL_AUDIO_PORT)
        assert cap.port == 0, "nothing is bound before start()"
        cap.start()
        try:
            bound = cap.port
            assert bound != 0
            # The reported port is the one packets actually arrive on --
            # a number that merely looks plausible is not enough.
            _send_test_packets(bound, [(0, _make_pcm(4))])
            time.sleep(0.2)
            assert cap.packets_received == 1, (
                f"port property reported {bound} but nothing arrived there"
            )
        finally:
            cap.stop()

    def test_two_ephemeral_captures_get_different_ports(self) -> None:
        a = AudioCapture(port=EPHEMERAL_AUDIO_PORT)
        b = AudioCapture(port=EPHEMERAL_AUDIO_PORT)
        a.start()
        try:
            b.start()
        except OSError:  # pragma: no cover - the bug this test exists for
            a.stop()
            raise
        try:
            assert a.port != b.port
            _send_test_packets(a.port, [(0, _make_pcm(4))])
            time.sleep(0.2)
            assert a.packets_received == 1
            assert b.packets_received == 0, (
                "the two captures are sharing a port"
            )
        finally:
            a.stop()
            b.stop()

    def test_busy_port_raises_a_named_error(self) -> None:
        """Not a bare Errno 48: the message must name the port."""
        squatter = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        squatter.bind(("127.0.0.1", 0))
        busy = squatter.getsockname()[1]
        try:
            cap = AudioCapture(port=busy, bind_addr="127.0.0.1")
            with pytest.raises(AudioCapturePortInUseError) as exc:
                cap.start()
            assert exc.value.port == busy
            assert str(busy) in str(exc.value)
            assert cap.is_capturing is False
        finally:
            squatter.close()
        # A failed start must not leave the object half-started: with the
        # squatter gone, the same instance must be startable, which is
        # the consequence a caller would actually notice (a leaked socket
        # makes the retry raise "already started" instead).
        cap.start()
        cap.stop()

    def test_busy_port_raises_for_the_default_bind_address(self) -> None:
        """The case a `127.0.0.1`-only test cannot see.

        Production callers get ``bind_addr=""``. With ``SO_REUSEADDR``
        set unconditionally, a loopback squatter and a wildcard capture
        bound *both* -- silently, which is the whole failure mode of
        #230. Measured on this host: 127.0.0.1 vs "" bound with the flag
        and raised without it, so the flag was the cause, not the
        address family.
        """
        squatter = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        squatter.bind(("127.0.0.1", 0))
        busy = squatter.getsockname()[1]
        try:
            cap = AudioCapture(port=busy)  # default bind_addr=""
            with pytest.raises(AudioCapturePortInUseError):
                cap.start()
        finally:
            squatter.close()

    def test_multicast_captures_may_still_share_a_port(self) -> None:
        """The exception, and why the flag is conditional rather than gone.

        Several listeners on one multicast group and port is the normal
        case, so that must keep working or the loud-collision fix above
        has broken it silently.

        Two successful binds do not demonstrate this: ``SO_REUSEPORT``
        distributes *unicast* datagrams to exactly one of the sockets,
        so a probe sent to 127.0.0.1 arrives at one capture and the test
        would pass with sharing broken. The probe therefore goes to the
        group address, which is the traffic the property is about.
        ``IP_MULTICAST_IF`` is left at the default on purpose -- pinning
        it to 127.0.0.1 makes both captures receive nothing at all.
        """
        first = AudioCapture(
            port=EPHEMERAL_AUDIO_PORT,
            multicast_group="239.0.1.65",
            bind_addr="",
        )
        first.start()
        try:
            second = AudioCapture(
                port=first.port,
                multicast_group="239.0.1.65",
                bind_addr="",
            )
            second.start()  # must not raise
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                try:
                    for seq in range(6):
                        sock.sendto(
                            struct.pack("<H", seq) + _make_pcm(4),
                            ("239.0.1.65", first.port),
                        )
                        time.sleep(0.01)
                finally:
                    sock.close()
                time.sleep(0.3)
                assert first.packets_received == 6
                assert second.packets_received == 6, (
                    "only one capture received the group traffic -- the "
                    "port is shared but the frames are not"
                )
            finally:
                second.stop()
        finally:
            first.stop()

    def test_the_error_carries_errno_eaddrinuse(self) -> None:
        """The OSError-subclass promise has to be real, not prose.

        The idiomatic handler is
        ``except OSError as e: if e.errno == errno.EADDRINUSE`` -- which
        is the exact shape of the failure #230 was filed for. Without an
        errno set, subclassing OSError silently stops such a caller from
        matching.
        """
        squatter = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        squatter.bind(("127.0.0.1", 0))
        busy = squatter.getsockname()[1]
        try:
            cap = AudioCapture(port=busy, bind_addr="127.0.0.1")
            with pytest.raises(OSError) as exc:
                cap.start()
            assert exc.value.errno == errno.EADDRINUSE
            # ...without the errno leaking into the message, which is
            # what passing it through OSError.__init__ would have done.
            assert not str(exc.value).startswith("[Errno")
        finally:
            squatter.close()

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root may bind a privileged port, so there is no EACCES",
    )
    def test_a_privilege_failure_is_not_reported_as_in_use(self) -> None:
        """Port 80 as an ordinary user: nobody holds it, we may not have it.

        Reporting that as "already in use" sends the reader after a
        holder that does not exist, with a remedy (bind an ephemeral
        port) that does not address the actual problem.
        """
        cap = AudioCapture(port=80, bind_addr="127.0.0.1")
        with pytest.raises(OSError) as exc:
            cap.start()
        assert not isinstance(exc.value, AudioCapturePortInUseError), (
            "a permission failure was reported as a busy port"
        )
        assert exc.value.errno == errno.EACCES

    def test_port_holder_never_raises_and_never_hangs(
        self, monkeypatch
    ) -> None:
        """The diagnostic must not be able to fail the call it diagnoses.

        ``_port_holder`` runs inside the exception path of ``start()``.
        A host without ``lsof``, or one where it is slow, must yield a
        message without a holder rather than an exception on top of the
        original one.
        """
        free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        assert uac._port_holder(port) is None  # nobody holds it

        def _no_such_binary(*args, **kwargs):
            raise FileNotFoundError("lsof")

        monkeypatch.setattr(uac.subprocess, "run", _no_such_binary)
        assert uac._port_holder(port) is None

        def _times_out(*args, **kwargs):
            raise subprocess.TimeoutExpired("lsof", 2.0)

        monkeypatch.setattr(uac.subprocess, "run", _times_out)
        assert uac._port_holder(port) is None

    def test_restart_redraws_an_ephemeral_port(self) -> None:
        """A second start() must not re-bind the first port.

        Freezing the resolved port would be invisible while the port
        stays free, so the first port is held by someone else before the
        restart -- exactly the shared-host case.
        """
        cap = AudioCapture(port=EPHEMERAL_AUDIO_PORT, bind_addr="127.0.0.1")
        cap.start()
        first = cap.port
        cap.stop()

        squatter = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        squatter.bind(("127.0.0.1", first))
        try:
            cap.start()  # must pick a different port, not raise
            try:
                assert cap.port != first
            finally:
                cap.stop()
        finally:
            squatter.close()


# ---------------------------------------------------------------- constants


def test_constants() -> None:
    assert DEFAULT_AUDIO_PORT == 11001
    assert DEFAULT_SAMPLE_RATE == 48000
    assert CHANNELS == 2
    assert SAMPLE_WIDTH == 2


# ---------------------------------------------------------------- multicast


def test_audio_capture_multicast_join() -> None:
    """Verify multicast_group parameter is accepted at construction time."""
    cap = AudioCapture(port=EPHEMERAL_AUDIO_PORT, multicast_group="239.0.1.65")
    # Just verify construction succeeds and the attribute is stored
    assert cap._multicast_group == "239.0.1.65"


# ---------------------------------------------------------------- runt packet


def test_audio_capture_runt_packet_ignored() -> None:
    cap = _started_capture()
    port = cap.port
    try:
        # Send a 1-byte packet (runt) and a 2-byte packet (just header, also runt)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"\x00", ("127.0.0.1", port))
            sock.sendto(b"\x00\x00", ("127.0.0.1", port))
            time.sleep(0.01)
            # Now send a valid packet
            pcm = _make_pcm(10)
            header = struct.pack("<H", 0)
            sock.sendto(header + pcm, ("127.0.0.1", port))
            time.sleep(0.1)
        finally:
            sock.close()
    finally:
        result = cap.stop()
    # Only the valid packet should be counted
    assert result.packets_received == 1
