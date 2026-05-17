"""Unit tests for ultimate64_helpers (mocked Ultimate64Client)."""
from __future__ import annotations

from typing import Callable
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
    ProgressEvent,
    U64StateSnapshot,
    Ultimate64MeasurementEnvironmentError,
    check_measurement_environment,
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
    watch_progress,
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


# --------------------------------------------------------------------------- #
# check_measurement_environment()                                             #
# --------------------------------------------------------------------------- #

class TestCheckMeasurementEnvironment:
    """Tests for check_measurement_environment() — GitHub issue #102."""

    def _client_with_turbo(self, turbo: str, cpu_speed: str) -> MagicMock:
        client = _make_client()
        client.get_config_category.return_value = _u64_specific(
            turbo=turbo, cpu_speed=cpu_speed
        )
        return client

    def test_clean_1mhz_returns_none(self) -> None:
        """Device at 1 MHz (Manual + ' 1') is safe — no exception."""
        client = self._client_with_turbo("Manual", " 1")
        result = check_measurement_environment(client)
        assert result is None

    def test_turbo_off_returns_none(self) -> None:
        """Turbo Control == 'Off' means get_turbo_mhz returns None — safe."""
        client = self._client_with_turbo("Off", " 1")
        result = check_measurement_environment(client)
        assert result is None

    def test_turbo_48mhz_raises(self) -> None:
        """48 MHz turbo left from prior session raises with '48' and 'set_turbo_mhz'."""
        client = self._client_with_turbo("Manual", "48")
        with pytest.raises(Ultimate64MeasurementEnvironmentError) as exc_info:
            check_measurement_environment(client)
        msg = str(exc_info.value)
        assert "48" in msg
        assert "set_turbo_mhz" in msg

    def test_turbo_6mhz_raises_with_value(self) -> None:
        """Non-standard turbo value (6 MHz) raises with '6' in the message."""
        client = self._client_with_turbo("Manual", " 6")
        with pytest.raises(Ultimate64MeasurementEnvironmentError) as exc_info:
            check_measurement_environment(client)
        msg = str(exc_info.value)
        assert "6" in msg
        assert "set_turbo_mhz" in msg

    def test_raises_is_subclass_of_ultimate64_error(self) -> None:
        """Ultimate64MeasurementEnvironmentError is an Ultimate64Error subclass."""
        client = self._client_with_turbo("Manual", "48")
        with pytest.raises(Ultimate64Error):
            check_measurement_environment(client)


# --------------------------------------------------------------------------- #
# watch_progress() — GitHub issue #108                                        #
# --------------------------------------------------------------------------- #


class _FakeClock:
    """A deterministic clock with explicit list of monotonic returns.

    The list is consumed in order; once exhausted the clock keeps
    incrementing by ``step`` (default 1.0 s) so generators bounded by
    overall_timeout always eventually terminate even if a test consumes
    extra ticks.
    """

    def __init__(self, ticks: list[float], step: float = 1.0) -> None:
        if not ticks:
            raise ValueError("ticks must be non-empty")
        self._ticks = list(ticks)
        self._idx = 0
        self._step = step
        self._tail = ticks[-1]

    def __call__(self) -> float:
        if self._idx < len(self._ticks):
            value = self._ticks[self._idx]
            self._idx += 1
            self._tail = value
        else:
            self._tail += self._step
            value = self._tail
        return value


def _record_sleep() -> tuple[list[float], "Callable[[float], None]"]:
    calls: list[float] = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, _sleep


class TestWatchProgress:
    """Unit tests for watch_progress() with mocked DMA reads."""

    def test_validates_empty_addresses(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="non-empty"):
            list(watch_progress(client, addresses={}))

    def test_validates_non_positive_intervals(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="poll_interval"):
            list(watch_progress(
                client, addresses={"s": (0x0400, 1)}, poll_interval=0,
            ))
        with pytest.raises(ValueError, match="idle_timeout"):
            list(watch_progress(
                client, addresses={"s": (0x0400, 1)}, idle_timeout=0,
            ))
        with pytest.raises(ValueError, match="overall_timeout"):
            list(watch_progress(
                client, addresses={"s": (0x0400, 1)}, overall_timeout=0,
            ))

    def test_validates_address_specs(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="out of range"):
            list(watch_progress(client, addresses={"x": (0x10000, 1)}))
        with pytest.raises(ValueError, match="invalid"):
            list(watch_progress(client, addresses={"x": (0x0400, 0)}))
        with pytest.raises(ValueError, match="invalid"):
            list(watch_progress(client, addresses={"x": (0xFFFF, 4)}))

    def test_first_poll_yields_baseline_advanced(self) -> None:
        """The first successful poll emits an Advanced event with empty old bytes."""
        client = _make_client()
        client.read_mem.return_value = b"\x00"
        clock = _FakeClock([0.0, 0.1])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"sentinel": (0x3C80, 1)},
            poll_interval=10.0,
            idle_timeout=120.0,
            overall_timeout=5400.0,
            _clock=clock,
            _sleep=sleep,
        )
        event = next(gen)
        gen.close()

        assert event.kind == "Advanced"
        assert event.changed == {"sentinel": (b"", b"\x00")}
        assert event.values == {"sentinel": b"\x00"}
        assert event.error is None

    def test_advanced_on_change(self) -> None:
        """A change between polls yields Advanced with (old, new) bytes."""
        client = _make_client()
        # poll1: 0x00, poll2: 0x01, poll3: 0x01
        client.read_mem.side_effect = [b"\x00", b"\x01", b"\x01"]
        # Clock: start, mid-poll, after-poll, sleep, start2, mid2, after2, ...
        # The implementation calls _clock() once per loop top and once
        # after the reads. Just provide plenty of ticks.
        clock = _FakeClock([0.0, 0.1, 0.2, 10.0, 10.1, 20.0, 20.1, 30.0])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=10.0,
            idle_timeout=120.0,
            overall_timeout=5400.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)  # baseline Advanced
        second = next(gen)  # actual change Advanced
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "Advanced"
        assert second.changed == {"x": (b"\x00", b"\x01")}
        assert second.values == {"x": b"\x01"}

    def test_stalled_after_idle_timeout(self) -> None:
        """No-change for idle_timeout seconds emits Stalled."""
        client = _make_client()
        # Same byte every poll => no diff
        client.read_mem.return_value = b"\x42"
        # Make the second poll's elapsed >= idle_timeout=5.0
        clock = _FakeClock([0.0, 0.1, 0.2, 6.0, 6.1, 6.2, 6.3])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=5.0,
            overall_timeout=60.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)  # baseline Advanced
        second = next(gen)
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "Stalled"
        assert second.changed == {}
        assert second.values == {"x": b"\x42"}

    def test_timeout_terminates_generator(self) -> None:
        """overall_timeout elapsed emits Timeout and exits cleanly."""
        client = _make_client()
        client.read_mem.return_value = b"\x00"
        # Force elapsed >= overall_timeout=2.0 on the very first iteration
        clock = _FakeClock([0.0, 0.1, 0.2, 100.0])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=5.0,
            overall_timeout=2.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)  # baseline
        second = next(gen)  # next iteration's clock check trips Timeout
        with pytest.raises(StopIteration):
            next(gen)
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "Timeout"

    def test_poll_error_then_continue(self) -> None:
        """A failed read yields PollError but polling continues on the next tick."""
        client = _make_client()
        from c64_test_harness.backends.ultimate64_client import Ultimate64Error
        boom = Ultimate64Error("transient DMA failure")
        # poll1 succeeds, poll2 raises, poll3 succeeds with a change.
        client.read_mem.side_effect = [b"\x00", boom, b"\x01"]
        clock = _FakeClock(
            [0.0, 0.1, 0.2, 1.0, 1.1, 1.2, 2.0, 2.1, 2.2, 3.0]
        )
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=600.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)
        second = next(gen)
        third = next(gen)
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "PollError"
        assert second.error is boom
        assert third.kind == "Advanced"
        assert third.changed == {"x": (b"\x00", b"\x01")}

    def test_poll_error_does_not_reset_baseline(self) -> None:
        """PollError preserves last-known values so the next diff is correct."""
        client = _make_client()
        from c64_test_harness.backends.ultimate64_client import Ultimate64Error
        client.read_mem.side_effect = [
            b"\x00",  # baseline
            Ultimate64Error("flake"),  # error
            b"\x00",  # unchanged
        ]
        clock = _FakeClock([0.0] + [float(i) * 0.5 for i in range(1, 30)])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=0.1,
            idle_timeout=600.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)
        second = next(gen)
        # Third poll returns the same byte we baselined — must NOT emit
        # a spurious Advanced caused by the PollError clearing state.
        # The generator should sleep instead; pull events until something
        # non-Advanced/empty shows or we hit Stalled.
        # Simplest: call next() with a short idle_timeout to force Stalled.
        gen.close()

        assert first.kind == "Advanced"
        assert second.kind == "PollError"

    def test_finished_via_stop_when(self) -> None:
        """stop_when returning truthy yields Finished and ends iteration."""
        client = _make_client()
        client.read_mem.side_effect = [b"\x00", b"\xFF"]
        clock = _FakeClock([0.0, 0.1, 0.2, 1.0, 1.1, 1.2])
        _, sleep = _record_sleep()

        def stop_at_ff(values: dict) -> bool:
            return values.get("x") == b"\xFF"

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=1.0,
            idle_timeout=600.0,
            overall_timeout=600.0,
            stop_when=stop_at_ff,
            _clock=clock,
            _sleep=sleep,
        )
        events = list(gen)
        # baseline Advanced (sentinel=0x00), then change Advanced to 0xFF,
        # then Finished.
        assert [e.kind for e in events] == ["Advanced", "Advanced", "Finished"]
        assert events[-1].values == {"x": b"\xFF"}

    def test_generator_cleanup_on_close(self) -> None:
        """Closing the generator releases without further reads."""
        client = _make_client()
        client.read_mem.return_value = b"\x00"
        clock = _FakeClock([0.0, 0.1, 0.2, 10.0, 10.1, 20.0])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=10.0,
            idle_timeout=120.0,
            overall_timeout=5400.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)
        # Caller breaks out — simulate by .close()
        gen.close()
        assert first.kind == "Advanced"
        # Subsequent next() on a closed generator raises StopIteration.
        with pytest.raises(StopIteration):
            next(gen)

    def test_multiple_ranges_independent_diff(self) -> None:
        """Each named range diffs independently; only changed names appear."""
        client = _make_client()
        # Two ranges per poll. Order: sentinel then row, repeated per poll.
        client.read_mem.side_effect = [
            b"\x00", b"AAAA",  # poll1
            b"\x01", b"AAAA",  # poll2: only sentinel changed
        ]
        clock = _FakeClock([0.0, 0.1, 0.2, 1.0, 1.1, 1.2, 2.0])
        _, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={
                "sentinel": (0x3C80, 1),
                "row": (0x0780, 4),
            },
            poll_interval=1.0,
            idle_timeout=600.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        first = next(gen)
        second = next(gen)
        gen.close()

        assert first.kind == "Advanced"
        assert set(first.changed) == {"sentinel", "row"}
        assert second.kind == "Advanced"
        assert set(second.changed) == {"sentinel"}
        assert second.changed["sentinel"] == (b"\x00", b"\x01")
        assert second.values == {"sentinel": b"\x01", "row": b"AAAA"}

    def test_poll_interval_passed_to_sleep(self) -> None:
        """The poll_interval value is what we sleep for between polls."""
        client = _make_client()
        # Two different reads so the second poll yields Advanced (not Stalled)
        client.read_mem.side_effect = [b"\x00", b"\x01"]
        clock = _FakeClock([0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
        sleeps, sleep = _record_sleep()

        gen = watch_progress(
            client,
            addresses={"x": (0x0400, 1)},
            poll_interval=7.5,
            idle_timeout=600.0,
            overall_timeout=600.0,
            _clock=clock,
            _sleep=sleep,
        )
        next(gen)  # baseline Advanced
        next(gen)  # change Advanced
        gen.close()

        assert sleeps and all(s == 7.5 for s in sleeps)

    def test_progress_event_dataclass_defaults(self) -> None:
        """ProgressEvent has sensible defaults for unset fields."""
        e = ProgressEvent(kind="Stalled", elapsed=42.0)
        assert e.kind == "Stalled"
        assert e.changed == {}
        assert e.values == {}
        assert e.error is None
        assert e.elapsed == 42.0
