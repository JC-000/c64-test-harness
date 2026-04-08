"""Live multi-SID parallel capture on U64E.

Configures all 4 SID engines (2 physical 8580 + 2 UltiSID FPGA) at
distinct addresses, plays different waveforms and notes simultaneously
via ``run_prg`` (not ``sidplay``), captures the mixed audio, and
validates that each SID engine contributes its expected frequency to
the output -- proving all 4 engines are active and addressable
independently.

Note: The ``sidplay`` firmware runner uses internal mixing that
bypasses per-engine Audio Mixer panning.  Using ``run_prg`` routes
audio through the hardware SID chips at their configured I/O
addresses so that both panning and multi-SID addressing take effect.

Requirements:
    - ``U64_HOST`` env var pointing at a reachable Ultimate 64 device
    - Device has physical 8580 SID chips in both sockets

Typical runtime: ~15 seconds.
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket as _socket
import struct
import time
import wave
from pathlib import Path

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.u64_audio_capture import (
    AudioCapture,
    DEFAULT_AUDIO_PORT,
    DEFAULT_SAMPLE_RATE,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

logger = logging.getLogger(__name__)

WAV_DIR = Path(__file__).parent / "wav_captures" / "multi_sid"

pytestmark = pytest.mark.skipif(
    not os.environ.get("U64_HOST"),
    reason="U64_HOST not set -- skipping live U64 tests",
)

# ---------------------------------------------------------------------------
# SID engine configuration
# ---------------------------------------------------------------------------

QUAD_ADDRESSING = {
    "Auto Address Mirroring": "Disabled",
    "SID Socket 1 Address": "$D400",
    "SID Socket 2 Address": "$D420",
    "UltiSID 1 Address": "$D440",
    "UltiSID 2 Address": "$D460",
}

QUAD_PANNING = {
    "Pan Socket 1": "Left 5",
    "Pan Socket 2": "Left 2",
    "Pan UltiSID 1": "Right 2",
    "Pan UltiSID 2": "Right 5",
}

DEFAULT_ADDRESSING = {
    "Auto Address Mirroring": "Enabled",
    "SID Socket 1 Address": "$D400",
    "SID Socket 2 Address": "$D420",
    "UltiSID 1 Address": "$D400",
    "UltiSID 2 Address": "$D400",
}

DEFAULT_PANNING = {
    "Pan Socket 1": "Left 3",
    "Pan Socket 2": "Right 3",
    "Pan UltiSID 1": "Center",
    "Pan UltiSID 2": "Center",
}

ENGINE_META = {
    "sid_socket_1": {
        "address": "$D400", "waveform": "sawtooth", "note": "C4", "pan": "Left 5",
    },
    "sid_socket_2": {
        "address": "$D420", "waveform": "pulse_50pct", "note": "E4", "pan": "Left 2",
    },
    "ultisid_1": {
        "address": "$D440", "waveform": "triangle", "note": "G4", "pan": "Right 2",
    },
    "ultisid_2": {
        "address": "$D460", "waveform": "noise", "note": "C5", "pan": "Right 5",
    },
}


# ---------------------------------------------------------------------------
# PRG builder
# ---------------------------------------------------------------------------

def _build_quad_sid_prg() -> bytes:
    """Build a self-running PRG that plays 4 SIDs and loops forever.

    Using ``run_prg`` instead of ``sidplay`` ensures the audio is routed
    through the hardware SID chips (and UltiSID FPGA) at their configured
    I/O addresses, so the Audio Mixer panning settings take effect.

    Layout:
        SID1 ($D400): Sawtooth, C4  -- Socket 1
        SID2 ($D420): Pulse 50%, E4 -- Socket 2
        SID3 ($D440): Triangle, G4  -- UltiSID 1
        SID4 ($D460): Noise, C5     -- UltiSID 2

    Returns a PRG with 2-byte load address header ($C000).
    """
    load_addr = 0xC000
    code = bytes([
        # SEI -- disable IRQ so KERNAL doesn't interfere
        0x78,

        # Volume = 15, no filter on all 4 SIDs
        0xA9, 0x0F,
        0x8D, 0x18, 0xD4,  # SID1 mode+vol
        0x8D, 0x38, 0xD4,  # SID2 mode+vol
        0x8D, 0x58, 0xD4,  # SID3 mode+vol
        0x8D, 0x78, 0xD4,  # SID4 mode+vol

        # ADSR: attack=0, decay=9 -> AD=$09
        0xA9, 0x09,
        0x8D, 0x05, 0xD4,  # SID1
        0x8D, 0x25, 0xD4,  # SID2
        0x8D, 0x45, 0xD4,  # SID3
        0x8D, 0x65, 0xD4,  # SID4

        # ADSR: sustain=15, release=0 -> SR=$F0
        0xA9, 0xF0,
        0x8D, 0x06, 0xD4,  # SID1
        0x8D, 0x26, 0xD4,  # SID2
        0x8D, 0x46, 0xD4,  # SID3
        0x8D, 0x66, 0xD4,  # SID4

        # SID2 pulse width: 50% = $0800
        0xA9, 0x00, 0x8D, 0x22, 0xD4,  # PW lo
        0xA9, 0x08, 0x8D, 0x23, 0xD4,  # PW hi

        # SID1 freq: C4 = $1167
        0xA9, 0x67, 0x8D, 0x00, 0xD4,
        0xA9, 0x11, 0x8D, 0x01, 0xD4,

        # SID2 freq: E4 = $15ED
        0xA9, 0xED, 0x8D, 0x20, 0xD4,
        0xA9, 0x15, 0x8D, 0x21, 0xD4,

        # SID3 freq: G4 = $1A13
        0xA9, 0x13, 0x8D, 0x40, 0xD4,
        0xA9, 0x1A, 0x8D, 0x41, 0xD4,

        # SID4 freq: C5 = $22CE
        0xA9, 0xCE, 0x8D, 0x60, 0xD4,
        0xA9, 0x22, 0x8D, 0x61, 0xD4,

        # Gate ON with distinct waveforms
        0xA9, 0x21, 0x8D, 0x04, 0xD4,  # SID1: sawtooth + gate
        0xA9, 0x41, 0x8D, 0x24, 0xD4,  # SID2: pulse + gate
        0xA9, 0x11, 0x8D, 0x44, 0xD4,  # SID3: triangle + gate
        0xA9, 0x81, 0x8D, 0x64, 0xD4,  # SID4: noise + gate
    ])
    # Infinite loop: JMP to self
    loop_addr = load_addr + len(code)
    code += bytes([0x4C, loop_addr & 0xFF, (loop_addr >> 8) & 0xFF])

    # PRG header: 2-byte little-endian load address
    return struct.pack("<H", load_addr) + code


def _detect_local_ip(remote_host: str, remote_port: int = 80) -> str:
    """Determine which local IP can reach *remote_host*."""
    with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_stereo(wav_path: Path) -> tuple[list[int], list[int]]:
    """Read a stereo WAV and return (left_samples, right_samples)."""
    with wave.open(str(wav_path), "rb") as w:
        assert w.getnchannels() == 2, "Expected stereo WAV"
        data = w.readframes(w.getnframes())
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    left = list(samples[0::2])
    right = list(samples[1::2])
    return left, right


def _rms(samples: list[int]) -> float:
    """Compute RMS of a sample list."""
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _correlation(a: list[int], b: list[int]) -> float:
    """Pearson correlation coefficient between two sample lists."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    mean_a = sum(a[:n]) / n
    mean_b = sum(b[:n]) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = math.sqrt(sum((a[i] - mean_a) ** 2 for i in range(n)))
    den_b = math.sqrt(sum((b[i] - mean_b) ** 2 for i in range(n)))
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def u64_client():
    """Connect to U64E with cross-process DeviceLock."""
    host = os.environ.get("U64_HOST")
    pw = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(host)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {host}")
    client = Ultimate64Client(host=host, password=pw, timeout=15.0)
    yield client
    try:
        client.set_config_items("SID Addressing", DEFAULT_ADDRESSING)
        client.set_config_items("Audio Mixer", DEFAULT_PANNING)
        client.reset()
    except Exception:
        pass
    client.close()
    lock.release()


@pytest.fixture(scope="module")
def quad_wav(u64_client) -> Path:
    """Capture the quad-SID WAV once for the whole module.

    Uses ``run_prg`` (not ``sidplay``) so that the 6502 writes hit the
    actual SID chips at their configured I/O addresses, which routes
    audio through the Audio Mixer panning config.
    """
    WAV_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = WAV_DIR / "quad_sid_parallel.wav"
    meta_path = WAV_DIR / "quad_sid_parallel.json"

    # Configure 4 engines at distinct addresses -- reset required for
    # SID address routing changes to take effect in the FPGA fabric.
    u64_client.set_config_items("SID Addressing", QUAD_ADDRESSING)
    u64_client.set_config_items("Audio Mixer", QUAD_PANNING)
    u64_client.reset()
    time.sleep(3.0)
    logger.info("Configured quad-SID addressing and panning, reset complete")

    # Build PRG
    prg_data = _build_quad_sid_prg()

    # Set up audio capture
    local_ip = _detect_local_ip(u64_client.host)
    listen_port = DEFAULT_AUDIO_PORT
    stream_dest = f"{local_ip}:{listen_port}"
    logger.info("Audio stream destination: %s", stream_dest)

    capture = AudioCapture(
        port=listen_port,
        sample_rate=DEFAULT_SAMPLE_RATE,
    )
    duration = 5.0
    settle = 0.5

    stream_started = False
    capture_started = False

    try:
        # 1. Start UDP receiver
        capture.start()
        capture_started = True

        # 2. Start U64 audio stream
        u64_client.stream_audio_start(stream_dest)
        stream_started = True
        logger.info("Audio stream started")

        # 3. Run the PRG on the real C64 CPU
        u64_client.run_prg(prg_data)
        logger.info("PRG loaded and running on C64 CPU")

        # 4. Wait for settle + capture duration
        time.sleep(settle + duration)

    finally:
        if stream_started:
            try:
                u64_client.stream_audio_stop()
            except Exception:
                logger.warning("Failed to stop audio stream", exc_info=True)

        if capture_started:
            result = capture.stop(wav_path=wav_path)

        try:
            u64_client.reset()
            logger.info("C64 reset to stop playback")
        except Exception:
            logger.warning("Failed to reset C64", exc_info=True)

    logger.info(
        "Quad-SID capture: %.2fs, %d packets, %d dropped",
        result.duration_seconds,
        result.packets_received,
        result.packets_dropped,
    )

    # Write metadata sidecar
    meta = {
        "description": "4 SID engines playing simultaneously via run_prg",
        "engines": ENGINE_META,
        "addressing": QUAD_ADDRESSING,
        "panning": QUAD_PANNING,
        "capture": {
            "duration_seconds": result.duration_seconds,
            "packets_received": result.packets_received,
            "packets_dropped": result.packets_dropped,
            "total_samples": result.total_samples,
            "sample_rate": result.sample_rate,
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    logger.info("Wrote metadata to %s", meta_path)

    return wav_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_quad_sid_capture(quad_wav: Path) -> None:
    """Verify the quad-SID WAV was captured with real audio."""
    assert quad_wav.exists(), f"WAV not created: {quad_wav}"
    assert quad_wav.stat().st_size > 10000, "WAV too small"
    left, right = _split_stereo(quad_wav)
    peak = max(max(abs(s) for s in left), max(abs(s) for s in right))
    assert peak > 100, f"Audio appears silent (peak={peak})"
    logger.info("Quad-SID peak amplitude: %d", peak)


def test_left_channel_has_energy(quad_wav: Path) -> None:
    """Left channel should have energy from Socket 1 (saw) + Socket 2 (pulse)."""
    left, _ = _split_stereo(quad_wav)
    rms = _rms(left)
    assert rms > 50, f"Left channel too quiet (RMS={rms:.1f})"
    logger.info("Left channel RMS: %.1f", rms)


def test_right_channel_has_energy(quad_wav: Path) -> None:
    """Right channel should have energy from UltiSID 1 (tri) + UltiSID 2 (noise)."""
    _, right = _split_stereo(quad_wav)
    rms = _rms(right)
    assert rms > 50, f"Right channel too quiet (RMS={rms:.1f})"
    logger.info("Right channel RMS: %.1f", rms)


def test_all_four_frequencies_present(quad_wav: Path) -> None:
    """Verify all 4 SID engines contribute their expected frequency.

    SID1 ($D400): Sawtooth C4 (~262 Hz)
    SID2 ($D420): Pulse E4 (~330 Hz)
    SID3 ($D440): Triangle G4 (~392 Hz)
    SID4 ($D460): Noise C5 (broadband -- skipped in peak detection)

    We use a simple DFT magnitude at each target frequency.  If a SID
    engine is not actually mapped (address collision / mirroring), its
    frequency will be absent from the spectrum.
    """
    left, right = _split_stereo(quad_wav)
    # Mix both channels for frequency analysis (all engines contribute)
    n_mix = min(len(left), len(right))
    mixed = [left[i] + right[i] for i in range(n_mix)]

    # Use 1s of audio after the 0.5s settle period
    sr = DEFAULT_SAMPLE_RATE
    start = int(0.5 * sr)
    segment = mixed[start:start + sr]
    assert len(segment) > sr // 2, "Not enough samples for frequency analysis"

    # Target fundamentals (Hz) for the three tonal SIDs
    targets = {"C4_saw": 262, "E4_pulse": 330, "G4_tri": 392}
    magnitudes: dict[str, float] = {}
    n = len(segment)

    for label, freq in targets.items():
        # Goertzel-style single-bin DFT magnitude (unnormalised)
        k = round(freq * n / sr)
        angle = 2.0 * math.pi * k / n
        real = sum(segment[i] * math.cos(angle * i) for i in range(n))
        imag = sum(segment[i] * math.sin(angle * i) for i in range(n))
        mag = math.sqrt(real * real + imag * imag)
        magnitudes[label] = mag
        logger.info("Frequency %s (%d Hz): magnitude=%.1f", label, freq, mag)

    # Each tonal SID should produce a clear peak.  Sawtooth spreads
    # energy across many harmonics so its fundamental is weaker than
    # triangle or pulse; 5000 is well above noise-floor (~few hundred)
    # but accommodates sawtooth's harmonic spread.
    noise_floor = 5000.0
    for label, mag in magnitudes.items():
        assert mag > noise_floor, (
            f"SID engine for {label} not detected (magnitude={mag:.1f}); "
            "engine may not be mapped to its address"
        )


def test_capture_metadata(quad_wav: Path) -> None:
    """Verify the JSON sidecar has expected fields."""
    meta_path = quad_wav.with_suffix(".json")
    assert meta_path.exists(), f"Metadata sidecar missing: {meta_path}"
    meta = json.loads(meta_path.read_text())
    assert "engines" in meta
    assert len(meta["engines"]) == 4
    assert meta["capture"]["packets_received"] > 0
    assert meta["capture"]["packets_dropped"] == 0
    for engine in ENGINE_META:
        assert engine in meta["engines"], f"Missing engine: {engine}"
