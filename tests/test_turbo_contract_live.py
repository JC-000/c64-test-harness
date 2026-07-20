"""Live-hardware contract test for the cross-generation CPU Speed enum.

PR #131 widened ``CPU_SPEED_VALUES`` into a *superset* across device
generations: the U64E (firmware 3.14) reports ``" 1".." 5".."48"`` and
lacks ``"64"``; the C64 Ultimate (firmware 1.1.0) drops ``" 5"`` and adds
``"64"``. Because the harness-side schema is the union, ``set_turbo_mhz``
no longer raises ``ValueError`` locally for a speed that is valid on the
*other* generation — the value goes out on the wire.

The schema docstring asserts a foreign speed "is rejected by its firmware
at set time". That is a HYPOTHESIS, not a verified fact. This test checks
it empirically against whatever real device is on the LAN, and is written
to PASS-with-documentation whichever way the firmware actually behaves:

* firmware rejects with a 4xx  -> plain PASS (hypothesis confirmed)
* firmware silently accepts     -> ``xfail`` (hypothesis wrong; recorded)
* firmware accepts-but-ignores  -> ``xfail`` (hypothesis wrong; recorded)

A later phase reads the recorded observation to decide whether
``set_turbo_mhz`` needs a read-back verification step.

Env gates (all unset -> everything skips cleanly):

* ``TURBO_CONTRACT_LIVE=1`` — master switch for this module.
* ``U64_HOST``              — device hostname/IP (no IPs are committed).
* ``U64_PASSWORD``          — optional; sent as ``X-Password`` when set.
* ``U64_ALLOW_MUTATE=1``    — required for the mutating contract test;
                              the read-only smoke test runs without it.

What the mutating tests touch: ``U64 Specific Settings / Turbo Control``
and ``.../CPU Speed`` only. ``test_foreign_speed_contract`` pokes a single
foreign speed; ``test_native_speed_sweep`` walks every native speed for the
detected generation. Both snapshot the original values up front and restore
them in a ``finally``; nothing is written to flash, and the device is never
rebooted or powered off.
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64_helpers import (
    get_turbo_enabled,
    get_turbo_mhz,
    restore_state,
    set_turbo_mhz,
    snapshot_state,
)
from c64_test_harness.backends.ultimate64_schema import (
    CPU_SPEED_BY_MHZ,
    cpu_speed_enum,
)


# --------------------------------------------------------------------------- #
# Environment gates                                                           #
# --------------------------------------------------------------------------- #

_LIVE = os.environ.get("TURBO_CONTRACT_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="TURBO_CONTRACT_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]


# --------------------------------------------------------------------------- #
# Device-generation profiles                                                  #
# --------------------------------------------------------------------------- #

# product string (from GET /v1/info) -> (native_mhz, foreign_mhz)
#   native  : a speed valid on THIS generation (used for the round-trip).
#   foreign : a speed valid on the OTHER generation but absent here — the
#             value whose firmware-side handling this test exists to probe.
_DEVICE_PROFILES: dict[str, tuple[int, int]] = {
    "Ultimate 64 Elite": (2, 64),  # U64E fw 3.14: has " 5", lacks "64"
    "C64 Ultimate": (2, 5),        # C64U fw 1.1.0: has "64", lacks " 5"
}


def _profile_for(info: dict) -> tuple[str, int, int]:
    """Resolve (product, native_mhz, foreign_mhz) or skip on an unknown device."""
    product = str(info.get("product", ""))
    profile = _DEVICE_PROFILES.get(product)
    if profile is None:
        pytest.skip(f"unrecognized U64 product — get_info()={info!r}")
    return product, profile[0], profile[1]


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Locked, stateless HTTP client for the live device.

    Uses the queue-aware lock: a live, progressing holder extends the wait
    indefinitely; only a genuinely stuck/dead holder trips the timeout, and
    that becomes a clean skip (never a reboot/recover — see the device-lock
    contract in docs/u64_recovery.md).
    """
    assert _HOST is not None
    lock = DeviceLock(_HOST)
    try:
        lock.acquire_or_raise(timeout=120.0, progress_window=60.0)
    except DeviceLockTimeout as exc:
        pytest.skip(str(exc))
    try:
        yield Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# Read-only smoke test (runs without U64_ALLOW_MUTATE)                        #
# --------------------------------------------------------------------------- #

def test_turbo_contract_smoke(client: Ultimate64Client) -> None:
    """Device is reachable, recognized, and reports a sane turbo speed.

    Read-only: gated only by TURBO_CONTRACT_LIVE + U64_HOST so it can run
    as a cheap pre-flight before the double-gated mutating test.
    """
    info = client.get_info()
    product, _native, _foreign = _profile_for(info)
    assert product in _DEVICE_PROFILES

    mhz = get_turbo_mhz(client)
    assert mhz is None or isinstance(mhz, int)


# --------------------------------------------------------------------------- #
# Mutating contract test (double-gated: + U64_ALLOW_MUTATE)                   #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — skipping mutating turbo contract test",
)
def test_foreign_speed_contract(
    client: Ultimate64Client,
    record_property,
) -> None:
    """Set a foreign CPU speed and record how the firmware responds.

    Flow: snapshot -> native round-trip (sanity) -> foreign attempt ->
    restore -> assert state fully restored. The foreign attempt has three
    branches; only the "rejected with 4xx" branch is a hard PASS, the two
    silent-behavior branches xfail so the observation is preserved without
    turning a firmware quirk into a red build.
    """
    info = client.get_info()
    product, native, foreign = _profile_for(info)
    record_property("product", product)
    record_property("native_mhz", native)
    record_property("foreign_mhz", foreign)

    native_enum = cpu_speed_enum(native)
    foreign_enum = cpu_speed_enum(foreign)

    snap = snapshot_state(client)
    observation = "unset"
    try:
        # (a) Native round-trip — establishes the baseline the reject branch
        #     checks against, and proves the set/read path works at all.
        set_turbo_mhz(client, native)
        assert get_turbo_mhz(client) == native, (
            f"native round-trip failed: expected {native} MHz, "
            f"got {get_turbo_mhz(client)}"
        )
        assert get_turbo_enabled(client) is True
        # Confirm the raw device enum matches what we sent (space-padded).
        after_native = snapshot_state(client)
        assert after_native.cpu_speed == native_enum, (
            f"native CPU Speed enum mismatch: sent {native_enum!r}, "
            f"device reports {after_native.cpu_speed!r}"
        )

        # (b) Foreign attempt — the actual contract probe.
        try:
            set_turbo_mhz(client, foreign)
        except Ultimate64Error as exc:
            # --- Branch 1: firmware rejected the foreign speed. ---
            status = exc.status
            observation = (
                f"REJECTED foreign speed {foreign} "
                f"(enum {foreign_enum!r}) with HTTP {status}: {exc}"
            )
            record_property("observed_behavior", observation)
            print(observation)
            assert status is not None and 400 <= status < 500, (
                f"expected a 4xx client-error rejection, got status={status!r} "
                f"({exc})"
            )
            # Rejection must not have half-applied: CPU Speed unchanged from (a).
            after_reject = snapshot_state(client)
            assert after_reject.cpu_speed == native_enum, (
                f"firmware rejected foreign speed but CPU Speed changed from "
                f"{native_enum!r} to {after_reject.cpu_speed!r} — half-applied"
            )
        else:
            # No raise: inspect what the device actually did.
            after_foreign = snapshot_state(client)
            readback = after_foreign.cpu_speed
            if readback == foreign_enum:
                # --- Branch 2: firmware silently accepted it. ---
                observation = (
                    f"ACCEPTED foreign speed {foreign} silently: "
                    f"readback cpu_speed={readback!r}, "
                    f"turbo_control={after_foreign.turbo_control!r}"
                )
                record_property("observed_behavior", observation)
                print(observation)
                pytest.xfail(observation)
            else:
                # --- Branch 3: firmware accepted the call but ignored it. ---
                observation = (
                    f"IGNORED foreign speed {foreign} (no error, no change): "
                    f"readback cpu_speed={readback!r} "
                    f"(expected foreign {foreign_enum!r} if applied), "
                    f"turbo_control={after_foreign.turbo_control!r}"
                )
                record_property("observed_behavior", observation)
                print(observation)
                pytest.xfail(observation)
    finally:
        restore_state(client, snap)

    # Post-restore: device is back exactly where we found it.
    after = snapshot_state(client)
    assert after.turbo_control == snap.turbo_control, (
        f"Turbo Control not restored: {snap.turbo_control!r} -> "
        f"{after.turbo_control!r}"
    )
    assert after.cpu_speed == snap.cpu_speed, (
        f"CPU Speed not restored: {snap.cpu_speed!r} -> {after.cpu_speed!r}"
    )


@pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — skipping native speed sweep test",
)
def test_native_speed_sweep(
    client: Ultimate64Client,
    record_property,
) -> None:
    """Set every native CPU speed in turn and verify the device applies it.

    The sweep list is every speed in :data:`CPU_SPEED_BY_MHZ` *except* the
    detected generation's foreign speed, ascending — so it self-adjusts:
    on the C64 Ultimate it covers 1,2,3,4,6,8,...,48,64 (excludes 5); on the
    U64 Elite it covers 1..48 including 5 (excludes 64).

    Mismatches are accumulated rather than failing fast, so one bad speed
    still yields the full picture. A speed that raises ``Ultimate64Error``
    is itself a finding: it is recorded as a mismatch and the sweep
    continues.
    """
    info = client.get_info()
    product, native, foreign = _profile_for(info)
    record_property("product", product)
    record_property("foreign_mhz", foreign)

    sweep = sorted(mhz for mhz in CPU_SPEED_BY_MHZ if mhz != foreign)
    record_property("sweep", sweep)

    snap = snapshot_state(client)
    mismatches: list[str] = []
    try:
        for mhz in sweep:
            expected_enum = cpu_speed_enum(mhz)
            try:
                set_turbo_mhz(client, mhz)
            except Ultimate64Error as exc:
                entry = f"{mhz} MHz: HTTP {exc.status}: {exc}"
                mismatches.append(entry)
                print(f"[sweep] {entry}")
                continue
            read_mhz = get_turbo_mhz(client)
            read_enum = snapshot_state(client).cpu_speed
            ok = read_mhz == mhz and read_enum == expected_enum
            line = (
                f"[sweep] {mhz} MHz: {'ok' if ok else 'MISMATCH'} "
                f"(get_turbo_mhz={read_mhz}, cpu_speed={read_enum!r}, "
                f"expected {expected_enum!r})"
            )
            print(line)
            if not ok:
                mismatches.append(
                    f"{mhz} MHz: get_turbo_mhz={read_mhz}, "
                    f"cpu_speed={read_enum!r} (expected {expected_enum!r})"
                )
    finally:
        restore_state(client, snap)

    record_property(
        "sweep_result",
        f"{len(sweep) - len(mismatches)}/{len(sweep)} speeds applied cleanly",
    )

    # Post-restore: device is back exactly where we found it.
    after = snapshot_state(client)
    assert after.turbo_control == snap.turbo_control, (
        f"Turbo Control not restored: {snap.turbo_control!r} -> "
        f"{after.turbo_control!r}"
    )
    assert after.cpu_speed == snap.cpu_speed, (
        f"CPU Speed not restored: {snap.cpu_speed!r} -> {after.cpu_speed!r}"
    )

    assert not mismatches, (
        f"{len(mismatches)}/{len(sweep)} native speeds did not apply "
        f"correctly on {product}: {mismatches}"
    )
