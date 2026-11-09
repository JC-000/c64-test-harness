"""Guarded single-key save to flash (issue #559).

The bench baseline is the factory default held in flash, and nothing saves
to flash except this function (owner, 2026-11-09). Its job is to put back
a key that a run persisted, such as ``C64 and Cartridge Settings > Fast
Reset``. It saves exactly one key, and refuses unless that key is the only
RAM-vs-flash difference among the stores it can compare.

**There is no read-only path to flash values** (source read, C64U 1.1.0
``7b628eb1`` and U64E ``3a1ff9ff``):

* No REST route returns them. ``route_configs.cc`` ``emit_store`` reports
  RAM ``current`` and the compiled-in ``default`` only.
* The raw config pages are read only inside ``ConfigManager``
  (``config.cc:131/159/448``).
* FTP shows only overflow pages, as ``/flash/config/page_*.bin``
  (``config.cc:141-153``), keyed by numeric item ids that REST never shows.

So flash can only be read by loading it into RAM:
``PUT /v1/configs/<cat>:load_from_flash`` (``route_configs.cc:282-309``).
That runs ``ConfigStore::read``, which sets ``staleEffect`` unconditionally
(``config.cc:460``), and then ``at_close_config``, which calls the store's
``effectuate()``.

**Algorithm.** Every request is bodyless, so this costs zero ``/Temp``
attachments on any firmware. ``set_config_items_batch`` and the global
``load``/``save`` routes are never used.

1. Static refusals, before any request:

   * the process does not hold the device's ``DeviceLock``;
   * the category or item name is a glob;
   * the category is in :data:`FLASH_DIFF_SKIPPED`;
   * the category shares a flash page with a skipped store
     (:data:`FLASH_SHARED_PAGES`). ``ConfigPage::pack`` writes every store
     on the page (``config.cc:274-289``), so saving such a page would also
     persist RAM that was never compared.

2. Reads (GETs only). The category list, the target's category and item
   map (the value is validated against it), and a RAM snapshot of every
   listed category that is not skipped. **A dry run stops here.**
3. For each compared category, in device order:

   * load it from flash, and read it (these are the flash values);
   * put back every item that differs with
     ``set_config_item(..., verify=True)``;
   * re-read the category, and require it to equal the snapshot.

   A failed or rejected restore raises :class:`FlashSaveRestoreError`
   at once.
4. Refuse with :class:`FlashSaveRefusedError` if any difference other than
   the target remains. RAM has already been verified back at this point.
5. ``set_config_item(target, value, verify=True)``. If flash already held
   *value*, stop here: ``saved=False``, and no flash write.
6. ``save_config_to_flash(category)`` must answer ``written == [category]``.
7. Confirm: load that category from flash again. It must read exactly the
   RAM it had after step 5. Otherwise :class:`FlashSaveVerifyError`.

**Side effects, accepted by the owner (#559).** These happen even when the
call refuses. Every compared store re-runs ``effectuate()`` when it is
loaded, whether or not any value differs. On
``C64 and Cartridge Settings`` that is ``C64::effectuate_settings``
(``c64.cc:267-277``): the bus is reconfigured, and the REU enable and the
Command Interface slot are zeroed and then set again from config (a pulse,
``c64.cc:306-329``). It is not a C64 reset. On ``Drive A/B Settings``,
``C1541::effectuate_settings`` forces ``current_drive_type = unset`` and
re-runs ``set_drive_type`` (``c1541.cc:194-196``), which likely resets the
emulated drive. A restore PUT runs that item's change hook, if it has one.
Config is put back; these transient effects are not.

**Known blind spot.** ``ConfigStore::unpack`` sets only the item ids that
are present in the flash page (``config.cc:404-422``). An item that the
firmware gained after the page was last written keeps its RAM value across
a load, so it compares as equal even though a power-on would give its
default. REST cannot detect this.

**Empty strings restore fine** (source read). ``?value=`` parses to the
empty string, not to a missing argument: ``ArgsURI::ParseReqHeader``
``set(name, "")`` (1.1.0 ``routes.h:285-293``), and ``Dict::operator[]``
returns that stored pointer (``dict.h:91-110``). So ``PUT configs`` takes
the two-element path, and ``setString("")`` applies it
(``route_configs.cc:194-245``). The client sends ``value=`` for ``""``. A
device that rejects it anyway ends in :class:`FlashSaveRestoreError`, like
any other rejected restore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .ultimate64_baseline import (
    BASELINE_NEVER_TOUCH,
    _differs,
    _resolve_present,
    _unwrap_category,
)
from .ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    _config_values_match,
    _strtol0,
)

_log = logging.getLogger(__name__)

__all__ = [
    "FLASH_DIFF_SKIPPED",
    "FLASH_SHARED_PAGES",
    "FlashSaveRefusedError",
    "FlashSaveReport",
    "FlashSaveRestoreError",
    "FlashSaveVerifyError",
    "save_config_item_to_flash",
]


def _skip_reason(name: str, why_not_loaded: str) -> str:
    return f"{why_not_loaded} (BASELINE_NEVER_TOUCH: {BASELINE_NEVER_TOUCH[name]})"


#: Stores that are never loaded from flash, so they are never compared.
#: Each reason says why loading it is unsafe, then quotes the
#: :data:`~.ultimate64_baseline.BASELINE_NEVER_TOUCH` entry for the same
#: store. None of them shares a flash page with a store that can be a target.
#: The one page shared with a skipped store is refused through
#: :data:`FLASH_SHARED_PAGES`.
FLASH_DIFF_SKIPPED: dict[str, str] = {
    "Ethernet Settings": _skip_reason(
        "Ethernet Settings",
        "a load re-runs NetworkInterface::effectuate_settings, which on 1.1.0 "
        "calls dhcp_stop() unconditionally on a link-up interface "
        "(network_interface.cc:403-407): the address the REST connection uses "
        "is cleared and every connection on it is aborted (U64E 3a1ff9ff "
        ":427-437 guards this; the C64U does not). Own flash page 0x4E657477",
    ),
    "WiFi settings": _skip_reason(
        "WiFi settings",
        "a load re-runs NetworkLWIP_WiFi::effectuate_settings -> "
        "NetworkInterface::effectuate_settings (network_esp32.cc:123-125), "
        "the same dhcp_stop() path on the wireless interface. Own flash page "
        "0x57494649",
    ),
    "Network Settings": _skip_reason(
        "Network Settings",
        "REST checks X-Password against the live Network Password "
        "(routes.h:305-313), so a flash password that differs from RAM would "
        "lock this client out before RAM could be put back; and on the C64U "
        "the /Temp hygiene's FTP enable lives in RAM only, so comparing this "
        "store would refuse every call there. Own flash page 0x4E455400",
    ),
    "SID Sockets Configuration": _skip_reason(
        "SID Sockets Configuration",
        "a load re-runs U64SidSockets::effectuate_settings, which writes the "
        "socket regulator bits from the loaded values: a flash value that "
        "differs would cut socket power, and the restore PUT would re-apply "
        "voltage without the 12 V approval. Shares flash page 0x55363443, "
        "which is why FLASH_SHARED_PAGES refuses its neighbours as targets",
    ),
    "Clock Settings": _skip_reason(
        "Clock Settings",
        "RTC-backed, with no flash page to compare against (rtc.h:17-27); on "
        "the U64E a load arms at_close_config, which writes the RAM items to "
        "the clock chip. Not listed on the C64U at all",
    ),
}

#: Flash pages that hold more than one store, from source
#: (``u64_config.cc``: ``STORE_PAGE_ID`` ``0x55363443`` at 1.1.0 :417-825 and
#: 3a1ff9ff :480-906). Saving any store on a page writes all of them, so
#: a target on a page that also holds a skipped store is refused.
FLASH_SHARED_PAGES: tuple[frozenset[str], ...] = (
    frozenset({
        "U64 Specific Settings",
        "Audio Mixer",
        "SID Sockets Configuration",
        "UltiSID Configuration",
        "SID Addressing",
    }),
)

_DRY_RUN_REASON = (
    "dry run: the firmware has no read-only path to flash values; reading "
    "them means PUT <category>:load_from_flash, which replaces RAM and "
    "re-runs the store's effectuate(), so a dry run that writes nothing "
    "cannot compare RAM with flash and cannot predict whether the real run "
    "would refuse"
)

_SIDE_EFFECTS = (
    "every compared category was loaded from flash and re-ran its "
    "effectuate() whether or not any value differed (C64 and Cartridge "
    "Settings: REU enable / Command Interface slot pulsed; Drive A/B "
    "Settings: set_drive_type re-run, which likely resets the emulated "
    "drive); config values were put back, these transient effects were not"
)


@dataclass
class FlashSaveReport:
    """What :func:`save_config_item_to_flash` did or, in a dry run, would do.

    ``differences`` maps ``(category, item)`` to ``(ram, flash)`` for every
    compared item whose RAM value differed from flash. It stays empty in a
    dry run, where ``flash_compared`` is ``False`` and
    ``flash_compared_reason`` says why.
    """

    host: str
    category: str
    item: str
    value: Any
    dry_run: bool
    flash_compared: bool = False
    flash_compared_reason: str | None = None
    ram_before: Any = None
    flash_before: Any = None
    compared_categories: list[str] = field(default_factory=list)
    skipped_categories: dict[str, str] = field(default_factory=dict)
    differences: dict[tuple[str, str], tuple[Any, Any]] = field(default_factory=dict)
    planned_requests: list[str] = field(default_factory=list)
    side_effects: str | None = None
    saved: bool = False
    written: list[str] = field(default_factory=list)


class FlashSaveRefusedError(Ultimate64Error):
    """The save was refused, and nothing reached flash.

    Raised before any request when the refusal is static: no lock, a glob,
    a skipped or page-sharing store. Raised after the comparison when
    another item differs, and by then every compared category has been
    verified back to its snapshot. ``report`` holds what was found. The
    side effects in the module docstring still happened if any category
    was loaded (``report.side_effects`` is set then).
    """

    def __init__(self, message: str, report: FlashSaveReport) -> None:
        super().__init__(message)
        self.report = report


class FlashSaveRestoreError(Ultimate64Error):
    """RAM could not be verified back to its snapshot.

    ``category`` was loaded from flash, and ``items`` are the ones whose
    restore was rejected or read back wrong. Those items may now hold flash
    values. Nothing was saved. This is not a clean refusal: put those items
    back by hand, or power-cycle (a boot reloads flash anyway).
    """

    def __init__(self, message: str, category: str, items: list[str],
                 report: FlashSaveReport) -> None:
        super().__init__(message)
        self.category = category
        self.items = items
        self.report = report


class FlashSaveVerifyError(Ultimate64Error):
    """``save_to_flash`` ran, but its answer or the read-back is wrong.

    Flash may hold something other than intended; ``report`` has the
    details.
    """

    def __init__(self, message: str, report: FlashSaveReport) -> None:
        super().__init__(message)
        self.report = report


def _is_glob(name: str) -> bool:
    return "*" in name or "?" in name


def _skipped_name(category: str) -> str | None:
    for name in FLASH_DIFF_SKIPPED:
        if name.casefold() == category.casefold():
            return name
    return None


def _shared_page_with_skipped(category: str) -> frozenset[str] | None:
    folded = category.casefold()
    for page in FLASH_SHARED_PAGES:
        if folded in {s.casefold() for s in page} and any(
            _skipped_name(s) for s in page
        ):
            return page
    return None


def _shown(item: str, value: Any) -> str:
    return "<redacted>" if "password" in item.casefold() else repr(value)


def _normalise_value(item_map: dict, value: Any) -> Any:
    """The value as the firmware would store it, or ``None`` if it would refuse."""
    if "values" in item_map:
        for choice in item_map["values"]:
            if str(choice).casefold() == str(value).casefold():
                return choice
        return None
    if "min" in item_map:
        parsed = _strtol0(value)
        if parsed is None:
            return None
        if not (int(item_map["min"]) <= parsed <= int(item_map["max"])):
            return None
        return parsed
    return str(value)


def _diff(ram: dict, flash: dict) -> dict[str, tuple[Any, Any]]:
    out: dict[str, tuple[Any, Any]] = {}
    for item in sorted(set(ram) | set(flash)):
        a, b = ram.get(item), flash.get(item)
        if item not in ram or item not in flash or _differs(a, b):
            out[item] = (a, b)
    return out


def save_config_item_to_flash(
    client: Ultimate64Client,
    category: str,
    item: str,
    value: Any,
    *,
    dry_run: bool = False,
) -> FlashSaveReport:
    """Persist one config key to flash, refusing unless it is the only difference.

    See the module docstring for the algorithm, the stores that are never
    compared, and the side effects. The caller must hold the device's
    ``DeviceLock`` for the whole call. The lock is never taken here, and
    that also applies to a dry run.

    :param client: connected :class:`Ultimate64Client`.
    :param category: the exact category name. A glob is refused.
    :param item: the exact item name. A glob is refused.
    :param value: the value to persist, validated against the item map.
    :param dry_run: plan only. It sends bodyless GETs and nothing else,
        writes nothing to disk, and returns a report with
        ``flash_compared=False``, the reason, and ``planned_requests``.
    :returns: :class:`FlashSaveReport`. ``saved`` is ``False`` when flash
        already held *value* (RAM is then set to it, and flash is not
        written).
    :raises FlashSaveRefusedError: a static refusal (before any request),
        an invalid value, or another RAM-vs-flash difference.
    :raises FlashSaveRestoreError: RAM could not be verified back.
    :raises FlashSaveVerifyError: the save's answer or the flash read-back
        was wrong.
    """
    if not isinstance(category, str) or not category:
        raise ValueError("category must be a non-empty string")
    if not isinstance(item, str) or not item:
        raise ValueError("item must be a non-empty string")
    report = FlashSaveReport(
        host=client.host, category=category, item=item, value=value, dry_run=dry_run,
    )

    def refuse(why: str) -> FlashSaveRefusedError:
        return FlashSaveRefusedError(
            f"refusing to save {category!r}/{item!r} to flash on {client.host}: {why}",
            report,
        )

    # 1. Static refusals: no request has been sent.
    if not client._temp_lock_held():
        raise refuse(
            "this process does not hold the device's DeviceLock. Take it "
            "first, e.g. DeviceLock(host).acquire_or_raise(timeout=...)"
        )
    if _is_glob(category) or _is_glob(item):
        raise refuse("category and item must be exact names, not patterns")
    skipped = _skipped_name(category)
    if skipped is not None:
        raise refuse(f"{skipped!r} is never loaded from flash: "
                     f"{FLASH_DIFF_SKIPPED[skipped]}")
    page = _shared_page_with_skipped(category)
    if page is not None:
        raise refuse(
            f"{category!r} shares a flash page with {sorted(page)!r}; saving "
            "it writes the whole page, including a store that is never "
            "compared (" + ", ".join(s for s in sorted(page) if _skipped_name(s))
            + ")"
        )

    # 2. Reads.
    listed = client.list_configs()
    cat = _resolve_present(category, listed)
    if cat is None:
        raise refuse(f"the device does not list category {category!r}")
    report.category = cat
    ram: dict[str, dict[str, Any]] = {}
    target_ram = _unwrap_category(client.get_config_category(cat), cat)
    key = _resolve_present(item, [str(k) for k in target_ram])
    if key is None:
        raise refuse(f"category {cat!r} has no item {item!r}")
    report.item = key
    item_map = client.get_config_item(cat, key)
    stored = _normalise_value(item_map, value)
    if stored is None:
        raise refuse(f"{_shown(key, value)} is not a value the firmware accepts "
                     f"for {key!r} ({item_map!r})")
    report.value = stored
    report.ram_before = target_ram[key]

    compared: list[str] = []
    for name in listed:
        skip = _skipped_name(name)
        if skip is not None:
            report.skipped_categories[name] = FLASH_DIFF_SKIPPED[skip]
            continue
        compared.append(name)
    report.compared_categories = compared
    for name in compared:
        ram[name] = (target_ram if name == cat
                     else _unwrap_category(client.get_config_category(name), name))

    for name in compared:
        enc = f"/v1/configs/{name}"
        report.planned_requests += [
            f"PUT {enc}:load_from_flash",
            f"GET {enc}",
            f"PUT {enc}/<item>?value=<RAM value> + GET (verify), per differing item",
            f"GET {enc}",
        ]
    item_path = f"/v1/configs/{cat}/{key}"
    report.planned_requests += [
        f"PUT {item_path}?value={stored} + GET (verify)",
        "(the save, load and GET below are skipped if flash already holds the value)",
        f"PUT /v1/configs/{cat}:save_to_flash",
        f"PUT /v1/configs/{cat}:load_from_flash",
        f"GET /v1/configs/{cat}",
    ]

    if dry_run:
        report.flash_compared_reason = _DRY_RUN_REASON
        return report

    # 3. Load each compared category, read flash, put RAM back, verify.
    flash: dict[str, dict[str, Any]] = {}
    for name in compared:
        report.side_effects = _SIDE_EFFECTS
        loaded = client.load_config_from_flash(name)
        flash[name] = _unwrap_category(client.get_config_category(name), name)
        diffs = _diff(ram[name], flash[name])
        failed: list[str] = []
        for it, (ram_value, flash_value) in diffs.items():
            report.differences[(name, it)] = (ram_value, flash_value)
            if it not in ram[name]:
                failed.append(it)
                continue
            try:
                client.set_config_item(name, it, ram_value, verify=True)
            except Ultimate64Error as e:
                _log.warning("restore of %r/%r failed: %s", name, it, e)
                failed.append(it)
        after = _unwrap_category(client.get_config_category(name), name)
        failed += [it for it in _diff(ram[name], after) if it not in failed]
        if failed:
            raise FlashSaveRestoreError(
                f"{name!r} was loaded from flash for comparison and could not be "
                f"verified back to its RAM values: {failed!r} may now hold flash "
                "values. Nothing was saved",
                name, failed, report,
            )
        if loaded != [name]:
            raise FlashSaveRestoreError(
                f"PUT {name!r}:load_from_flash reported loading {loaded!r}, not "
                f"[{name!r}]; any other store it reloaded was not put back. "
                "Nothing was saved",
                name, [], report,
            )
    report.flash_compared = True
    report.flash_before = flash[cat].get(key)

    # 4. The guard.
    others = {k: v for k, v in report.differences.items() if k != (cat, key)}
    if others:
        listing = ", ".join(
            f"{c}/{i}: RAM {_shown(i, r)} vs flash {_shown(i, f)}"
            for (c, i), (r, f) in sorted(others.items())
        )
        raise refuse(
            f"{len(others)} other item(s) differ between RAM and flash, so a save "
            f"would not change only this key: {listing}. RAM was verified back "
            "to its values before this call"
        )

    # 5. Set the key; put it back if the write does not take.
    try:
        client.set_config_item(cat, key, stored, verify=True)
    except Ultimate64Error as e:
        try:
            client.set_config_item(cat, key, report.ram_before, verify=True)
        except Ultimate64Error as e2:
            raise FlashSaveRestoreError(
                f"setting {cat!r}/{key!r} failed ({e}) and putting back its RAM "
                f"value failed too ({e2}). Nothing was saved",
                cat, [key], report,
            ) from e2
        raise refuse(f"the write did not take ({e}); RAM was put back") from e
    if _config_values_match(item_map, stored, report.flash_before):
        _log.info("%s/%s already holds %s in flash on %s; not saving",
                  cat, key, _shown(key, stored), client.host)
        return report
    expected = _unwrap_category(client.get_config_category(cat), cat)

    # 6. Save the one category.
    written = client.save_config_to_flash(cat)
    report.written = written
    if written != [cat]:
        raise FlashSaveVerifyError(
            f"PUT {cat!r}:save_to_flash answered written={written!r}, not "
            f"[{cat!r}]; flash may hold more or less than intended",
            report,
        )

    # 7. Confirm from flash.
    client.load_config_from_flash(cat)
    confirmed = _unwrap_category(client.get_config_category(cat), cat)
    wrong = _diff(expected, confirmed)
    if wrong or not _config_values_match(item_map, stored, confirmed.get(key)):
        raise FlashSaveVerifyError(
            f"after save_to_flash, {cat!r} reloaded from flash differs from the "
            f"RAM it was saved from: {sorted(wrong)!r}",
            report,
        )
    report.saved = True
    _log.warning("saved %s/%s = %s to flash on %s (#559)", cat, key,
                 _shown(key, stored), client.host)
    return report
