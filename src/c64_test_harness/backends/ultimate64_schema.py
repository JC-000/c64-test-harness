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
from enum import Enum
from typing import Mapping, Union

# --------------------------------------------------------------------------- #
# Turbo / CPU Speed                                                           #
# --------------------------------------------------------------------------- #

#: Raw CPU Speed enum values from ``U64 Specific Settings / CPU Speed``.
#: Single-digit speeds are space-padded to width 2 (e.g. ``" 1"``).
#: Superset across device generations: U64E firmware 3.14 reports
#: " 1".." 5".."48"; the C64 Ultimate (firmware 1.1.0, core 1.49) drops
#: " 5" and adds "64". Speeds absent on a given device are rejected by
#: its firmware at set time.
CPU_SPEED_VALUES: tuple[str, ...] = (
    " 1", " 2", " 3", " 4", " 5", " 6", " 8", "10",
    "12", "14", "16", "20", "24", "32", "40", "48",
    "64",
)

#: Mapping from integer MHz to the device's enum string.
CPU_SPEED_BY_MHZ: dict[int, str] = {
    1: " 1", 2: " 2", 3: " 3", 4: " 4", 5: " 5", 6: " 6",
    8: " 8", 10: "10", 12: "12", 14: "14", 16: "16",
    20: "20", 24: "24", 32: "32", 40: "40", 48: "48",
    64: "64",
}


def cpu_speed_enum(mhz: int) -> str:
    """Convert an integer MHz value to the device's CPU Speed enum string.

    :param mhz: CPU speed in MHz. Must be one of the supported values
        in :data:`CPU_SPEED_BY_MHZ`.
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


#: Socket-enable values for ``SID Sockets Configuration / SID Socket N``.
#: This item is a plain enable toggle, *not* a chip-type selector.
#: Firmware: ``u64_sid_detection_cfg`` binds CFG_SOCKET1/2_ENABLE to
#: ``en_dis`` (u64_config.cc:393-394; en_dis at config.cc:927).
SID_SOCKET_ENABLE_VALUES: tuple[str, ...] = ("Disabled", "Enabled")

#: Chip types the firmware can report in ``SID Detected Socket N``.
#: Read-only on the device: the store constructor calls
#: ``cfg->disable(CFG_SID1_TYPE)`` / ``CFG_SID2_TYPE``
#: (u64_config.cc:517-518), so this is a detection result, not a
#: selector. Firmware table ``sid_types`` at u64_config.cc:271.
#:
#: Firmware 3.14 shipped the first nine entries only; 3.15 appends
#: ``"PDsid"``, ``"SIDKick (Teensy)"`` and ``"SIDKick Pico"`` (compare
#: ``git show ult_v3.14:software/u64/u64_config.cc`` line 236).
SID_DETECTED_TYPE_VALUES: tuple[str, ...] = (
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

#: ``SID Addressing / Ext DualSID Range Split`` — which address line
#: splits a dual-SID cartridge across the two socket decodes.
#: Firmware ``stereo_addr`` at u64_config.cc:256.
SID_STEREO_SPLIT_VALUES: tuple[str, ...] = ("Off", "A5", "A6", "A7", "A8", "A9")

#: ``SID Addressing / UltiSID Range Split``.
#: Firmware ``sid_split`` at u64_config.cc:257.
ULTISID_SPLIT_VALUES: tuple[str, ...] = (
    "Off",
    "1/2 (A5)",
    "1/2 (A6)",
    "1/2 (A7)",
    "1/2 (A8)",
    "1/4 (A5,A6)",
    "1/4 (A5,A8)",
    "1/4 (A7,A8)",
)

#: ``UltiSID Configuration / UltiSID N Filter Curve``.
#: Firmware ``filter_sel`` at u64_config.cc:274.
ULTISID_FILTER_VALUES: tuple[str, ...] = (
    "8580 Lo",
    "8580 Hi",
    "6581",
    "6581 Alt",
    "U2 Low",
    "U2 Mid",
    "U2 High",
)

#: ``UltiSID Configuration / UltiSID N Filter Resonance`` (``filter_res``,
#: u64_config.cc:275).
ULTISID_RESONANCE_VALUES: tuple[str, ...] = ("Low", "High")

#: ``UltiSID Configuration / UltiSID N Combined Waveforms`` (``comb_wave``,
#: u64_config.cc:276).
ULTISID_WAVEFORM_VALUES: tuple[str, ...] = ("6581", "8580")

#: ``UltiSID Configuration / UltiSID N Digis Level`` (``digi_levels``,
#: u64_config.cc:261).
ULTISID_DIGI_VALUES: tuple[str, ...] = ("Off", "Low", "Medium", "High")


class SidSlot(str, Enum):
    """One of the four independently addressable SID decodes.

    The device has two physical sockets and two FPGA-emulated
    ("UltiSID") cores, and all four take their base address from the
    same 49-entry enum (u64_config.cc:404-408). The member value is the
    firmware's name for the slot, which is also the prefix of its
    address item.
    """

    SOCKET1 = "SID Socket 1"
    SOCKET2 = "SID Socket 2"
    ULTISID1 = "UltiSID 1"
    ULTISID2 = "UltiSID 2"


#: Slot -> the ``SID Addressing`` item name that holds its base address.
SID_SLOT_ADDRESS_ITEMS: dict[SidSlot, str] = {
    slot: f"{slot.value} Address" for slot in SidSlot
}

#: The value ``u64_sid_offsets[0]`` carries for ``"Unmapped"``
#: (``#define UNMAPPED_BASE 0x01``, u64_config.cc:193). It is an odd
#: number, and every real decode is even, so an unmapped slot can never
#: alias a mapped one.
SID_UNMAPPED_OFFSET: int = 0x01

#: Address enum string -> the 8-bit decode value the firmware programs
#: into ``C64_SID1_BASE`` and friends. Mirrors ``u64_sid_offsets``
#: (u64_config.cc:219-227).
SID_ADDRESS_OFFSETS: dict[str, int] = {
    "Unmapped": SID_UNMAPPED_OFFSET,
    **{
        addr: (int(addr[1:], 16) & 0x0FF0) >> 4
        for addr in SID_ADDRESS_VALUES[1:]
    },
}


def sid_address_offset(address: str) -> int:
    """Return the firmware decode offset for a SID address enum string.

    :param address: One of :data:`SID_ADDRESS_VALUES`.
    :returns: The 8-bit base value, e.g. ``0x40`` for ``"$D400"`` and
        :data:`SID_UNMAPPED_OFFSET` for ``"Unmapped"``.
    :raises ValueError: If *address* is not a known SID address.
    """
    try:
        return SID_ADDRESS_OFFSETS[address]
    except (KeyError, TypeError):
        raise ValueError(
            f"Invalid SID address {address!r}. "
            f"Valid values: {list(SID_ADDRESS_VALUES)}"
        ) from None


@dataclass(frozen=True)
class SidAddressConflict:
    """Two or more slots decoding the same base address.

    :param address: The shared address enum string.
    :param slots: The slots that share it, in :class:`SidSlot` order.
    """

    address: str
    slots: tuple[SidSlot, ...]


def _as_slot(key: Union[SidSlot, str]) -> SidSlot:
    """Coerce a slot name or address-item name to a :class:`SidSlot`."""
    if isinstance(key, SidSlot):
        return key
    if isinstance(key, str):
        name = key[: -len(" Address")] if key.endswith(" Address") else key
        for slot in SidSlot:
            if slot.value == name:
                return slot
    raise ValueError(
        f"Invalid SID slot {key!r}. Valid values: "
        f"{[s.value for s in SidSlot]} (optionally suffixed ' Address')"
    )


def sid_address_occupancy(
    mapping: "Mapping[Union[SidSlot, str], str]",
) -> dict[str, tuple[SidSlot, ...]]:
    """Group slots by the base address they decode.

    ``"Unmapped"`` slots are omitted entirely: the firmware gives them
    the odd offset ``0x01`` while every mapped decode is even, so they
    can never alias anything.

    :param mapping: Slot (or address-item name) -> address enum string.
    :returns: Address enum string -> the slots on it, in
        :class:`SidSlot` order, keyed in ascending decode order.
    :raises ValueError: On an unknown slot key or address value.
    """
    order = {slot: i for i, slot in enumerate(SidSlot)}
    by_offset: dict[int, list[SidSlot]] = {}
    for key, address in mapping.items():
        slot = _as_slot(key)
        offset = sid_address_offset(address)
        if offset == SID_UNMAPPED_OFFSET:
            continue
        by_offset.setdefault(offset, []).append(slot)

    result: dict[str, tuple[SidSlot, ...]] = {}
    for offset in sorted(by_offset):
        slots = sorted(by_offset[offset], key=order.__getitem__)
        address = next(
            a for a, o in SID_ADDRESS_OFFSETS.items() if o == offset
        )
        result[address] = tuple(slots)
    return result


def sid_address_conflicts(
    mapping: "Mapping[Union[SidSlot, str], str]",
) -> list[SidAddressConflict]:
    """Find slots that would decode the same base address.

    ``"Unmapped"`` slots are never in conflict: the firmware gives them
    the odd offset ``0x01`` while every mapped decode is even.

    Only exact base equality counts. ``Auto Address Mirroring`` widens
    the decodes but is documented to fill the address space "without
    introducing overlaps that were not already there"
    (u64_config.cc:2381-2384), so it cannot create a conflict that
    exact-base comparison misses.

    :param mapping: Slot (or address-item name) -> address enum string.
    :returns: One :class:`SidAddressConflict` per shared address, in
        ascending offset order.
    :raises ValueError: On an unknown slot key or address value.
    """
    occupancy = sid_address_occupancy(mapping)
    return [
        SidAddressConflict(address=address, slots=slots)
        for address, slots in occupancy.items()
        if len(slots) >= 2
    ]


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

    Validates *sid_type* against :data:`SID_SOCKET_ENABLE_VALUES` and
    *address* against :data:`SID_ADDRESS_VALUES` at construction time.

    .. note::

       Despite the field name, *sid_type* is the socket's **enable
       state**, because that is the only thing ``SID Socket N`` accepts
       (``en_dis``, u64_config.cc:393-394). A chip type is rejected: the
       detected type lives in a separate, probe-filled item and is not a
       selector. The field previously validated against a fabricated
       union of both domains, which let callers build a config the
       device answers HTTP 400 to.

    :param sid_type: ``"Enabled"`` or ``"Disabled"``.
    :param address: Device address enum (e.g. ``"$D400"`` or ``"Unmapped"``).
    """

    sid_type: str
    address: str

    def __post_init__(self) -> None:
        validate_enum(
            self.sid_type, SID_SOCKET_ENABLE_VALUES, "SID socket state"
        )
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
    "SID_ADDRESS_VALUES",
    "SID_SOCKET_ENABLE_VALUES",
    "SID_DETECTED_TYPE_VALUES",
    "SID_STEREO_SPLIT_VALUES",
    "ULTISID_SPLIT_VALUES",
    "ULTISID_FILTER_VALUES",
    "ULTISID_RESONANCE_VALUES",
    "ULTISID_WAVEFORM_VALUES",
    "ULTISID_DIGI_VALUES",
    "SidSlot",
    "SID_SLOT_ADDRESS_ITEMS",
    "SID_UNMAPPED_OFFSET",
    "SID_ADDRESS_OFFSETS",
    "sid_address_offset",
    "SidAddressConflict",
    "sid_address_occupancy",
    "sid_address_conflicts",
    "DRIVE_TYPE_VALUES",
    "CARTRIDGE_VALUES",
    "DISK_IMAGE_TYPES",
    "MOUNT_MODES",
    "validate_enum",
    "SIDSocketConfig",
]
