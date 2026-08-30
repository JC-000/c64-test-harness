"""Live integration tests for U64 SID audio capture over the network.

Gated by ``U64_HOST`` env var. These tests use the real hardware.
DeviceLock is used for cross-process safety.

Example::

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_u64_audio_capture_live.py -v
"""
from __future__ import annotations

import logging
import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.render_wav_u64 import capture_sid_u64
from c64_test_harness.backends.u64_audio_capture import AudioCapture
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_SOCKETS,
    configure_multi_sid,
    get_audio_mixer_config,
    get_physical_sid_sockets,
    get_sid_address_map,
    get_sid_addresses,
    get_sid_socket_enabled,
    get_sid_socket_types,
    set_sid_address_map,
    set_sid_socket,
)
from c64_test_harness.backends.ultimate64_schema import (
    SID_DETECTED_TYPE_VALUES,
    SID_SOCKET_ENABLE_VALUES,
    SIDSocketConfig,
    SidSlot,
)
from c64_test_harness.sid import SidFile, build_test_psid

logger = logging.getLogger(__name__)

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set -- live Ultimate device tests disabled",
)


def _build_test_sid() -> SidFile:
    """Same test PSID as the other U64 live tests: sentinel+counter."""
    init_code = bytes([0xA9, 0x42, 0x8D, 0x60, 0x03])  # LDA #$42; STA $0360
    play_code = bytes([0xEE, 0x61, 0x03])                # INC $0361
    sid_bytes = build_test_psid(
        load_addr=0x1000, init_code=init_code, play_code=play_code
    )
    return SidFile.from_bytes(sid_bytes)


@pytest.fixture(scope="module")
def u64_client():
    """Acquire device lock and return client for the module."""
    host = os.environ.get("U64_HOST")
    pw = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(host)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {host}")
    client = Ultimate64Client(host=host, password=pw, timeout=15.0)
    yield client
    try:
        client.reset()
    except Exception:
        pass
    client.close()
    lock.release()


@pytest.fixture(autouse=True)
def restore_sid_config(u64_client: Ultimate64Client):
    """Put back any SID socket or addressing state a test moved.

    Modelled on ``test_sid_addressing_live.py::restore_addressing``, with
    two differences.

    It is **autouse**. Two tests here mutate SID configuration and
    neither restored it; requiring each author to remember an opt-in
    fixture is exactly what failed. The cost of covering the read-only
    tests too is two GETs each, because the restore is a diff -- nothing
    is written when nothing moved.

    And it restores with ``allow_conflicts``, which is not laziness: the
    device's stock allocation is all four slots on ``$D400``, so putting
    a slot *back* onto that pile grows an occupant set and the delta
    guard would correctly refuse it. Restoring a known-good snapshot is
    precisely the case the override exists for.

    Why this matters beyond tidiness: leaked state poisons every later
    measurement on the bench, including the ones checking whether
    anything leaked. A drifted ``SID Socket 2 Address`` was once
    captured as a baseline by a later run and "restored" to the drifted
    value, reporting success. Four independent read paths had to agree
    before it was noticed.
    """
    try:
        before_addresses = get_sid_address_map(u64_client)
        before_sockets = get_sid_socket_enabled(u64_client)
    except Exception:  # noqa: BLE001 -- a probe failure must not eat the test
        logger.exception("SID snapshot failed; test will run unprotected")
        yield
        return

    try:
        yield
    finally:
        # Restore even if the test raised part-way through its own
        # writes -- that is the case most likely to leave drift.
        try:
            after_addresses = get_sid_address_map(u64_client)
            moved = {
                slot: addr
                for slot, addr in before_addresses.items()
                if after_addresses.get(slot) != addr
            }
            if moved:
                logger.warning(
                    "restoring SID addresses moved by this test: %s",
                    {s.value: a for s, a in moved.items()},
                )
                set_sid_address_map(
                    u64_client,
                    moved,
                    allow_conflicts="restoring the pre-test snapshot",
                )

            after_sockets = get_sid_socket_enabled(u64_client)
            for socket, enabled in before_sockets.items():
                if after_sockets.get(socket) != enabled:
                    logger.warning(
                        "restoring SID socket %d enable state to %s",
                        socket,
                        enabled,
                    )
                    u64_client.set_config_items(
                        CAT_SID_SOCKETS,
                        {
                            f"SID Socket {socket}":
                                "Enabled" if enabled else "Disabled"
                        },
                    )
        except Exception:
            # Loud on purpose: a failed restore leaves the bench dirty
            # for every later run, which is worse than a failed test.
            logger.exception("SID CONFIG RESTORE FAILED -- device left drifted")
            raise


# ======================================================================
# Stream control
# ======================================================================

def test_stream_audio_start_stop(u64_client: Ultimate64Client) -> None:
    """Verify stream_audio_start/stop endpoints work without error."""
    try:
        u64_client.stream_audio_start("239.0.1.65:11001")
        time.sleep(0.5)
    finally:
        try:
            u64_client.stream_audio_stop()
        except Exception:
            pass


# ======================================================================
# Capture tests
# ======================================================================

def test_capture_silence(u64_client: Ultimate64Client, tmp_path) -> None:
    """Capture audio without playing a SID -- U64 still streams PCM."""
    wav_path = tmp_path / "silence.wav"
    capture = AudioCapture(port=11001)
    stream_started = False
    try:
        capture.start()
        u64_client.stream_audio_start(f"239.0.1.65:11001")
        stream_started = True
        time.sleep(1.0)
    finally:
        if stream_started:
            try:
                u64_client.stream_audio_stop()
            except Exception:
                pass
        result = capture.stop(wav_path=wav_path)

    assert wav_path.exists(), "WAV file was not created"
    assert wav_path.stat().st_size > 0, "WAV file is empty"
    logger.info(
        "Silence capture: %.2fs, %d packets, %d dropped",
        result.duration_seconds,
        result.packets_received,
        result.packets_dropped,
    )


def test_capture_sid_basic(u64_client: Ultimate64Client, tmp_path) -> None:
    """Capture SID audio using capture_sid_u64() and verify WAV output."""
    sid = _build_test_sid()
    wav_path = tmp_path / "sid_basic.wav"
    try:
        result = capture_sid_u64(
            client=u64_client,
            sid=sid,
            out_wav=wav_path,
            duration_seconds=2.0,
            song=0,
            settle_time=0.3,
        )
    except Exception:
        # capture_sid_u64 resets internally, but ensure reset on unexpected error
        try:
            u64_client.reset()
        except Exception:
            pass
        raise

    assert wav_path.exists(), "WAV file was not created"
    assert wav_path.stat().st_size > 0, "WAV file is empty"
    assert result.duration_seconds > 0.5, (
        f"Captured duration too short: {result.duration_seconds:.2f}s"
    )
    assert result.packets_received > 0, "No audio packets received"
    logger.info(
        "SID capture: %.2fs, %d packets, %d samples, %d dropped",
        result.duration_seconds,
        result.packets_received,
        result.total_samples,
        result.packets_dropped,
    )


def test_capture_sid_with_physical_sids(
    u64_client: Ultimate64Client, tmp_path
) -> None:
    """Capture audio using a physical SID chip if one is installed."""
    physical = get_physical_sid_sockets(u64_client)
    if not physical:
        pytest.skip("No physical SID chips detected")

    chip_socket = physical[0]
    chip_type = get_sid_socket_types(u64_client).get(chip_socket, "unknown")
    logger.info(
        "Using physical SID '%s' in socket %d at $D400",
        chip_type,
        chip_socket,
    )

    # Enable the socket the chip is in and map it to $D400.  The chip
    # type is NOT passed here: 'SID Socket N' is an enable toggle, and
    # this line used to feed it the detected type, which the firmware
    # answers with HTTP 400.
    set_sid_socket(
        u64_client, socket=chip_socket, sid_type="Enabled", address="$D400"
    )

    sid = _build_test_sid()
    wav_path = tmp_path / "sid_physical.wav"
    try:
        result = capture_sid_u64(
            client=u64_client,
            sid=sid,
            out_wav=wav_path,
            duration_seconds=2.0,
            song=0,
        )
    finally:
        try:
            u64_client.reset()
        except Exception:
            pass

    assert wav_path.exists(), "WAV file was not created"
    assert wav_path.stat().st_size > 0, "WAV file is empty"
    assert result.packets_received > 0, "No audio packets received"
    logger.info(
        "Physical SID capture: %.2fs, %d packets, %d dropped",
        result.duration_seconds,
        result.packets_received,
        result.packets_dropped,
    )


# ======================================================================
# Configuration probes
# ======================================================================

def test_probe_audio_mixer(u64_client: Ultimate64Client) -> None:
    """Verify get_audio_mixer_config() returns a non-empty dict."""
    mixer = get_audio_mixer_config(u64_client)
    assert isinstance(mixer, dict), f"Expected dict, got {type(mixer).__name__}"
    assert len(mixer) > 0, "Audio mixer config is empty"
    for key, value in mixer.items():
        logger.info("Mixer: %s = %s", key, value)


def test_probe_sid_sockets(u64_client: Ultimate64Client) -> None:
    """Verify SID socket type and address queries return non-empty dicts.

    ``get_sid_socket_types`` now reports the *detected chip*, not the
    socket enable state, so the values here are drawn from
    ``SID_DETECTED_TYPE_VALUES``.
    """
    types = get_sid_socket_types(u64_client)
    addresses = get_sid_addresses(u64_client)

    assert isinstance(types, dict) and len(types) > 0, (
        f"SID socket types empty or wrong type: {types!r}"
    )
    assert isinstance(addresses, dict) and len(addresses) > 0, (
        f"SID addresses empty or wrong type: {addresses!r}"
    )

    for idx, typ in sorted(types.items()):
        addr = addresses.get(idx, "?")
        assert typ in SID_DETECTED_TYPE_VALUES, (idx, typ)
        assert typ not in SID_SOCKET_ENABLE_VALUES, (
            f"socket {idx} reported {typ!r} -- that is an enable state, so "
            f"get_sid_socket_types is reading the wrong item again"
        )
        logger.info("Socket %d: type=%s, address=%s", idx, typ, addr)


# ======================================================================
# Multi-SID addressing
# ======================================================================

def test_multi_sid_addressing(u64_client: Ultimate64Client, tmp_path) -> None:
    """Configure two SIDs at $D400 and $D420, capture audio."""
    types = get_sid_socket_types(u64_client)
    if len(types) < 2:
        pytest.skip("Fewer than 2 SID sockets available")

    # Use "Enabled" for both sockets (UltiSID emulation) at distinct addresses
    configure_multi_sid(u64_client, [
        SIDSocketConfig(sid_type="Enabled", address="$D400"),
        SIDSocketConfig(sid_type="Enabled", address="$D420"),
    ])
    logger.info("Configured dual SID: socket 1=$D400, socket 2=$D420")
    # NB: this used to leak $D420 into SID Socket 2 Address permanently.
    # The autouse restore_sid_config fixture puts it back.

    sid = _build_test_sid()
    wav_path = tmp_path / "sid_multi.wav"
    try:
        result = capture_sid_u64(
            client=u64_client,
            sid=sid,
            out_wav=wav_path,
            duration_seconds=2.0,
            song=0,
        )
    finally:
        try:
            u64_client.reset()
        except Exception:
            pass

    assert wav_path.exists(), "WAV file was not created"
    assert wav_path.stat().st_size > 0, "WAV file is empty"
    assert result.packets_received > 0, "No audio packets received"
    logger.info(
        "Multi-SID capture: %.2fs, %d packets, %d dropped",
        result.duration_seconds,
        result.packets_received,
        result.packets_dropped,
    )
