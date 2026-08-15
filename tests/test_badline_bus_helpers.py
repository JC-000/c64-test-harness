"""Unit tests for the Badline Timing and Bus Operation Mode helpers.

Mock-only -- no network, no hardware.  Covers issues #150 (badline
timing as a first-class control, including the measurement-environment
guard) and #145 (cartridge-port bus mode as a recordable benchmark
variable).

The enum value sets asserted here were live-probed on a U64E running
firmware 3.14d:

* ``U64 Specific Settings / Badline Timing`` -> ``["Disabled", "Enabled"]``,
  default ``"Enabled"``.
* ``C64 and Cartridge Settings / Bus Operation Mode`` ->
  ``["Quiet", "Writes", "Dynamic", "Dyn. & Writes"]``, default ``"Quiet"``.
* All four ``Bus Sharing - *`` items -> ``["Internal", "External", "Both"]``,
  default ``"Both"``.

The C64 Ultimate is *assumed* to spell these identically; that is
unverified (the two generations already diverge on the CPU Speed enum
and on cartridge presets), which is why the absent-item paths below are
tested as explicit contracts rather than incidental behaviour.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.backends.ultimate64_helpers import (
    BUS_SHARING_ITEMS,
    CAT_CART,
    CAT_U64_SPECIFIC,
    BusConfig,
    U64StateSnapshot,
    Ultimate64MeasurementEnvironmentError,
    check_measurement_environment,
    get_badline_timing,
    get_bus_config,
    restore_state,
    set_badline_timing,
    set_bus_operation_mode,
    snapshot_state,
)
from c64_test_harness.backends.ultimate64_schema import (
    BADLINE_TIMING_VALUES,
    BUS_OPERATION_MODE_VALUES,
    BUS_SHARING_VALUES,
)


def _u64_specific(
    turbo: str = "Off",
    cpu_speed: str = " 1",
    badline: str | None = "Enabled",
) -> dict:
    """Build a U64 Specific Settings response.

    *badline* of ``None`` omits the item entirely -- the shape a device
    generation that does not expose it would return.
    """
    inner: dict[str, str] = {
        "Turbo Control": turbo,
        "CPU Speed": cpu_speed,
        "System Mode": "NTSC",
    }
    if badline is not None:
        inner["Badline Timing"] = badline
    return {CAT_U64_SPECIFIC: inner, "errors": []}


def _cart(
    reu_enabled: str = "Enabled",
    reu_size: str = "512 KB",
    cartridge: str = "",
    bus_mode: str | None = "Quiet",
    sharing: str | None = "Both",
) -> dict:
    inner: dict[str, str] = {
        "RAM Expansion Unit": reu_enabled,
        "REU Size": reu_size,
        "Cartridge": cartridge,
    }
    if bus_mode is not None:
        inner["Bus Operation Mode"] = bus_mode
    if sharing is not None:
        for item in BUS_SHARING_ITEMS:
            inner[item] = sharing
    return {CAT_CART: inner, "errors": []}


def _client_for(u64: dict, cart: dict) -> MagicMock:
    """Mock client that dispatches get_config_category by category name."""
    client = MagicMock()
    client.host = "192.0.2.81"

    def side_effect(category: str) -> dict:
        return {CAT_U64_SPECIFIC: u64, CAT_CART: cart}[category]

    client.get_config_category.side_effect = side_effect
    return client


# --------------------------------------------------------------------------- #
# Schema constants                                                            #
# --------------------------------------------------------------------------- #

class TestSchemaConstants:
    def test_badline_values_match_device_probe(self) -> None:
        assert BADLINE_TIMING_VALUES == ("Disabled", "Enabled")

    def test_bus_operation_mode_values_match_device_probe(self) -> None:
        assert BUS_OPERATION_MODE_VALUES == (
            "Quiet", "Writes", "Dynamic", "Dyn. & Writes",
        )

    def test_bus_sharing_values_match_device_probe(self) -> None:
        assert BUS_SHARING_VALUES == ("Internal", "External", "Both")

    def test_four_bus_sharing_items(self) -> None:
        assert BUS_SHARING_ITEMS == (
            "Bus Sharing - ROMs",
            "Bus Sharing - I/O1",
            "Bus Sharing - I/O2",
            "Bus Sharing - Interrupts",
        )


# --------------------------------------------------------------------------- #
# Badline timing                                                              #
# --------------------------------------------------------------------------- #

class TestBadlineTiming:
    def test_get_returns_true_when_enabled(self) -> None:
        client = _client_for(_u64_specific(badline="Enabled"), _cart())
        assert get_badline_timing(client) is True

    def test_get_returns_false_when_disabled(self) -> None:
        client = _client_for(_u64_specific(badline="Disabled"), _cart())
        assert get_badline_timing(client) is False

    def test_get_raises_when_item_absent(self) -> None:
        """A missing item must not read as False -- Disabled means a ~20-25%
        faster CPU, so a silent wrong answer would corrupt a benchmark."""
        client = _client_for(_u64_specific(badline=None), _cart())
        with pytest.raises(Ultimate64Error, match="Badline Timing"):
            get_badline_timing(client)

    def test_set_true_writes_enabled(self) -> None:
        client = _client_for(_u64_specific(), _cart())
        set_badline_timing(client, True)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC, {"Badline Timing": "Enabled"}
        )

    def test_set_false_writes_disabled(self) -> None:
        client = _client_for(_u64_specific(), _cart())
        set_badline_timing(client, False)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC, {"Badline Timing": "Disabled"}
        )

    @pytest.mark.parametrize("bad", [1, 0, "Enabled", None])
    def test_set_rejects_non_bool_without_network(self, bad: object) -> None:
        client = _client_for(_u64_specific(), _cart())
        with pytest.raises(ValueError, match="must be bool"):
            set_badline_timing(client, bad)  # type: ignore[arg-type]
        client.set_config_items.assert_not_called()

    def test_set_refuses_to_write_absent_item(self) -> None:
        client = _client_for(_u64_specific(badline=None), _cart())
        with pytest.raises(Ultimate64Error, match="Badline Timing"):
            set_badline_timing(client, False)
        client.set_config_items.assert_not_called()


# --------------------------------------------------------------------------- #
# Bus operation mode                                                          #
# --------------------------------------------------------------------------- #

class TestBusConfig:
    def test_get_reads_mode_and_all_sharing_items(self) -> None:
        client = _client_for(_u64_specific(), _cart(bus_mode="Dynamic"))
        cfg = get_bus_config(client)
        assert isinstance(cfg, BusConfig)
        assert cfg.operation_mode == "Dynamic"
        assert cfg.sharing == dict.fromkeys(BUS_SHARING_ITEMS, "Both")

    def test_get_only_fetches_category_once(self) -> None:
        client = _client_for(_u64_specific(), _cart())
        get_bus_config(client)
        client.get_config_category.assert_called_once_with(CAT_CART)

    def test_get_tolerates_absent_items(self) -> None:
        client = _client_for(
            _u64_specific(), _cart(bus_mode=None, sharing=None)
        )
        cfg = get_bus_config(client)
        assert cfg.operation_mode == ""
        assert cfg.sharing == {}

    def test_set_writes_validated_mode(self) -> None:
        client = _client_for(_u64_specific(), _cart())
        set_bus_operation_mode(client, "Dyn. & Writes")
        client.set_config_items.assert_called_once_with(
            CAT_CART, {"Bus Operation Mode": "Dyn. & Writes"}
        )

    @pytest.mark.parametrize("mode", BUS_OPERATION_MODE_VALUES)
    def test_every_probed_value_is_accepted(self, mode: str) -> None:
        client = _client_for(_u64_specific(), _cart())
        set_bus_operation_mode(client, mode)
        client.set_config_items.assert_called_once_with(
            CAT_CART, {"Bus Operation Mode": mode}
        )

    @pytest.mark.parametrize("bad", ["quiet", "Dyn & Writes", "Loud", ""])
    def test_set_rejects_unknown_mode_without_network(self, bad: str) -> None:
        client = _client_for(_u64_specific(), _cart())
        with pytest.raises(ValueError, match="Bus Operation Mode"):
            set_bus_operation_mode(client, bad)
        client.set_config_items.assert_not_called()

    def test_bus_config_is_frozen(self) -> None:
        cfg = BusConfig(operation_mode="Quiet", sharing={})
        with pytest.raises(Exception):
            cfg.operation_mode = "Dynamic"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Snapshot / restore                                                          #
# --------------------------------------------------------------------------- #

class TestSnapshotRestore:
    def test_snapshot_captures_both_new_fields(self) -> None:
        client = _client_for(
            _u64_specific(badline="Disabled"), _cart(bus_mode="Writes")
        )
        snap = snapshot_state(client)
        assert snap.badline_timing == "Disabled"
        assert snap.bus_operation_mode == "Writes"

    def test_snapshot_leaves_fields_empty_when_device_omits_them(self) -> None:
        client = _client_for(
            _u64_specific(badline=None), _cart(bus_mode=None, sharing=None)
        )
        snap = snapshot_state(client)
        assert snap.badline_timing == ""
        assert snap.bus_operation_mode == ""

    def test_new_fields_default_so_positional_construction_still_works(
        self,
    ) -> None:
        """Back-compat: callers built snapshots with five positional args
        before these fields existed."""
        snap = U64StateSnapshot(" 1", " 1", "Enabled", "512 KB", "")
        assert snap.badline_timing == ""
        assert snap.bus_operation_mode == ""

    def test_restore_writes_both_fields(self) -> None:
        client = _client_for(_u64_specific(), _cart())
        client.get_config_item.return_value = {
            CAT_CART: {"Cartridge": {"presets": [""]}}
        }
        restore_state(
            client,
            U64StateSnapshot(
                turbo_control="Off",
                cpu_speed=" 1",
                reu_enabled="Enabled",
                reu_size="512 KB",
                cartridge="",
                badline_timing="Enabled",
                bus_operation_mode="Quiet",
            ),
        )
        u64_updates = next(
            call.args[1]
            for call in client.set_config_items.call_args_list
            if call.args[0] == CAT_U64_SPECIFIC
        )
        cart_updates = next(
            call.args[1]
            for call in client.set_config_items.call_args_list
            if call.args[0] == CAT_CART
        )
        assert u64_updates["Badline Timing"] == "Enabled"
        assert cart_updates["Bus Operation Mode"] == "Quiet"

    def test_restore_skips_empty_fields(self) -> None:
        """Writing "" back produces HTTP 400, and an old snapshot (or one from
        a device without the items) carries "" for both."""
        client = _client_for(_u64_specific(), _cart())
        client.get_config_item.return_value = {
            CAT_CART: {"Cartridge": {"presets": [""]}}
        }
        restore_state(client, U64StateSnapshot(" 1", " 1", "Enabled", "512 KB", ""))
        for call in client.set_config_items.call_args_list:
            assert "Badline Timing" not in call.args[1]
            assert "Bus Operation Mode" not in call.args[1]

    def test_restore_round_trips_a_snapshot(self) -> None:
        """The hazard #150 names: a run that disables badlines must be able to
        put the device back exactly as it found it."""
        client = _client_for(
            _u64_specific(badline="Enabled"), _cart(bus_mode="Quiet")
        )
        snap = snapshot_state(client)
        client.get_config_item.return_value = {
            CAT_CART: {"Cartridge": {"presets": [""]}}
        }
        set_badline_timing(client, False)
        client.set_config_items.reset_mock()
        restore_state(client, snap)
        u64_updates = next(
            call.args[1]
            for call in client.set_config_items.call_args_list
            if call.args[0] == CAT_U64_SPECIFIC
        )
        assert u64_updates["Badline Timing"] == "Enabled"


# --------------------------------------------------------------------------- #
# Measurement-environment guard                                               #
# --------------------------------------------------------------------------- #

class TestMeasurementEnvironmentGuard:
    def test_passes_on_clean_environment(self) -> None:
        client = _client_for(_u64_specific(badline="Enabled"), _cart())
        assert check_measurement_environment(client) is None

    def test_raises_when_badlines_disabled(self) -> None:
        client = _client_for(_u64_specific(badline="Disabled"), _cart())
        with pytest.raises(
            Ultimate64MeasurementEnvironmentError, match="badline"
        ):
            check_measurement_environment(client)

    def test_badline_error_names_the_fix_and_the_issue(self) -> None:
        client = _client_for(_u64_specific(badline="Disabled"), _cart())
        with pytest.raises(Ultimate64MeasurementEnvironmentError) as exc:
            check_measurement_environment(client)
        assert "set_badline_timing" in str(exc.value)
        assert "#150" in str(exc.value)

    def test_skips_badline_check_when_item_absent(self) -> None:
        """An unreadable item is not evidence of a dirty environment -- the
        C64U spelling is unverified, so absence must not fail the guard."""
        client = _client_for(_u64_specific(badline=None), _cart())
        assert check_measurement_environment(client) is None

    def test_turbo_check_still_takes_priority(self) -> None:
        client = _client_for(
            _u64_specific(turbo="Manual", cpu_speed="48", badline="Disabled"),
            _cart(),
        )
        with pytest.raises(Ultimate64MeasurementEnvironmentError) as exc:
            check_measurement_environment(client)
        assert "turbo" in str(exc.value).lower()
        assert "#102" in str(exc.value)
