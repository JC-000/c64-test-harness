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

import logging
import os
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from ..progress import ProgressEvent, ProgressEventKind, watch_progress as _watch_progress
from .ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64RunnerStuckError,
    Ultimate64UnreachableError,
)


class Ultimate64MeasurementEnvironmentError(Ultimate64Error):
    """Raised when the device is in a state that would silently corrupt cycle-accurate measurements
    (e.g., CPU turbo left enabled from a prior session). See GitHub issue #102."""
from .ultimate64_probe import is_u64_reachable
from .ultimate64_schema import (
    BADLINE_TIMING_VALUES,
    BUS_OPERATION_MODE_VALUES,
    BUS_SHARING_VALUES,
    CPU_SPEED_VALUES,
    DISK_IMAGE_TYPES,
    MOUNT_MODES,
    REU_ENABLED_VALUES,
    REU_SIZE_VALUES,
    SID_ADDRESS_VALUES,
    SID_SLOT_ADDRESS_ITEMS,
    SID_DETECTED_TYPE_VALUES,
    SID_SOCKET_ENABLE_VALUES,
    SIDSocketConfig,
    SidAddressConflict,
    SidSlot,
    _as_slot as _as_sid_slot,
    sid_address_occupancy,
    TURBO_CONTROL_VALUES,
    cpu_speed_enum,
    cpu_speed_mhz,
    reu_size_enum,
    validate_enum,
)

_log = logging.getLogger(__name__)

__all__ = [
    "get_turbo_mhz",
    "set_turbo_mhz",
    "get_turbo_enabled",
    "max_cpu_speed_mhz",
    "get_reu_config",
    "set_reu",
    "get_badline_timing",
    "set_badline_timing",
    "BusConfig",
    "get_bus_config",
    "set_bus_operation_mode",
    "BUS_SHARING_ITEMS",
    "get_sid_config",
    "set_sid_socket",
    "enable_sid_socket",
    "get_sid_socket_enabled",
    "get_detected_sid_types",
    "get_sid_address_map",
    "set_sid_address_map",
    "mount_disk_file",
    "unmount",
    "run_prg_file",
    "load_prg_file",
    "reset",
    "reboot",
    "recover",
    "runner_health_check",
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
    "get_data_streams_config",
    "set_stream_destination",
    "get_debug_stream_mode",
    "set_debug_stream_mode",
    "DEBUG_MODE_6510",
    "DEBUG_MODE_VIC",
    "DEBUG_MODE_6510_VIC",
    "DEBUG_MODE_1541",
    "DEBUG_MODE_6510_1541",
    "DEBUG_MODES",
    "Ultimate64MeasurementEnvironmentError",
    "check_measurement_environment",
    "ProgressEvent",
    "ProgressEventKind",
    "watch_progress",
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
_ITEM_BADLINE_TIMING = "Badline Timing"
_ITEM_BUS_OPERATION_MODE = "Bus Operation Mode"

#: The four ``Bus Sharing - *`` items, in device-report order. All four
#: share :data:`BUS_SHARING_VALUES`.
BUS_SHARING_ITEMS: tuple[str, ...] = (
    "Bus Sharing - ROMs",
    "Bus Sharing - I/O1",
    "Bus Sharing - I/O2",
    "Bus Sharing - Interrupts",
)


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


#: Per-client cache of conclusively-probed CPU Speed presets.  Keyed
#: weakly so a cached probe result never outlives (or pins) its client;
#: inconclusive probes are NOT cached, so a transient GET failure does
#: not permanently disable generation-aware validation.
_CPU_SPEED_PRESETS_CACHE: "weakref.WeakKeyDictionary[Ultimate64Client, tuple[str, ...]]" = (
    weakref.WeakKeyDictionary()
)


def _cpu_speed_presets(client: Ultimate64Client) -> tuple[str, ...] | None:
    """Probe (once, cached) the device's settable ``CPU Speed`` presets.

    The schema's :data:`CPU_SPEED_VALUES` is the cross-generation
    *superset*: the U64 Elite (firmware 3.14) has ``" 5"`` but no
    ``"64"``, the C64 Ultimate (firmware 1.1.0) has ``"64"`` but no
    ``" 5"``. The device's actual preset list is dug out of the
    ``get_config_item`` response with the same defensive parsing as
    :func:`_cartridge_preset_supported`: any structural surprise (or a
    probe that raises) yields the inconclusive ``None`` rather than a
    wrong answer. Conclusive results are cached per client (weakly), so
    the probe costs one GET per client lifetime.

    :param client: Connected Ultimate64 client.
    :returns: Tuple of preset enum strings, or ``None`` when the probe
        was inconclusive.
    """
    try:
        cached = _CPU_SPEED_PRESETS_CACHE.get(client)
    except TypeError:  # unhashable / non-weakrefable client stand-in
        cached = None
    if cached is not None:
        return cached
    try:
        resp = client.get_config_item(CAT_U64_SPECIFIC, _ITEM_CPU_SPEED)
    except (Ultimate64Error, AttributeError, TypeError):
        # AttributeError/TypeError: minimal client stand-ins (test doubles)
        # without get_config_item — inconclusive, same as a wire failure.
        return None
    if not isinstance(resp, dict):
        return None
    category = resp.get(CAT_U64_SPECIFIC)
    if not isinstance(category, dict):
        return None
    item = category.get(_ITEM_CPU_SPEED)
    if not isinstance(item, dict):
        return None
    # NB: enum/value-list items carry their choices under "values";
    # only preset-file items (e.g. Cartridge) use "presets". Verified
    # live against U64E fw 3.14 and C64U fw 1.1.0 on 2026-07-28.
    presets = item.get("values")
    if not isinstance(presets, list):
        return None
    if not presets or not all(isinstance(p, str) for p in presets):
        return None
    result = tuple(presets)
    try:
        _CPU_SPEED_PRESETS_CACHE[client] = result
    except TypeError:
        pass
    return result


def max_cpu_speed_mhz(client: Ultimate64Client) -> int:
    """Return this device's maximum turbo CPU speed in MHz.

    Probes the device's ``CPU Speed`` presets (cached; see
    :func:`_cpu_speed_presets`) and returns the largest one that parses
    as a known MHz step — ``64`` on a C64 Ultimate (firmware 1.1.0),
    ``48`` on a U64 Elite (firmware 3.14). Falls back to ``48`` (the
    U64E maximum, always firmware-accepted on both generations) when
    the probe is inconclusive.

    :param client: Connected Ultimate64 client.
    :returns: Maximum CPU speed in MHz (``48`` fallback).
    """
    presets = _cpu_speed_presets(client)
    if presets is None:
        return 48
    best: int | None = None
    for preset in presets:
        try:
            mhz = cpu_speed_mhz(preset)
        except ValueError:
            continue
        if best is None or mhz > best:
            best = mhz
    return best if best is not None else 48


def set_turbo_mhz(client: Ultimate64Client, mhz: int | None) -> None:
    """Set (or disable) U64 CPU turbo.

    Passing ``None`` sets Turbo Control to ``"Off"`` and leaves the
    CPU Speed enum alone. Passing an integer enables turbo in
    ``"Manual"`` mode and sets the CPU Speed enum to the matching
    schema value; the integer is validated by :func:`cpu_speed_enum`
    so unsupported speeds raise :class:`ValueError` locally.

    The schema enum is the cross-generation superset (the U64 Elite
    lacks ``64``, the C64 Ultimate lacks ``5``), so a superset-valid
    speed can still be generation-foreign. The device's actual preset
    list is probed once (cached; see :func:`_cpu_speed_presets`) and,
    when the probe is conclusive, a generation-foreign speed raises
    :class:`ValueError` locally instead of going on the wire and
    surfacing as an HTTP 400 :class:`Ultimate64Error`. An inconclusive
    probe preserves the legacy behaviour (the firmware rejects a
    foreign speed with HTTP 400 before turbo is enabled).

    :param client: Connected Ultimate64 client.
    :param mhz: CPU speed in MHz, or ``None`` to disable turbo.
    """
    if mhz is None:
        client.set_config_items(CAT_U64_SPECIFIC, {_ITEM_TURBO_CONTROL: "Off"})
        return
    if not isinstance(mhz, int) or isinstance(mhz, bool):
        raise ValueError(f"mhz must be int or None, got {type(mhz).__name__}")
    speed_enum = cpu_speed_enum(mhz)  # raises ValueError on bad speed
    presets = _cpu_speed_presets(client)
    if presets is not None and speed_enum not in presets:
        supported = sorted(
            cpu_speed_mhz(p) for p in presets if p in CPU_SPEED_VALUES
        )
        raise ValueError(
            f"speed {mhz} not supported by this device generation; "
            f"supported: {supported}"
        )
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

    Plain, uncached read of the config store. Live-verified on U64E fw
    3.15 (issue #168): ``REU Size`` is stable across quiet reads, reflects
    a :func:`set_reu` write immediately (no reset needed), and is not moved
    by the ``Cartridge`` item — so a size that differs between two reads
    means something wrote the config in between (another lane's
    ``set_reu``/``restore_state``, ``load_config_from_flash``,
    ``reset_config_to_default`` — the item's default is ``"2 MB"``). Read
    it when you need it; don't hold it across other config writes. See
    ``tests/test_reu_size_readback_live.py``.

    ``enabled`` is ``True`` only for ``"Enabled"``; the U64E's third
    ``RAM Expansion Unit`` value, ``"GeoRAM Mode"``, reads as ``False``.

    :param client: Connected Ultimate64 client.
    :returns: Tuple of (enabled bool, REU Size enum string). Size is
        whatever the device currently reports, even when REU is disabled.
    """
    inner = _unwrap(client.get_config_category(CAT_CART), CAT_CART)
    enabled_raw = inner.get(_ITEM_REU_ENABLED, "Disabled")
    size_raw = inner.get(_ITEM_REU_SIZE, "")
    return (enabled_raw == "Enabled", str(size_raw))


def _cartridge_preset_supported(
    client: Ultimate64Client, value: str
) -> bool | None:
    """Report whether *value* is a settable ``Cartridge`` preset on this device.

    Cross-generation quirk: on the U64 Elite (firmware 3.14) the
    ``Cartridge`` item exposes ``"REU"`` as a real preset, and writing it
    is what exposes the REU to the C64. On the C64 Ultimate (firmware
    1.1.0) the same item reports ``presets: [""]`` — ``"REU"`` is only a
    *mirrored* display value, and PUTting it back is rejected with HTTP
    400 ("not a valid choice"). This probe distinguishes the two.

    Live-verified response shape::

        {"C64 and Cartridge Settings":
            {"Cartridge": {"current": "REU", "presets": [""], "default": ""}},
         "errors": []}

    Parsed defensively: the presets list is dug out of the nested
    category/item maps, and any structural surprise (or a probe that
    raises) yields the inconclusive ``None`` rather than a wrong answer.

    :param client: Connected Ultimate64 client.
    :param value: Candidate ``Cartridge`` value to test for settability.
    :returns: ``True`` if *value* is among the parsed presets, ``False``
        if presets parsed cleanly and *value* is absent, ``None`` if the
        probe failed or the response could not be parsed.
    """
    try:
        resp = client.get_config_item(CAT_CART, _ITEM_CARTRIDGE)
    except Ultimate64Error:
        return None
    if not isinstance(resp, dict):
        return None
    category = resp.get(CAT_CART)
    if not isinstance(category, dict):
        return None
    item = category.get(_ITEM_CARTRIDGE)
    if not isinstance(item, dict):
        return None
    presets = item.get("presets")
    if not isinstance(presets, list):
        return None
    return value in presets


def set_reu(
    client: Ultimate64Client,
    enabled: bool,
    size: str | int | None = None,
) -> None:
    """Enable or disable the REU and optionally set its size.

    When *enabled* is ``True`` this enables the ``RAM Expansion Unit``
    item and optionally sets ``REU Size``. Whether it *also* writes the
    ``Cartridge`` preset depends on the device generation:

    - **U64 Elite (firmware 3.14):** the ``Cartridge`` item exposes
      ``"REU"`` as a real preset, and writing it is REQUIRED — that is
      what actually exposes the expansion to the C64.
    - **C64 Ultimate (firmware 1.1.0):** ``Cartridge`` reports
      ``presets: [""]``; ``"REU"`` is a mirrored display value only and
      PUTting it back is rejected with HTTP 400. Enabling the
      ``RAM Expansion Unit`` item alone is the verified working method.

    To pick the right behavior this probes the ``Cartridge`` presets once
    (via :func:`_cartridge_preset_supported`). When the probe says the
    preset is unsupported (the C64U case) the ``Cartridge`` write is
    omitted; when it is supported *or* inconclusive (``None`` — e.g. the
    probe GET failed) the ``Cartridge`` write is included so legacy U64E
    behavior is preserved.

    Ordering matters: when included, ``Cartridge`` is inserted into the
    updates dict FIRST — before ``RAM Expansion Unit`` and ``REU Size``.
    :meth:`Ultimate64Client.set_config_items` iterates in insertion order
    and does not catch per-item failures, so a firmware rejection of the
    ``Cartridge`` write aborts the batch before the REU is half-enabled.

    When *enabled* is ``False`` the size argument is ignored, no probe is
    issued, and only a single ``RAM Expansion Unit: "Disabled"`` write is
    sent.

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
        # Probe once; include the Cartridge write (ordered first) unless
        # the device positively reports "REU" as an unsupported preset.
        if _cartridge_preset_supported(client, "REU") is not False:
            updates[_ITEM_CARTRIDGE] = "REU"
        updates[_ITEM_REU_ENABLED] = "Enabled"
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


def _validate_socket_state(value: str) -> str:
    """Validate a ``SID Socket N`` enable value, naming the usual mistake.

    A chip type gets a message that says where the type actually lives,
    because the fabricated ``SID_TYPE_VALUES`` union used to accept one
    here and the resulting HTTP 400 named neither item usefully.
    """
    if isinstance(value, str) and value in SID_DETECTED_TYPE_VALUES:
        raise ValueError(
            f"SID chip type {value!r} is not settable. 'SID Socket N' is an "
            f"enable toggle taking {list(SID_SOCKET_ENABLE_VALUES)}; the chip "
            f"type is reported by 'SID Detected Socket N' (see "
            f"get_detected_sid_types) and is filled by the device's boot-time "
            f"probe, not chosen."
        )
    return validate_enum(value, SID_SOCKET_ENABLE_VALUES, "SID socket state")


def set_sid_socket(
    client: Ultimate64Client,
    socket: int,
    sid_type: str,
    address: str,
) -> None:
    """Enable or disable a SID socket and set its base address.

    *sid_type* keeps its name for compatibility but means the socket's
    **enable state**: ``"Enabled"`` or ``"Disabled"``. That is the only
    domain ``SID Socket N`` has (``en_dis``, u64_config.cc:393-394).

    A chip type such as ``"8580"`` raises :class:`ValueError` here
    rather than going on the wire. It used to be sent and answered with
    HTTP 400 "Value '8580' is not a valid choice for item SID Socket 1"
    (route_configs.cc:85-88, verified live 2026-08-30). There is no
    other item it could go to: the detected type is filled by the
    boot-time probe and is not a selector.

    :param client: Connected Ultimate64 client.
    :param socket: Socket index (1 or 2).
    :param sid_type: ``"Enabled"`` or ``"Disabled"``. See
        :func:`enable_sid_socket` for a boolean-typed alternative.
    :param address: One of :data:`SID_ADDRESS_VALUES` — e.g. ``"$D400"``
        or ``"Unmapped"``.
    :raises ValueError: On a bad socket index, a *sid_type* outside
        :data:`SID_SOCKET_ENABLE_VALUES`, or a bad address.
    """
    if socket not in (1, 2):
        raise ValueError(f"socket must be 1 or 2, got {socket!r}")
    _validate_socket_state(sid_type)
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

    An alias for :func:`get_detected_sid_types`, kept because callers
    use this name. It previously read ``SID Socket N`` -- the *enable*
    toggle -- and returned ``{1: "Enabled"}`` labelled as a chip type.

    .. warning::

       The detected type is advisory. REST can write the item even
       though the device's own menu marks it read-only, so this reports
       the last value *written*, which is the detection result only if
       nothing has overwritten it since boot. See
       :func:`get_detected_sid_types` for the full caveat.

    :param client: Connected Ultimate64 client.
    :returns: Dict mapping 1-based socket index to type string
        (e.g. ``{1: "8580", 2: "None"}``).
    """
    return get_detected_sid_types(client)


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
        _validate_socket_state(cfg.sid_type)
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
    """Return socket indices that have a physical SID chip detected.

    A socket counts as populated when its detected type is anything
    other than ``"None"``. That deliberately includes replacement chips
    -- ARMSID, FPGASID, SIDKick, SwinSID and the rest of
    :data:`SID_DETECTED_TYPE_VALUES` -- because those are physically in
    the socket too; restricting to ``"6581"``/``"8580"`` would report an
    empty socket on every device fitted with one.

    This function previously read the socket *enable* item, whose values
    are ``"Enabled"``/``"Disabled"``, and filtered them for chip types.
    Nothing ever matched, so it returned ``[]`` on every device.

    .. warning::

       Built on :func:`get_detected_sid_types`, so it inherits that
       item's advisory nature: REST can overwrite the detected type, and
       this function believes what it reads. A socket reported populated
       has not necessarily been probed since the last write.

    :param client: Connected Ultimate64 client.
    :returns: Sorted list of 1-based socket indices with a chip fitted
        (e.g. ``[1, 2]`` or ``[2]`` or ``[]``).
    """
    types = get_detected_sid_types(client)
    return sorted(idx for idx, typ in types.items() if typ != "None")


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
# Recovery / health                                                           #
# --------------------------------------------------------------------------- #

# Minimal viable PRG: load address $0801 (BASIC start) + RTS.
_HEALTH_CHECK_PRG = bytes([0x01, 0x08, 0x60])

# Firmware signature for a wedged runner subsystem.
_STUCK_RUNNER_SIGNATURE = "Cannot open file"


def recover(
    client: Ultimate64Client,
    *,
    reset_settle_seconds: float = 2.0,
    reboot_settle_seconds: float = 12.0,
    escalate_to_reboot: bool = True,
) -> str:
    """Bring an unresponsive U64 back to a known-good state.

    Strategy: :meth:`Ultimate64Client.reset` (instant; recovers most
    CPU-stuck states) then probe for liveness; if still unreachable AND
    *escalate_to_reboot* is ``True``, :meth:`Ultimate64Client.reboot`
    (full FPGA reinit ~8s; recovers REU/DMA stuck state) then probe
    again.

    NEVER calls :meth:`Ultimate64Client.poweroff` -- that's irrecoverable
    over the network and requires physical access to power-cycle. If
    both reset and reboot fail to bring the device back, raises
    :class:`Ultimate64UnreachableError`; at that point human
    intervention is required.

    :param client: Connected Ultimate64 client.
    :param reset_settle_seconds: Sleep after ``reset()`` before probing.
    :param reboot_settle_seconds: Sleep after ``reboot()`` before probing.
    :param escalate_to_reboot: When ``False``, skip the reboot fallback;
        if reset alone fails to recover, raise immediately.
    :returns: ``"reset"`` or ``"reboot"`` -- whichever step ultimately
        restored reachability.
    :raises Ultimate64UnreachableError: When recovery fails.
    """
    _log.info("recover: issuing reset() on %s", client.host)
    try:
        client.reset()
    except Ultimate64Error as exc:
        _log.warning("recover: reset() raised %s -- continuing", exc)
    time.sleep(reset_settle_seconds)
    if is_u64_reachable(client.host, port=client.port, password=client.password):
        _log.info("recover: device reachable after reset")
        return "reset"

    if not escalate_to_reboot:
        raise Ultimate64UnreachableError(
            f"U64 at {client.host} unreachable after reset(); "
            f"escalate_to_reboot=False so not retrying with reboot()"
        )

    _log.info("recover: reset insufficient -- issuing reboot() on %s", client.host)
    try:
        client.reboot()
    except Ultimate64Error as exc:
        _log.warning("recover: reboot() raised %s -- continuing", exc)
    time.sleep(reboot_settle_seconds)
    if is_u64_reachable(client.host, port=client.port, password=client.password):
        _log.info("recover: device reachable after reboot")
        return "reboot"

    raise Ultimate64UnreachableError(
        f"U64 at {client.host} unreachable after reset() and reboot(); "
        f"physical power-cycle required (do NOT call poweroff() -- it is "
        f"irrecoverable over the network)"
    )


def runner_health_check(client: Ultimate64Client) -> None:
    """Verify the U64 firmware's runner subsystem accepts new programs.

    Posts a tiny no-op PRG (load address $0801 + RTS) via
    :meth:`Ultimate64Client.run_prg` and inspects the response. Returns
    silently when the runner accepts the program. Raises
    :class:`Ultimate64RunnerStuckError` when the device returns the
    firmware's wedged-runner signature (``"Cannot open file"``);
    :func:`recover` can usually clear that state.

    Other failures (auth, timeout, generic ``Ultimate64Error``) are
    re-raised unchanged -- this helper only specialises the
    stuck-runner case.

    :param client: Connected Ultimate64 client.
    :raises Ultimate64RunnerStuckError: When the runner is wedged.
    :raises Ultimate64Error: On other API failures (auth, network, etc.).
    """
    try:
        client.run_prg(_HEALTH_CHECK_PRG)
    except Ultimate64Error as exc:
        body = exc.body or ""
        if _STUCK_RUNNER_SIGNATURE in body or _STUCK_RUNNER_SIGNATURE in str(exc):
            raise Ultimate64RunnerStuckError(
                f"U64 runner is wedged at {client.host}: {body[:200]!r}",
                status=exc.status,
                body=body,
            ) from exc
        raise


def check_measurement_environment(client: Ultimate64Client) -> None:
    """Validate that the device is in a state suitable for cycle-accurate (CIA-timer) measurements.

    Checks performed:
      - CPU turbo is disabled (effective speed = 1 MHz). A non-1 MHz speed causes
        CIA-timer-based measurements to read as ``target_cycles / turbo_factor``
        with no exception, because the CIA continues counting at its fixed rate
        while the CPU runs N× faster.
      - VIC-II badline DMA is enabled. Badlines cost the 6510 ~20-25% of its
        cycles at 1 MHz, so a device left with them disabled by a prior run
        reports uniformly optimistic figures. This is the same hazard shape as
        turbo: runtime-only state on a queue-shared device, persisting until
        power cycle, with no symptom that looks like a misconfiguration.

    Raises Ultimate64MeasurementEnvironmentError on a state that would produce
    silently-wrong measurements. Returns None on a clean environment.

    The badline check is skipped (not failed) when the device does not expose
    ``Badline Timing`` -- the item is live-verified on U64E firmware 3.14d but
    unverified on the C64 Ultimate, and an unreadable item is not evidence of a
    dirty environment.

    See GitHub issues #102 and #150 for the failure-mode walkthroughs.

    :param client: Connected Ultimate64 client.
    :raises Ultimate64MeasurementEnvironmentError: When turbo is active at a
        non-1 MHz speed, or badline DMA is disabled.
    """
    mhz = get_turbo_mhz(client)
    if mhz is not None and mhz != 1:
        raise Ultimate64MeasurementEnvironmentError(
            f"CPU turbo is enabled at {mhz} MHz; CIA-timer measurements will read as "
            f"target_cycles/{mhz}. Call set_turbo_mhz(client, 1) before benchmarking. "
            f"See GitHub issue #102."
        )
    try:
        badline_raw = _read_badline_raw(client)
    except Ultimate64Error:
        badline_raw = None
    if badline_raw == "Disabled":
        raise Ultimate64MeasurementEnvironmentError(
            "VIC-II badline DMA is disabled; the 6510 gets ~20-25% more cycles "
            "than a stock C64, so measurements will read uniformly fast. Call "
            "set_badline_timing(client, True) before benchmarking. "
            "See GitHub issue #150."
        )


# --------------------------------------------------------------------------- #
# Badline timing                                                              #
# --------------------------------------------------------------------------- #

def _read_badline_raw(client: Ultimate64Client) -> str | None:
    """Return the raw ``Badline Timing`` enum string, or ``None`` if absent.

    ``None`` means the item was not present in the category dump -- the
    expected outcome on a device generation that does not expose it, or
    spells it differently. Callers decide whether that is fatal.
    """
    inner = _unwrap(
        client.get_config_category(CAT_U64_SPECIFIC), CAT_U64_SPECIFIC
    )
    raw = inner.get(_ITEM_BADLINE_TIMING)
    return None if raw is None else str(raw)


def get_badline_timing(client: Ultimate64Client) -> bool:
    """Return ``True`` when VIC-II badline DMA is enabled (authentic C64 behaviour).

    Badlines cost the 6510 roughly 20-25% of its cycles at 1 MHz, so this
    is a timing-relevant variable for any benchmark. Disabling it is the
    clean way to isolate badline cost while holding the PRG byte-identical
    (as opposed to ``$D011`` blanking inside the program, which changes the
    shipped image and hides on-screen progress markers).

    :param client: Connected Ultimate64 client.
    :returns: ``True`` if ``Badline Timing`` is ``"Enabled"``.
    :raises Ultimate64Error: If the device does not expose the item.
    """
    raw = _read_badline_raw(client)
    if raw is None:
        raise Ultimate64Error(
            f"{_ITEM_BADLINE_TIMING!r} is not exposed under {CAT_U64_SPECIFIC!r} "
            f"on {client.host}. Verified present on U64E firmware 3.14d; other "
            f"generations may spell it differently."
        )
    return raw == "Enabled"


def set_badline_timing(client: Ultimate64Client, enabled: bool) -> None:
    """Enable or disable VIC-II badline DMA.

    .. warning::
       This is **runtime-only state that persists until power cycle**, on a
       queue-shared device. A run that disables badlines and dies before
       restoring leaves every subsequent run on that device quietly ~20-25%
       fast, with no symptom that looks like a misconfiguration. Capture and
       restore it with :func:`snapshot_state` / :func:`restore_state`, which
       both cover this field; :func:`check_measurement_environment` also
       fails closed on a device left with badlines disabled.

    Cross-generation caveat: live-verified on the U64 Elite (firmware
    3.14d), where the item accepts ``"Enabled"`` / ``"Disabled"``. The C64
    Ultimate is *assumed* to spell the category and item identically, but
    that is unverified -- the two generations already diverge on the CPU
    Speed enum and on cartridge presets, so same-name is an assumption.
    On a device that does not expose the item this raises rather than
    silently no-opping.

    :param client: Connected Ultimate64 client.
    :param enabled: ``True`` for authentic badline DMA, ``False`` to
        suppress it (giving the 6510 ~20-25% more cycles at 1 MHz).
    :raises ValueError: If *enabled* is not a bool.
    :raises Ultimate64Error: If the device does not expose the item.
    """
    if not isinstance(enabled, bool):
        raise ValueError(f"enabled must be bool, got {type(enabled).__name__}")
    if _read_badline_raw(client) is None:
        raise Ultimate64Error(
            f"{_ITEM_BADLINE_TIMING!r} is not exposed under {CAT_U64_SPECIFIC!r} "
            f"on {client.host}; refusing to write an item the device does not report."
        )
    value = validate_enum(
        "Enabled" if enabled else "Disabled",
        BADLINE_TIMING_VALUES,
        _ITEM_BADLINE_TIMING,
    )
    client.set_config_items(CAT_U64_SPECIFIC, {_ITEM_BADLINE_TIMING: value})


# --------------------------------------------------------------------------- #
# Cartridge-port bus behaviour                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BusConfig:
    """Cartridge-port bus settings that can influence expansion-bus timing.

    :param operation_mode: ``Bus Operation Mode`` enum value, one of
        :data:`BUS_OPERATION_MODE_VALUES`.
    :param sharing: Mapping of each :data:`BUS_SHARING_ITEMS` name to its
        current value. Items the device did not report are omitted.
    """

    operation_mode: str
    sharing: Mapping[str, str]


def get_bus_config(client: Ultimate64Client) -> BusConfig:
    """Read the cartridge-port bus settings in one category fetch.

    Intended for recording alongside a benchmark result: an REU-DMA-bound
    workload's headline number may depend on these values, and a run whose
    artifact does not carry them is not reproducible in the way the numbers
    imply.

    :param client: Connected Ultimate64 client.
    :returns: A :class:`BusConfig`. ``operation_mode`` is ``""`` when the
        device did not report the item.
    """
    inner = _unwrap(client.get_config_category(CAT_CART), CAT_CART)
    return BusConfig(
        operation_mode=str(inner.get(_ITEM_BUS_OPERATION_MODE, "")),
        sharing={
            item: str(inner[item])
            for item in BUS_SHARING_ITEMS
            if item in inner
        },
    )


def set_bus_operation_mode(client: Ultimate64Client, mode: str) -> None:
    """Set ``Bus Operation Mode``, validating *mode* before touching the network.

    .. warning::
       Runtime-only state that reverts on power cycle, the same caveat the
       REU helpers carry. On a queue-shared device, restore it after a run
       -- :func:`snapshot_state` / :func:`restore_state` cover this field.

    :param client: Connected Ultimate64 client.
    :param mode: One of :data:`BUS_OPERATION_MODE_VALUES` (device default
        is ``"Quiet"``).
    :raises ValueError: If *mode* is not a known enum value.
    """
    value = validate_enum(
        mode, BUS_OPERATION_MODE_VALUES, _ITEM_BUS_OPERATION_MODE
    )
    client.set_config_items(CAT_CART, {_ITEM_BUS_OPERATION_MODE: value})


# --------------------------------------------------------------------------- #
# State snapshot / restore                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class U64StateSnapshot:
    """Snapshot of the U64 config fields mutated by helpers in this module.

    Holds exactly the raw string enum values needed to reconstruct the
    device state touched by :func:`set_turbo_mhz`, :func:`set_reu`,
    :func:`set_sid_socket`, :func:`set_badline_timing`, and
    :func:`set_bus_operation_mode`. All strings preserve device-side
    formatting (e.g. the leading space in ``" 1"`` for CPU Speed).

    ``badline_timing`` and ``bus_operation_mode`` default to ``""`` so that
    snapshots constructed positionally by existing callers keep working; an
    empty value is skipped at restore time, exactly like ``reu_size``.
    """

    turbo_control: str
    cpu_speed: str
    reu_enabled: str
    reu_size: str
    cartridge: str
    badline_timing: str = ""
    bus_operation_mode: str = ""


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
        badline_timing=str(u64.get(_ITEM_BADLINE_TIMING, "")),
        bus_operation_mode=str(cart.get(_ITEM_BUS_OPERATION_MODE, "")),
    )


def restore_state(client: Ultimate64Client, snap: U64StateSnapshot) -> None:
    """Restore a previously captured :class:`U64StateSnapshot`.

    Writes the snapshotted values back into U64 Specific Settings and
    C64 and Cartridge Settings in a single batch per category.

    Cross-generation caveat for the ``Cartridge`` field: on a C64 Ultimate
    (firmware 1.1.0) :func:`snapshot_state` captures the *mirrored*
    ``Cartridge: "REU"`` value, but that value is not a settable preset on
    that firmware (writing it back is rejected with HTTP 400). Before
    restoring a non-empty cartridge value this checks
    :func:`_cartridge_preset_supported` and skips the write when the value
    is positively reported as unsupported; ``True`` or an inconclusive
    ``None`` (legacy U64E behavior) still writes it.

    :param client: Connected Ultimate64 client.
    :param snap: Snapshot previously returned by :func:`snapshot_state`.
    """
    if not isinstance(snap, U64StateSnapshot):
        raise TypeError(
            f"snap must be U64StateSnapshot, got {type(snap).__name__}"
        )
    u64_updates: dict[str, Any] = {
        _ITEM_TURBO_CONTROL: snap.turbo_control,
        _ITEM_CPU_SPEED: snap.cpu_speed,
    }
    # Skipped when empty: a snapshot taken before this field existed, or
    # from a device generation that does not expose the item. Writing ""
    # back produces HTTP 400, same as the reu_size case below.
    if snap.badline_timing:
        u64_updates[_ITEM_BADLINE_TIMING] = snap.badline_timing
    client.set_config_items(CAT_U64_SPECIFIC, u64_updates)
    cart_updates: dict[str, Any] = {}
    # Cartridge FIRST — the same ordering invariant :func:`set_reu`
    # documents: :meth:`Ultimate64Client.set_config_items` iterates in
    # insertion order and does not catch per-item failures, so a
    # firmware rejection of the Cartridge write aborts the batch before
    # the REU is half-enabled.
    #
    # A snapshotted cartridge value may be a firmware-mirrored display
    # value (C64U reports "REU" but rejects it as a PUT). Skip the write
    # only when the device positively reports it as unsupported.
    if snap.cartridge and _cartridge_preset_supported(
        client, snap.cartridge
    ) is not False:
        cart_updates[_ITEM_CARTRIDGE] = snap.cartridge
    cart_updates[_ITEM_REU_ENABLED] = snap.reu_enabled
    # Only restore non-empty enum values — the device reports "" for
    # unset preset fields, and writing "" back produces HTTP 400
    # ("Function none requires parameter value").
    if snap.reu_size:
        cart_updates[_ITEM_REU_SIZE] = snap.reu_size
    if snap.bus_operation_mode:
        cart_updates[_ITEM_BUS_OPERATION_MODE] = snap.bus_operation_mode
    client.set_config_items(CAT_CART, cart_updates)


# --------------------------------------------------------------------------- #
# Data Streams                                                                #
# --------------------------------------------------------------------------- #

_ITEM_STREAM_VIC = "Stream VIC to"
_ITEM_STREAM_AUDIO = "Stream Audio to"
_ITEM_STREAM_DEBUG = "Stream Debug to"
_ITEM_DEBUG_MODE = "Debug Stream Mode"

DEBUG_MODE_6510 = "6510 Only"
DEBUG_MODE_VIC = "VIC Only"
DEBUG_MODE_6510_VIC = "6510 & VIC"
DEBUG_MODE_1541 = "1541 Only"
DEBUG_MODE_6510_1541 = "6510 & 1541"

DEBUG_MODES = (
    DEBUG_MODE_6510,
    DEBUG_MODE_VIC,
    DEBUG_MODE_6510_VIC,
    DEBUG_MODE_1541,
    DEBUG_MODE_6510_1541,
)

_STREAM_TYPE_MAP = {
    "video": _ITEM_STREAM_VIC,
    "audio": _ITEM_STREAM_AUDIO,
    "debug": _ITEM_STREAM_DEBUG,
}


def get_data_streams_config(client: Ultimate64Client) -> dict[str, str]:
    """Return all items from the Data Streams configuration category.

    :param client: Connected Ultimate64 client.
    :returns: Dict of item names to their current values.
    """
    return dict(
        _unwrap(client.get_config_category(CAT_DATA_STREAMS), CAT_DATA_STREAMS)
    )


def set_stream_destination(
    client: Ultimate64Client,
    stream_type: str,
    destination: str,
) -> None:
    """Set the default destination for a stream type.

    :param client: Connected Ultimate64 client.
    :param stream_type: One of ``"video"``, ``"audio"``, ``"debug"``.
    :param destination: Multicast or unicast address string
        (e.g. ``"239.0.1.64:11000"``).
    :raises ValueError: If *stream_type* is not recognised.
    """
    item = _STREAM_TYPE_MAP.get(stream_type)
    if item is None:
        raise ValueError(
            f"Unknown stream_type {stream_type!r}; "
            f"expected one of {list(_STREAM_TYPE_MAP)}"
        )
    client.set_config_items(CAT_DATA_STREAMS, {item: destination})


def get_debug_stream_mode(client: Ultimate64Client) -> str:
    """Return the current Debug Stream Mode setting.

    :param client: Connected Ultimate64 client.
    :returns: One of :data:`DEBUG_MODES`.
    """
    inner = _unwrap(
        client.get_config_category(CAT_DATA_STREAMS), CAT_DATA_STREAMS
    )
    return str(inner.get(_ITEM_DEBUG_MODE, ""))


def set_debug_stream_mode(client: Ultimate64Client, mode: str) -> None:
    """Set the Debug Stream Mode.

    :param client: Connected Ultimate64 client.
    :param mode: One of :data:`DEBUG_MODES`.
    :raises ValueError: If *mode* is not a valid debug stream mode.
    """
    if mode not in DEBUG_MODES:
        raise ValueError(
            f"Unknown debug stream mode {mode!r}; expected one of {list(DEBUG_MODES)}"
        )
    client.set_config_items(CAT_DATA_STREAMS, {_ITEM_DEBUG_MODE: mode})


# --------------------------------------------------------------------------- #
# Live memory watcher (pexpect-for-DMA) -- backwards-compat shim               #
# --------------------------------------------------------------------------- #
#
# The canonical implementation now lives in
# :mod:`c64_test_harness.progress` (backend-agnostic, takes a
# ``C64Transport``).  This shim adapts an :class:`Ultimate64Client` to
# the transport protocol's ``read_memory`` so existing callers using
# ``from c64_test_harness.backends.ultimate64_helpers import
# watch_progress`` keep working without source changes.
#
# ``ProgressEvent`` and ``ProgressEventKind`` are re-exported above
# (in the module imports) for the same reason.


class _ClientReadMemoryAdapter:
    """Adapt :class:`Ultimate64Client` to the ``read_memory`` shape that
    :func:`c64_test_harness.progress.watch_progress` consumes.

    Only the single method the watcher needs is implemented; this is not
    a full :class:`C64Transport`. Keeping the adapter tiny means the
    shim has no behavioural surface area to maintain.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Ultimate64Client) -> None:
        self._client = client

    def read_memory(self, addr: int, length: int) -> bytes:
        return self._client.read_mem(addr, length)


def watch_progress(
    client: Ultimate64Client,
    addresses: Mapping[str, tuple[int, int]],
    *,
    poll_interval: float = 10.0,
    idle_timeout: float = 120.0,
    overall_timeout: float = 5400.0,
    stop_when: Callable[[Mapping[str, bytes]], bool] | None = None,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ProgressEvent]:
    """Backwards-compatible shim around :func:`c64_test_harness.progress.watch_progress`.

    See the canonical implementation for the full contract. This shim
    accepts an :class:`Ultimate64Client` directly (as the original
    helper did) and adapts it to the ``read_memory`` shape the canonical
    function expects. New code should import ``watch_progress`` from
    :mod:`c64_test_harness` (package root) and pass a transport.

    :param client: Connected Ultimate64 client used for DMA reads.
    :param addresses: See canonical implementation.
    :param poll_interval: See canonical implementation.
    :param idle_timeout: See canonical implementation.
    :param overall_timeout: See canonical implementation.
    :param stop_when: See canonical implementation. ``None`` (the
        default) means "never stop"; passed through to the canonical
        function only when non-None so the shared default sentinel is
        preserved.
    :param _clock: Test injection point.
    :param _sleep: Test injection point.
    :returns: Generator of :class:`ProgressEvent`.
    """
    adapter = _ClientReadMemoryAdapter(client)
    kwargs: dict[str, Any] = {
        "poll_interval": poll_interval,
        "idle_timeout": idle_timeout,
        "overall_timeout": overall_timeout,
        "_clock": _clock,
        "_sleep": _sleep,
    }
    if stop_when is not None:
        kwargs["stop_when"] = stop_when
    return _watch_progress(adapter, addresses, **kwargs)


# --------------------------------------------------------------------------- #
# SID selection & address allocation                                          #
# --------------------------------------------------------------------------- #
_ITEM_SID_PROBE = SID_SLOT_ADDRESS_ITEMS[SidSlot.SOCKET1]

#: Per-client cache of the device's declared SID address choices.
#: Weakly keyed, and inconclusive probes are not cached, matching
#: :data:`_CPU_SPEED_PRESETS_CACHE`.
_SID_ADDRESS_CHOICES_CACHE: "weakref.WeakKeyDictionary[Ultimate64Client, tuple[str, ...]]" = (
    weakref.WeakKeyDictionary()
)


def _sid_address_choices(client: Ultimate64Client) -> tuple[str, ...] | None:
    """Probe (once, cached) the addresses this device offers a SID slot.

    All four slots bind the same firmware enum (``u64_sid_base``,
    u64_config.cc:404-408), so one item is probed and the answer is
    used for every slot. Enum items carry their choices under
    ``"values"``; only preset-file items use ``"presets"``
    (route_configs.cc:31-45).

    Any structural surprise, or a probe that raises, yields the
    inconclusive ``None`` rather than a wrong answer -- callers then
    fall back to the schema superset and let the firmware arbitrate.

    :param client: Connected Ultimate64 client.
    :returns: Tuple of address enum strings, or ``None`` when the probe
        was inconclusive.
    """
    try:
        cached = _SID_ADDRESS_CHOICES_CACHE.get(client)
    except TypeError:  # unhashable / non-weakrefable client stand-in
        cached = None
    if cached is not None:
        return cached
    try:
        resp = client.get_config_item(CAT_SID_ADDRESSING, _ITEM_SID_PROBE)
    except (Ultimate64Error, AttributeError, TypeError):
        return None
    if not isinstance(resp, dict):
        return None
    category = resp.get(CAT_SID_ADDRESSING)
    if not isinstance(category, dict):
        return None
    item = category.get(_ITEM_SID_PROBE)
    if not isinstance(item, dict):
        return None
    values = item.get("values")
    if not isinstance(values, list):
        return None
    if not values or not all(isinstance(v, str) for v in values):
        return None
    result = tuple(values)
    try:
        _SID_ADDRESS_CHOICES_CACHE[client] = result
    except TypeError:
        pass
    return result


def enable_sid_socket(
    client: Ultimate64Client, socket: int, enabled: bool
) -> None:
    """Enable or disable a physical SID socket.

    ``SID Sockets Configuration / SID Socket N`` is a plain enable
    toggle over ``en_dis`` (u64_config.cc:393-394). The chip *type* is
    a separate, read-only item -- see :func:`get_detected_sid_types`.

    :param client: Connected Ultimate64 client.
    :param socket: Socket index, 1 or 2.
    :param enabled: ``True`` to enable the socket, ``False`` to disable.
    :raises ValueError: If *socket* is not 1 or 2.
    """
    if socket not in (1, 2):
        raise ValueError(f"socket must be 1 or 2, got {socket!r}")
    value = "Enabled" if enabled else "Disabled"
    client.set_config_items(CAT_SID_SOCKETS, {f"SID Socket {socket}": value})


def get_sid_socket_enabled(client: Ultimate64Client) -> dict[int, bool]:
    """Return whether each physical SID socket is enabled.

    :param client: Connected Ultimate64 client.
    :returns: Dict mapping 1-based socket index to its enable state,
        e.g. ``{1: True, 2: False}``.
    """
    inner = _unwrap(
        client.get_config_category(CAT_SID_SOCKETS), CAT_SID_SOCKETS
    )
    result: dict[int, bool] = {}
    for socket in (1, 2):
        value = inner.get(f"SID Socket {socket}")
        if isinstance(value, str) and value in SID_SOCKET_ENABLE_VALUES:
            result[socket] = value == "Enabled"
    return result


def get_detected_sid_types(client: Ultimate64Client) -> dict[int, str]:
    """Return the SID chip the firmware detected in each socket.

    Reads ``SID Sockets Configuration / SID Detected Socket N``, which
    is the item that actually holds a chip type -- filled in by the
    boot-time probe in ``U64SidSockets::detect()``.

    Values come from :data:`SID_DETECTED_TYPE_VALUES` and include
    replacement chips (``"ARMSID"``, ``"SIDKick Pico"``, ...), not just
    ``"6581"`` / ``"8580"``.

    .. warning::

       **This is advisory, not authoritative.** The firmware marks the
       item read-only for its *menu* (``cfg->disable(CFG_SID1_TYPE)``,
       u64_config.cc:517-518), but ``set_item`` never consults that flag
       (route_configs.cc:63-91), so REST can write it. Verified live on
       a U64E (firmware 3.15) on 2026-08-30: ``PUT SID Detected
       Socket 1 = 6581`` returned HTTP 200 on a device with a real 8580
       fitted, and the value read back as ``6581``.

       So what this returns is the last value *written*, which is the
       detection result only if nothing has overwritten it since boot.
       Do not use it to decide what is physically socketed without a
       reboot first, and do not treat a mismatch against expectations as
       a hardware fault. Nothing in the harness writes this item; a
       stale value means something else did.

    :param client: Connected Ultimate64 client.
    :returns: Dict mapping 1-based socket index to the detected type
        string, e.g. ``{1: "8580", 2: "None"}``. Sockets whose item is
        absent are omitted.
    """
    inner = _unwrap(
        client.get_config_category(CAT_SID_SOCKETS), CAT_SID_SOCKETS
    )
    result: dict[int, str] = {}
    for socket in (1, 2):
        value = inner.get(f"SID Detected Socket {socket}")
        if isinstance(value, str) and value:
            result[socket] = value
    return result


def get_sid_address_map(client: Ultimate64Client) -> dict[SidSlot, str]:
    """Return the base address of all four SID decodes.

    Unlike :func:`get_sid_addresses`, which covers the two physical
    sockets only, this reports the UltiSID cores too -- and those share
    the same address space, so an allocation is only sound when all
    four are considered together.

    :param client: Connected Ultimate64 client.
    :returns: Dict mapping :class:`SidSlot` to its address enum string
        (e.g. ``"$D400"`` or ``"Unmapped"``). Slots whose item is
        absent from the response are omitted.
    """
    inner = _unwrap(
        client.get_config_category(CAT_SID_ADDRESSING), CAT_SID_ADDRESSING
    )
    result: dict[SidSlot, str] = {}
    for slot, item in SID_SLOT_ADDRESS_ITEMS.items():
        value = inner.get(item)
        if isinstance(value, str) and value:
            result[slot] = value
    return result


def _introduced_sid_conflicts(
    current: Mapping[SidSlot, str],
    resulting: Mapping[SidSlot, str],
    touched: set[SidSlot],
) -> list[SidAddressConflict]:
    """Conflicts in *resulting* that are not already in *current*.

    An address counts as newly conflicted when it ends up with two or
    more occupants AND either the pile grew, or a slot the caller named
    joined it. The second test matters: swapping one occupant for
    another keeps the count the same while still putting a caller-named
    slot somewhere it was not.

    :param current: The device's map before the write.
    :param resulting: That map with the caller's changes overlaid.
    :param touched: The slots the caller named.
    :returns: One :class:`SidAddressConflict` per newly-conflicted
        address, in ascending decode order.
    """
    before = sid_address_occupancy(current)
    after = sid_address_occupancy(resulting)
    introduced: list[SidAddressConflict] = []
    for address, slots in after.items():
        if len(slots) < 2:
            continue
        was = set(before.get(address, ()))
        now = set(slots)
        if len(now) > len(was) or (now - was) & touched:
            introduced.append(
                SidAddressConflict(address=address, slots=slots)
            )
    return introduced


def set_sid_address_map(
    client: Ultimate64Client,
    mapping: Mapping[SidSlot | str, str],
    *,
    allow_conflicts: str | None = None,
) -> None:
    """Assign base addresses to one or more SID slots.

    Slots not named in *mapping* are left alone.

    Addresses are validated locally before anything goes on the wire.
    The schema's :data:`SID_ADDRESS_VALUES` is the baseline; the
    device's own choice list is probed once (cached, see
    :func:`_sid_address_choices`) and, when the probe is conclusive, an
    address this device does not offer raises :class:`ValueError`
    rather than surfacing as an HTTP 400. This mirrors
    :func:`set_turbo_mhz`.

    The firmware performs no conflict check of its own: ``set_item``
    only tests membership of the enum (route_configs.cc:76-88), and
    nothing downstream rejects two slots decoding one address -- the
    FPGA simply answers from both. This helper therefore reads the
    current map, overlays *mapping*, and refuses the write if it would
    *introduce* an overlap: an address that ends up with more occupants
    than it had, or that gains a slot the caller named.

    **The check is a delta, and that is a real limitation.** Overlap
    the caller did not cause is tolerated, because the device ships
    with all four slots on ``$D400`` under Auto Address Mirroring and a
    whole-map check would fire on the factory state -- refusing even a
    move that strictly *reduces* overlap. But pre-existing mirroring and
    a genuine accidental collision are not distinguishable from the map
    alone: ``$D400`` with four occupants looks identical either way.
    What separates them is provenance, not shape, and provenance is not
    in the map. So this guard promises only *"I did not let you make it
    worse"* -- never *"your resulting allocation is sane"*. Validating
    the latter needs knowledge the device does not expose.

    That is the same contract the firmware's own ``auto_mirror`` states
    for itself: it widens decodes "without introducing overlaps that
    were not already there" (u64_config.cc:2381-2384).

    Pass ``allow_conflicts="<reason>"`` to introduce an overlap
    deliberately (which also skips the read); the reason is logged at
    WARNING, matching :meth:`MemoryPolicy.write_memory`'s
    ``override="reason"`` idiom.

    :param client: Connected Ultimate64 client.
    :param mapping: :class:`SidSlot` (or address-item name) -> address
        enum string.
    :param allow_conflicts: Reason string permitting two slots on one
        address. A bare ``True`` is rejected -- an overlap has to be
        justified in the diff, not merely enabled.
    :raises ValueError: On an empty mapping, an unknown slot, an
        address outside the schema or outside this device's probed
        choices, or -- unless *allow_conflicts* gives a reason -- an
        overlap this call would introduce. Also if *allow_conflicts* is
        not a non-empty string or ``None``.
    """
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValueError(
            "mapping must be a non-empty mapping of SidSlot to address"
        )
    if allow_conflicts is not None and (
        not isinstance(allow_conflicts, str) or not allow_conflicts
    ):
        raise ValueError(
            "allow_conflicts must be a non-empty reason string, e.g. "
            'allow_conflicts="reproducing #NNN overlap"'
        )

    requested: dict[SidSlot, str] = {}
    for key, address in mapping.items():
        slot = _as_sid_slot(key)  # raises ValueError on an unknown slot
        validate_enum(address, SID_ADDRESS_VALUES, "SID address")
        requested[slot] = address

    choices = _sid_address_choices(client)
    if choices is not None:
        for slot, address in requested.items():
            if address not in choices:
                raise ValueError(
                    f"SID address {address!r} for {slot.value} is not "
                    f"offered by this device; offered: {list(choices)}"
                )

    if allow_conflicts:
        _log.warning(
            "SID address conflict check bypassed for %s (reason: %s)",
            ", ".join(
                f"{slot.value}={addr}" for slot, addr in requested.items()
            ),
            allow_conflicts,
        )
    else:
        current = dict(get_sid_address_map(client))
        resulting = dict(current)
        resulting.update(requested)
        conflicts = _introduced_sid_conflicts(
            current, resulting, set(requested)
        )
        if conflicts:
            detail = "; ".join(
                f"{c.address} <- {', '.join(s.value for s in c.slots)}"
                for c in conflicts
            )
            raise ValueError(
                f"SID address conflict introduced by this call: {detail}. "
                f"Pass "
                f'allow_conflicts="<reason>" to stack slots on one '
                f"address deliberately."
            )

    client.set_config_items(
        CAT_SID_ADDRESSING,
        {SID_SLOT_ADDRESS_ITEMS[slot]: address
         for slot, address in requested.items()},
    )
