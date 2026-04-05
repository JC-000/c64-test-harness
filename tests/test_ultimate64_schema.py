"""Tests for ultimate64_schema constants + helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from c64_test_harness.backends.ultimate64_schema import (
    CARTRIDGE_VALUES,
    CPU_SPEED_BY_MHZ,
    CPU_SPEED_VALUES,
    DISK_IMAGE_TYPES,
    DRIVE_TYPE_VALUES,
    MOUNT_MODES,
    REU_ENABLED_VALUES,
    REU_SIZE_VALUES,
    SID_ADDRESS_VALUES,
    SID_TYPE_VALUES,
    TURBO_CONTROL_VALUES,
    SIDSocketConfig,
    cpu_speed_enum,
    cpu_speed_mhz,
    reu_size_enum,
    validate_enum,
)


# --------------------------------------------------------------------------- #
# CPU Speed                                                                   #
# --------------------------------------------------------------------------- #

def test_cpu_speed_enum_single_digit_is_space_padded() -> None:
    assert cpu_speed_enum(1) == " 1"
    assert cpu_speed_enum(8) == " 8"


def test_cpu_speed_enum_double_digit_has_no_space() -> None:
    assert cpu_speed_enum(10) == "10"
    assert cpu_speed_enum(48) == "48"


def test_cpu_speed_enum_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        cpu_speed_enum(100)
    with pytest.raises(ValueError):
        cpu_speed_enum(7)  # gap between 6 and 8


def test_cpu_speed_mhz_parses_padded_values() -> None:
    assert cpu_speed_mhz(" 1") == 1
    assert cpu_speed_mhz(" 8") == 8
    assert cpu_speed_mhz("48") == 48


def test_cpu_speed_mhz_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        cpu_speed_mhz("64")
    with pytest.raises(ValueError):
        cpu_speed_mhz("1")  # missing leading space


def test_cpu_speed_round_trip_all_values() -> None:
    for raw in CPU_SPEED_VALUES:
        mhz = cpu_speed_mhz(raw)
        assert cpu_speed_enum(mhz) == raw


def test_cpu_speed_values_count() -> None:
    assert len(CPU_SPEED_VALUES) == 16
    assert len(CPU_SPEED_BY_MHZ) == 16


# --------------------------------------------------------------------------- #
# REU                                                                         #
# --------------------------------------------------------------------------- #

def test_reu_size_values_count() -> None:
    assert len(REU_SIZE_VALUES) == 8


def test_reu_size_enum_accepts_strings() -> None:
    assert reu_size_enum("16 MB") == "16 MB"
    assert reu_size_enum("128 KB") == "128 KB"


def test_reu_size_enum_accepts_bytes() -> None:
    assert reu_size_enum(16 * 1024 * 1024) == "16 MB"
    assert reu_size_enum(128 * 1024) == "128 KB"
    assert reu_size_enum(512 * 1024) == "512 KB"


def test_reu_size_enum_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        reu_size_enum("32 MB")
    with pytest.raises(ValueError):
        reu_size_enum(999)
    with pytest.raises(TypeError):
        reu_size_enum(3.14)  # type: ignore[arg-type]


def test_reu_enabled_values() -> None:
    assert "Enabled" in REU_ENABLED_VALUES
    assert "Disabled" in REU_ENABLED_VALUES


# --------------------------------------------------------------------------- #
# SID                                                                         #
# --------------------------------------------------------------------------- #

def test_sid_address_values_count() -> None:
    assert len(SID_ADDRESS_VALUES) == 49
    assert SID_ADDRESS_VALUES[0] == "Unmapped"
    assert "$D400" in SID_ADDRESS_VALUES
    assert "$DFE0" in SID_ADDRESS_VALUES


def test_sid_socket_config_validates() -> None:
    cfg = SIDSocketConfig(sid_type="8580", address="$D400")
    assert cfg.sid_type == "8580"
    assert cfg.address == "$D400"


def test_sid_socket_config_rejects_bad_type() -> None:
    with pytest.raises(ValueError):
        SIDSocketConfig(sid_type="9999", address="$D400")


def test_sid_socket_config_rejects_bad_address() -> None:
    with pytest.raises(ValueError):
        SIDSocketConfig(sid_type="Enabled", address="$C000")


def test_sid_socket_config_frozen() -> None:
    cfg = SIDSocketConfig(sid_type="Enabled", address="$D400")
    with pytest.raises(Exception):
        cfg.sid_type = "Disabled"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Drive / Cartridge / Disk types                                              #
# --------------------------------------------------------------------------- #

def test_drive_type_values() -> None:
    assert DRIVE_TYPE_VALUES == ("1541", "1571", "1581")


def test_cartridge_values_contains_empty_default() -> None:
    assert "" in CARTRIDGE_VALUES


def test_disk_image_types() -> None:
    assert DISK_IMAGE_TYPES == ("d64", "d71", "d81", "g64")


def test_mount_modes() -> None:
    assert MOUNT_MODES == ("readwrite", "readonly", "unlinked")


def test_turbo_control_values() -> None:
    assert "Off" in TURBO_CONTROL_VALUES


# --------------------------------------------------------------------------- #
# validate_enum                                                               #
# --------------------------------------------------------------------------- #

def test_validate_enum_passthrough() -> None:
    assert validate_enum("1541", DRIVE_TYPE_VALUES, "drive type") == "1541"


def test_validate_enum_raises() -> None:
    with pytest.raises(ValueError) as exc:
        validate_enum("9999", DRIVE_TYPE_VALUES, "drive type")
    assert "drive type" in str(exc.value)


# --------------------------------------------------------------------------- #
# Cross-check against probe JSON files on disk                                #
# --------------------------------------------------------------------------- #

_PROBE_DIR = Path("/tmp")


def _load_probe(name: str) -> dict:
    path = _PROBE_DIR / name
    if not path.exists():
        pytest.skip(f"probe file not present: {path}")
    return json.loads(path.read_text())


def test_probe_cpu_speed_matches() -> None:
    data = _load_probe("u64_probe_item_cpu_speed.json")
    values = data["U64 Specific Settings"]["CPU Speed"]["values"]
    assert tuple(values) == CPU_SPEED_VALUES


def test_probe_reu_size_matches() -> None:
    data = _load_probe("u64_probe_item_reu_size.json")
    values = data["C64 and Cartridge Settings"]["REU Size"]["values"]
    assert tuple(values) == REU_SIZE_VALUES


def test_probe_sid_address_matches() -> None:
    data = _load_probe("u64_probe_item_sid1_addr.json")
    values = data["SID Addressing"]["SID Socket 1 Address"]["values"]
    assert tuple(values) == SID_ADDRESS_VALUES
    assert len(values) == 49


def test_probe_drive_type_matches() -> None:
    data = _load_probe("u64_probe_item_drive_type.json")
    values = data["Drive A Settings"]["Drive Type"]["values"]
    assert tuple(values) == DRIVE_TYPE_VALUES
