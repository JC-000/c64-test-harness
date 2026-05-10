"""Unit tests for ultimate64_helpers (mocked Ultimate64Client)."""
from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import pytest

from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Error,
    Ultimate64RunnerStuckError,
    Ultimate64UnreachableError,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_CART,
    CAT_SID_ADDRESSING,
    CAT_SID_SOCKETS,
    CAT_U64_SPECIFIC,
    U64StateSnapshot,
    get_reu_config,
    get_sid_config,
    get_turbo_enabled,
    get_turbo_mhz,
    load_prg_file,
    mount_disk_file,
    reboot,
    recover,
    reset,
    restore_state,
    run_prg_file,
    runner_health_check,
    set_reu,
    set_sid_socket,
    set_turbo_mhz,
    snapshot_state,
    unmount,
)


def _make_client() -> MagicMock:
    """Build a MagicMock that looks like an Ultimate64Client."""
    return MagicMock()


def _make_recover_client() -> MagicMock:
    """Mock client with the attributes recover() uses for probing."""
    client = MagicMock()
    client.host = "192.0.2.81"
    client.port = 80
    client.password = None
    return client


def _u64_specific(turbo: str = "Off", cpu_speed: str = " 1") -> dict:
    return {
        "U64 Specific Settings": {
            "Turbo Control": turbo,
            "CPU Speed": cpu_speed,
            "System Mode": "NTSC",
        },
        "errors": [],
    }


def _cart(
    reu_enabled: str = "Enabled",
    reu_size: str = "512 KB",
    cartridge: str = "",
) -> dict:
    return {
        "C64 and Cartridge Settings": {
            "RAM Expansion Unit": reu_enabled,
            "REU Size": reu_size,
            "Cartridge": cartridge,
        },
        "errors": [],
    }


# --------------------------------------------------------------------------- #
# Turbo / CPU speed                                                           #
# --------------------------------------------------------------------------- #

class TestTurbo:
    def test_set_turbo_mhz_int_sends_both_items(self) -> None:
        client = _make_client()
        set_turbo_mhz(client, 1)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC,
            {"CPU Speed": " 1", "Turbo Control": "Manual"},
        )

    def test_set_turbo_mhz_2mhz(self) -> None:
        client = _make_client()
        set_turbo_mhz(client, 2)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC,
            {"CPU Speed": " 2", "Turbo Control": "Manual"},
        )

    def test_set_turbo_mhz_48mhz(self) -> None:
        client = _make_client()
        set_turbo_mhz(client, 48)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC,
            {"CPU Speed": "48", "Turbo Control": "Manual"},
        )

    def test_set_turbo_mhz_none_disables(self) -> None:
        client = _make_client()
        set_turbo_mhz(client, None)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC,
            {"Turbo Control": "Off"},
        )

    def test_set_turbo_mhz_unsupported_raises(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="Unsupported CPU speed 100"):
            set_turbo_mhz(client, 100)
        client.set_config_items.assert_not_called()

    def test_set_turbo_mhz_bad_type_raises(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="must be int or None"):
            set_turbo_mhz(client, "2")  # type: ignore[arg-type]

    def test_get_turbo_mhz_on(self) -> None:
        client = _make_client()
        client.get_config_category.return_value = _u64_specific(
            turbo="Manual", cpu_speed=" 2"
        )
        assert get_turbo_mhz(client) == 2
        client.get_config_category.assert_called_once_with(CAT_U64_SPECIFIC)

    def test_get_turbo_mhz_off_returns_none(self) -> None:
        client = _make_client()
        client.get_config_category.return_value = _u64_specific(
            turbo="Off", cpu_speed=" 1"
        )
        assert get_turbo_mhz(client) is None

    def test_get_turbo_enabled_true(self) -> None:
        client = _make_client()
        client.get_config_category.return_value = _u64_specific(turbo="Manual")
        assert get_turbo_enabled(client) is True

    def test_get_turbo_enabled_false(self) -> None:
        client = _make_client()
        client.get_config_category.return_value = _u64_specific(turbo="Off")
        assert get_turbo_enabled(client) is False


# --------------------------------------------------------------------------- #
# REU                                                                         #
# --------------------------------------------------------------------------- #

class TestREU:
    def test_get_reu_config_enabled(self) -> None:
        client = _make_client()
        client.get_config_category.return_value = _cart(
            reu_enabled="Enabled", reu_size="512 KB"
        )
        assert get_reu_config(client) == (True, "512 KB")

    def test_get_reu_config_disabled(self) -> None:
        client = _make_client()
        client.get_config_category.return_value = _cart(
            reu_enabled="Disabled", reu_size="128 KB"
        )
        assert get_reu_config(client) == (False, "128 KB")

    def test_set_reu_enable_with_size_string(self) -> None:
        client = _make_client()
        set_reu(client, True, "16 MB")
        client.set_config_items.assert_called_once_with(
            CAT_CART,
            {
                "RAM Expansion Unit": "Enabled",
                "Cartridge": "REU",
                "REU Size": "16 MB",
            },
        )

    def test_set_reu_enable_with_size_int_mb(self) -> None:
        client = _make_client()
        set_reu(client, True, 16)
        call_kwargs = client.set_config_items.call_args
        assert call_kwargs.args[0] == CAT_CART
        updates = call_kwargs.args[1]
        assert updates["REU Size"] == "16 MB"
        assert updates["Cartridge"] == "REU"
        assert updates["RAM Expansion Unit"] == "Enabled"

    def test_set_reu_enable_without_size(self) -> None:
        client = _make_client()
        set_reu(client, True)
        updates = client.set_config_items.call_args.args[1]
        assert "REU Size" not in updates
        assert updates["RAM Expansion Unit"] == "Enabled"
        assert updates["Cartridge"] == "REU"

    def test_set_reu_disable_ignores_size(self) -> None:
        client = _make_client()
        set_reu(client, False)
        client.set_config_items.assert_called_once_with(
            CAT_CART, {"RAM Expansion Unit": "Disabled"}
        )

    def test_set_reu_bad_size_string_raises(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="Unknown REU size"):
            set_reu(client, True, "9 MB")
        client.set_config_items.assert_not_called()

    def test_set_reu_bad_enabled_type(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="enabled must be bool"):
            set_reu(client, 1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# SID                                                                         #
# --------------------------------------------------------------------------- #

class TestSID:
    def test_get_sid_config(self) -> None:
        client = _make_client()

        def side_effect(cat: str) -> dict:
            if cat == CAT_SID_SOCKETS:
                return {
                    "SID Sockets Configuration": {"SID Socket 1": "Enabled"},
                    "errors": [],
                }
            if cat == CAT_SID_ADDRESSING:
                return {
                    "SID Addressing": {"SID Socket 1 Address": "$D400"},
                    "errors": [],
                }
            raise AssertionError(f"unexpected {cat}")

        client.get_config_category.side_effect = side_effect
        result = get_sid_config(client)
        assert result["sockets"] == {"SID Socket 1": "Enabled"}
        assert result["addressing"] == {"SID Socket 1 Address": "$D400"}

    def test_set_sid_socket_valid(self) -> None:
        client = _make_client()
        set_sid_socket(client, 1, "8580", "$D400")
        calls = client.set_config_items.call_args_list
        assert len(calls) == 2
        assert calls[0].args == (CAT_SID_SOCKETS, {"SID Socket 1": "8580"})
        assert calls[1].args == (
            CAT_SID_ADDRESSING,
            {"SID Socket 1 Address": "$D400"},
        )

    def test_set_sid_socket_bad_socket(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="socket must be 1 or 2"):
            set_sid_socket(client, 3, "8580", "$D400")

    def test_set_sid_socket_bad_type(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="Invalid SID type"):
            set_sid_socket(client, 1, "FOO", "$D400")

    def test_set_sid_socket_bad_address(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="Invalid SID address"):
            set_sid_socket(client, 1, "8580", "$Z000")


# --------------------------------------------------------------------------- #
# Disk                                                                        #
# --------------------------------------------------------------------------- #

class TestDisk:
    def test_mount_disk_file_d64(self) -> None:
        client = _make_client()
        m = mock_open(read_data=b"IMAGE_BYTES")
        with patch("builtins.open", m):
            mount_disk_file(client, "a", "/tmp/foo.d64")
        client.mount_disk.assert_called_once_with(
            drive="a",
            image=b"IMAGE_BYTES",
            image_type="d64",
            mode="readwrite",
        )

    def test_mount_disk_file_d81_readonly(self) -> None:
        client = _make_client()
        m = mock_open(read_data=b"X")
        with patch("builtins.open", m):
            mount_disk_file(client, "b", "/tmp/foo.D81", mode="readonly")
        client.mount_disk.assert_called_once_with(
            drive="b",
            image=b"X",
            image_type="d81",
            mode="readonly",
        )

    def test_mount_disk_file_unknown_ext(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="Unknown disk image extension"):
            mount_disk_file(client, "a", "/tmp/foo.xyz")
        client.mount_disk.assert_not_called()

    def test_mount_disk_file_bad_mode(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="Invalid mount mode"):
            mount_disk_file(client, "a", "/tmp/foo.d64", mode="bogus")

    def test_unmount(self) -> None:
        client = _make_client()
        unmount(client, "a")
        client.unmount_disk.assert_called_once_with("a")


# --------------------------------------------------------------------------- #
# PRG                                                                         #
# --------------------------------------------------------------------------- #

class TestPRG:
    def test_run_prg_file(self) -> None:
        client = _make_client()
        m = mock_open(read_data=b"\x01\x08PRGDATA")
        with patch("builtins.open", m):
            run_prg_file(client, "/tmp/a.prg")
        client.run_prg.assert_called_once_with(b"\x01\x08PRGDATA")

    def test_load_prg_file(self) -> None:
        client = _make_client()
        m = mock_open(read_data=b"\x01\x08LOADED")
        with patch("builtins.open", m):
            load_prg_file(client, "/tmp/b.prg")
        client.load_prg.assert_called_once_with(b"\x01\x08LOADED")


# --------------------------------------------------------------------------- #
# Reset / reboot                                                              #
# --------------------------------------------------------------------------- #

class TestMachineControl:
    def test_reset(self) -> None:
        client = _make_client()
        reset(client)
        client.reset.assert_called_once_with()

    def test_reboot(self) -> None:
        client = _make_client()
        reboot(client)
        client.reboot.assert_called_once_with()


# --------------------------------------------------------------------------- #
# Snapshot / restore                                                          #
# --------------------------------------------------------------------------- #

class TestSnapshotRestore:
    def test_snapshot_state(self) -> None:
        client = _make_client()

        def side_effect(cat: str) -> dict:
            if cat == CAT_U64_SPECIFIC:
                return _u64_specific(turbo="Off", cpu_speed=" 1")
            if cat == CAT_CART:
                return _cart(
                    reu_enabled="Enabled",
                    reu_size="512 KB",
                    cartridge="",
                )
            raise AssertionError(f"unexpected {cat}")

        client.get_config_category.side_effect = side_effect
        snap = snapshot_state(client)
        assert snap.turbo_control == "Off"
        assert snap.cpu_speed == " 1"
        assert snap.reu_enabled == "Enabled"
        assert snap.reu_size == "512 KB"
        assert snap.cartridge == ""

    def test_restore_state(self) -> None:
        client = _make_client()
        snap = U64StateSnapshot(
            turbo_control="Off",
            cpu_speed=" 1",
            reu_enabled="Enabled",
            reu_size="512 KB",
            cartridge="",
        )
        restore_state(client, snap)
        calls = client.set_config_items.call_args_list
        assert len(calls) == 2
        assert calls[0].args == (
            CAT_U64_SPECIFIC,
            {"Turbo Control": "Off", "CPU Speed": " 1"},
        )
        # Empty Cartridge value is skipped (device rejects "" on write).
        assert calls[1].args == (
            CAT_CART,
            {
                "RAM Expansion Unit": "Enabled",
                "REU Size": "512 KB",
            },
        )

    def test_snapshot_restore_roundtrip(self) -> None:
        """Snapshot-then-restore issues writes matching the original values."""
        client = _make_client()

        def side_effect(cat: str) -> dict:
            if cat == CAT_U64_SPECIFIC:
                return _u64_specific(turbo="Manual", cpu_speed=" 4")
            if cat == CAT_CART:
                return _cart(
                    reu_enabled="Disabled",
                    reu_size="1 MB",
                    cartridge="REU",
                )
            raise AssertionError(f"unexpected {cat}")

        client.get_config_category.side_effect = side_effect
        snap = snapshot_state(client)
        restore_state(client, snap)
        calls = client.set_config_items.call_args_list
        assert calls[0].args[1] == {"Turbo Control": "Manual", "CPU Speed": " 4"}
        assert calls[1].args[1] == {
            "RAM Expansion Unit": "Disabled",
            "REU Size": "1 MB",
            "Cartridge": "REU",
        }

    def test_restore_state_bad_type(self) -> None:
        client = _make_client()
        with pytest.raises(TypeError, match="U64StateSnapshot"):
            restore_state(client, {"turbo_control": "Off"})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# recover()                                                                   #
# --------------------------------------------------------------------------- #

_HELPERS_PROBE = (
    "c64_test_harness.backends.ultimate64_helpers.is_u64_reachable"
)
_HELPERS_SLEEP = "c64_test_harness.backends.ultimate64_helpers.time.sleep"


class TestRecover:
    def test_recover_reset_succeeds(self) -> None:
        client = _make_recover_client()
        with patch(_HELPERS_PROBE, return_value=True) as probe, patch(
            _HELPERS_SLEEP
        ):
            result = recover(client)
        assert result == "reset"
        client.reset.assert_called_once_with()
        client.reboot.assert_not_called()
        assert probe.call_count == 1

    def test_recover_escalates_to_reboot(self) -> None:
        client = _make_recover_client()
        with patch(
            _HELPERS_PROBE, side_effect=[False, True]
        ) as probe, patch(_HELPERS_SLEEP):
            result = recover(client)
        assert result == "reboot"
        client.reset.assert_called_once_with()
        client.reboot.assert_called_once_with()
        assert probe.call_count == 2

    def test_recover_both_fail_raises(self) -> None:
        client = _make_recover_client()
        with patch(_HELPERS_PROBE, return_value=False), patch(_HELPERS_SLEEP):
            with pytest.raises(Ultimate64UnreachableError):
                recover(client)
        client.reset.assert_called_once_with()
        client.reboot.assert_called_once_with()

    def test_recover_never_calls_poweroff(self) -> None:
        client = _make_recover_client()
        with patch(_HELPERS_PROBE, return_value=False), patch(_HELPERS_SLEEP):
            with pytest.raises(Ultimate64UnreachableError):
                recover(client)
        client.poweroff.assert_not_called()

    def test_recover_escalate_to_reboot_false(self) -> None:
        client = _make_recover_client()
        with patch(_HELPERS_PROBE, return_value=False) as probe, patch(
            _HELPERS_SLEEP
        ):
            with pytest.raises(Ultimate64UnreachableError):
                recover(client, escalate_to_reboot=False)
        client.reset.assert_called_once_with()
        client.reboot.assert_not_called()
        assert probe.call_count == 1

    def test_recover_tolerates_reset_raising(self) -> None:
        client = _make_recover_client()
        client.reset.side_effect = Ultimate64Error("transient")
        with patch(_HELPERS_PROBE, return_value=True), patch(_HELPERS_SLEEP):
            assert recover(client) == "reset"


# --------------------------------------------------------------------------- #
# runner_health_check()                                                       #
# --------------------------------------------------------------------------- #

class TestRunnerHealthCheck:
    def test_runner_health_check_happy_path(self) -> None:
        client = _make_client()
        runner_health_check(client)
        client.run_prg.assert_called_once_with(bytes([0x01, 0x08, 0x60]))

    def test_runner_health_check_detects_stuck(self) -> None:
        client = _make_client()
        client.run_prg.side_effect = Ultimate64Error(
            "POST /v1/runners:run_prg returned HTTP 400: "
            '{"errors":["Cannot open file"]}',
            status=400,
            body='{"errors":["Cannot open file"]}',
        )
        with pytest.raises(Ultimate64RunnerStuckError):
            runner_health_check(client)

    def test_runner_health_check_passes_through_other_errors(self) -> None:
        client = _make_client()
        client.run_prg.side_effect = Ultimate64Error(
            "auth failed", status=401, body="unauthorized"
        )
        with pytest.raises(Ultimate64Error) as exc_info:
            runner_health_check(client)
        assert not isinstance(exc_info.value, Ultimate64RunnerStuckError)
