"""Live: flash holds the factory defaults (issue #227, the owner's procedure).

The entry reset (``apply_factory_baseline``) puts RAM config at the
firmware's factory defaults.  That is only the whole story if **flash**
is at factory defaults too: a boot reloads flash, and ``save_config_to_flash``
is global — it writes every store whose ``staleFlash`` flag is set, i.e.
every category any lane has PUT since the last boot.  This module is the
instrument that keeps the owner's premise true: run it whenever a
firmware is flashed or a lane is suspected of saving.

Procedure, per category listed by the device except the excluded ones
(measured by the owner on the U64E, 2026-09-05, lock held: flash ==
default for every item that reports a default, in all 14 readable
categories):

1. snapshot RAM (bare category GET);
2. ``load_config_from_flash(category)`` — RAM now holds the flash values;
3. per item, ``get_config_item`` -> ``current`` (flash) and ``default``;
   record every item with a ``default`` where ``current != default``;
4. PUT the snapshot back (every item that now differs; attempt all);
5. verify RAM == snapshot.

**Fails, does not skip,** on any flash item that differs from its
default, naming category / item / flash value / default — that is the
finding this module exists to surface.  Items without a ``default`` key
are listed, not compared.

Excluded from the reload: ``Ethernet Settings`` (loading it from flash
could change the address if a static config had ever been saved) and
the WiFi store (the C64 Ultimate reaches the bench over WiFi; its store
has not been read).  ``Network Settings`` is included, as in the owner's
measurement.

Env gates (all unset -> everything skips cleanly):

* ``FLASH_BASELINE_LIVE=1`` — master switch for this module.
* ``U64_HOST``              — device hostname/IP.
* ``U64_PASSWORD``          — optional.
* ``U64_ALLOW_MUTATE=1``    — required: the reload changes RAM config
                              (every change is PUT back).

Never: ``save_config_to_flash``, any ``reset_to_default``, ``reset``,
``reboot``, ``poweroff``.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)


# --------------------------------------------------------------------------- #
# Environment gates                                                           #
# --------------------------------------------------------------------------- #

_LIVE = os.environ.get("FLASH_BASELINE_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="FLASH_BASELINE_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
    pytest.mark.skipif(
        not _ALLOW_MUTATE,
        reason="U64_ALLOW_MUTATE not set — the flash reload rewrites RAM config",
    ),
]

#: Never reloaded from flash by this module (see the module docstring).
_NEVER_RELOAD: tuple[str, ...] = ("Ethernet Settings", "WiFi settings")


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    assert _HOST is not None
    lock = DeviceLock(_HOST, allow_nested=True)
    try:
        lock.acquire_or_raise(timeout=120.0, progress_window=60.0)
    except DeviceLockTimeout as exc:
        pytest.skip(str(exc))
    try:
        yield Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
    finally:
        lock.release()


def _bare(client: Ultimate64Client, category: str) -> dict[str, Any]:
    resp = client.get_config_category(category)
    inner = resp.get(category)
    assert isinstance(inner, dict), f"unexpected category shape for {category!r}: {resp!r}"
    return inner


def _categories_to_check(client: Ultimate64Client) -> list[str]:
    listed = client.list_configs()
    never = {n.lower() for n in _NEVER_RELOAD}
    return [c for c in listed if c.lower() not in never and "wifi" not in c.lower()
            and "ethernet" not in c.lower()]


def _restore(client: Ultimate64Client, category: str, snapshot: dict[str, Any]) -> list[str]:
    """PUT back every item that differs from *snapshot*; return failures."""
    failures: list[str] = []
    now = _bare(client, category)
    for item, want in snapshot.items():
        if now.get(item) == want:
            continue
        try:
            client.set_config_item(category, item, want)
        except Ultimate64Error as exc:
            failures.append(f"{category}/{item}={want!r}: {exc}")
    return failures


def test_flash_holds_the_factory_defaults(client: Ultimate64Client, record_property) -> None:
    info = client.get_info()
    record_property("product", str(info.get("product")))
    record_property("firmware_version", str(info.get("firmware_version")))
    print(f"[info] {info!r}")

    categories = _categories_to_check(client)
    record_property("categories_checked", categories)
    assert categories, "the device lists no reloadable category?"
    assert not any(c in categories for c in _NEVER_RELOAD)

    flash_off: list[str] = []            # flash != default, by name
    ram_drift_before: list[str] = []     # RAM != flash at the time of the check
    restore_failures: list[str] = []
    not_restored: list[str] = []
    unasserted: dict[str, list[str]] = {}

    for cat in categories:
        snapshot = _bare(client, cat)
        client.load_config_from_flash(cat)
        try:
            flash = _bare(client, cat)
            for item in flash:
                item_map = client.get_config_item(cat, item)
                if "default" not in item_map:
                    unasserted.setdefault(cat, []).append(item)
                    continue
                current, default = item_map.get("current"), item_map["default"]
                if current != default:
                    flash_off.append(f"{cat}/{item}: flash={current!r} default={default!r}")
                if snapshot.get(item) != flash.get(item):
                    ram_drift_before.append(
                        f"{cat}/{item}: ram={snapshot.get(item)!r} flash={flash.get(item)!r}"
                    )
        finally:
            restore_failures.extend(_restore(client, cat, snapshot))
            after = _bare(client, cat)
            if after != snapshot:
                not_restored.append(f"{cat}: {snapshot!r} -> {after!r}")
        print(f"[{cat}] items={len(flash)} unasserted={len(unasserted.get(cat, []))}")

    record_property("flash_off", flash_off)
    record_property("ram_drift_before", ram_drift_before)
    record_property("unasserted", unasserted)
    for line in ram_drift_before:
        print(f"[ram-drift] {line}")
    for line in flash_off:
        print(f"[FLASH != DEFAULT] {line}")

    assert not restore_failures, "RAM could not be put back: " + "; ".join(restore_failures)
    assert not not_restored, "RAM not restored exactly: " + "; ".join(not_restored)
    assert not flash_off, (
        f"{len(flash_off)} flash item(s) differ from the firmware default — somebody saved "
        f"to flash, or the firmware was flashed without the format-and-reset option:\n  "
        + "\n  ".join(flash_off)
    )
