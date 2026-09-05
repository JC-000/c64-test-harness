"""Live verification of U64 SID selection and SID address allocation.

Gated on ``U64_HOST`` so it skips cleanly with no device, following
``tests/test_ultimate64_helpers_live.py``. The mutating tests need
``U64_ALLOW_MUTATE`` as well and restore what they changed.

This module exists to settle the claims that could only be read out of
the firmware source, never observed. Each such test names the claim it
is checking in its docstring. Written against the Ultimate 3.15
pre-release tree (``~/Documents/1541u-315preview``); the U64E was
reported on firmware 3.15 / FPGA 123 / core 1.4E.

Nothing here changes flash: every write is in-memory only, because
``configs:save_to_flash`` is never called (route_configs.cc:413-457).
A reboot restores the saved configuration whatever this leaves behind.
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_ADDRESSING,
    CAT_SID_SOCKETS,
    CAT_ULTISID,
    enable_sid_socket,
    get_detected_sid_types,
    get_sid_address_map,
    get_sid_socket_enabled,
    set_sid_address_map,
)
from c64_test_harness.backends.ultimate64_helpers import (
    _introduced_sid_conflicts,
)
from c64_test_harness.backends.ultimate64_schema import (
    SID_ADDRESS_VALUES,
    SID_DETECTED_TYPE_VALUES,
    SID_SLOT_ADDRESS_ITEMS,
    SID_SOCKET_ENABLE_VALUES,
    SID_STEREO_SPLIT_VALUES,
    SidSlot,
    ULTISID_DIGI_VALUES,
    ULTISID_FILTER_VALUES,
    ULTISID_RESONANCE_VALUES,
    ULTISID_SPLIT_VALUES,
    ULTISID_WAVEFORM_VALUES,
    sid_address_conflicts,
    sid_address_occupancy,
)

_HOST = os.environ.get("U64_HOST")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set — skipping live U64 SID tests"
)

_mutating = pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — skipping mutating SID tests",
)


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Stateless HTTP client for the live device."""
    return Ultimate64Client(_HOST, password=os.environ.get("U64_PASSWORD"))


@pytest.fixture()
def locked(client: Ultimate64Client) -> Ultimate64Client:
    """Hold the cross-process device lock for a mutating test.

    ``allow_nested=True`` because conftest's autouse ``device_lock_guard``
    already holds this device's lock for every live test: this fixture is
    genuinely re-entering the library on a device it owns, which is
    exactly what the flag is for.

    It stays even though ``DeviceLock`` now fails a self-held wait fast
    rather than hanging. That fix turns a permanent hang into a bounded
    timeout; it does not make the acquire *succeed*. Joining the
    existing hold is still the only correct behaviour here.
    """
    with DeviceLock(_HOST, allow_nested=True):
        yield client


@pytest.fixture()
def restore_addressing(locked: Ultimate64Client) -> Ultimate64Client:
    """Snapshot the four slot addresses and put them back afterwards."""
    before = get_sid_address_map(locked)
    try:
        yield locked
    finally:
        if before:
            set_sid_address_map(
                locked, before, allow_conflicts="restoring pre-test map"
            )


# --------------------------------------------------------------------------- #
# Category and item shape                                                     #
# --------------------------------------------------------------------------- #
def test_sid_categories_present(client: Ultimate64Client) -> None:
    """The four SID-relevant categories are advertised by /v1/configs."""
    categories = client.list_configs()
    for name in (
        "Audio Mixer",
        CAT_SID_SOCKETS,
        CAT_ULTISID,
        CAT_SID_ADDRESSING,
    ):
        assert name in categories, f"{name!r} missing from {categories}"


def test_socket_enable_item_offers_only_en_dis(client: Ultimate64Client) -> None:
    """U: ``SID Socket N`` is an enable toggle, not a chip-type selector.

    S basis: u64_config.cc:393-394 binds it to ``en_dis``. If this
    fails, ``SID_SOCKET_ENABLE_VALUES`` is wrong.
    """
    values = client.get_config_item(CAT_SID_SOCKETS, "SID Socket 1")["values"]
    assert tuple(values) == SID_SOCKET_ENABLE_VALUES


def test_detected_type_item_matches_315_table(client: Ultimate64Client) -> None:
    """U: ``SID Detected Socket N`` carries the 12-entry 3.15 ``sid_types``.

    A 9-entry answer (no PDsid / SIDKick) means the device is running a
    3.14-era build, not the 3.15 the version endpoint reported.
    """
    values = client.get_config_item(CAT_SID_SOCKETS, "SID Detected Socket 1")["values"]
    assert tuple(values) == SID_DETECTED_TYPE_VALUES


def test_read_only_items_still_appear_over_rest(client: Ultimate64Client) -> None:
    """U: ``cfg->disable()`` hides an item from the menu, not from REST.

    ``emit_store`` iterates ``st->items`` without consulting the
    enabled flag (route_configs.cc:13-15), so the detected-type and
    capacitor items should be readable even though the UI greys them
    out (u64_config.cc:515-519).
    """
    inner = client.get_config_category(CAT_SID_SOCKETS)[CAT_SID_SOCKETS]
    for item in (
        "SID Detected Socket 1",
        "SID Detected Socket 2",
        "SID Socket 1 Capacitors",
        "SID Socket 2 Capacitors",
    ):
        assert item in inner, f"{item!r} absent from {sorted(inner)}"


def test_visual_editor_is_not_exposed(client: Ultimate64Client) -> None:
    """U: a CFG_TYPE_FUNC item is skipped by the serializer.

    ``Visual SID Address Editor`` is CFG_TYPE_FUNC (u64_config.cc:412);
    ``emit_store`` ``continue``s on any type it does not handle
    (route_configs.cc:23-24), so it should not appear at all.
    """
    inner = client.get_config_category(CAT_SID_ADDRESSING)[CAT_SID_ADDRESSING]
    assert "Visual SID Address Editor" not in inner


def test_all_four_slots_offer_the_same_address_enum(
    client: Ultimate64Client,
) -> None:
    """U: all four slots bind ``u64_sid_base`` (u64_config.cc:404-408)."""
    for item in SID_SLOT_ADDRESS_ITEMS.values():
        values = client.get_config_item(CAT_SID_ADDRESSING, item)["values"]
        assert tuple(values) == SID_ADDRESS_VALUES, item


def test_split_items_match_schema(client: Ultimate64Client) -> None:
    """U: ``stereo_addr`` and ``sid_split`` (u64_config.cc:256-257)."""
    stereo = client.get_config_item(CAT_SID_ADDRESSING, "Ext DualSID Range Split")["values"]
    assert tuple(stereo) == SID_STEREO_SPLIT_VALUES

    split = client.get_config_item(CAT_SID_ADDRESSING, "UltiSID Range Split")["values"]
    assert tuple(split) == ULTISID_SPLIT_VALUES


def test_ultisid_items_match_schema(client: Ultimate64Client) -> None:
    """U: ``filter_sel`` / ``filter_res`` / ``comb_wave`` / ``digi_levels``."""
    expected = {
        "UltiSID 1 Filter Curve": ULTISID_FILTER_VALUES,
        "UltiSID 1 Filter Resonance": ULTISID_RESONANCE_VALUES,
        "UltiSID 1 Combined Waveforms": ULTISID_WAVEFORM_VALUES,
        "UltiSID 1 Digis Level": ULTISID_DIGI_VALUES,
    }
    for item, values in expected.items():
        got = client.get_config_item(CAT_ULTISID, item)["values"]
        assert tuple(got) == values, item


def test_enums_use_values_not_presets(client: Ultimate64Client) -> None:
    """U: enum items serialize under ``"values"`` (route_configs.cc:31-37)."""
    item = SID_SLOT_ADDRESS_ITEMS[SidSlot.SOCKET1]
    body = client.get_config_item(CAT_SID_ADDRESSING, item)
    assert "values" in body
    assert "presets" not in body


# --------------------------------------------------------------------------- #
# Reads                                                                       #
# --------------------------------------------------------------------------- #
def test_get_sid_address_map_covers_four_slots(client: Ultimate64Client) -> None:
    mapping = get_sid_address_map(client)
    assert set(mapping) == set(SidSlot)
    for address in mapping.values():
        assert address in SID_ADDRESS_VALUES


def test_get_detected_sid_types_returns_chip_names(
    client: Ultimate64Client,
) -> None:
    """U: the detected item reports a chip type, never ``"Enabled"``.

    This is the check that distinguishes the correct item from the one
    ``get_sid_socket_types`` reads.
    """
    types = get_detected_sid_types(client)
    assert set(types) == {1, 2}
    for socket, value in types.items():
        assert value in SID_DETECTED_TYPE_VALUES, (socket, value)
        assert value not in SID_SOCKET_ENABLE_VALUES


def test_get_sid_socket_enabled_returns_booleans(
    client: Ultimate64Client,
) -> None:
    enabled = get_sid_socket_enabled(client)
    assert set(enabled) == {1, 2}
    assert all(isinstance(v, bool) for v in enabled.values())


def test_live_map_is_readable_and_self_consistent(
    client: Ultimate64Client,
) -> None:
    """Absence of overlap is NOT a property of a healthy device.

    This replaces a test that asserted the stock allocation has no
    overlapping slots. It failed on two separate live runs, and the
    premise was what was wrong: the U64E ships with all four slots on
    ``$D400`` -- ``u64_sid_addressing_cfg`` gives every address item
    ``def = 1``, and index 1 of ``u64_sid_base`` is ``$D400``
    (u64_config.cc:404-408, :209-217). Overlap *is* the factory
    condition, so "no conflicts" could only ever pass on a device
    someone had already reconfigured.

    What is actually checkable without believing anything about how the
    device was left: every slot reports a value from the address enum,
    and :func:`sid_address_occupancy` accounts for each slot exactly
    once. That falsifies on a garbled read, a renamed item, or an
    occupancy bug -- none of which the old assertion would have caught,
    since it drowned in the factory overlap first.
    """
    mapping = get_sid_address_map(client)
    assert set(mapping) == set(SidSlot), f"missing slots: {mapping}"
    for slot, address in mapping.items():
        assert address in SID_ADDRESS_VALUES, (slot.value, address)

    occupancy = sid_address_occupancy(mapping)
    placed = [slot for slots in occupancy.values() for slot in slots]
    expected = [
        slot for slot, address in mapping.items() if address != "Unmapped"
    ]
    assert sorted(placed, key=list(SidSlot).index) == sorted(
        expected, key=list(SidSlot).index
    ), f"occupancy does not account for every mapped slot: {occupancy}"
    for address, slots in occupancy.items():
        assert address in SID_ADDRESS_VALUES
        assert all(mapping[slot] == address for slot in slots), occupancy

    conflicts = sid_address_conflicts(mapping)
    if conflicts:
        # Reported, not asserted against -- see the docstring.
        print(f"device is running overlapped SIDs (expected on a stock "
              f"device): {conflicts}")


def test_delta_guard_tolerates_the_devices_current_map(
    client: Ultimate64Client,
) -> None:
    """The guard must never reject the state the device is already in.

    This is the property the old test was reaching for and got backwards.
    A whole-map conflict check made ``set_sid_address_map`` unusable
    against a factory device: every call was refused because the stock
    ``$D400 x4`` was already an overlap. The delta guard fixed that, and
    this pins it against whatever the bench is actually running.

    Pure: it re-checks the current map against itself and writes nothing.
    """
    current = get_sid_address_map(client)
    assert _introduced_sid_conflicts(current, dict(current), set()) == [], (
        "the delta guard objects to the device's own current allocation, so "
        "set_sid_address_map would refuse every write on this device"
    )


# --------------------------------------------------------------------------- #
# Writes                                                                      #
# --------------------------------------------------------------------------- #
@_mutating
def test_set_sid_address_map_round_trips(
    restore_addressing: Ultimate64Client,
) -> None:
    """U: a PUT to ``SID Addressing`` takes effect without a reboot.

    S basis: the PUT handler calls ``st->at_close_config()``
    (route_configs.cc:313), whose base implementation effectuates a
    stale store (config.h:197-203), which for this store reprograms
    ``C64_EMUSID2_BASE`` (u64_config.cc:848-869).
    """
    client = restore_addressing
    before = get_sid_address_map(client)
    target = "$D520" if before[SidSlot.ULTISID2] != "$D520" else "$D540"

    set_sid_address_map(client, {SidSlot.ULTISID2: target})
    assert get_sid_address_map(client)[SidSlot.ULTISID2] == target


@_mutating
def test_unmapped_is_accepted_for_every_slot(
    restore_addressing: Ultimate64Client,
) -> None:
    """U: ``"Unmapped"`` is a settable value, not just a display state."""
    client = restore_addressing
    set_sid_address_map(
        client,
        {slot: "Unmapped" for slot in SidSlot},
        allow_conflicts="all four unmapped is not a real overlap",
    )
    assert all(a == "Unmapped" for a in get_sid_address_map(client).values())


@_mutating
def test_firmware_accepts_an_overlapping_allocation(
    restore_addressing: Ultimate64Client,
) -> None:
    """U: the firmware does NOT reject two slots on one address.

    ``set_item`` only tests enum membership (route_configs.cc:76-88)
    and nothing downstream checks for overlap, so this should succeed.
    A failure here means the firmware validates after all, and
    ``allow_conflicts`` is a footgun that should be removed.
    """
    client = restore_addressing
    set_sid_address_map(
        client,
        {SidSlot.SOCKET1: "$D400", SidSlot.ULTISID1: "$D400"},
        allow_conflicts="checking the firmware accepts an overlap",
    )
    mapping = get_sid_address_map(client)
    assert mapping[SidSlot.SOCKET1] == "$D400"
    assert mapping[SidSlot.ULTISID1] == "$D400"


@_mutating
def test_enable_sid_socket_round_trips(locked: Ultimate64Client) -> None:
    """U: ``SID Socket N`` accepts ``en_dis`` strings over REST."""
    client = locked
    before = get_sid_socket_enabled(client)[2]
    try:
        enable_sid_socket(client, 2, not before)
        assert get_sid_socket_enabled(client)[2] is (not before)
    finally:
        enable_sid_socket(client, 2, before)


@_mutating
def test_setting_a_chip_type_on_the_enable_item_is_rejected(
    locked: Ultimate64Client,
) -> None:
    """U: this is the bug in ``set_sid_socket``, shown on the wire.

    ``set_sid_socket(client, 1, "8580", ...)`` PUTs ``"8580"`` to
    ``SID Socket 1``, whose domain is ``en_dis``. The firmware answers
    400 "Value '8580' is not a valid choice for item SID Socket 1"
    (route_configs.cc:85-88).
    """
    with pytest.raises(Ultimate64Error):
        locked.set_config_item(CAT_SID_SOCKETS, "SID Socket 1", "8580")


@_mutating
def test_detected_type_item_is_writable_over_rest(
    locked: Ultimate64Client,
) -> None:
    """REFUTED (2026-08-30): a menu-disabled item IS writable over REST.

    ``cfg->disable(CFG_SID1_TYPE)`` (u64_config.cc:517-518) greys the
    item out in the device's own menu, but ``set_item``
    (route_configs.cc:63-91) never consults the enabled flag. Observed
    live on a U64E running firmware 3.15: writing ``6581`` to a socket
    holding a real 8580 returned HTTP 200 and read back as ``6581``.

    This pins that behaviour, because it is what makes
    ``get_detected_sid_types`` advisory rather than authoritative. If
    this test starts failing, the firmware gained an enabled-flag check
    and the warning on that helper can be relaxed.

    Restores the original value; note a reboot would restore it anyway,
    since the boot-time probe rewrites it.
    """
    before = get_detected_sid_types(locked)[1]
    other = "6581" if before != "6581" else "8580"
    try:
        locked.set_config_item(CAT_SID_SOCKETS, "SID Detected Socket 1", other)
        assert get_detected_sid_types(locked)[1] == other, (
            "firmware now rejects writes to a menu-disabled item -- relax the "
            "warning on get_detected_sid_types"
        )
    finally:
        locked.set_config_item(
            CAT_SID_SOCKETS, "SID Detected Socket 1", before
        )
    assert get_detected_sid_types(locked)[1] == before


@_mutating
def test_address_enum_match_is_case_insensitive(
    restore_addressing: Ultimate64Client,
) -> None:
    """U: ``set_item`` compares with ``strcasecmp`` (route_configs.cc:79).

    So ``"$d520"`` should be accepted for ``"$D520"``. The harness
    always sends the canonical casing, but a caller reaching past it
    should know.
    """
    client = restore_addressing
    client.set_config_item(
        CAT_SID_ADDRESSING,
        SID_SLOT_ADDRESS_ITEMS[SidSlot.ULTISID2],
        "$d520",
    )
    assert get_sid_address_map(client)[SidSlot.ULTISID2] == "$D520"
