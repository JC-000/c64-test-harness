"""Static schema constants derived from Ultimate 64 Elite firmware 3.14 device probe.

Values pulled verbatim from device enum responses. Regenerate if firmware changes.
See ``scripts/probe_u64.py`` and ``scripts/U64_DEVICE_PROBE.md``.

All enum string values preserve the exact whitespace and casing returned by the
device's REST API (e.g. CPU Speed values are right-aligned to width 2, so
``" 1"`` has a leading space).

This module is pure-constants: no I/O, no side effects at import time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

# --------------------------------------------------------------------------- #
# Turbo / CPU Speed                                                           #
# --------------------------------------------------------------------------- #

#: Raw CPU Speed enum values from ``U64 Specific Settings / CPU Speed``.
#: Single-digit speeds are space-padded to width 2 (e.g. ``" 1"``).
CPU_SPEED_VALUES: tuple[str, ...] = (
    " 1", " 2", " 3", " 4", " 5", " 6", " 8", "10",
    "12", "14", "16", "20", "24", "32", "40", "48",
)

#: Mapping from integer MHz to the device's enum string.
CPU_SPEED_BY_MHZ: dict[int, str] = {
    1: " 1", 2: " 2", 3: " 3", 4: " 4", 5: " 5", 6: " 6",
    8: " 8", 10: "10", 12: "12", 14: "14", 16: "16",
    20: "20", 24: "24", 32: "32", 40: "40", 48: "48",
}


def cpu_speed_enum(mhz: int) -> str:
    """Convert an integer MHz value to the device's CPU Speed enum string.

    :param mhz: CPU speed in MHz. Must be one of the 16 supported values.
    :returns: The device enum string (e.g. ``" 1"``, ``"48"``).
    :raises ValueError: If *mhz* is not a supported CPU speed.
    """
    if mhz not in CPU_SPEED_BY_MHZ:
        raise ValueError(
            f"Unsupported CPU speed {mhz} MHz. "
            f"Valid values: {sorted(CPU_SPEED_BY_MHZ)}"
        )
    return CPU_SPEED_BY_MHZ[mhz]


def cpu_speed_mhz(enum_value: str) -> int:
    """Inverse of :func:`cpu_speed_enum`: device enum string -> MHz int.

    :param enum_value: The device enum string (e.g. ``" 1"`` or ``"48"``).
    :returns: CPU speed in MHz.
    :raises ValueError: If *enum_value* is not a known CPU Speed enum.
    """
    if enum_value not in CPU_SPEED_VALUES:
        raise ValueError(
            f"Unknown CPU Speed enum value {enum_value!r}. "
            f"Valid values: {list(CPU_SPEED_VALUES)}"
        )
    # int() tolerates leading/trailing whitespace.
    return int(enum_value)


# --------------------------------------------------------------------------- #
# Turbo Control mode                                                          #
# --------------------------------------------------------------------------- #

#: Turbo Control selector in ``U64 Specific Settings``. The device accepts
#: four values: ``"Off"`` disables turbo; ``"Manual"`` enables turbo using the
#: selected ``CPU Speed`` enum; ``"U64 Turbo Registers"`` and ``"TurboEnable
#: Bit"`` gate turbo on software / register signals.
TURBO_CONTROL_VALUES: tuple[str, ...] = (
    "Off",
    "Manual",
    "U64 Turbo Registers",
    "TurboEnable Bit",
)


# --------------------------------------------------------------------------- #
# REU (RAM Expansion Unit)                                                    #
# --------------------------------------------------------------------------- #

#: REU capacity enum values from ``C64 and Cartridge Settings / REU Size``.
REU_SIZE_VALUES: tuple[str, ...] = (
    "128 KB", "256 KB", "512 KB", "1 MB", "2 MB", "4 MB", "8 MB", "16 MB",
)

#: Master REU on/off switch (``C64 and Cartridge Settings / RAM Expansion Unit``).
REU_ENABLED_VALUES: tuple[str, ...] = ("Enabled", "Disabled")

#: Byte sizes corresponding to each REU_SIZE_VALUES entry.
_REU_SIZE_BYTES: dict[str, int] = {
    "128 KB": 128 * 1024,
    "256 KB": 256 * 1024,
    "512 KB": 512 * 1024,
    "1 MB": 1 * 1024 * 1024,
    "2 MB": 2 * 1024 * 1024,
    "4 MB": 4 * 1024 * 1024,
    "8 MB": 8 * 1024 * 1024,
    "16 MB": 16 * 1024 * 1024,
}

_REU_BYTES_TO_ENUM: dict[int, str] = {v: k for k, v in _REU_SIZE_BYTES.items()}


def reu_size_enum(size_spec: Union[str, int]) -> str:
    """Normalise a size specification to the device's REU Size enum string.

    Accepts either:

    - An existing enum string (``"16 MB"``) -- returned as-is after validation.
    - An integer byte count (``16777216``) -- mapped to the matching enum.

    :param size_spec: Size spec as str or int.
    :returns: A value drawn from :data:`REU_SIZE_VALUES`.
    :raises ValueError: If the spec cannot be mapped.
    """
    if isinstance(size_spec, str):
        if size_spec in REU_SIZE_VALUES:
            return size_spec
        raise ValueError(
            f"Unknown REU size string {size_spec!r}. "
            f"Valid values: {list(REU_SIZE_VALUES)}"
        )
    if isinstance(size_spec, int):
        if size_spec in _REU_BYTES_TO_ENUM:
            return _REU_BYTES_TO_ENUM[size_spec]
        raise ValueError(
            f"Unsupported REU byte count {size_spec}. "
            f"Valid byte counts: {sorted(_REU_BYTES_TO_ENUM)}"
        )
    raise TypeError(
        f"reu_size_enum expects str or int, got {type(size_spec).__name__}"
    )


# --------------------------------------------------------------------------- #
# SID types & addresses                                                       #
# --------------------------------------------------------------------------- #

#: SID socket detected-chip / type values, from ``SID Sockets Configuration``.
#: Includes detected physical types plus the socket enable toggle values.
SID_TYPE_VALUES: tuple[str, ...] = ("Enabled", "Disabled", "6581", "8580", "None")

#: SID address enum (49 entries) from ``SID Addressing / SID Socket 1 Address``.
SID_ADDRESS_VALUES: tuple[str, ...] = (
    "Unmapped",
    "$D400", "$D420", "$D440", "$D460", "$D480", "$D4A0", "$D4C0", "$D4E0",
    "$D500", "$D520", "$D540", "$D560", "$D580", "$D5A0", "$D5C0", "$D5E0",
    "$D600", "$D620", "$D640", "$D660", "$D680", "$D6A0", "$D6C0", "$D6E0",
    "$D700", "$D720", "$D740", "$D760", "$D780", "$D7A0", "$D7C0", "$D7E0",
    "$DE00", "$DE20", "$DE40", "$DE60", "$DE80", "$DEA0", "$DEC0", "$DEE0",
    "$DF00", "$DF20", "$DF40", "$DF60", "$DF80", "$DFA0", "$DFC0", "$DFE0",
)


# --------------------------------------------------------------------------- #
# Drive types                                                                 #
# --------------------------------------------------------------------------- #

#: Emulated floppy drive types from ``Drive A Settings / Drive Type``.
DRIVE_TYPE_VALUES: tuple[str, ...] = ("1541", "1571", "1581")


# --------------------------------------------------------------------------- #
# Cartridge                                                                   #
# --------------------------------------------------------------------------- #

#: Cartridge preset list from ``C64 and Cartridge Settings / Cartridge``.
#: Uses the ``presets`` schema, not ``values``. Empty on a freshly probed
#: device -- populated by user-installed cartridge images.
CARTRIDGE_VALUES: tuple[str, ...] = ("",)


# --------------------------------------------------------------------------- #
# Disk image types & mount modes (from REST API documentation)                #
# --------------------------------------------------------------------------- #

#: Disk image file formats accepted by the U64 mount endpoints.
DISK_IMAGE_TYPES: tuple[str, ...] = ("d64", "d71", "d81", "g64")

#: Mount modes for the U64 disk-mount REST endpoints.
MOUNT_MODES: tuple[str, ...] = ("readwrite", "readonly", "unlinked")


# --------------------------------------------------------------------------- #
# Validation helpers                                                          #
# --------------------------------------------------------------------------- #

def validate_enum(value: str, allowed: tuple[str, ...], name: str) -> str:
    """Validate that *value* is one of *allowed*; return it unchanged.

    :param value: The candidate value.
    :param allowed: Tuple of accepted enum strings.
    :param name: Human-readable name of the enum (for error messages).
    :returns: *value*, unchanged, when valid.
    :raises ValueError: If *value* is not in *allowed*.
    """
    if value not in allowed:
        raise ValueError(
            f"Invalid {name} {value!r}. Valid values: {list(allowed)}"
        )
    return value


@dataclass(frozen=True)
class SIDSocketConfig:
    """Structured config for one SID socket slot.

    Validates *sid_type* against :data:`SID_TYPE_VALUES` and *address*
    against :data:`SID_ADDRESS_VALUES` at construction time.

    :param sid_type: SID type / enable string (e.g. ``"Enabled"``, ``"8580"``).
    :param address: Device address enum (e.g. ``"$D400"`` or ``"Unmapped"``).
    """

    sid_type: str
    address: str

    def __post_init__(self) -> None:
        validate_enum(self.sid_type, SID_TYPE_VALUES, "SID type")
        validate_enum(self.address, SID_ADDRESS_VALUES, "SID address")


__all__ = [
    "CPU_SPEED_VALUES",
    "CPU_SPEED_BY_MHZ",
    "cpu_speed_enum",
    "cpu_speed_mhz",
    "TURBO_CONTROL_VALUES",
    "REU_SIZE_VALUES",
    "REU_ENABLED_VALUES",
    "reu_size_enum",
    "SID_TYPE_VALUES",
    "SID_ADDRESS_VALUES",
    "DRIVE_TYPE_VALUES",
    "CARTRIDGE_VALUES",
    "DISK_IMAGE_TYPES",
    "MOUNT_MODES",
    "validate_enum",
    "SIDSocketConfig",
]
