"""Live: reset-on-entry to the factory-default baseline (issue #227).

Runs ``apply_factory_baseline`` against a real Ultimate device and pins
the contract the mocked tests (``tests/test_entry_baseline.py``) encode:

* a covered item that another lane left drifted is reset, and afterwards
  **every** covered item on the device reads ``current == default``;
* the excluded stores (``Ethernet Settings``, ``Network Settings``, the
  WiFi store) are byte-identical before and after — the entry path does
  not even read them, this module does, once each side;
* the report names the drifted item by name with its old and default
  values;
* ``dry_run=True`` changes nothing;
* the manager path (``create_manager(backend="u64", baseline_on_entry=True)``)
  runs the reset inside the ``DeviceLock`` before handing out the target.

Env gates (all unset -> everything skips cleanly):

* ``U64_BASELINE_LIVE=1`` — master switch for this module.
* ``U64_HOST``            — device hostname/IP (no IPs are committed).
* ``U64_PASSWORD``        — optional; sent as ``X-Password`` when set.
* ``U64_ALLOW_MUTATE=1``  — required: every test here writes config.

What it touches: every category in ``BASELINE_CATEGORIES`` present on
the device is reset to factory default (memory-only; ``configs/
<category>:reset_to_default``), and ``Cartridge Preference`` / ``REU Size``
are deliberately PUT to a non-default value first.  Those two items — and
**only** those two — are snapshotted before any write and PUT back at
module end: a PUT re-effectuates every store it touches (for ``U64
Specific Settings`` that rewrites the CPU-speed registers), so a
"restore every covered item" sweep would be a second, larger
side-effect, not a courtesy (adversarial review of #227).  The rest of
the device is left at the factory baseline the entry reset produced.
The never-touch stores (``BASELINE_NEVER_TOUCH``: the three network
stores, ``SID Sockets Configuration``, ``Clock Settings``) are only read,
once on each side of a reset, and **never PUT** — a PUT of the SID socket
store applies socket voltage, a PUT of ``Clock Settings`` writes the RTC
chip.

Never: ``save_config_to_flash``, ``load_config_from_flash``, the global
``configs:reset_to_default``, any request to a never-touch store other
than a GET, ``reset``, ``reboot``, ``poweroff``.

Not yet measured (record when this runs): the wall-clock cost of one
``apply_factory_baseline`` on the U64E (one category GET + one reset PUT
+ one item GET per item, ~150 items) — ``record_property("apply_seconds")``
captures it; and whether any store's ``effectuate()`` pulses the C64
reset (a separate #217-style marker+jiffy arm, not in this module).
"""
from __future__ import annotations

import os
import time
from typing import Any

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_baseline import (
    BASELINE_CATEGORIES,
    BASELINE_EXCLUDED_CATEGORIES,
    BaselineReport,
    apply_factory_baseline,
)
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CARTRIDGE_PREFERENCE_ITEM,
    CARTRIDGE_SETTINGS_CATEGORY,
    CAT_CART,
)
from c64_test_harness.backends.unified_manager import create_manager


# --------------------------------------------------------------------------- #
# Environment gates                                                           #
# --------------------------------------------------------------------------- #

_LIVE = os.environ.get("U64_BASELINE_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="U64_BASELINE_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
    pytest.mark.skipif(
        not _ALLOW_MUTATE,
        reason="U64_ALLOW_MUTATE not set — every entry-baseline live test writes config",
    ),
]

_ITEM_REU_SIZE = "REU Size"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Locked, stateless HTTP client for the live device (module scope).

    ``allow_nested`` because the autouse ``device_lock_guard`` already
    holds this device's lock per test.
    """
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


def _present(client: Ultimate64Client, names: tuple[str, ...]) -> list[str]:
    listed = client.list_configs()
    return [n for n in names if n in listed]


#: The only items this module PUTs, and therefore the only ones it puts
#: back (review should-fix 5: a PUT re-effectuates the whole store, so
#: restoring items the module never changed is a side-effect, not a
#: restore).
_DRIFTED_BY_THIS_MODULE: tuple[tuple[str, str], ...] = (
    (CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM),
    (CAT_CART, _ITEM_REU_SIZE),
)


@pytest.fixture(scope="module")
def stock(client: Ultimate64Client) -> dict[str, dict[str, Any]]:
    """Every covered category BEFORE any write, bare values — printed so a
    run's log shows what the device inherited (the never-touch stores are
    printed too, read-only).  Only :data:`_DRIFTED_BY_THIS_MODULE` is put
    back at module end (:func:`restore_drifted`)."""
    snap = {cat: _bare(client, cat) for cat in _present(client, BASELINE_CATEGORIES)}
    for cat, items in snap.items():
        print(f"[stock] {cat}: {items!r}")
    for cat in _present(client, BASELINE_EXCLUDED_CATEGORIES):
        print(f"[stock, never-touch] {cat}: {_bare(client, cat)!r}")
    return snap


@pytest.fixture(scope="module", autouse=True)
def restore_drifted(client: Ultimate64Client, stock: dict[str, dict[str, Any]]):
    """PUT back only what this module itself drifted, if it still differs.

    Both PUTs are attempted; failures are raised together at the end.
    """
    yield
    failures: list[str] = []
    for cat, item in _DRIFTED_BY_THIS_MODULE:
        want = stock.get(cat, {}).get(item)
        if want is None or want == "":
            continue
        try:
            if client.get_config_value(cat, item) != want:
                client.set_config_item(cat, item, want)
        except Ultimate64Error as exc:
            failures.append(f"{cat}/{item}={want!r}: {exc}")
    if failures:
        raise Ultimate64Error(
            f"{len(failures)} drifted item(s) could not be put back: " + "; ".join(failures)
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _pick_non_default(client: Ultimate64Client, category: str, item: str,
                      prefer: tuple[str, ...] = ()) -> tuple[Any, Any]:
    """Return ``(a settable value != default, default)`` for an enum item."""
    item_map = client.get_config_item(category, item)
    assert "default" in item_map, f"{category!r}/{item!r} carries no default: {item_map!r}"
    default = item_map["default"]
    choices = item_map.get("values")
    assert isinstance(choices, list) and choices, f"{category!r}/{item!r} is not an enum: {item_map!r}"
    for v in (*prefer, *choices):
        if v in choices and v != default:
            return v, default
    raise AssertionError(f"no value of {category!r}/{item!r} differs from default {default!r}")


def _assert_every_covered_item_at_default(client: Ultimate64Client) -> dict[str, int]:
    """One item GET per covered item; returns items-checked per category.

    Only the covered categories: the SID socket store (detection results)
    and the RTC are never-touch and are not read here.
    """
    checked: dict[str, int] = {}
    off: list[str] = []
    for cat in _present(client, BASELINE_CATEGORIES):
        n = 0
        for item in _bare(client, cat):
            item_map = client.get_config_item(cat, item)
            if "default" not in item_map:
                continue
            n += 1
            if item_map.get("current") != item_map["default"]:
                off.append(f"{cat}/{item}: current={item_map.get('current')!r} "
                           f"default={item_map['default']!r}")
        checked[cat] = n
    assert not off, "after the entry reset these still differ from default:\n  " + "\n  ".join(off)
    return checked


# --------------------------------------------------------------------------- #
# (a) drift -> entry reset -> every covered item reads default                #
# --------------------------------------------------------------------------- #

def test_drifted_items_are_reset_and_every_covered_item_reads_default(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    info = client.get_info()
    record_property("product", str(info.get("product")))
    record_property("firmware_version", str(info.get("firmware_version")))
    print(f"[info] {info!r}")

    pref_value, pref_default = _pick_non_default(
        client, CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, prefer=("External",)
    )
    reu_value, reu_default = _pick_non_default(client, CAT_CART, _ITEM_REU_SIZE, prefer=("16 MB", "1 MB"))
    client.set_config_item(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, pref_value)
    client.set_config_item(CAT_CART, _ITEM_REU_SIZE, reu_value)
    assert client.get_config_value(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM) == pref_value
    assert client.get_config_value(CAT_CART, _ITEM_REU_SIZE) == reu_value

    t0 = time.monotonic()
    report = apply_factory_baseline(client)
    elapsed = time.monotonic() - t0
    record_property("apply_seconds", round(elapsed, 3))
    record_property("categories_reset", list(report.reset))
    record_property("categories_skipped", list(report.skipped))
    record_property("drifted_items", [f"{c}/{i}" for c, i in report.drifted_items()])
    print(f"[apply] {elapsed:.2f}s — {report.summary()}")
    assert not set(report.reset) & set(BASELINE_EXCLUDED_CATEGORIES)
    for cat, items in report.drifted.items():
        for item, (was, default) in items.items():
            print(f"[drift] {cat}/{item}: {was!r} -> default {default!r}")

    assert isinstance(report, BaselineReport) and report.ok
    assert set(report.reset) == set(_present(client, BASELINE_CATEGORIES)), (
        f"every present covered category is reset: {report.reset!r}"
    )
    assert client.get_config_value(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM) == pref_default
    assert client.get_config_value(CAT_CART, _ITEM_REU_SIZE) == reu_default

    checked = _assert_every_covered_item_at_default(client)
    record_property("items_checked", checked)
    assert sum(checked.values()) > 0


# --------------------------------------------------------------------------- #
# (b) the excluded stores are byte-identical before/after                     #
# --------------------------------------------------------------------------- #

def test_excluded_stores_are_byte_identical(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    """The five never-touch stores read the same before and after the
    entry reset.  For ``SID Sockets Configuration`` this is the proof the
    sockets kept their detected state (a reset there flips all six items,
    U64E n=3); for ``Clock Settings`` that the RTC store was not armed."""
    excluded = _present(client, BASELINE_EXCLUDED_CATEGORIES)
    record_property("excluded_present", excluded)
    assert excluded, "the device lists none of the excluded stores?"
    before = {cat: _bare(client, cat) for cat in excluded}
    client.set_config_item(CAT_CART, _ITEM_REU_SIZE, _pick_non_default(client, CAT_CART, _ITEM_REU_SIZE)[0])
    apply_factory_baseline(client)
    after = {cat: _bare(client, cat) for cat in excluded}
    for cat in excluded:
        assert after[cat] == before[cat], (
            f"{cat!r} changed across the entry reset: before={before[cat]!r} after={after[cat]!r}"
        )
    print(f"[excluded] unchanged: {excluded!r}")


# --------------------------------------------------------------------------- #
# (c) the report names the drifted item                                       #
# --------------------------------------------------------------------------- #

def test_report_lists_the_drifted_item_by_name(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    apply_factory_baseline(client)                       # start clean
    value, default = _pick_non_default(
        client, CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, prefer=("External",)
    )
    client.set_config_item(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, value)
    report = apply_factory_baseline(client)
    record_property("drifted", {f"{c}/{i}": v for c, items in report.drifted.items() for i, v in items.items()})
    assert report.drifted == {
        CARTRIDGE_SETTINGS_CATEGORY: {CARTRIDGE_PREFERENCE_ITEM: (value, default)}
    }, f"exactly the one drifted item, by name: {report.drifted!r}"
    assert report.mismatched == {}


# --------------------------------------------------------------------------- #
# (d) dry_run                                                                 #
# --------------------------------------------------------------------------- #

def test_dry_run_reports_without_writing(client: Ultimate64Client, stock: dict) -> None:
    apply_factory_baseline(client)
    value, default = _pick_non_default(client, CAT_CART, _ITEM_REU_SIZE, prefer=("16 MB", "1 MB"))
    client.set_config_item(CAT_CART, _ITEM_REU_SIZE, value)
    report = apply_factory_baseline(client, dry_run=True)
    assert report.dry_run and report.reset == () and report.mismatched == {}
    assert report.drifted.get(CAT_CART, {}).get(_ITEM_REU_SIZE) == (value, default)
    assert client.get_config_value(CAT_CART, _ITEM_REU_SIZE) == value, "dry run must not write"
    apply_factory_baseline(client)
    assert client.get_config_value(CAT_CART, _ITEM_REU_SIZE) == default


# --------------------------------------------------------------------------- #
# (e) the manager path                                                        #
# --------------------------------------------------------------------------- #

def test_manager_path_resets_on_acquire(client: Ultimate64Client, stock: dict) -> None:
    """``create_manager(backend="u64", baseline_on_entry=True)`` hands out a
    target whose device is already at baseline.  The module fixture holds
    the lock, so the manager's ``allow_nested`` acquire joins it."""
    value, default = _pick_non_default(
        client, CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, prefer=("External",)
    )
    client.set_config_item(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, value)
    with create_manager(backend="u64", u64_hosts=_HOST, u64_password=_PW,
                        baseline_on_entry=True, lock_timeout=120.0) as mgr:
        with mgr.instance() as target:
            assert target.backend == "u64"
            assert target.client.get_config_value(
                CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM
            ) == default, "the target must be at baseline before the test body runs"
