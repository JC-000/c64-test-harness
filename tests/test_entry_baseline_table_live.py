"""Live, read-only: a device against its recorded category/item-count table (#342).

``BASELINE_RECORDED_CATEGORY_SETS`` in
``src/c64_test_harness/backends/ultimate64_baseline.py`` records, per device
generation, the category list ``GET /v1/configs`` returns and the item count
of every category, with each count's basis.  Those are compile-time
properties of a firmware build, so this module exists to make the next
firmware flash show up as a failing test with a diff, rather than as a
figure somebody restates from memory.

What it compares, for the generation the device reports:

* the firmware version ``GET /v1/info`` reports against the record's;
* the category list ``GET /v1/configs`` returns against the record's;
* per category, the number of items one ``GET /v1/configs/<category>``
  returns against the record's count, and the covered / never-touch /
  neither / all totals recomputed from those live counts against the
  record's totals.

**Read-only, bodyless GETs only**: ``/v1/info``, ``/v1/configs`` and one
category GET per listed category.  Nothing is PUT or POSTed, so it costs
zero ``/Temp`` attachments even on the leak-prone C64 Ultimate, and it
needs no ``U64_ALLOW_MUTATE``.  The ``DeviceLock`` is held: the autouse
``device_lock_guard`` holds it per test and the module fixture takes a
nested hold for the module's client.  A static scan in
``tests/test_entry_baseline.py`` fails if a writing client call is added.

Env gates (all unset -> everything skips cleanly):

* ``BASELINE_TABLE_LIVE=1``      -- master switch for this module.
* ``U64_HOST``                   -- device hostname/IP (no IPs are committed).
* ``U64_PASSWORD``               -- optional; sent as ``X-Password`` when set.
* ``BASELINE_TABLE_LIVE_C64U=1`` -- required **in addition** when the host
  answers as the C64 Ultimate (generation ``cbm``).  Without it the module
  skips on that device.  The read is free there, but the device is shared,
  unattended and leak-prone, and the hardware-safety rule is that nobody
  sends it traffic as a side effect: somebody has to have meant it.

A device whose generation grades ``unknown`` skips: there is no record to
compare against, and guessing one would report a diff that is not one.

**Reading a failure.**  A category or count diff after a flash means the
build changed; re-read and update the record with its new firmware and
date, and re-derive from source where the record's basis says
``source-derived``.  The C64U's counts are source-derived only, so a first
successful C64U run is also the first device read of them: on a match, add
``device-read`` to that record's basis.  A listing that has gained a
store registered at runtime (the monitor-bookmarks store after the
machine-code monitor has been opened, the per-device SID stores) fails
here too; that is a diff to read, not a record to update.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64_baseline import (
    BASELINE_CATEGORIES,
    BASELINE_NEVER_TOUCH,
    BASELINE_RECORDED_CATEGORY_SETS,
    RecordedCategorySet,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

LIVE_ENV = "BASELINE_TABLE_LIVE"
C64U_ENV = "BASELINE_TABLE_LIVE_C64U"

_LIVE = os.environ.get(LIVE_ENV)
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason=f"{LIVE_ENV} not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]


def record_for(
    generation: str | None, *, c64u_allowed: bool
) -> tuple[RecordedCategorySet | None, str]:
    """The record to compare a device of *generation* against, or why not.

    Pure (no I/O) so the gating is unit-tested without a device.
    """
    if generation == "cbm" and not c64u_allowed:
        return None, (
            f"device answers as the C64 Ultimate (generation 'cbm'); set "
            f"{C64U_ENV}=1 as well to compare it"
        )
    record = BASELINE_RECORDED_CATEGORY_SETS.get(generation or "")
    if record is None:
        return None, f"no recorded table for generation {generation!r}"
    return record, ""


def totals_for(counts: dict[str, int]) -> dict[str, int]:
    """covered / never_touch / neither / all over *counts*, as the record buckets."""
    return {
        "covered": sum(n for c, n in counts.items() if c in BASELINE_CATEGORIES),
        "never_touch": sum(n for c, n in counts.items() if c in BASELINE_NEVER_TOUCH),
        "neither": sum(n for c, n in counts.items()
                       if c not in BASELINE_CATEGORIES and c not in BASELINE_NEVER_TOUCH),
        "all": sum(counts.values()),
    }


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Locked client (``allow_nested``: the autouse guard already holds it)."""
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


@pytest.fixture(scope="module")
def info(client: Ultimate64Client) -> dict[str, Any]:
    payload = client.get_info()
    print(f"[info] {payload!r}")
    return payload


@pytest.fixture(scope="module")
def record(info: dict[str, Any]) -> RecordedCategorySet:
    generation = DeviceCapabilities.from_info(info).generation
    rec, why = record_for(generation, c64u_allowed=bool(os.environ.get(C64U_ENV)))
    if rec is None:
        pytest.skip(why)
    return rec


@pytest.fixture(scope="module")
def listed(client: Ultimate64Client, record: RecordedCategorySet) -> list[str]:
    names = [str(c) for c in client.list_configs()]
    print(f"[configs] {len(names)} categories: {names!r}")
    return names


def _item_count(client: Ultimate64Client, category: str) -> int:
    resp = client.get_config_category(category)
    inner = resp.get(category)
    if inner is None:
        inner = next((v for k, v in resp.items()
                      if isinstance(k, str) and k.lower() == category.lower()), None)
    assert isinstance(inner, dict), f"unexpected shape for {category!r}: {resp!r}"
    return len(inner)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_firmware_is_the_recorded_build(
    info: dict[str, Any], record: RecordedCategorySet
) -> None:
    reported = str(info.get("firmware_version"))
    recorded = record.firmware.split()[0]
    assert reported == recorded, (
        f"{record.generation}: device reports firmware {reported!r}, the record "
        f"is for {record.firmware!r} -- re-read the category list and counts "
        f"and update the record (firmware and date included)"
    )


def test_category_list_matches_the_record(
    listed: list[str], record: RecordedCategorySet
) -> None:
    extra = sorted(set(listed) - record.categories)
    missing = sorted(record.categories - set(listed))
    assert not extra and not missing, (
        f"{record.generation} ({record.firmware}): device lists {extra!r} not in "
        f"the record, and not {missing!r} which the record has"
    )


def test_item_counts_and_totals_match_the_record(
    client: Ultimate64Client, listed: list[str], record: RecordedCategorySet
) -> None:
    live = {cat: _item_count(client, cat) for cat in listed if cat in record.categories}
    for cat, n in sorted(live.items()):
        print(f"[count] {cat}: device {n}, record {record.item_counts[cat].items} "
              f"({'+'.join(record.item_counts[cat].basis)})")
    diffs = {cat: (n, record.item_counts[cat].items)
             for cat, n in live.items() if n != record.item_counts[cat].items}
    assert not diffs, (
        f"{record.generation} ({record.firmware}): per-category item counts "
        f"differ, category -> (device, record): {diffs!r}"
    )
    # Vacuity guard: a count comparison over nothing passes.
    assert set(live) == record.categories, (
        f"compared {len(live)} of {len(record.categories)} recorded categories"
    )
    assert totals_for(live) == dict(record.totals), (
        f"{record.generation}: live totals {totals_for(live)!r} != record "
        f"{dict(record.totals)!r}"
    )
