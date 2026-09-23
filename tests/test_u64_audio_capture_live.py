"""Live integration tests for U64 SID audio capture over the network.

Gated by ``U64_HOST`` env var. These tests use the real hardware. The two
tests that write SID socket/addressing config additionally need
``U64_ALLOW_MUTATE=1`` (#268).
DeviceLock is used for cross-process safety.

Example::

    U64_HOST=<device> python3 -m pytest tests/test_u64_audio_capture_live.py -v
"""
from __future__ import annotations

import functools
import logging
import os
import socket
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.render_wav_u64 import capture_sid_u64
from c64_test_harness.backends.u64_audio_capture import (
    EPHEMERAL_AUDIO_PORT,
    AudioCapture,
)
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64ProtocolError,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_ADDRESSING,
    CAT_SID_SOCKETS,
    configure_multi_sid,
    get_audio_mixer_config,
    get_physical_sid_sockets,
    get_sid_addresses,
    get_sid_socket_types,
    set_sid_socket,
)
from c64_test_harness.backends.ultimate64_schema import (
    SID_DETECTED_TYPE_VALUES,
    SID_SLOT_ADDRESS_ITEMS,
    SID_SOCKET_ENABLE_VALUES,
    SIDSocketConfig,
    SidSlot,
)
from c64_test_harness.sid import SidFile, build_test_psid
from live_fixture_teardown import (
    attempt_steps,
    raise_teardown_failures,
    teardown_then_release,
)

logger = logging.getLogger(__name__)

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set -- live Ultimate device tests disabled",
)

#: The tests that write SID socket/addressing config also need
#: ``U64_ALLOW_MUTATE`` (#268); the read-only probes run on ``U64_HOST``.
_requires_mutate = pytest.mark.skipif(
    not os.environ.get("U64_ALLOW_MUTATE"),
    reason="U64_ALLOW_MUTATE not set -- this test writes device config",
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
    client = None
    failures: list = []
    try:
        client = Ultimate64Client(host=host, password=pw, timeout=15.0)
        yield client
    finally:
        steps = [] if client is None else [("client.reset()", client.reset), ("client.close()", client.close)]
        failures = teardown_then_release(steps, lock.release)
    raise_teardown_failures('audio u64_client teardown', failures)


#: The socket-enable items of ``SID Sockets Configuration``.  Restored to
#: their ENTRY value, never their default -- see ``restore_sid_config``.
SOCKET_ENABLE_ITEMS = ("SID Socket 1", "SID Socket 2")


def _mutate_allowed() -> bool:
    """Read at exit, like ``_requires_mutate`` reads it at collection."""
    return bool(os.environ.get("U64_ALLOW_MUTATE"))


def _names_absent(envelope, category: str, item: str) -> bool:
    """Whether a raw envelope is the firmware saying "no such item".

    Stock firmware answers an unknown category with HTTP 200 and no category
    key, and an unknown item with an empty category map, both with an empty
    ``errors`` (``get_config_item``).  Names match case-insensitively, as
    the firmware matches them.  Anything else -- an ``errors`` entry, a
    non-dict envelope or category, the item present -- is not absence.
    """
    if not isinstance(envelope, dict) or envelope.get("errors"):
        return False
    categories = [
        v for k, v in envelope.items()
        if k != "errors" and k.lower() == category.lower()
    ]
    if not categories:
        return True
    if len(categories) != 1 or not isinstance(categories[0], dict):
        return False
    return not any(k.lower() == item.lower() for k in categories[0])


def _read_present_item(client, category: str, item: str):
    """The item map, or ``None`` when this device does not expose the item.

    Absence is HTTP 404 (the 3.15 fork) or, on stock firmware, an
    ``Ultimate64ProtocolError`` whose raw envelope :func:`_names_absent`
    reads as "no such item".  ``get_config_item`` raises the same error for
    invalid JSON, an ``errors`` array and malformed shapes, so every other
    failure propagates: a device that cannot be read must not be restored
    blind, nor have its items quietly dropped from the plan.
    """
    try:
        return client.get_config_item(category, item)
    except Ultimate64ProtocolError as exc:
        if not _names_absent(client.get_config_item_raw(category, item), category, item):
            raise
        reason = exc
    except Ultimate64Error as exc:
        if exc.status != 404:
            raise
        reason = exc
    logger.warning(
        "%s / %s is not exposed by this device (%s); the SID restore "
        "leaves it alone", category, item, reason,
    )
    return None


def _restore_plan(client) -> list:
    """``(category, item, target)`` for every item the device has.

    The target is the reported ``default`` for ``SID Addressing`` and the
    entry ``current`` for the socket enables (the ``BASELINE_NEVER_TOUCH``
    exemption, see ``restore_sid_config``).  An item that exists but
    reports no target refuses the start: exit could not put it back.
    """
    plan: list = []
    wanted = [
        *((CAT_SID_ADDRESSING, item, "default") for item in SID_SLOT_ADDRESS_ITEMS.values()),
        *((CAT_SID_SOCKETS, item, "current") for item in SOCKET_ENABLE_ITEMS),
    ]
    for category, item, key in wanted:
        entry = _read_present_item(client, category, item)
        if entry is None:
            continue
        target = entry.get(key) if isinstance(entry, dict) else None
        if target is None or target == "":
            what = "current value" if key == "current" else "default"
            raise RuntimeError(
                f"{category} / {item} reports no {what} ({entry!r}); "
                "refusing to start, because exit could not restore it"
            )
        current = entry.get("current")
        if current != target:
            logger.warning(
                "%s / %s drifted at entry: current %r, default %r; exit "
                "corrects it only under U64_ALLOW_MUTATE",
                category, item, current, target,
            )
        plan.append((category, item, target))
    return plan


def _restore_steps(client, plan) -> list:
    """One teardown step per item: re-read, then PUT only if it differs."""

    def restore(category: str, item: str, target) -> None:
        entry = client.get_config_item(category, item)
        current = entry.get("current") if isinstance(entry, dict) else None
        if current == target:
            return
        if not _mutate_allowed():
            logger.warning(
                "%s / %s holds %r, not %r; not writing it without "
                "U64_ALLOW_MUTATE", category, item, current, target,
            )
            return
        logger.warning(
            "restoring %s / %s from %r to %r", category, item, current, target,
        )
        client.set_config_item(category, item, target)

    return [
        (
            f"restore {category} / {item} = {target!r}",
            functools.partial(restore, category, item, target),
        )
        for category, item, target in plan
    ]


@pytest.fixture(autouse=True)
def restore_sid_config(u64_client: Ultimate64Client):
    """Put SID addressing and socket enables back where they belong.

    It is **autouse**. Two tests here mutate SID configuration and
    neither restored it; requiring each author to remember an opt-in
    fixture is exactly what failed.

    **It writes only what differs.** At exit each item is re-read and
    PUT only where ``current`` differs from its target, so an untouched
    bench costs reads and no writes.  That matters twice over: most tests
    here are read-only and run on ``U64_HOST`` alone, without
    ``U64_ALLOW_MUTATE``, and the firmware has no same-value short-circuit
    -- every config PUT re-runs the store's ``effectuate_settings``,
    which for ``SID Sockets Configuration`` is the socket power path.

    **The addressing target is the ``default`` the device reports, not
    the value read at entry (#447).** The bench baseline is ``current ==
    default`` per item (#334), so an entry snapshot is the baseline only
    while nothing has drifted -- and a drifted value is precisely what a
    SIGKILLed predecessor leaves behind. This module learned that the
    hard way: a drifted ``SID Socket 2 Address`` was once captured as a
    baseline by a later run and "restored" to the drifted value,
    reporting success, and four independent read paths had to agree
    before anyone noticed.

    **Nothing is written without ``U64_ALLOW_MUTATE``.**  Without it the
    module's config-writing tests are skipped, so any difference at exit
    -- drift inherited at entry, or a change during the test from another
    lane, a reboot or the on-device menu -- is not the test's own, and
    correcting it is a config write the operator did not allow.  The
    fixture logs a WARNING naming the item and writes nothing.

    An item that reports no target refuses the start -- a fixture error,
    deliberately, in place of the old "test will run unprotected" path.
    An item the device **does not expose** (HTTP 404 on the 3.15 fork,
    or on stock firmware an envelope with no such category or item --
    ``_names_absent``) is left out of the plan with a WARNING: there is
    nothing to restore on a device without it, and the tests that need it
    fail on their own.  Any other read failure stops the start.

    Each item goes back through its own bodyless ``set_config_item`` PUT
    (never ``set_config_items``, which stops at the first rejection and
    leaves the rest holding this test's values), and every step is
    attempted even when an earlier one raises.

    **The socket enables are a named exemption: their target is the value
    read at entry, never their default.**  ``SID Sockets Configuration``
    is in :data:`BASELINE_NEVER_TOUCH` (``ultimate64_baseline.py``) and
    the reason is measured, not inferred: ``ConfigStore::reset`` sets
    ``SID Socket 1/2 = Disabled`` and
    ``U64SidSockets::effectuate_settings`` (``u64_config.cc:744-800``)
    then writes regulator bits 0 to the PLD SIDCTRL / I2C, so **writing
    that default cuts power to the socketed SIDs** while the config read
    still looks clean (U64E, two 8580s, 2026-09-05, n=3).  Detection
    never re-runs over REST -- only at boot or from the on-device menu.
    So the whole store holds *detection results*, not reset products:
    this bench reads ``Enabled``/``8580``/``22 nF`` against defaults
    ``Disabled``/``None``/``470 pF`` (recorded in
    ``tests/test_entry_baseline.py``).  That is the same argument that
    exempts ``SID Detected Socket 1`` in
    ``test_sid_addressing_live.py``, and it applies to every item of
    this store, siblings included -- the #334 "restore the default" rule
    is for *selector* items, and these are measurements.  The entry
    value is the baseline here, so it is what goes back.
    """
    plan = _restore_plan(u64_client)
    failures: list = []
    try:
        yield
    finally:
        failures = attempt_steps(_restore_steps(u64_client, plan))
    raise_teardown_failures("SID config restore", failures)


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

def _local_ip_towards(host: str) -> str:
    """The local address the kernel would use to reach *host*."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 80))
        return s.getsockname()[0]


def test_capture_silence(u64_client: Ultimate64Client, tmp_path) -> None:
    """Capture audio without playing a SID -- U64 still streams PCM.

    Streams **unicast** to this host on an OS-chosen port, and asserts
    packets arrived.  It used to stream to the multicast default
    ``239.0.1.65:11001`` with a receiver that never joined the group, and
    asserted only that a WAV existed -- which a zero-packet capture
    satisfies, because ``write_wav`` writes a 44-byte header for no frames
    (#239).  Measured on the U64E 2026-09-15, 3 interleaved rounds of 1 s:
    unjoined, joined on INADDR_ANY and joined on the en0 address all
    received **0** packets; unicast received 278/281/339.  So the old
    pass never proved a capture on this bench.  The fixed port was also a
    cross-lane collision (#237); ``EPHEMERAL_AUDIO_PORT`` removes it.
    """
    wav_path = tmp_path / "silence.wav"
    capture = AudioCapture(port=EPHEMERAL_AUDIO_PORT)
    stream_started = False
    try:
        capture.start()
        u64_client.stream_audio_start(
            f"{_local_ip_towards(u64_client.host)}:{capture.port}"
        )
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
    assert result.packets_received > 0, (
        "No audio packets received -- a WAV exists either way, so this "
        "is the assertion that proves the stream reached the capture"
    )
    assert result.total_samples > 0, "Capture holds no PCM frames"
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


@_requires_mutate
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

@_requires_mutate
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
