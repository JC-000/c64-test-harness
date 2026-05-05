"""Ergonomic config helpers for the Ultimate 64 REST API.

This module wraps :class:`Ultimate64Client` plus the schema enum
constants in :mod:`.ultimate64_schema` to provide friendly,
developer-oriented APIs for common configuration tasks: turbo / CPU
speed, the REU (Ram Expansion Unit), SID socket configuration, disk
mounting, and PRG execution.

All helpers are module-level functions that take an
:class:`Ultimate64Client` as the first argument. Input values are
validated against the schema enums *before* touching the network, so
bad values raise :class:`ValueError` locally rather than producing a
cryptic device-side error.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .ultimate64_client import Ultimate64Client
from .ultimate64_schema import (
    CPU_SPEED_VALUES,
    DISK_IMAGE_TYPES,
    MOUNT_MODES,
    REU_ENABLED_VALUES,
    REU_SIZE_VALUES,
    SID_ADDRESS_VALUES,
    SID_TYPE_VALUES,
    SIDSocketConfig,
    TURBO_CONTROL_VALUES,
    cpu_speed_enum,
    cpu_speed_mhz,
    reu_size_enum,
    validate_enum,
)

__all__ = [
    "get_turbo_mhz",
    "set_turbo_mhz",
    "get_turbo_enabled",
    "get_reu_config",
    "set_reu",
    "get_sid_config",
    "set_sid_socket",
    "mount_disk_file",
    "unmount",
    "run_prg_file",
    "load_prg_file",
    "reset",
    "reboot",
    "U64StateSnapshot",
    "snapshot_state",
    "restore_state",
    "CAT_U64_SPECIFIC",
    "CAT_CART",
    "CAT_SID_SOCKETS",
    "CAT_SID_ADDRESSING",
    "CAT_ULTISID",
    "CAT_AUDIO_MIXER",
    "CAT_DATA_STREAMS",
    "get_sid_socket_types",
    "get_sid_addresses",
    "configure_multi_sid",
    "get_physical_sid_sockets",
    "get_ultisid_config",
    "get_audio_mixer_config",
    "set_audio_mixer_item",
]


# --------------------------------------------------------------------------- #
# Category / item name constants                                              #
# --------------------------------------------------------------------------- #

CAT_U64_SPECIFIC = "U64 Specific Settings"
CAT_CART = "C64 and Cartridge Settings"
CAT_SID_SOCKETS = "SID Sockets Configuration"
CAT_SID_ADDRESSING = "SID Addressing"
CAT_ULTISID = "UltiSID Configuration"
CAT_AUDIO_MIXER = "Audio Mixer"
CAT_DATA_STREAMS = "Data Streams"

_ITEM_TURBO_CONTROL = "Turbo Control"
_ITEM_CPU_SPEED = "CPU Speed"
_ITEM_REU_ENABLED = "RAM Expansion Unit"
_ITEM_REU_SIZE = "REU Size"
_ITEM_CARTRIDGE = "Cartridge"


def _unwrap(resp: dict, category: str) -> dict:
    """Pull the inner category dict out of a config GET response.

    The device always wraps item maps under the category name, with an
    ``errors`` array alongside. This helper returns the inner dict.
    """
    if not isinstance(resp, dict):
        raise ValueError(f"expected dict response, got {type(resp).__name__}")
    inner = resp.get(category)
    if not isinstance(inner, dict):
        raise ValueError(
            f"response missing category {category!r}; keys: {list(resp)!r}"
        )
    return inner


# --------------------------------------------------------------------------- #
# Turbo / CPU speed                                                           #
# --------------------------------------------------------------------------- #

def get_turbo_mhz(client: Ultimate64Client) -> int | None:
    """Return the current CPU speed in MHz if turbo is active, else ``None``.

    :param client: Connected Ultimate64 client.
    :returns: Integer MHz (e.g. ``2``) when Turbo Control is anything
        other than ``"Off"``, otherwise ``None``.
    """
    inner = _unwrap(client.get_config_category(CAT_U64_SPECIFIC), CAT_U64_SPECIFIC)
    turbo = inner.get(_ITEM_TURBO_CONTROL)
    if turbo == "Off" or turbo is None:
        return None
    speed_enum = inner.get(_ITEM_CPU_SPEED)
    if not isinstance(speed_enum, str):
        return None
    return cpu_speed_mhz(speed_enum)


def get_turbo_enabled(client: Ultimate64Client) -> bool:
    """Return ``True`` when Turbo Control is not ``"Off"``."""
    inner = _unwrap(client.get_config_category(CAT_U64_SPECIFIC), CAT_U64_SPECIFIC)
    value = inner.get(_ITEM_TURBO_CONTROL)
    return isinstance(value, str) and value != "Off"


def set_turbo_mhz(client: Ultimate64Client, mhz: int | None) -> None:
    """Set (or disable) U64 CPU turbo.

    Passing ``None`` sets Turbo Control to ``"Off"`` and leaves the
    CPU Speed enum alone. Passing an integer enables turbo in
    ``"Manual"`` mode and sets the CPU Speed enum to the matching
    schema value; the integer is validated by :func:`cpu_speed_enum`
    so unsupported speeds raise :class:`ValueError` locally.

    :param client: Connected Ultimate64 client.
    :param mhz: CPU speed in MHz, or ``None`` to disable turbo.
    """
    if mhz is None:
        client.set_config_items(CAT_U64_SPECIFIC, {_ITEM_TURBO_CONTROL: "Off"})
        return
    if not isinstance(mhz, int) or isinstance(mhz, bool):
        raise ValueError(f"mhz must be int or None, got {type(mhz).__name__}")
    speed_enum = cpu_speed_enum(mhz)  # raises ValueError on bad speed
    client.set_config_items(
        CAT_U64_SPECIFIC,
        {
            _ITEM_CPU_SPEED: speed_enum,
            _ITEM_TURBO_CONTROL: "Manual",
        },
    )


# --------------------------------------------------------------------------- #
# REU                                                                         #
# --------------------------------------------------------------------------- #

def get_reu_config(client: Ultimate64Client) -> tuple[bool, str]:
    """Return ``(enabled, size_str)`` describing current REU state.

    :param client: Connected Ultimate64 client.
    :returns: Tuple of (enabled bool, REU Size enum string). Size is
        whatever the device currently reports, even when REU is disabled.
    """
    inner = _unwrap(client.get_config_category(CAT_CART), CAT_CART)
    enabled_raw = inner.get(_ITEM_REU_ENABLED, "Disabled")
    size_raw = inner.get(_ITEM_REU_SIZE, "")
    return (enabled_raw == "Enabled", str(size_raw))


def set_reu(
    client: Ultimate64Client,
    enabled: bool,
    size: str | int | None = None,
) -> None:
    """Enable or disable the REU and optionally set its size.

    When *enabled* is ``True``, this also switches the Cartridge preset
    to ``"REU"`` so the device actually exposes the expansion to the
    C64. When *enabled* is ``False``, the size argument is ignored and
    the Cartridge preset is left unchanged.

    :param client: Connected Ultimate64 client.
    :param enabled: ``True`` to enable the REU, ``False`` to disable.
    :param size: REU size as an enum string (``"16 MB"``), MB integer
        (``16``), or ``None`` to leave size unchanged. MB ints are
        converted to the corresponding byte count internally.
    """
    if not isinstance(enabled, bool):
        raise ValueError(f"enabled must be bool, got {type(enabled).__name__}")

    updates: dict[str, Any] = {}
    if enabled:
        updates[_ITEM_REU_ENABLED] = "Enabled"
        updates[_ITEM_CARTRIDGE] = "REU"
        if size is not None:
            if isinstance(size, int) and not isinstance(size, bool):
                # Caller passes MB as an int -- map to bytes first.
                updates[_ITEM_REU_SIZE] = reu_size_enum(size * 1024 * 1024)
            elif isinstance(size, str):
                updates[_ITEM_REU_SIZE] = reu_size_enum(size)
            else:
                raise ValueError(
                    f"size must be str, int or None, got {type(size).__name__}"
                )
    else:
        updates[_ITEM_REU_ENABLED] = "Disabled"
    client.set_config_items(CAT_CART, updates)


# --------------------------------------------------------------------------- #
# SID                                                                         #
# --------------------------------------------------------------------------- #

def get_sid_config(client: Ultimate64Client) -> dict:
    """Return a snapshot of the current SID configuration.

    :param client: Connected Ultimate64 client.
    :returns: Dict containing ``sockets`` (sockets-category items) and
        ``addressing`` (addressing-category items).
    """
    sockets = _unwrap(
        client.get_config_category(CAT_SID_SOCKETS), CAT_SID_SOCKETS
    )
    addressing = _unwrap(
        client.get_config_category(CAT_SID_ADDRESSING), CAT_SID_ADDRESSING
    )
    return {
        "sockets": dict(sockets),
        "addressing": dict(addressing),
    }


def set_sid_socket(
    client: Ultimate64Client,
    socket: int,
    sid_type: str,
    address: str,
) -> None:
    """Configure a SID socket's type and address.

    :param client: Connected Ultimate64 client.
    :param socket: Socket index (1 or 2).
    :param sid_type: One of :data:`SID_TYPE_VALUES` — e.g. ``"Enabled"``,
        ``"Disabled"``, ``"6581"``, ``"8580"``, ``"None"``.
    :param address: One of :data:`SID_ADDRESS_VALUES` — e.g. ``"$D400"``
        or ``"Unmapped"``.
    """
    if socket not in (1, 2):
        raise ValueError(f"socket must be 1 or 2, got {socket!r}")
    validate_enum(sid_type, SID_TYPE_VALUES, "SID type")
    validate_enum(address, SID_ADDRESS_VALUES, "SID address")
    client.set_config_items(
        CAT_SID_SOCKETS,
        {f"SID Socket {socket}": sid_type},
    )
    client.set_config_items(
        CAT_SID_ADDRESSING,
        {f"SID Socket {socket} Address": address},
    )


def get_sid_socket_types(client: Ultimate64Client) -> dict[int, str]:
    """Return which SID type is detected in each socket.

    Reads the ``SID Sockets Configuration`` category and extracts the
    SID type for each numbered socket item (e.g. ``"SID Socket 1"``).

    :param client: Connected Ultimate64 client.
    :returns: Dict mapping 1-based socket index to type string
        (e.g. ``{1: "8580", 2: "6581"}`` or ``{1: "None", 2: "8580"}``).
    """
    inner = _unwrap(
        client.get_config_category(CAT_SID_SOCKETS), CAT_SID_SOCKETS
    )
    result: dict[int, str] = {}
    for key, value in inner.items():
        # Match items like "SID Socket 1", "SID Socket 2"
        if key.startswith("SID Socket ") and key[-1].isdigit():
            idx = int(key.split()[-1])
            result[idx] = str(value)
    return result


def get_sid_addresses(client: Ultimate64Client) -> dict[int, str]:
    """Return the current address mapping for each SID socket.

    Reads the ``SID Addressing`` category and extracts the address for
    each numbered socket address item (e.g. ``"SID Socket 1 Address"``).

    :param client: Connected Ultimate64 client.
    :returns: Dict mapping 1-based socket index to address string
        (e.g. ``{1: "$D400", 2: "$D420"}``).
    """
    inner = _unwrap(
        client.get_config_category(CAT_SID_ADDRESSING), CAT_SID_ADDRESSING
    )
    result: dict[int, str] = {}
    for key, value in inner.items():
        # Match items like "SID Socket 1 Address", "SID Socket 2 Address"
        if key.startswith("SID Socket ") and key.endswith(" Address"):
            idx_str = key.removeprefix("SID Socket ").removesuffix(" Address")
            if idx_str.isdigit():
                result[int(idx_str)] = str(value)
    return result


def configure_multi_sid(
    client: Ultimate64Client,
    configs: list[SIDSocketConfig],
) -> None:
    """Configure multiple SID sockets at once.

    Takes a list of :class:`SIDSocketConfig` where index 0 corresponds
    to socket 1, index 1 to socket 2, etc.  All configs are validated
    before any writes are issued, so a bad value in any position raises
    :class:`ValueError` without touching the device.

    :param client: Connected Ultimate64 client.
    :param configs: List of :class:`SIDSocketConfig` (max 2 for current
        hardware). Index 0 = Socket 1, index 1 = Socket 2.
    :raises ValueError: If any config has invalid type/address values,
        or if the list is empty or too long.
    """
    if not isinstance(configs, list) or not configs:
        raise ValueError("configs must be a non-empty list of SIDSocketConfig")
    if len(configs) > 2:
        raise ValueError(
            f"at most 2 SID socket configs supported, got {len(configs)}"
        )
    # Validate all configs upfront (SIDSocketConfig.__post_init__ already
    # validates against schema enums, but re-check in case caller built
    # raw instances bypassing __post_init__).
    for i, cfg in enumerate(configs):
        if not isinstance(cfg, SIDSocketConfig):
            raise TypeError(
                f"configs[{i}] must be SIDSocketConfig, "
                f"got {type(cfg).__name__}"
            )
        validate_enum(cfg.sid_type, SID_TYPE_VALUES, "SID type")
        validate_enum(cfg.address, SID_ADDRESS_VALUES, "SID address")

    # Write all socket types, then all addresses.
    socket_updates: dict[str, str] = {}
    address_updates: dict[str, str] = {}
    for i, cfg in enumerate(configs):
        socket_num = i + 1
        socket_updates[f"SID Socket {socket_num}"] = cfg.sid_type
        address_updates[f"SID Socket {socket_num} Address"] = cfg.address

    client.set_config_items(CAT_SID_SOCKETS, socket_updates)
    client.set_config_items(CAT_SID_ADDRESSING, address_updates)


def get_physical_sid_sockets(client: Ultimate64Client) -> list[int]:
    """Return socket indices that have physical SID chips detected.

    A socket is considered to have a physical chip when its type is
    ``"6581"`` or ``"8580"`` (not ``"None"``, ``"Disabled"``, or
    ``"Enabled"``).

    :param client: Connected Ultimate64 client.
    :returns: Sorted list of 1-based socket indices with physical SIDs
        (e.g. ``[1, 2]`` or ``[2]`` or ``[]``).
    """
    types = get_sid_socket_types(client)
    physical_types = {"6581", "8580"}
    return sorted(idx for idx, typ in types.items() if typ in physical_types)


def get_ultisid_config(client: Ultimate64Client) -> dict:
    """Read the UltiSID FPGA core configuration.

    :param client: Connected Ultimate64 client.
    :returns: Raw dict of UltiSID configuration items as returned by
        the device.
    """
    return dict(
        _unwrap(client.get_config_category(CAT_ULTISID), CAT_ULTISID)
    )


def get_audio_mixer_config(client: Ultimate64Client) -> dict:
    """Read the Audio Mixer configuration.

    The mixer provides per-SID-channel volume and panning controls,
    needed for parallel capture of individual SID outputs.

    :param client: Connected Ultimate64 client.
    :returns: Raw dict of Audio Mixer configuration items as returned
        by the device.
    """
    return dict(
        _unwrap(client.get_config_category(CAT_AUDIO_MIXER), CAT_AUDIO_MIXER)
    )


def set_audio_mixer_item(
    client: Ultimate64Client,
    item: str,
    value: Any,
) -> None:
    """Set a single Audio Mixer configuration item.

    :param client: Connected Ultimate64 client.
    :param item: Item name within the Audio Mixer category (e.g.
        a volume or panning control name).
    :param value: New value for the item (string enum or numeric).
    :raises ValueError: If *item* is empty.
    """
    if not isinstance(item, str) or not item:
        raise ValueError("item must be a non-empty string")
    client.set_config_items(CAT_AUDIO_MIXER, {item: value})


# --------------------------------------------------------------------------- #
# Disk mount / unmount                                                        #
# --------------------------------------------------------------------------- #

def _detect_disk_type(path: str) -> str:
    """Infer a disk image type from its filename extension.

    :param path: Host filesystem path.
    :returns: One of :data:`DISK_IMAGE_TYPES`.
    :raises ValueError: On unknown extension.
    """
    _, ext = os.path.splitext(path)
    ext = ext.lower().lstrip(".")
    if ext not in DISK_IMAGE_TYPES:
        raise ValueError(
            f"Unknown disk image extension {ext!r} for {path!r}. "
            f"Supported: {list(DISK_IMAGE_TYPES)}"
        )
    return ext


def mount_disk_file(
    client: Ultimate64Client,
    drive: str,
    path: str,
    mode: str = "readwrite",
) -> None:
    """Mount a local disk image file on the given device drive.

    :param client: Connected Ultimate64 client.
    :param drive: Drive slot id — ``"a"`` or ``"b"`` (colon optional).
    :param path: Host filesystem path to a d64/d71/d81/g64 image.
    :param mode: Mount mode — one of :data:`MOUNT_MODES`.
    """
    validate_enum(mode, MOUNT_MODES, "mount mode")
    image_type = _detect_disk_type(path)
    with open(path, "rb") as f:
        image = f.read()
    client.mount_disk(drive=drive, image=image, image_type=image_type, mode=mode)


def unmount(client: Ultimate64Client, drive: str) -> None:
    """Unmount a drive.

    :param client: Connected Ultimate64 client.
    :param drive: Drive slot id — ``"a"`` or ``"b"``.
    """
    client.unmount_disk(drive)


# --------------------------------------------------------------------------- #
# PRG runners                                                                 #
# --------------------------------------------------------------------------- #

def run_prg_file(client: Ultimate64Client, path: str) -> None:
    """Read a PRG file from host disk and RUN it on the device.

    :param client: Connected Ultimate64 client.
    :param path: Host filesystem path to a .prg file.
    """
    with open(path, "rb") as f:
        data = f.read()
    client.run_prg(data)


def load_prg_file(client: Ultimate64Client, path: str) -> None:
    """Read a PRG file from host disk and LOAD it (no RUN) on the device.

    :param client: Connected Ultimate64 client.
    :param path: Host filesystem path to a .prg file.
    """
    with open(path, "rb") as f:
        data = f.read()
    client.load_prg(data)


# --------------------------------------------------------------------------- #
# Machine control (thin wrappers for discoverability)                         #
# --------------------------------------------------------------------------- #

def reset(client: Ultimate64Client) -> None:
    """Soft-reset the C64 (``PUT /v1/machine:reset``).

    Resets the 6510 CPU but does NOT reinitialize the FPGA or DMA
    controllers.  Use :func:`reboot` instead when switching turbo
    speeds with REU-heavy workloads — stale REU DMA state from a
    prior turbo speed can cause hangs after a soft reset.
    """
    client.reset()


def reboot(client: Ultimate64Client) -> None:
    """Full reboot of the Ultimate device (``PUT /v1/machine:reboot``).

    Reinitializes the entire FPGA including DMA controllers and REU.
    Required when switching turbo speeds between REU-heavy workloads
    (a soft :func:`reset` leaves stale DMA state).  Allow ~8 seconds
    for the device to become responsive after reboot.
    """
    client.reboot()


# --------------------------------------------------------------------------- #
# State snapshot / restore                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class U64StateSnapshot:
    """Snapshot of the U64 config fields mutated by helpers in this module.

    Holds exactly the raw string enum values needed to reconstruct the
    device state touched by :func:`set_turbo_mhz`, :func:`set_reu`,
    and :func:`set_sid_socket`. All strings preserve device-side
    formatting (e.g. the leading space in ``" 1"`` for CPU Speed).
    """

    turbo_control: str
    cpu_speed: str
    reu_enabled: str
    reu_size: str
    cartridge: str


def snapshot_state(client: Ultimate64Client) -> U64StateSnapshot:
    """Capture current turbo + REU + cartridge state for later restore.

    :param client: Connected Ultimate64 client.
    :returns: :class:`U64StateSnapshot` of the current raw values.
    """
    u64 = _unwrap(client.get_config_category(CAT_U64_SPECIFIC), CAT_U64_SPECIFIC)
    cart = _unwrap(client.get_config_category(CAT_CART), CAT_CART)
    return U64StateSnapshot(
        turbo_control=str(u64.get(_ITEM_TURBO_CONTROL, "")),
        cpu_speed=str(u64.get(_ITEM_CPU_SPEED, "")),
        reu_enabled=str(cart.get(_ITEM_REU_ENABLED, "")),
        reu_size=str(cart.get(_ITEM_REU_SIZE, "")),
        cartridge=str(cart.get(_ITEM_CARTRIDGE, "")),
    )


def restore_state(client: Ultimate64Client, snap: U64StateSnapshot) -> None:
    """Restore a previously captured :class:`U64StateSnapshot`.

    Writes the snapshotted values back into U64 Specific Settings and
    C64 and Cartridge Settings in a single batch per category.

    :param client: Connected Ultimate64 client.
    :param snap: Snapshot previously returned by :func:`snapshot_state`.
    """
    if not isinstance(snap, U64StateSnapshot):
        raise TypeError(
            f"snap must be U64StateSnapshot, got {type(snap).__name__}"
        )
    client.set_config_items(
        CAT_U64_SPECIFIC,
        {
            _ITEM_TURBO_CONTROL: snap.turbo_control,
            _ITEM_CPU_SPEED: snap.cpu_speed,
        },
    )
    cart_updates: dict[str, Any] = {_ITEM_REU_ENABLED: snap.reu_enabled}
    # Only restore non-empty enum values — the device reports "" for
    # unset preset fields, and writing "" back produces HTTP 400
    # ("Function none requires parameter value").
    if snap.reu_size:
        cart_updates[_ITEM_REU_SIZE] = snap.reu_size
    if snap.cartridge:
        cart_updates[_ITEM_CARTRIDGE] = snap.cartridge
    client.set_config_items(CAT_CART, cart_updates)
