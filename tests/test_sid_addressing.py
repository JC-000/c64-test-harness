"""Unit tests for U64 SID selection and SID address allocation.

Everything here is offline: the device is mocked. Behavioural claims
about what the firmware *does* with these values are marked U in the
task report and are covered by ``tests/test_sid_addressing_live.py``.

Firmware references (Ultimate 3.15 pre-release tree,
``~/Documents/1541u-315preview``):

- ``software/u64/u64_config.cc:392-400`` — ``u64_sid_detection_cfg``
- ``software/u64/u64_config.cc:403-413`` — ``u64_sid_addressing_cfg``
- ``software/u64/u64_config.cc:209-227`` — ``u64_sid_base`` / ``u64_sid_offsets``
- ``software/u64/u64_config.cc:271`` — ``sid_types``
- ``software/components/config.cc:927`` — ``en_dis``
- ``software/api/route_configs.cc:31-37`` — enums emit under ``"values"``
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.backends import ultimate64_helpers as _helpers
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_ADDRESSING,
    CAT_SID_SOCKETS,
    enable_sid_socket,
    get_detected_sid_types,
    get_sid_address_map,
    get_sid_socket_enabled,
    set_sid_address_map,
)
from c64_test_harness.backends.ultimate64_schema import (
    SID_ADDRESS_VALUES,
    SID_DETECTED_TYPE_VALUES,
    SID_SLOT_ADDRESS_ITEMS,
    SID_SOCKET_ENABLE_VALUES,
    SID_STEREO_SPLIT_VALUES,
    SID_UNMAPPED_OFFSET,
    SidAddressConflict,
    SidSlot,
    ULTISID_FILTER_VALUES,
    ULTISID_SPLIT_VALUES,
    sid_address_conflicts,
    sid_address_offset,
)

_HELPERS_LOGGER = _helpers.__name__


# --------------------------------------------------------------------------- #
# Schema constants vs firmware tables                                         #
# --------------------------------------------------------------------------- #
class TestSchemaConstants:
    def test_socket_enable_values_are_en_dis(self) -> None:
        """``SID Socket N`` uses ``en_dis``, not a chip-type list.

        S: u64_config.cc:393-394 binds CFG_SOCKET1/2_ENABLE to ``en_dis``;
        config.cc:927 defines ``en_dis = { "Disabled", "Enabled" }``.
        """
        assert SID_SOCKET_ENABLE_VALUES == ("Disabled", "Enabled")

    def test_detected_type_values_match_315_sid_types(self) -> None:
        """S: u64_config.cc:271 (12 entries; 3.14 had 9)."""
        assert SID_DETECTED_TYPE_VALUES == (
            "None",
            "6581",
            "8580",
            "FPGASID",
            "SwinSID Ultimate",
            "ARMSID",
            "ARM2SID",
            "SidFx",
            "FPGASID Dukestah",
            "PDsid",
            "SIDKick (Teensy)",
            "SIDKick Pico",
        )

    def test_stereo_and_ultisid_split_values(self) -> None:
        """S: u64_config.cc:256-257."""
        assert SID_STEREO_SPLIT_VALUES == ("Off", "A5", "A6", "A7", "A8", "A9")
        assert ULTISID_SPLIT_VALUES == (
            "Off",
            "1/2 (A5)",
            "1/2 (A6)",
            "1/2 (A7)",
            "1/2 (A8)",
            "1/4 (A5,A6)",
            "1/4 (A5,A8)",
            "1/4 (A7,A8)",
        )

    def test_ultisid_filter_values(self) -> None:
        """S: u64_config.cc:274."""
        assert ULTISID_FILTER_VALUES == (
            "8580 Lo",
            "8580 Hi",
            "6581",
            "6581 Alt",
            "U2 Low",
            "U2 Mid",
            "U2 High",
        )

    def test_all_four_slots_share_the_address_enum(self) -> None:
        """S: u64_config.cc:404-408 — all four bind ``u64_sid_base``."""
        assert set(SID_SLOT_ADDRESS_ITEMS) == set(SidSlot)
        assert SID_SLOT_ADDRESS_ITEMS[SidSlot.SOCKET1] == "SID Socket 1 Address"
        assert SID_SLOT_ADDRESS_ITEMS[SidSlot.SOCKET2] == "SID Socket 2 Address"
        assert SID_SLOT_ADDRESS_ITEMS[SidSlot.ULTISID1] == "UltiSID 1 Address"
        assert SID_SLOT_ADDRESS_ITEMS[SidSlot.ULTISID2] == "UltiSID 2 Address"


# --------------------------------------------------------------------------- #
# Address offsets                                                             #
# --------------------------------------------------------------------------- #
class TestSidAddressOffset:
    def test_offsets_mirror_firmware_table(self) -> None:
        """S: u64_config.cc:219-227 ``u64_sid_offsets``."""
        assert sid_address_offset("Unmapped") == SID_UNMAPPED_OFFSET == 0x01
        assert sid_address_offset("$D400") == 0x40
        assert sid_address_offset("$D420") == 0x42
        assert sid_address_offset("$D4E0") == 0x4E
        assert sid_address_offset("$D500") == 0x50
        assert sid_address_offset("$D7E0") == 0x7E
        assert sid_address_offset("$DE00") == 0xE0
        assert sid_address_offset("$DFE0") == 0xFE

    def test_every_schema_address_has_a_distinct_offset(self) -> None:
        offsets = [sid_address_offset(a) for a in SID_ADDRESS_VALUES]
        assert len(offsets) == len(set(offsets)) == 49

    def test_rejects_unknown_address(self) -> None:
        with pytest.raises(ValueError, match="SID address"):
            sid_address_offset("$C000")


# --------------------------------------------------------------------------- #
# Conflict detection (pure)                                                   #
# --------------------------------------------------------------------------- #
class TestSidAddressConflicts:
    def test_no_conflict_when_all_distinct(self) -> None:
        assert sid_address_conflicts(
            {
                SidSlot.SOCKET1: "$D400",
                SidSlot.SOCKET2: "$D420",
                SidSlot.ULTISID1: "$D500",
                SidSlot.ULTISID2: "$D520",
            }
        ) == []

    def test_unmapped_slots_never_conflict(self) -> None:
        """All four may sit on ``Unmapped`` (offset 0x01) at once."""
        assert sid_address_conflicts(
            {slot: "Unmapped" for slot in SidSlot}
        ) == []

    def test_detects_a_shared_base_address(self) -> None:
        conflicts = sid_address_conflicts(
            {
                SidSlot.SOCKET1: "$D400",
                SidSlot.ULTISID1: "$D400",
                SidSlot.SOCKET2: "$D420",
            }
        )
        assert conflicts == [
            SidAddressConflict(
                address="$D400",
                slots=(SidSlot.SOCKET1, SidSlot.ULTISID1),
            )
        ]

    def test_accepts_plain_item_name_keys(self) -> None:
        conflicts = sid_address_conflicts(
            {"SID Socket 1 Address": "$D400", "UltiSID 1 Address": "$D400"}
        )
        assert len(conflicts) == 1
        assert conflicts[0].slots == (SidSlot.SOCKET1, SidSlot.ULTISID1)

    def test_rejects_unknown_slot_key(self) -> None:
        with pytest.raises(ValueError, match="SID slot"):
            sid_address_conflicts({"UltiSID 9 Address": "$D400"})


# --------------------------------------------------------------------------- #
# Socket enable / detected type                                               #
# --------------------------------------------------------------------------- #
class TestSocketSelection:
    def test_enable_sid_socket_writes_en_dis_value(self) -> None:
        client = MagicMock()
        enable_sid_socket(client, 1, True)
        client.set_config_items.assert_called_once_with(
            CAT_SID_SOCKETS, {"SID Socket 1": "Enabled"}
        )

    def test_disable_sid_socket_writes_en_dis_value(self) -> None:
        client = MagicMock()
        enable_sid_socket(client, 2, False)
        client.set_config_items.assert_called_once_with(
            CAT_SID_SOCKETS, {"SID Socket 2": "Disabled"}
        )

    def test_enable_sid_socket_rejects_bad_socket(self) -> None:
        client = MagicMock()
        with pytest.raises(ValueError, match="socket must be 1 or 2"):
            enable_sid_socket(client, 3, True)
        client.set_config_items.assert_not_called()

    def test_get_sid_socket_enabled(self) -> None:
        client = MagicMock()
        client.get_config_category.return_value = {
            CAT_SID_SOCKETS: {
                "SID Socket 1": "Enabled",
                "SID Socket 2": "Disabled",
                "SID Detected Socket 1": "8580",
                "SID Detected Socket 2": "None",
                "SID Socket 1 Capacitors": "22 nF",
            },
            "errors": [],
        }
        assert get_sid_socket_enabled(client) == {1: True, 2: False}

    def test_get_detected_sid_types_reads_the_detected_item(self) -> None:
        """The chip type lives in ``SID Detected Socket N``, not ``SID Socket N``.

        S: u64_config.cc:395-396.
        """
        client = MagicMock()
        client.get_config_category.return_value = {
            CAT_SID_SOCKETS: {
                "SID Socket 1": "Enabled",
                "SID Socket 2": "Enabled",
                "SID Detected Socket 1": "8580",
                "SID Detected Socket 2": "SIDKick Pico",
            },
            "errors": [],
        }
        assert get_detected_sid_types(client) == {1: "8580", 2: "SIDKick Pico"}

    def test_get_detected_sid_types_tolerates_missing_items(self) -> None:
        client = MagicMock()
        client.get_config_category.return_value = {
            CAT_SID_SOCKETS: {"SID Socket 1": "Enabled"},
            "errors": [],
        }
        assert get_detected_sid_types(client) == {}


# --------------------------------------------------------------------------- #
# Address map read / write                                                    #
# --------------------------------------------------------------------------- #
def _addressing_category(**overrides: str) -> dict:
    inner = {
        "SID Socket 1 Address": "$D400",
        "SID Socket 2 Address": "Unmapped",
        "Ext DualSID Range Split": "Off",
        "UltiSID 1 Address": "$D420",
        "UltiSID 2 Address": "Unmapped",
        "UltiSID Range Split": "Off",
        "Paddle Override": "Enabled",
        "Auto Address Mirroring": "Enabled",
    }
    inner.update(overrides)
    return {CAT_SID_ADDRESSING: inner, "errors": []}


def _client_with_addresses(**overrides: str) -> MagicMock:
    client = MagicMock()
    client.get_config_category.return_value = _addressing_category(**overrides)
    # Item map, unwrapped from the REST envelope (issue #214).
    client.get_config_item.return_value = {
        "current": "$D400",
        "values": list(SID_ADDRESS_VALUES),
        "default": "$D400",
    }
    return client


class TestAddressMap:
    def test_get_sid_address_map_covers_all_four_slots(self) -> None:
        client = _client_with_addresses()
        assert get_sid_address_map(client) == {
            SidSlot.SOCKET1: "$D400",
            SidSlot.SOCKET2: "Unmapped",
            SidSlot.ULTISID1: "$D420",
            SidSlot.ULTISID2: "Unmapped",
        }

    def test_set_sid_address_map_writes_only_named_slots(self) -> None:
        client = _client_with_addresses()
        set_sid_address_map(client, {SidSlot.ULTISID2: "$D500"})
        client.set_config_items.assert_called_once_with(
            CAT_SID_ADDRESSING, {"UltiSID 2 Address": "$D500"}
        )

    def test_set_sid_address_map_rejects_conflict_with_existing_slot(self) -> None:
        """Socket 1 already sits at $D400 on the device."""
        client = _client_with_addresses()
        with pytest.raises(ValueError, match="conflict"):
            set_sid_address_map(client, {SidSlot.ULTISID2: "$D400"})
        client.set_config_items.assert_not_called()

    def test_allow_conflicts_reason_permits_the_overlap(self) -> None:
        """The firmware itself never rejects an overlap (route_configs.cc:76-88)."""
        client = _client_with_addresses()
        set_sid_address_map(
            client,
            {SidSlot.ULTISID2: "$D400"},
            allow_conflicts="reproducing a stacked-SID decode",
        )
        client.set_config_items.assert_called_once_with(
            CAT_SID_ADDRESSING, {"UltiSID 2 Address": "$D400"}
        )

    def test_allow_conflicts_logs_the_reason_at_warning(self, caplog) -> None:
        """Mirrors MemoryPolicy.write_memory's logged ``override="reason"``."""
        client = _client_with_addresses()
        with caplog.at_level(logging.WARNING, logger=_HELPERS_LOGGER):
            set_sid_address_map(
                client,
                {SidSlot.ULTISID2: "$D400"},
                allow_conflicts="reproducing #148",
            )
        assert any(
            r.levelno == logging.WARNING and "reproducing #148" in r.getMessage()
            for r in caplog.records
        ), caplog.text

    def test_allow_conflicts_rejects_a_bare_bool(self) -> None:
        """A reason has to be justified in the diff, not merely enabled."""
        client = _client_with_addresses()
        with pytest.raises(ValueError, match="non-empty reason string"):
            set_sid_address_map(
                client, {SidSlot.ULTISID2: "$D400"}, allow_conflicts=True
            )
        client.set_config_items.assert_not_called()

    def test_allow_conflicts_rejects_an_empty_reason(self) -> None:
        client = _client_with_addresses()
        with pytest.raises(ValueError, match="non-empty reason string"):
            set_sid_address_map(
                client, {SidSlot.ULTISID2: "$D400"}, allow_conflicts=""
            )
        client.set_config_items.assert_not_called()

    def test_set_sid_address_map_rejects_conflict_within_the_request(self) -> None:
        client = _client_with_addresses()
        with pytest.raises(ValueError, match="conflict"):
            set_sid_address_map(
                client,
                {SidSlot.SOCKET2: "$D500", SidSlot.ULTISID2: "$D500"},
            )
        client.set_config_items.assert_not_called()

    def test_set_sid_address_map_rejects_unknown_address(self) -> None:
        client = _client_with_addresses()
        with pytest.raises(ValueError, match="SID address"):
            set_sid_address_map(client, {SidSlot.SOCKET1: "$C000"})
        client.set_config_items.assert_not_called()

    def test_set_sid_address_map_rejects_empty_mapping(self) -> None:
        client = _client_with_addresses()
        with pytest.raises(ValueError, match="non-empty"):
            set_sid_address_map(client, {})

    def test_probed_choices_narrow_validation(self) -> None:
        """A device whose enum omits an address rejects it locally.

        Mirrors the ``set_turbo_mhz`` generation-aware pattern: the
        schema tuple is the superset, the probe is the authority.
        """
        client = _client_with_addresses()
        client.get_config_item.return_value = {
            "current": "$D400",
            "values": ["Unmapped", "$D400", "$D420"],
            "default": "$D400",
        }
        with pytest.raises(ValueError, match="not offered by this device"):
            set_sid_address_map(client, {SidSlot.ULTISID2: "$DFE0"})
        client.set_config_items.assert_not_called()

    def test_inconclusive_probe_falls_back_to_schema(self) -> None:
        client = _client_with_addresses()
        client.get_config_item.side_effect = Ultimate64Error("boom")
        set_sid_address_map(client, {SidSlot.ULTISID2: "$DFE0"})
        client.set_config_items.assert_called_once_with(
            CAT_SID_ADDRESSING, {"UltiSID 2 Address": "$DFE0"}
        )

    def test_probe_is_cached_per_client(self) -> None:
        client = _client_with_addresses()
        set_sid_address_map(client, {SidSlot.ULTISID2: "$D500"})
        set_sid_address_map(client, {SidSlot.ULTISID2: "$D520"})
        assert client.get_config_item.call_count == 1


# --------------------------------------------------------------------------- #
# Delta-based conflict guard                                                  #
# --------------------------------------------------------------------------- #
def _stock_client() -> MagicMock:
    """A device in its factory allocation: all four slots on ``$D400``.

    This is what the U64E actually ships with under Auto Address
    Mirroring (observed live 2026-08-30), so it is the state the guard
    has to stay usable in.
    """
    return _client_with_addresses(
        **{
            "SID Socket 1 Address": "$D400",
            "SID Socket 2 Address": "$D400",
            "UltiSID 1 Address": "$D400",
            "UltiSID 2 Address": "$D400",
        }
    )


class TestDeltaConflicts:
    def test_moving_a_slot_off_a_stock_mirror_is_allowed(self) -> None:
        """Strictly reducing overlap must not be rejected.

        $D400 goes from four occupants to three. Under a whole-map
        check this raised, which made the helper unusable against a
        factory-state device.
        """
        client = _stock_client()
        set_sid_address_map(client, {SidSlot.ULTISID2: "$D520"})
        client.set_config_items.assert_called_once_with(
            CAT_SID_ADDRESSING, {"UltiSID 2 Address": "$D520"}
        )

    def test_untouched_preexisting_overlap_is_tolerated(self) -> None:
        """A conflict the caller did not cause is not the caller's problem."""
        client = _stock_client()
        set_sid_address_map(client, {SidSlot.SOCKET2: "Unmapped"})
        client.set_config_items.assert_called_once_with(
            CAT_SID_ADDRESSING, {"SID Socket 2 Address": "Unmapped"}
        )

    def test_growing_an_occupant_set_is_rejected(self) -> None:
        client = _client_with_addresses(
            **{
                "SID Socket 1 Address": "$D400",
                "SID Socket 2 Address": "$D400",
                "UltiSID 1 Address": "$D500",
                "UltiSID 2 Address": "Unmapped",
            }
        )
        with pytest.raises(ValueError, match="conflict"):
            set_sid_address_map(client, {SidSlot.ULTISID2: "$D400"})
        client.set_config_items.assert_not_called()

    def test_moving_into_an_occupied_address_is_rejected(self) -> None:
        """Same occupant count, but a caller-named slot joins the pile.

        $D400 holds {S1, S2}; the caller moves S2 away and U2 in. The
        set stays size 2, so a size comparison alone would miss it.
        """
        client = _client_with_addresses(
            **{
                "SID Socket 1 Address": "$D400",
                "SID Socket 2 Address": "$D400",
                "UltiSID 1 Address": "$D500",
                "UltiSID 2 Address": "Unmapped",
            }
        )
        with pytest.raises(ValueError, match="conflict"):
            set_sid_address_map(
                client,
                {SidSlot.SOCKET2: "$D520", SidSlot.ULTISID2: "$D400"},
            )
        client.set_config_items.assert_not_called()

    def test_all_four_to_unmapped_from_stock_is_allowed(self) -> None:
        client = _stock_client()
        set_sid_address_map(client, {slot: "Unmapped" for slot in SidSlot})
        assert client.set_config_items.call_count == 1

    def test_allow_conflicts_still_bypasses_the_delta_check(self) -> None:
        client = _client_with_addresses()
        set_sid_address_map(
            client,
            {SidSlot.ULTISID2: "$D400"},
            allow_conflicts="deliberately stacking on socket 1",
        )
        client.set_config_items.assert_called_once_with(
            CAT_SID_ADDRESSING, {"UltiSID 2 Address": "$D400"}
        )

    def test_error_names_only_the_introduced_conflict(self) -> None:
        """A pre-existing pile must not be listed alongside the new one."""
        client = _client_with_addresses(
            **{
                "SID Socket 1 Address": "$D400",
                "SID Socket 2 Address": "$D400",
                "UltiSID 1 Address": "$D500",
                "UltiSID 2 Address": "Unmapped",
            }
        )
        with pytest.raises(ValueError) as excinfo:
            set_sid_address_map(client, {SidSlot.ULTISID2: "$D500"})
        message = str(excinfo.value)
        assert "$D500" in message
        assert "$D400" not in message, message
