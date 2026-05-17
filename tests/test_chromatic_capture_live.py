"""Live chromatic scale WAV capture across SID configurations on U64E.

Captures 14-second WAV files of a 25-note chromatic scale (C3-C5) played
through four different SID configurations: three UltiSID FPGA emulation
modes (6581 curve, 8580 Lo curve, 8580 Hi curve) and the physical 8580
chip.  Each test writes a WAV file and a JSON metadata sidecar to
``tests/wav_captures/chromatic/``.

These WAV outputs are committed artifacts -- they are the primary output
of this test suite, useful for offline spectral analysis and A/B
comparison of filter curves.

Requirements:
    - ``U64_HOST`` env var pointing at a reachable Ultimate 64 device
    - Device has physical 8580 SID chips (NEVER use sid_type "6581")
    - Network path for UDP audio streaming between host and device

Typical runtime: ~60 seconds (4 configs x ~14s each).
"""
from __future__ import annotations

import json
import logging
import os
import struct
import sys
import time
import wave
from pathlib import Path

import pytest

# Allow importing from scripts/ which is not a package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from play_chromatic_u64 import (  # noqa: E402
    FRAMES_PER_NOTE,
    NOTES,
    NUM_NOTES,
    build_chromatic_psid,
)

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout  # noqa: E402
from c64_test_harness.backends.render_wav_u64 import capture_sid_u64  # noqa: E402
from c64_test_harness.backends.ultimate64_client import Ultimate64Client  # noqa: E402
from c64_test_harness.sid import SidFile  # noqa: E402

logger = logging.getLogger(__name__)

WAV_DIR = Path(__file__).parent / "wav_captures" / "chromatic"

# Skip entire module when no U64 device is available.
pytestmark = pytest.mark.skipif(
    not os.environ.get("U64_HOST"),
    reason="U64_HOST not set -- skipping live U64 tests",
)

# ---------------------------------------------------------------------------
# SID configurations to capture
# ---------------------------------------------------------------------------
# SAFETY: The device has physical 8580 chips.  Address routing isolates sources.

SID_CONFIGS = [
    {
        "name": "ultisid_6581_curve",
        "description": "UltiSID FPGA with 6581 filter curve",
        "instrument_chip": "6581",
        "ultisid_settings": {
            "UltiSID 1 Filter Curve": "6581",
            "UltiSID 1 Combined Waveforms": "6581",
        },
        "addressing": {
            "SID Socket 1 Address": "Unmapped",
            "SID Socket 2 Address": "Unmapped",
            "UltiSID 1 Address": "$D400",
            "UltiSID 2 Address": "Unmapped",
        },
    },
    {
        "name": "ultisid_8580lo_curve",
        "description": "UltiSID FPGA with 8580 Lo filter curve",
        "instrument_chip": "8580",
        "ultisid_settings": {
            "UltiSID 1 Filter Curve": "8580 Lo",
            "UltiSID 1 Combined Waveforms": "8580",
        },
        "addressing": {
            "SID Socket 1 Address": "Unmapped",
            "SID Socket 2 Address": "Unmapped",
            "UltiSID 1 Address": "$D400",
            "UltiSID 2 Address": "Unmapped",
        },
    },
    {
        "name": "ultisid_8580hi_curve",
        "description": "UltiSID FPGA with 8580 Hi filter curve",
        "instrument_chip": "8580",
        "ultisid_settings": {
            "UltiSID 1 Filter Curve": "8580 Hi",
            "UltiSID 1 Combined Waveforms": "8580",
        },
        "addressing": {
            "SID Socket 1 Address": "Unmapped",
            "SID Socket 2 Address": "Unmapped",
            "UltiSID 1 Address": "$D400",
            "UltiSID 2 Address": "Unmapped",
        },
    },
    {
        "name": "physical_8580",
        "description": "Physical 8580 SID chip",
        "instrument_chip": "8580",
        "ultisid_settings": None,
        "addressing": {
            "SID Socket 1 Address": "$D400",
            "SID Socket 2 Address": "Unmapped",
            "UltiSID 1 Address": "Unmapped",
            "UltiSID 2 Address": "Unmapped",
        },
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def u64_client():
    """Connect to the U64, holding a cross-process DeviceLock for the session."""
    host = os.environ.get("U64_HOST")
    pw = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(host)
    try:
        lock.acquire_or_raise(timeout=120.0)
    except DeviceLockTimeout as e:
        pytest.skip(str(e))

    client = Ultimate64Client(host=host, password=pw, timeout=15.0)
    yield client

    # Teardown: restore defaults
    try:
        client.set_config_items("SID Addressing", {
            "SID Socket 1 Address": "$D400",
            "SID Socket 2 Address": "$D420",
            "UltiSID 1 Address": "$D400",
            "UltiSID 2 Address": "$D400",
        })
        client.set_config_items("UltiSID Configuration", {
            "UltiSID 1 Filter Curve": "8580 Lo",
            "UltiSID 1 Combined Waveforms": "6581",
        })
        client.reset()
    except Exception:
        pass
    client.close()
    lock.release()


# ---------------------------------------------------------------------------
# Parametrized capture test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "config",
    SID_CONFIGS,
    ids=[c["name"] for c in SID_CONFIGS],
)
def test_chromatic_capture(u64_client, config):
    """Capture chromatic scale WAV for a given SID configuration."""
    WAV_DIR.mkdir(parents=True, exist_ok=True)

    wav_path = WAV_DIR / f"chromatic_{config['name']}.wav"

    # 1. Configure SID address routing (isolate source)
    u64_client.set_config_items("SID Addressing", config["addressing"])

    # 2. Configure UltiSID settings if applicable
    if config["ultisid_settings"]:
        u64_client.set_config_items("UltiSID Configuration", config["ultisid_settings"])

    # 3. Build PSID with matching instrument params
    psid_bytes, meta = build_chromatic_psid(config["instrument_chip"])
    sid = SidFile.from_bytes(psid_bytes)

    # 4. Allow the config to take effect
    time.sleep(0.5)

    # 5. Capture audio -- duration is 25 notes * 0.5s = 12.5s, add margin
    result = capture_sid_u64(
        client=u64_client,
        sid=sid,
        out_wav=wav_path,
        duration_seconds=14.0,
        song=0,
        settle_time=0.5,
    )

    # 6. Validate file was created
    assert wav_path.exists(), f"WAV not created: {wav_path}"
    assert wav_path.stat().st_size > 0, "WAV is empty"
    assert result.packets_received > 0, "No audio packets"

    # 7. Check audio is not just silence
    with wave.open(str(wav_path), "rb") as w:
        data = w.readframes(w.getnframes())
        samples = struct.unpack(f"<{len(data) // 2}h", data)
        peak = max(abs(s) for s in samples)

    assert peak > 100, f"Audio appears silent (peak={peak})"

    # 8. Write metadata sidecar
    meta_path = wav_path.with_suffix(".json")
    meta_info = {
        "config": config,
        "capture": {
            "duration_seconds": result.duration_seconds,
            "packets_received": result.packets_received,
            "packets_dropped": result.packets_dropped,
            "total_samples": result.total_samples,
            "sample_rate": result.sample_rate,
            "peak_amplitude": int(peak),
        },
        "psid": meta,
    }
    meta_path.write_text(json.dumps(meta_info, indent=2) + "\n")

    logger.info(
        "%s: %.2fs, %d packets, peak=%d, %s",
        config["name"],
        result.duration_seconds,
        result.packets_received,
        peak,
        wav_path.name,
    )


# ---------------------------------------------------------------------------
# Summary validation
# ---------------------------------------------------------------------------

def test_all_captures_present(u64_client):
    """Verify all expected WAV files were generated."""
    for config in SID_CONFIGS:
        wav = WAV_DIR / f"chromatic_{config['name']}.wav"
        assert wav.exists(), f"Missing: {wav.name}"
        assert wav.stat().st_size > 10000, (
            f"Too small: {wav.name} ({wav.stat().st_size} bytes)"
        )
