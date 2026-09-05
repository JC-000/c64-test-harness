"""Live-hardware characterisation of ``REU Size`` read-back (issue #168).

Issue #168 reports ``get_reu_config()`` returning a *stale* ``REU Size`` on
a U64E: ``(False, '512 KB')`` at one point and ``(False, '2 MB')`` at
another, with no known change in between. The harness side is a plain
category read with no cache, so the value is either stale device-side
(explanation 1) or the two observations straddled a write (explanation 2,
which the issue pins on the ``Cartridge`` item because the C64 Ultimate's
``Cartridge`` value mirrors REU state).

This module runs the issue's procedure against a real device and encodes
what was FOUND (U64E, firmware 3.15, 2026-09-01 local = 02:21-02:40 UTC
2026-09-02, device lock held for the whole run; the C64 Ultimate
generation was not reachable and is untested):

* ``REU Size`` did **not** move on its own: five category reads over ~60 s
  with nothing else touching the device were byte-identical, and
  ``get_reu_config()`` agreed with the raw category on every read. (The
  committed test keeps five reads but at ``_QUIET_INTERVAL_S`` = 4 s; the
  characterisation is settled and the source excludes a cache — see
  ``test_reu_size_stable_across_quiet_reads``.)
* The ``Cartridge`` item on U64E fw 3.15 exposes ``presets: [""]`` — there
  is **no** ``"REU"`` preset (the shape previously documented only for the
  C64 Ultimate), so a ``Cartridge`` write cannot be what moved the size on
  this generation: the only settable value is the current one. ``Cartridge``
  also does **not** mirror REU state here — it stays ``""`` while the REU
  is enabled.
* ``set_reu(client, True, <other size>)`` is reflected by ``REU Size`` and
  ``RAM Expansion Unit`` **immediately** and identically 2 s later, and
  ``restore_state`` puts the whole category back byte-for-byte. The REST
  item reports the config-store value, not a size pending a reset — so no
  ``reset()`` was performed (the procedure calls for one only on a
  discrepancy).

* Flash and RAM disagreed on this bench: a per-category
  ``configs/<C64 and Cartridge Settings>:load_from_flash`` moved ``REU
  Size`` from ``512 KB`` to ``2 MB`` — the item default and exactly the
  reporter's second value — with no config write anywhere.

Verdict: neither explanation reproduces. ``REU Size`` is trustworthy at
the moment it is read; a value that differs between two reads means the
*config changed in between* — a config write (``set_reu`` /
``restore_state`` from another lane, ``reset_config_to_default``), a
reload from flash (``load_config_from_flash``), or — the likeliest for the
reporter's data — a **reboot/power-cycle**: config PUTs are volatile
until ``save_config_to_flash`` and a boot reloads flash (firmware
``software/api/route_configs.cc:239, :329, :374``). Not a ``Cartridge``
interaction and not firmware staleness. The tests below fail if the
firmware ever behaves the other way.

Env gates (all unset -> everything skips cleanly):

* ``REU_READBACK_LIVE=1`` — master switch for this module.
* ``U64_HOST``            — device hostname/IP (no IPs are committed).
* ``U64_PASSWORD``        — optional; sent as ``X-Password`` when set.
* ``U64_ALLOW_MUTATE=1``  — required for the four mutating tests; the
                            quiet-read test runs without it.

What the mutating tests touch:

* ``C64 and Cartridge Settings`` — ``RAM Expansion Unit`` and ``REU Size``
  (via ``set_reu`` / ``restore_state``), a ``Cartridge=""`` smoke PUT
  (a no-op only with no ``.crt`` selected; otherwise it detaches the
  cartridge and the ``finally`` re-selects it), and one per-category
  ``configs/<C64 and Cartridge Settings>:load_from_flash`` (the
  flash-vs-RAM measurement; every item it changes is PUT back).
* ``U64 Specific Settings`` — ``restore_state`` rewrites ``Turbo Control``,
  ``CPU Speed`` and ``Badline Timing`` there with their snapshotted values
  (``ultimate64_helpers.restore_state``), so that category sees same-value
  PUTs even though nothing in this module changes it.

The stock cartridge category is snapshotted before any write and every
mutating test ends by diffing the full category against that snapshot.
Never: ``save_config_to_flash``, ``reset``, ``reboot``, ``poweroff``.
"""
from __future__ import annotations

import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_CART,
    get_reu_config,
    restore_state,
    set_reu,
    snapshot_state,
)
from c64_test_harness.backends.ultimate64_schema import REU_SIZE_VALUES


# --------------------------------------------------------------------------- #
# Environment gates                                                           #
# --------------------------------------------------------------------------- #

_LIVE = os.environ.get("REU_READBACK_LIVE")
_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="REU_READBACK_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

requires_mutate = pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — skipping mutating REU read-back test",
)

_ITEM_REU_ENABLED = "RAM Expansion Unit"
_ITEM_REU_SIZE = "REU Size"
_ITEM_CARTRIDGE = "Cartridge"

#: Quiet-read window: ``_QUIET_READS`` reads, ``_QUIET_INTERVAL_S`` apart.
#: The original characterisation used 15 s (60 s total); 4 s keeps the
#: whole module well under 30 s now that the finding is settled.
_QUIET_READS = 5
_QUIET_INTERVAL_S = 4.0


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Locked, stateless HTTP client for the live device.

    Queue-aware lock held for the whole module: a live, progressing holder
    extends the wait indefinitely; a genuinely stuck/dead holder trips the
    timeout and becomes a clean skip (never a reboot/recover). ``allow_nested``
    because the autouse ``device_lock_guard`` already holds this device's
    lock per test.
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


@pytest.fixture(scope="module")
def stock(client: Ultimate64Client) -> dict:
    """The full ``C64 and Cartridge Settings`` category BEFORE any write.

    Every mutating test diffs against this — a restore compared against a
    mid-session snapshot proves nothing.
    """
    return _category(client)


def _category(client: Ultimate64Client) -> dict:
    """Return the unwrapped ``{item: value}`` map for the cartridge category."""
    resp = client.get_config_category(CAT_CART)
    inner = resp.get(CAT_CART)
    assert isinstance(inner, dict) and inner, f"unexpected category shape: {resp!r}"
    return inner


def _item(client: Ultimate64Client, item: str) -> dict:
    """Return the ``{current, default, values|presets}`` map for *item*.

    ``get_config_item`` unwraps the category/``errors`` envelope itself
    since issue #214; this only pins the shape.
    """
    inner = client.get_config_item(CAT_CART, item)
    assert isinstance(inner, dict), f"unexpected item shape for {item!r}: {inner!r}"
    assert "current" in inner, f"item {item!r} has no 'current': {inner!r}"
    return inner


def _diff(before: dict, after: dict) -> dict:
    return {
        k: (before.get(k), after.get(k))
        for k in set(before) | set(after)
        if before.get(k) != after.get(k)
    }


def _restore_category_items(client, stock: dict, now: dict) -> None:
    """PUT back every item in *now* that differs from *stock*; raise afterwards.

    Used by the flash-reload tests, where ``restore_state`` alone is not
    enough (the reload also flips items the snapshot does not carry, e.g.
    ``Command Interface``, which ``set_emulation_flags`` really applies on
    the device). Every PUT is attempted even if an earlier one is rejected
    — the firmware answers 400 for ``value=""`` on some items — and the
    failures are raised together at the end, so one bad item cannot leave
    the rest un-restored.
    """
    failures: list[str] = []
    for item, (want, _got) in _diff(stock, now).items():
        try:
            client.set_config_item(CAT_CART, item, want)
        except Ultimate64Error as exc:
            failures.append(f"{item}={want!r}: {exc}")
    if failures:
        raise Ultimate64Error(
            f"{len(failures)} item(s) could not be restored in {CAT_CART!r}: "
            + "; ".join(failures)
        )


def _pick_ram_target(stock_size: str, default_size: str | None) -> str:
    """Pick a ``REU Size`` to PUT into RAM before a flash reload.

    Must differ from the RAM value (so the PUT is observable) AND from the
    item default (the value flash most likely holds) — if it equalled the
    flash value, ``flash_size != ram_target`` would fail with the wrong
    diagnosis. Preferred candidates first, then anything else in the enum.
    """
    for v in ("1 MB", "4 MB", *REU_SIZE_VALUES):
        if v != stock_size and v != default_size:
            return v
    raise AssertionError(  # pragma: no cover — the enum has 8 values
        f"no REU Size differs from stock {stock_size!r} and default {default_size!r}"
    )


def _observe(client: Ultimate64Client, tag: str) -> tuple[dict, tuple[bool, str]]:
    """One raw category read plus one ``get_reu_config`` read, printed."""
    cat = _category(client)
    cfg = get_reu_config(client)
    print(
        f"[{tag}] {_ITEM_REU_SIZE}={cat.get(_ITEM_REU_SIZE)!r} "
        f"{_ITEM_REU_ENABLED}={cat.get(_ITEM_REU_ENABLED)!r} "
        f"{_ITEM_CARTRIDGE}={cat.get(_ITEM_CARTRIDGE)!r} get_reu_config={cfg!r}"
    )
    # The helper is a plain read: it must agree with the raw category.
    assert cfg == (
        cat.get(_ITEM_REU_ENABLED) == "Enabled",
        str(cat.get(_ITEM_REU_SIZE, "")),
    ), f"get_reu_config disagrees with the raw category: {cfg!r} vs {cat!r}"
    return cat, cfg


# --------------------------------------------------------------------------- #
# Step 1 — quiet reads (runs without U64_ALLOW_MUTATE)                        #
# --------------------------------------------------------------------------- #

def test_reu_size_stable_across_quiet_reads(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    """``REU Size`` does not change on its own while nothing writes config.

    Explanation 1 from the issue (firmware reports a stale size) predicts a
    drift across these reads; it did not happen on U64E fw 3.15.

    Scope of what this proves: the reads are compared with the module's
    own first read, so a value that was stale *before* the module started
    and stayed stale would pass. Correctness is carried by the firmware
    source, which this test corroborates rather than establishes: the GET
    is emitted straight from the in-memory ``ConfigStore`` with no cache
    (``software/api/route_configs.cc:6-58`` — ``emit_store`` reads
    ``i->getValue()`` per item) and the same store feeds the REU hardware
    register on every PUT (``at_close_config -> effectuate ->
    set_emulation_flags``; ``components/config.h:199``,
    ``io/c64/c64.cc:270-280, 315-318``). The test corroborates; the source
    excludes a cache.
    """
    info = client.get_info()
    record_property("product", str(info.get("product")))
    record_property("firmware_version", str(info.get("firmware_version")))
    print(f"[info] {info!r}")
    print(f"[stock] {stock!r}")

    reads = []
    for i in range(_QUIET_READS):
        cat, cfg = _observe(client, f"quiet read {i + 1}/{_QUIET_READS}")
        reads.append((cat, cfg))
        if i < _QUIET_READS - 1:
            time.sleep(_QUIET_INTERVAL_S)

    sizes = [cfg[1] for _cat, cfg in reads]
    record_property("quiet_sizes", sizes)
    assert sizes == [stock[_ITEM_REU_SIZE]] * _QUIET_READS, (
        f"REU Size drifted across quiet reads: {sizes} (stock "
        f"{stock[_ITEM_REU_SIZE]!r}) — firmware-side staleness (issue #168 "
        f"explanation 1)"
    )
    for i, (cat, _cfg) in enumerate(reads, 1):
        assert _diff(stock, cat) == {}, (
            f"category changed on quiet read {i}: {_diff(stock, cat)!r}"
        )


# --------------------------------------------------------------------------- #
# Step 2 — Cartridge PUT control (mutate)                                     #
# --------------------------------------------------------------------------- #

@requires_mutate
def test_cartridge_write_does_not_move_reu_size(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    """The ``Cartridge`` item has no ``"REU"`` preset, so it cannot move ``REU Size``.

    Found on U64E fw 3.15 (and confirmed in the firmware source, branch
    ``issue-807``): ``Cartridge`` is declared ``CFG_TYPE_STRFUNC`` backed by
    ``C64::list_crts`` — a ``.crt`` *file chooser*, not an enum
    (``software/io/c64/c64.cc:73``; ``:1650`` "Always return at least the
    empty string"), and the REU is driven only by ``CFG_C64_REU_SIZE`` /
    ``CFG_C64_REU_EN`` (``c64.cc:315-316``). The 3.14-era ``"REU"`` preset
    is gone, so the issue's explanation 2 has no lever to pull on this
    firmware. The load-bearing assertions are on the item's shape: they
    FAIL if the firmware ever regrows a ``"REU"`` preset or a non-empty
    default. The ``Cartridge=""`` PUT that follows is a smoke write kept
    only to show the endpoint accepts the empty chooser value; it proves
    nothing about REU coupling by itself. It is a same-value no-op ONLY on
    a bench with no ``.crt`` selected (``current == ""``, the state this
    was characterised in). On a bench with a ``.crt`` selected the PUT
    **detaches that cartridge** — a real mutation, restored in the
    ``finally``. Other chooser values are deliberately never written: they
    attach a cartridge image.
    """
    cart = _item(client, _ITEM_CARTRIDGE)
    presets = cart.get("presets", cart.get("values"))
    current = cart.get("current")
    record_property("cartridge_presets", presets)
    record_property("cartridge_current", current)
    record_property("cartridge_default", cart.get("default"))
    print(f"[cartridge item] {cart!r}")
    assert isinstance(presets, list), f"Cartridge item has no preset list: {cart!r}"
    assert current == stock[_ITEM_CARTRIDGE]
    # Load-bearing: the .crt chooser shape. A "REU" entry here would mean
    # the firmware went back to the 3.14 enum and this module's verdict
    # (a Cartridge write cannot move REU Size) no longer holds.
    assert "REU" not in presets, (
        f'Cartridge exposes a "REU" preset again ({presets!r}) — the item is '
        f"an enum, not the .crt chooser this test characterised; re-run the "
        f"issue-#168 procedure with a real Cartridge write"
    )
    assert cart.get("default") == "", (
        f"Cartridge default is {cart.get('default')!r}, expected the empty "
        f"chooser value (c64.cc:73 declares the default as \"\")"
    )
    assert "" in presets, (
        f"Cartridge presets {presets!r} lack the empty chooser value "
        f"(c64.cc:1650 always returns at least the empty string)"
    )

    # No-op smoke write: Cartridge="" on a device whose current value is
    # already "" (asserted above via `stock`). Not a coupling test.
    before, _ = _observe(client, "before no-op Cartridge PUT")
    client.set_config_item(CAT_CART, _ITEM_CARTRIDGE, "")
    after, _ = _observe(client, 'after no-op Cartridge PUT ""')
    try:
        assert after[_ITEM_REU_SIZE] == before[_ITEM_REU_SIZE], (
            f"REU Size moved on a same-value Cartridge write: "
            f"{before[_ITEM_REU_SIZE]!r} -> {after[_ITEM_REU_SIZE]!r}"
        )
        assert after[_ITEM_REU_ENABLED] == before[_ITEM_REU_ENABLED], (
            f"RAM Expansion Unit moved on a same-value Cartridge write: "
            f"{before[_ITEM_REU_ENABLED]!r} -> {after[_ITEM_REU_ENABLED]!r}"
        )
    finally:
        if current is not None and current != "":
            client.set_config_item(CAT_CART, _ITEM_CARTRIDGE, current)
    final = _category(client)
    assert _diff(stock, final) == {}, f"category not back to stock: {_diff(stock, final)!r}"


# --------------------------------------------------------------------------- #
# Step 3 — set_reu / restore_state round-trip (mutate)                        #
# --------------------------------------------------------------------------- #

@requires_mutate
def test_set_reu_readback_is_immediate_and_restore_is_exact(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    """``set_reu`` shows up in ``REU Size`` at once; ``restore_state`` is exact.

    Rules out "the REST item reports the *applied* size while the config
    holds a *pending* one": the read-back right after the PUT already
    equals the requested size and is unchanged 2 s later, with no reset in
    between. The post-restore full-category diff against the pre-write
    ``stock`` snapshot must be empty.
    """
    snap = snapshot_state(client)
    assert snap.reu_size == stock[_ITEM_REU_SIZE]
    assert snap.reu_enabled == stock[_ITEM_REU_ENABLED]

    target = next(v for v in ("1 MB", "2 MB", "512 KB") if v != stock[_ITEM_REU_SIZE])
    assert target in REU_SIZE_VALUES
    record_property("stock_size", stock[_ITEM_REU_SIZE])
    record_property("target_size", target)

    try:
        _observe(client, "before set_reu")
        t0 = time.monotonic()
        set_reu(client, True, size=target)
        print(f"[set_reu] True, {target!r} in {time.monotonic() - t0:.3f}s")

        immediate, cfg_now = _observe(client, "immediately after set_reu")
        assert cfg_now == (True, target), (
            f"read-back right after set_reu is not the requested state: "
            f"{cfg_now!r} != {(True, target)!r} — pending-until-reset behaviour"
        )
        time.sleep(2.0)
        later, cfg_later = _observe(client, "2 s after set_reu")
        assert cfg_later == cfg_now, (
            f"REU read-back changed with no write in between: {cfg_now!r} -> {cfg_later!r}"
        )
        # On this generation Cartridge does not mirror REU state.
        record_property("cartridge_while_reu_enabled", immediate[_ITEM_CARTRIDGE])
        # The only items set_reu touched.
        assert set(_diff(stock, immediate)) <= {_ITEM_REU_SIZE, _ITEM_REU_ENABLED, _ITEM_CARTRIDGE}, (
            f"set_reu touched unexpected items: {_diff(stock, immediate)!r}"
        )
    finally:
        restore_state(client, snap)

    restored, cfg_restored = _observe(client, "after restore_state")
    assert cfg_restored == (
        stock[_ITEM_REU_ENABLED] == "Enabled",
        stock[_ITEM_REU_SIZE],
    ), f"REU state not restored: {cfg_restored!r}"
    assert _diff(stock, restored) == {}, (
        f"category differs from the pre-write snapshot after restore_state: "
        f"{_diff(stock, restored)!r}"
    )


# --------------------------------------------------------------------------- #
# Step 5 — flash vs RAM: a reload from flash moves REU Size, no config write   #
# --------------------------------------------------------------------------- #

@requires_mutate
def test_flash_reload_moves_reu_size_without_a_config_write(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    """``configs:load_from_flash`` changes ``REU Size`` with no PUT to the item.

    Firmware (branch ``issue-807``, ``software/api/route_configs.cc``): a
    config PUT "takes effect at once but lives only in memory ... until the
    device reboots ... unless ``configs:save_to_flash`` writes it" (:239,
    :329); ``load_from_flash`` "throws away the settings in memory and reads
    them back from flash" (:374) — which is also what a boot does. So a lane
    that PUT ``REU Size`` and never saved, followed by any reboot or
    power-cycle, flips the read-back with no config write anywhere.
    Measured on this bench (U64E fw 3.15, 2026-09-01 local = 02:39 UTC
    2026-09-02): RAM held ``512 KB``, flash held ``2 MB`` — the item
    default and exactly the reporter's second value.

    Mechanism (hard assertions): the per-category reload leaves the item at
    the flash value, not at what was PUT; the change is visible at once (no
    reset). The bench-state observation (flash holds the item default
    ``"2 MB"``) lives in its own test below so a changed bench can never
    hide a green mechanism. The RAM value PUT here is chosen to differ from
    both the stock size and the item default, so it cannot coincide with
    the flash value (``_pick_ram_target``).

    Blast radius: the per-category form reloads only ``C64 and Cartridge
    Settings`` (``/v1/configs/<category>:load_from_flash``; live-verified
    that ``U64 Specific Settings`` is untouched). Every item the reload
    changed is PUT back from ``stock`` in the ``finally`` — ``restore_state``
    alone is not enough here (live: flash also differed in ``Command
    Interface``, which the snapshot does not carry).
    """
    reu_item = _item(client, _ITEM_REU_SIZE)
    default_size = reu_item.get("default")
    record_property("reu_size_default", default_size)
    record_property("reu_size_ram_at_entry", stock[_ITEM_REU_SIZE])

    # Make RAM differ from whatever flash holds via a plain item PUT (no
    # save_to_flash), so the reload has something to undo even on a freshly
    # booted bench where RAM == flash.
    ram_target = _pick_ram_target(stock[_ITEM_REU_SIZE], default_size)
    try:
        client.set_config_item(CAT_CART, _ITEM_REU_SIZE, ram_target)
        _before, cfg_before = _observe(client, f"after PUT REU Size={ram_target!r}")
        assert cfg_before[1] == ram_target

        t0 = time.monotonic()
        client.load_config_from_flash(CAT_CART)
        print(f"[load_from_flash] {CAT_CART!r} in {time.monotonic() - t0:.3f}s")
        after, cfg_after = _observe(client, "immediately after load_from_flash")
        flash_size = cfg_after[1]
        record_property("reu_size_flash", flash_size)
        record_property("flash_vs_stock_diff", _diff(stock, after))
        print(f"[flash vs stock] {_diff(stock, after)!r}")

        # Mechanism: the PUT was volatile; the reload replaced it with the
        # flash value, with no write to the item and no reset.
        assert flash_size != ram_target, (
            f"REU Size still {ram_target!r} after load_from_flash — the PUT "
            f"reached flash, or the reload did not replace memory "
            f"(ram_target was chosen to differ from stock {stock[_ITEM_REU_SIZE]!r} "
            f"and default {default_size!r}, so this is not a flash coincidence)"
        )
        assert flash_size in reu_item["values"]
    finally:
        # Put back every item the reload (or our PUT) changed, from the
        # pre-write snapshot — not just the REU pair.
        _restore_category_items(client, stock, _category(client))

    restored, cfg_restored = _observe(client, "after full-category restore")
    assert cfg_restored[1] == stock[_ITEM_REU_SIZE]
    assert _diff(stock, restored) == {}, (
        f"category differs from the pre-write snapshot: {_diff(stock, restored)!r}"
    )


@requires_mutate
def test_flash_holds_the_item_default_reu_size(
    client: Ultimate64Client, stock: dict, record_property
) -> None:
    """Bench-state: flash holds ``REU Size`` = item default ``"2 MB"``.

    This is the observation that explains the reporter's second value
    (2026-09-01 local: RAM ``512 KB``, flash ``2 MB``). It is a fact about
    what has been saved to this bench's flash, not about the firmware, so a
    mismatch is reported as ``xfail`` ("bench-state changed") rather than a
    failure — it must never mask the mechanism test above. The item's
    *default* being ``"2 MB"`` is a firmware fact (``c64_config[]`` def
    index) and stays a hard assertion. Same reload/restore dance as the
    mechanism test, so it stands alone under ``-k``.
    """
    reu_item = _item(client, _ITEM_REU_SIZE)
    default_size = reu_item.get("default")
    record_property("reu_size_default", default_size)
    assert default_size == "2 MB", (
        f"REU Size item default is {default_size!r}, not the \"2 MB\" this "
        f"module documents — firmware definition changed"
    )

    ram_target = _pick_ram_target(stock[_ITEM_REU_SIZE], default_size)
    try:
        client.set_config_item(CAT_CART, _ITEM_REU_SIZE, ram_target)
        client.load_config_from_flash(CAT_CART)
        _after, cfg_after = _observe(client, "flash value after load_from_flash")
        flash_size = cfg_after[1]
        record_property("reu_size_flash", flash_size)
    finally:
        _restore_category_items(client, stock, _category(client))

    restored = _category(client)
    assert _diff(stock, restored) == {}, (
        f"category differs from the pre-write snapshot: {_diff(stock, restored)!r}"
    )
    if flash_size != default_size:
        pytest.xfail(
            f"bench-state changed: flash holds REU Size {flash_size!r}, not the "
            f"item default {default_size!r} — someone saved a non-default REU "
            f"size to flash; the mechanism test's verdict is unaffected"
        )
