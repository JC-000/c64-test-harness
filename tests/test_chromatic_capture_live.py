"""Live chromatic scale WAV capture across SID configurations on U64E.

Captures 14-second WAV files of a 25-note chromatic scale (C3-C5) played
through four different SID configurations: three UltiSID FPGA emulation
modes (6581 curve, 8580 Lo curve, 8580 Hi curve) and the physical 8580
chip.  Each test writes a WAV file and a JSON metadata sidecar.

The committed reference captures live in ``tests/wav_captures/chromatic/``
and are useful for offline spectral analysis and A/B comparison of filter
curves.  A live run writes to a per-run scratch directory by default and
only refreshes the tracked reference under ``WAV_CAPTURES_REFRESH=1``
(issue #220; see ``tests/wav_capture_paths.py``), so an ordinary bench
run never dirties the working tree or silently drifts the reference.

Requirements:
    - ``U64_HOST`` env var pointing at a reachable Ultimate 64 device
    - ``U64_ALLOW_MUTATE=1`` -- the suite rewrites ``SID Addressing`` and
      ``UltiSID Configuration`` and resets the machine (#268)
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

from live_fixture_teardown import (  # noqa: E402
    raise_teardown_failures,
    read_restore_defaults,
    restore_default_steps,
    teardown_then_release,
)
from wav_capture_paths import capture_dir  # noqa: E402
from audio_link_loss import (  # noqa: E402
    CAPTURE_ATTEMPTS,
    MAX_FILL_FRACTION,
    MAX_LOST_TIME_FRACTION,
    capture_usable,
    payloads_discarded,
)

logger = logging.getLogger(__name__)

# Skip entire module when no U64 device is available.
pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("U64_HOST"),
        reason="U64_HOST not set -- skipping live U64 tests",
    ),
    # Rewrites SID Addressing / UltiSID Configuration and resets (#268).
    pytest.mark.skipif(
        not os.environ.get("U64_ALLOW_MUTATE"),
        reason="U64_ALLOW_MUTATE not set -- suite rewrites SID Addressing "
        "and UltiSID Configuration and resets the machine",
    ),
]

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

#: Every item a capture test writes, by category -- what the exit restore
#: puts back to the device's own ``default`` (#334; previously hard-coded
#: values that were not the defaults).
_RESTORED_ITEMS = {
    "SID Addressing": list(dict.fromkeys(
        item for config in SID_CONFIGS for item in config["addressing"]
    )),
    "UltiSID Configuration": list(dict.fromkeys(
        item for config in SID_CONFIGS for item in (config["ultisid_settings"] or {})
    )),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wav_dir(tmp_path_factory: pytest.TempPathFactory, record_testsuite_property) -> Path:
    """Where this module's captures go: scratch unless WAV_CAPTURES_REFRESH=1.

    The directory is recorded as a testsuite property and printed, so a
    scratch capture can be found after the run (``-s`` or the junit XML).
    """
    path = capture_dir("chromatic", tmp_path_factory.mktemp("chromatic_captures"))
    path.mkdir(parents=True, exist_ok=True)
    record_testsuite_property("chromatic_capture_dir", str(path))
    print(f"\n[chromatic] captures -> {path}")
    logger.info("chromatic captures -> %s", path)
    return path


@pytest.fixture(scope="module")
def u64_client():
    """Connect to the U64, holding a cross-process DeviceLock for the session.

    Before any test writes, the ``default`` of every item in
    :data:`_RESTORED_ITEMS` is read; at exit each is PUT back to it (one
    bodyless PUT per item), then the machine is reset, the client closed and
    the lock released last -- every step attempted, failures raised after
    the release (#334, ``live_fixture_teardown``).
    """
    host = os.environ.get("U64_HOST")
    pw = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(host)
    try:
        lock.acquire_or_raise(timeout=120.0)
    except DeviceLockTimeout as e:
        pytest.skip(str(e))

    client = None
    plan: list = []
    failures: list = []
    try:
        client = Ultimate64Client(host=host, password=pw, timeout=15.0)
        plan = read_restore_defaults(client, _RESTORED_ITEMS)
        yield client
    finally:
        steps = []
        if client is not None:
            steps += restore_default_steps(client, plan)
            if plan:
                steps.append(("client.reset()", client.reset))
            steps.append(("client.close()", client.close))
        failures = teardown_then_release(steps, lock.release)
    raise_teardown_failures("chromatic u64_client teardown", failures)


# ---------------------------------------------------------------------------
# Parametrized capture test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "config",
    SID_CONFIGS,
    ids=[c["name"] for c in SID_CONFIGS],
)
def test_chromatic_capture(u64_client, wav_dir: Path, config):
    """Capture chromatic scale WAV for a given SID configuration."""
    wav_path = wav_dir / f"chromatic_{config['name']}.wav"

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

    # 5. Capture audio -- duration is 25 notes * 0.5s = 12.5s, add margin.
    #
    #    #453: since #410 a lost packet is zero-filled rather than dropped,
    #    so a lossy capture keeps an exact time base but carries runs of
    #    silence.  This module analyses amplitude, and fill enters that
    #    analysis silently -- it depresses the peak and puts a step edge
    #    where the signal has none.  ``packets_dropped`` alone does not see
    #    it (a filled drop leaves the time base intact by design), so the
    #    capture is gated on ``capture_usable`` and retried, exactly as the
    #    other audio live tests do.  Each attempt is a fresh
    #    ``capture_sid_u64``, which resets the machine and restarts the
    #    player -- that is the same cost those tests pay.
    for attempt in range(CAPTURE_ATTEMPTS):
        result = capture_sid_u64(
            client=u64_client,
            sid=sid,
            out_wav=wav_path,
            duration_seconds=14.0,
            song=0,
            settle_time=0.5,
        )
        if capture_usable(result):
            break
        logger.warning(
            "%s: attempt %d unusable (time_base_intact=%s, fill=%.1f%%, "
            "dropped=%d, discarded=%d); retrying",
            config["name"], attempt + 1, result.time_base_intact,
            result.fill_fraction * 100, result.packets_dropped,
            payloads_discarded(result),
        )
    else:
        pytest.fail(
            f"{CAPTURE_ATTEMPTS} captures in a row had a broken time base, "
            f"more than {MAX_FILL_FRACTION:.0%} fill, or more than "
            f"{MAX_LOST_TIME_FRACTION:.0%} lost time; the last was "
            f"{result.fill_fraction:.1%} fill with "
            f"{result.packets_dropped} packet(s) dropped and "
            f"{payloads_discarded(result)} discarded. The link is too lossy "
            f"to measure amplitude on right now (#410, #453)"
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
            # #453: what the amplitude figure below was measured over.
            # A peak read off a capture that is part silence is not
            # comparable with one that is not, so the fill bookkeeping
            # travels with it.
            "packets_filled": result.packets_filled,
            "fill_fraction": result.fill_fraction,
            "time_base_intact": result.time_base_intact,
            "payloads_discarded": payloads_discarded(result),
            "capture_attempts": attempt + 1,
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

def test_all_captures_present(u64_client, wav_dir: Path):
    """Verify all expected WAV files were generated.

    Reads the directory the capture tests of *this session* wrote to, so
    it only means something after them; when none ran (``-k`` selected
    this test alone) it skips rather than failing on an empty scratch dir.
    """
    expected = [wav_dir / f"chromatic_{config['name']}.wav" for config in SID_CONFIGS]
    if not any(wav.exists() for wav in expected):
        pytest.skip(
            f"no chromatic capture ran in this session (nothing under {wav_dir}); "
            "run the whole module"
        )
    for wav in expected:
        assert wav.exists(), f"Missing: {wav.name}"
        assert wav.stat().st_size > 10000, (
            f"Too small: {wav.name} ({wav.stat().st_size} bytes)"
        )
