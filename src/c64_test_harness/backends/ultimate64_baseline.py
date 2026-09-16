"""Reset-on-entry to the factory-default baseline (issue #227).

Setup is verifiable, teardown is not: a killed run restores nothing, and
the next run can always reset.  So a lane that opts in starts by putting
the Ultimate 64's configuration back to the firmware's own factory
defaults and *then* asserting that it got there, instead of trusting the
previous lane's ``restore_state`` to have run.

The contract, as decided by the owner in #227:

* **The baseline is the firmware's factory default.**  Every flash on the
  bench formats the flash and resets settings, so power-on and
  ``reset_to_default`` converge on the same state.  There is no
  harness-owned table: each item map carries its own ``default``
  (``get_config_item``, #214) and the check is ``current == default`` per
  item.  Items without a ``default`` key (preset-file and info types) are
  reported, never asserted.
* **Scope of the guard.**  :func:`apply_factory_baseline` refuses the
  :data:`BASELINE_NEVER_TOUCH` names outright, in any casing, before any
  request.  Two boundaries worth knowing, both pre-existing:
  ``Ultimate64Client.reset_config_category_to_default`` type-checks its
  argument and nothing else, so **a direct client call bypasses the
  never-touch list entirely** -- the guard is in this module, not on the
  wire; and :data:`_EXCLUDED_MARKERS` is a spelling guard for those three
  names, not a network-store detector (see its own note).
* **Mechanism:** per-category ``PUT /v1/configs/{category}:reset_to_default``
  over the fixed covered set :data:`BASELINE_CATEGORIES`.  **Never** the
  global route (it iterates every store, network stores included, and each
  network store re-effectuates onto the live stack the device is reached
  over) and
  **never** a store in :data:`BASELINE_NEVER_TOUCH` — the three network
  stores, the SID socket store (its ``effectuate`` powers the socketed
  SIDs off) and the RTC (the next PUT writes the clock chip -- Ultimate
  line only; the C64 Ultimate has no ``Clock Settings`` store at all);
  each entry carries its reason, and
  :data:`BASELINE_NEVER_TOUCH_BY_GENERATION` says whether each store
  exists on each generation and what its reason was read against.
  Categories the device does not list are skipped with a log line — the
  C64 Ultimate's set differs (both devices' lists and per-category item
  counts are recorded, with their basis, in
  :data:`BASELINE_RECORDED_CATEGORY_SETS`).
* **Reset, then assert.**  On a shared device ``current != default`` at
  entry is the ordinary state whenever another lane is mid-run or just
  finished; that pre-reset drift is logged per item at INFO ("inherited
  drift") and is never a failure.  A mismatch *after* the reset means the
  reset did not take (the #204 shape: accepted, not applied) and raises
  :class:`U64BaselineError` naming category, item, current and default.
* **Memory-only.**  ``ConfigStore::reset`` sets ``staleFlash`` and writes
  nothing; a reboot reloads flash.  Applies immediately through each
  store's ``effectuate()`` (~100 ms per category); items the C64 only
  picks up at its own reset (cartridge ``.crt``, kernal, REU enable) take
  effect at the next ``reset()``, which a run does anyway.
* **Default on for the Ultimate line, off for the CBM line, off for
  unknown** (#266; it was globally opt-in before, "until the C64U is
  measured").  Two things settled it and they point opposite ways.  The
  ``/Temp`` question is answered, and **the argument is the body gate, not
  the route table**: every request this module makes carries **no body at
  all**, and ``attachment_writer`` returns ``NULL`` for a body-less request
  before it constructs any ``TempfileWriter`` -- an explicit zero-length
  branch (``software/api/routes.cc:40-46``), not a property of which route
  was hit.  For this route the writer slot is ``NULL`` anyway
  (``API_CALL(PUT, configs, reset_to_default, NULL, ...)``,
  ``route_configs.cc:473``).  So the hypothetical "a PUT that attaches"
  cannot reach a request with no body, and the reset is free even on the
  leak-prone C64 Ultimate.
  **The residual is closed by source (#436)**: the body gate was first
  read on the 3.15 line, and whether 1.1.0 carried the same five-line
  gate was for a while an inference rather than a read.  The C64U's own
  route table has since been read at tag ``1.1.0`` (``7b628eb1``), and
  **every PUT route binds NULL** there -- so no PUT can attach on 1.1.0
  whatever the body gate does, while the upload POSTs bind
  ``&attachment_writer``.  The direction of that former residual is kept
  on the record because the counting rule itself has not changed:
  counting POST-with-body only is the **permissive** side,
  not the conservative one: an uncounted attachment never advances
  ``_pending_temp_attachments``, so the budget is never reached, the
  hygiene pass never fires, and the counter reads zero while the device
  accumulates.  That direction is why the table was read rather than
  assumed.  The read establishes only that such a request *creates* an
  attachment; that accumulated attachments crash the firmware is the
  owner's account (CLAUDE.md), not something this citation shows.
  But ``/Temp`` was not the
  only hazard on that device -- it reaches the bench over WiFi whose
  reconnection after a power cycle is known unreliable, with nobody
  present -- so the default consults
  :data:`BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION` rather than being one
  global boolean.  An ``unknown`` generation (an unreadable or timed-out
  probe, #262) resolves **off**: a reset must never arm on a device the
  harness failed to identify.  Above that the switch is tri-state and
  device-independent (:func:`resolve_baseline_on_entry`): an explicit
  ``baseline_on_entry=`` wins, then ``U64_BASELINE_ON_ENTRY`` either way
  -- which is how a C64U is opted in, for someone at the bench -- then the
  generation default.  ``HarnessConfig.u64_baseline_on_entry = None`` still
  means "nobody asked".  Opting out means no requests at all, and the
  resolved value is logged at INFO on every acquire with the device, the
  generation and what decided it.  One measurement remains open and is
  *not* what any of this rests on: whether a covered store's
  ``effectuate()`` pulses the C64 reset.
* **Inside the lock.**  ``create_manager(backend="u64")`` runs the reset
  right after the ``DeviceLock`` is acquired and before the transport is
  handed out.  Calling :func:`apply_factory_baseline` directly without the
  lock gets the #194 unlocked-client notice (the same mechanism, not a
  second one); it is a notice, not a refusal.

``snapshot_state``/``restore_state`` (restore-on-exit) are untouched: they
remain the courtesy, this is the correctness mechanism.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..config import U64_BASELINE_ON_ENTRY_ENV, resolve_baseline_on_entry_env
from .ultimate64_client import Ultimate64Client, Ultimate64Error

try:
    from .device_lock import warn_unlocked_client as _warn_unlocked_client

    _HAS_DEVICE_LOCK = True
except ImportError:  # pragma: no cover
    _HAS_DEVICE_LOCK = False

_log = logging.getLogger(__name__)

__all__ = [
    "BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION",
    "BASELINE_ON_ENTRY_ENV",
    "BASELINE_CATEGORIES",
    "BASELINE_EXCLUDED_CATEGORIES",
    "BASELINE_NEVER_TOUCH",
    "BASELINE_NEVER_TOUCH_BY_GENERATION",
    "BASELINE_RECORDED_CATEGORY_SETS",
    "BASELINE_UNCLASSIFIED_CATEGORIES",
    "BaselineReport",
    "CategoryItemCount",
    "RecordedCategorySet",
    "U64BaselineError",
    "apply_factory_baseline",
    "baseline_default_for_generation",
    "baseline_on_entry_enabled",
    "resolve_baseline_on_entry",
]

#: The one environment switch, shared with ``HarnessConfig.from_env``.
#: Both paths resolve it through
#: :func:`c64_test_harness.config.resolve_baseline_on_entry_env`, so the
#: precedence is identical: the convention form
#: ``C64TEST_U64_BASELINE_ON_ENTRY`` wins when both are set.  ``1`` /
#: ``true`` / ``yes`` / ``on``, case-insensitive.
BASELINE_ON_ENTRY_ENV = U64_BASELINE_ON_ENTRY_ENV

#: What a lane gets when **neither** switch is set, **per device
#: generation** (``DeviceCapabilities.generation``).  Deliberately not a
#: single global boolean: the two lines are not equally recoverable.
#:
#: * ``"ultimate"`` (the U64E, 3.x) -- **on**.  The entry reset is bodyless
#:   throughout (category GET, bodyless ``reset_to_default`` PUT, item
#:   GETs), so it costs no ``/Temp`` attachment, and the device is on
#:   wired ethernet.
#: * ``"cbm"`` (the C64 Ultimate, 1.x) -- **off**.  Not because of
#:   ``/Temp`` (free there too, on the 3.15-route-table assumption above --
#:   unverified against 1.1.0), but because that device reaches the
#:   bench **over WiFi** and its reconnection after a power cycle is known
#:   to be unreliable; nothing on the never-touch list protects a store
#:   that a future edit adds to the covered set by mistake, and there is no
#:   remote remedy if it does not come back.  Opt in explicitly when
#:   someone is at the bench.
#: * ``"unknown"`` -- **off**.  An unreadable or timed-out probe (#262)
#:   must never arm a reset on a device the harness failed to identify;
#:   the C64U is exactly the device a slow probe mis-grades.
#:
#: Anything not listed resolves off (:func:`baseline_default_for_generation`).
BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION: dict[str, bool] = {
    "ultimate": True,
    "cbm": False,
    "unknown": False,
}

#: The stores the entry reset covers, by canonical firmware name (the
#: per-category route is a case-insensitive exact match for a name with
#: no glob characters).  Machine, SID addressing, audio, drive, tape,
#: printer, LED, modem and UI stores, plus the two C64U-only stores
#: ``Speaker Mixer`` and ``Keyboard Lighting``.  ``U64 Specific Settings``
#: is included even though its ``effectuate()`` rewrites the CPU-speed
#: registers unconditionally (owner decision, #227; the C64U UCI hazard
#: is unestablished here).  Absent categories are skipped, not errors --
#: which is what makes the two C64U-only stores no-ops on the U64E.
#:
#: ``Speaker Mixer`` and ``Keyboard Lighting`` are covered by owner decision
#: 2026-09-15 (#310), on a source reading at tag 1.1.0 -- not a measurement;
#: nobody has reset either on the device.  ``Speaker Mixer`` is compiled
#: only under ``#if U64 == 2`` (``software/u64/u64_config.cc:433-449``) and
#: its apply step only sets speaker enable, volume and pan
#: (``u64_config.cc:1164-1199``); ``Keyboard Lighting``'s rewrites the
#: keyboard LED registers (``software/u64/bling_board.cc:757-787``).
#: Neither has a network, power-rail, detection or persistent-chip
#: write-back path.  The C64U entry reset stays off by default, so they are
#: reset only on a run that opts in with ``U64_BASELINE_ON_ENTRY=1``.
#:
#: Per-store item counts, per generation and with their basis, are in
#: :data:`BASELINE_RECORDED_CATEGORY_SETS`.
#: ``SID Sockets Configuration`` and ``Clock Settings`` are **not** here
#: and can never be — see :data:`BASELINE_NEVER_TOUCH`.
BASELINE_CATEGORIES: tuple[str, ...] = (
    "C64 and Cartridge Settings",
    "U64 Specific Settings",
    "SID Addressing",
    "Audio Mixer",
    "Drive A Settings",
    "Drive B Settings",
    "SoftIEC Drive Settings",
    "Tape Settings",
    "Printer Settings",
    "LED Strip Settings",
    "Modem Settings",
    "User Interface Settings",
    "Speaker Mixer",
    "Keyboard Lighting",
)

#: Stores the entry-baseline reset never resets and never asserts, each with
#: the reason it is here (firmware citations are to the 1541ultimate
#: ``v3.15`` line, ``~/Documents/1541u-315preview``).  Passing one to
#: :func:`apply_factory_baseline` as a category or in ``exempt=`` raises
#: ``ValueError`` carrying the reason, before any request.  Pinned literally
#: by ``tests/test_entry_baseline.py``.
#:
#: **This is apply_factory_baseline's contract, not a claim that the harness
#: never writes these stores** (owner decision on #263, 2026-09-15).  The one
#: sanctioned write is the ``/Temp`` hygiene pass,
#: ``Ultimate64Client._run_temp_hygiene``: it may write exactly one item,
#: ``Network Settings > FTP File Service = Enabled``, at most once per
#: client, only for a client that has leaked attachments, and only after that
#: client's sweep failed (FTP refused is the case it exists for -- the
#: service is off by default on C64U 1.1.0).  A client that leaked nothing
#: never writes config.  The write persists until a firmware power-on and is
#: not restored, so a ``Network Settings`` state found on a device may be a
#: hygiene pass rather than anyone's decision.
BASELINE_NEVER_TOUCH: dict[str, str] = {
    "Ethernet Settings": (
        "a reset DROPS THE LEASE MID-REQUEST on a DHCP device.  "
        "ConfigStore::reset is followed by effectuate(), and "
        "NetworkInterface::effectuate_settings, on an initialised, link-up "
        "interface, calls dhcp_stop() -> dhcp_release_and_stop "
        "(lwip/src/core/ipv4/dhcp.c:1325-1390), which sends DHCP_RELEASE and "
        "then netif_set_addr(netif, IP4_ADDR_ANY4, ...): the address the "
        "REST request arrived on is zeroed and DISCOVER re-runs.  On a "
        "statically addressed device the same path takes the else branch, "
        "dhcp_stop() + netif_set_addr(my_ip, ...) -- which is why TESTS MUST "
        "NEVER CONFIGURE A STATIC ADDRESS: it is this code path, reached "
        "through a store that looks inert (pinned by "
        "tests/test_entry_baseline_default_on.py).  On the C64 Ultimate, "
        "reached over a WiFi link whose reconnection is known unreliable "
        "with nobody present, that is the device-loss path -- see the WiFi "
        "settings entry.  "
        "TWO RETRACTED READINGS, both of which looked cited and were not: "
        "(1) 'reset_to_default flips a static-addressed device to DHCP "
        "(192.168.2.64/24) and strands it' -- wrong, the device is already "
        "on DHCP and those static fields are unused factory defaults "
        "(net_config[], network_interface.cc:26-38), measured U64E "
        "2026-09-10, all five items already at default.  (2) 'the 3.15 "
        "source only re-starts DHCP when it is not already running, so the "
        "reset is a live no-op' -- that guard is REAL but it is NOT a "
        "property of 3.15: it arrived post-tag in 6b5ffc21 (upstream #805) "
        "and exists only in the v3.15-8x fork line this bench flashed onto "
        "the U64E.  Upstream (v3.14e checkout, network_interface.cc:406) "
        "and the C64U's 1.1.0 line call dhcp_stop() UNCONDITIONALLY on a "
        "link-up interface.  So the no-op holds for exactly one device on "
        "this bench and must never be generalised to the line"
    ),
    "Network Settings": (
        "reset blanks Network Password (default \"\") and the syslog server, "
        "restores Host Name to the product default, and re-ENABLES every "
        "service: Ultimate Ident/DMA, Telnet, FTP, Web and SNTP all default "
        "to 1 = Enabled (network_config.cc:15-36), and "
        "NetworkConfig::effectuate_settings restarts SNTP (:71-75).  "
        "CORRECTED 2026-09-11 from the 3.15 source: the earlier reason said "
        "the reset \"blanks ... the FTP/Telnet/Web/SNTP service flags\", "
        "which has the direction backwards -- it turns them on, which is a "
        "security-shaped change on a shared bench rather than a loss of "
        "function, and it would silently undo a deliberate service-off "
        "state.  It does not touch the addressing, so it is a milder case "
        "than Ethernet Settings; it stays never-touch for the blanked "
        "password"
    ),
    "WiFi settings": (
        "DEVICE-LOSS RISK, not a connectivity inconvenience.  The C64 "
        "Ultimate reaches the bench over WiFi, and its reconnection after a "
        "power cycle is KNOWN UNRELIABLE: the owner has seen it fail to "
        "rejoin the wireless network after a hard power cycle, saved a "
        "working WiFi configuration to flash deliberately, and confirmed the "
        "rejoin only by standing at the device (owner testimony, measured "
        "2026-09-11).  Nobody is at the bench now.  reset_to_default here "
        "would discard that saved-and-verified configuration in RAM and "
        "re-effectuate the stack the device is reached over "
        "(NetworkLWIP_WiFi::effectuate_settings, network_esp32.cc:126-147 -> "
        "NetworkInterface::effectuate_settings, network_interface.cc:364-423; "
        "the store carries the same Use DHCP / 192.168.2.64 addressing block, "
        "wifi_config[] :34-58).  If it does not come back there is no remote "
        "remedy AT ALL -- not reboot(), not FTP, not any REST route -- only "
        "someone physically present.  This is also why the entry baseline "
        "defaults OFF for the cbm generation "
        "(BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION).  Read from the 3.15 "
        "source; the C64U runs the 1.1.0 line, and on U64 == 2 the radio "
        "on/off inside that effectuate is commented out, so what a reset "
        "does to a live C64U WiFi link is unmeasured and must stay that way"
    ),
    "SID Sockets Configuration": (
        "ConfigStore::reset sets SID Socket 1/2=Disabled, then "
        "U64SidSockets::effectuate_settings (u64_config.cc:744-800) writes "
        "regulator bits 0 to the PLD SIDCTRL / I2C: the reset cuts socket "
        "power and the detection state (measured U64E, two 8580s, 2026-09-05, "
        "n=3: all six items flip Enabled->Disabled, 8580->None, 22 nF->470 pF) "
        "while the report reads clean, so the socketed SIDs are POWERED OFF "
        "with nothing to say so.  Detection never re-runs over REST (only at "
        "boot or from the on-device menu); recovery is a per-item PUT of the "
        "detected values, which re-effectuates the regulators (both 8580s "
        "alive on the OSC3 stride probe afterwards, 3/3) -- and on a 6581 "
        "bench that PUT applies socket voltage without the human 12 V "
        "approval detection waits for (u64_config.cc:698-705).  The values "
        "are detection results, not reset products ('8580'/'Enabled'/'22 nF' "
        "vs defaults 'None'/'Disabled'/'470 pF', power-cycle-stable, n=1; a "
        "6581 bench also writes the 1K Ohm Resistor items, "
        "u64_config.cc:704/708)"
    ),
    "Clock Settings": (
        "ULTIMATE LINE ONLY (read at 7f6fcb51 = v3.15-85, the U64E's flashed "
        "build: rtc_i2c.cc:79-80 registers the store, "
        "target/u64/nios2/ultimate/Makefile:67 builds it).  "
        "the RTC (Year..Seconds, defaults 2015-10-13 16:52:55, rtc.cc:26-34). "
        "RtcConfigStore::effectuate is empty, so a reset only sets RAM + "
        "staleEffect and shows neither drift nor mismatch -- the RAM items "
        "already read the 2015 defaults before any reset, because "
        "at_open_config fills them from the chip only when the on-device "
        "menu opens (measured U64E 2026-09-05) -- while arming the rollback: "
        "the next PUT to the category runs at_close_config (rtc.cc:350-409), "
        "which writes every item, 2015 included, to the RTC chip.  "
        "ON THE C64 ULTIMATE THIS STORE DOES NOT EXIST and this reason has "
        "no subject: the u64ii target at tag 1.1.0 builds rtc_dummy.cc "
        "(target/u64ii/riscv/ultimate/Makefile:70), which registers no store "
        "(read from source), and Clock Settings is absent from that device's "
        "GET /v1/configs (measured C64U fw 1.1.0, 2026-09-15, n=1, #287).  "
        "The entry stays: on the C64U its absence is harmless -- "
        "apply_factory_baseline skips unlisted categories, and refusing a "
        "name that cannot be passed costs nothing -- while removing it would "
        "unprotect the U64E, where the rollback argument above holds"
    ),
}

#: The never-touch names as a tuple (the pre-review name; kept as the
#: public alias of :data:`BASELINE_NEVER_TOUCH`'s keys).
BASELINE_EXCLUDED_CATEGORIES: tuple[str, ...] = tuple(BASELINE_NEVER_TOUCH)


@dataclass(frozen=True)
class CategoryItemCount:
    """One category's REST-visible item count, and how it is known.

    ``basis`` is ``("device-read",)``, ``("source-derived",)`` or both.  A
    device read is one bodyless ``GET /v1/configs/<category>``; a source
    derivation preprocesses the store's ``t_cfg_definition[]`` with the
    target's own build flags and counts the five types ``emit_store``
    emits (STRING, STRFUNC, STRPASS, ENUM, VALUE).  The count is a
    compile-time property of the build (items are appended only when a
    store is constructed), so ``firmware`` is part of the claim.
    """

    items: int
    basis: tuple[str, ...]
    firmware: str
    date: str


def _counted(
    counts: Mapping[str, int], *, basis: tuple[str, ...], firmware: str, date: str
) -> dict[str, CategoryItemCount]:
    return {
        cat: CategoryItemCount(items=n, basis=basis, firmware=firmware, date=date)
        for cat, n in counts.items()
    }


@dataclass(frozen=True)
class RecordedCategorySet:
    """One device's ``GET /v1/configs`` category list and item counts.

    The category set and the item counts are compile-time properties of a
    firmware build, so a record is per generation *and* firmware: a store
    appearing in a newer build's list, or an item count moving, is exactly
    what these records exist to make visible.

    ``totals`` holds ``covered`` / ``never_touch`` / ``neither`` / ``all``
    item totals over this record's categories, bucketed by
    :data:`BASELINE_CATEGORIES` and :data:`BASELINE_NEVER_TOUCH`;
    ``counts_source`` says how the counts were obtained and ``residuals``
    records what is known to disagree with them and is not resolved.
    """

    generation: str
    device: str
    firmware: str
    date: str
    source: str
    categories: frozenset[str]
    item_counts: Mapping[str, CategoryItemCount]
    totals: Mapping[str, int]
    counts_source: str
    residuals: str = ""


#: **The one place the entry baseline's category lists and item counts
#: live, per device generation.**  Docs cite this table by name instead of
#: restating its figures: the item count moved four times in three days
#: while it lived only in prose (#286, #288, #292; #342).
#:
#: Each record has its category list and source, then per category an item
#: count with basis, firmware and date, then the covered / never-touch /
#: neither / all totals.  The C64U category list is a device listing taken
#: verbatim, never projected from source; the U64E names were read directly
#: on 2026-09-15 (and earlier reconstructed from a per-category read and
#: cross-checked against an older listing and the firmware source -- its
#: ``source`` says which).  The U64E counts are device-read and match
#: source; the C64U counts are **source-derived only, unverified** until a
#: per-category read succeeds.  The counts are a derivation, not a
#: literal to copy: re-derive them with the method each record's
#: ``counts_source`` names (and a device read) rather than trusting the
#: literal copies in the tests, which only catch an edit, not a wrong count.
#:
#: ``tests/test_entry_baseline.py`` asserts that every name in every record
#: is classified (in :data:`BASELINE_CATEGORIES`, :data:`BASELINE_NEVER_TOUCH`
#: or :data:`BASELINE_UNCLASSIFIED_CATEGORIES`), so a store a device lists
#: without anyone having decided about it fails a test instead of being
#: silently never reset.  It also asserts that each record's totals agree
#: with those lists and with the sum of its per-category counts, and
#: ``tests/test_entry_baseline_table_live.py`` compares a device against
#: its record.  When a firmware update changes a device's list or counts,
#: re-read them (bodyless GETs, zero ``/Temp`` cost) and update the record's
#: firmware and date too.
BASELINE_RECORDED_CATEGORY_SETS: dict[str, RecordedCategorySet] = {
    "ultimate": RecordedCategorySet(
        generation="ultimate",
        device="U64E (Ultimate 64 Elite, 10.43.23.81)",
        firmware="3.15 (v3.15-85, 7f6fcb51)",
        date="2026-09-15",
        source=(
            "category names reconstructed from the 2026-09-12 read-only "
            "per-category read (U64E fw 3.15, DeviceLock held; #288) -- 19 "
            "categories = the covered set as it then stood (12 stores) + the "
            "five never-touch + UltiSID "
            "Configuration + Data Streams; no saved listing of the names.  "
            "Cross-checked against scripts/U64_DEVICE_PROBE.md section 5 "
            "(fw 3.14, 2026-04-05, 'all 19') and against firmware source at "
            "7f6fcb51 (v3.15-85): target/u64/nios2/ultimate/Makefile builds "
            "rtc_i2c.cc (:67, Clock Settings), network_esp32.cc (:143, WiFi "
            "settings) and data_streamer.cc (:170, Data Streams) and not "
            "bling_board.cc; Speaker Mixer is #if U64 == 2 only.  "
            "Confirmed when the names were read directly on 2026-09-15 "
            "(U64E fw 3.15, one bodyless GET /v1/configs, DeviceLock held; "
            "#287): 19 categories, identical to this record (#316); the "
            "record's date is that read's, and it reported only firmware 3.15, "
            "fpga 125, core 1.4F.  The flash record gave v3.15-85 (7f6fcb51).  "
            "After the owner's power-cycle, GET /v1/info reported "
            "bce4535e (v3.15-132) on 2026-09-15 (#292).  The counts were "
            "derived at 7f6fcb51 and carry to bce4535e by the tree diff and "
            "the device read recorded in residuals"
        ),
        categories=frozenset({
            "Audio Mixer", "SID Sockets Configuration", "UltiSID Configuration",
            "SID Addressing", "U64 Specific Settings",
            "C64 and Cartridge Settings", "Clock Settings",
            "SoftIEC Drive Settings", "Printer Settings", "Network Settings",
            "Ethernet Settings", "WiFi settings", "Tape Settings",
            "LED Strip Settings", "Drive A Settings", "Drive B Settings",
            "Data Streams", "Modem Settings", "User Interface Settings",
        }),
        item_counts=_counted(
            {
                "Audio Mixer": 21, "SID Sockets Configuration": 8,
                "UltiSID Configuration": 8, "SID Addressing": 8,
                "U64 Specific Settings": 27, "C64 and Cartridge Settings": 19,
                "Clock Settings": 7, "SoftIEC Drive Settings": 2,
                "Printer Settings": 11, "Network Settings": 14,
                "Ethernet Settings": 5, "WiFi settings": 6, "Tape Settings": 1,
                "LED Strip Settings": 8, "Drive A Settings": 14,
                "Drive B Settings": 14, "Data Streams": 4, "Modem Settings": 16,
                "User Interface Settings": 10,
            },
            basis=("device-read", "source-derived"),
            firmware="3.15 (v3.15-85, 7f6fcb51)",
            date="2026-09-15",
        ),
        totals={"covered": 151, "never_touch": 40, "neither": 12, "all": 203},
        counts_source=(
            "device-read 2026-09-15: one bodyless per-category GET on the U64E "
            "(fw 3.15, fpga 125, core 1.4F; DeviceLock held; #342).  "
            "The same per-category counts were reproduced from firmware source "
            "at 7f6fcb51 (v3.15-85) -- first in #288's adversarial review, "
            "against the 2026-09-12 read-only count, and again for #342: each "
            "store's t_cfg_definition[] run through cc -E -P with the U64E "
            "build flags (-DU64=1 -DDEVELOPER=0 -DCLOCK_FREQ=66666667, "
            "target/u64/nios2/ultimate/Makefile:251), counting the five "
            "REST-visible types STRING, STRFUNC, STRPASS, ENUM and VALUE "
            "(route_configs.cc emit_store).  Two instruments, no per-category "
            "delta"
        ),
        residuals=(
            "Unexplained residual (#292): #276 records \"201 items compared\" "
            "(category scope and firmware build not recorded) on the U64E on "
            "2026-09-10, against this record's all-category total of 203.  The "
            "candidates are a different counting basis or a different firmware "
            "build: firmware source rules out drift within one build -- store "
            "items are appended only when a store is constructed, and the REST "
            "listing emits every item of the five value types -- except for "
            "stores registered at runtime (the monitor-bookmarks store, the "
            "per-device SID stores).  Nobody has established why.  Do not "
            "average the two figures, and do not drop one.  "
            "#292 investigation, 2026-09-15 (U64E, bodyless GETs, DeviceLock "
            "held, n=1): on the device freshly power-cycled by the owner, "
            "GET /v1/configs and one per-category GET matched this record in "
            "every category, and a per-item GET showed that every one of the "
            "203 items carries both current and default, so an item-level "
            "'no default key' basis cannot produce #276's figure.  GET /v1/info "
            "reported git_commit_hash bce4535e (v3.15-132-gbce4535e in "
            "~/Documents/1541ultimate), not this record's 7f6fcb51, which is "
            "not its ancestor.  In the tree diff 7f6fcb51 -> bce4535e (not a "
            "linear range) no added or removed line under software/ or "
            "target/ carries CFG_TYPE_, t_cfg_definition or register_store, "
            "and route_configs.cc and config.cc are unchanged (config.h gains "
            "only effectuate_registered_settings).  That grep cannot see "
            "compile-gate changes such as new #if U64 == 2 blocks, which do not "
            "reach the U64E's -DU64=1 build; the device read is the authority, "
            "identical in every category.  203 was also recorded on "
            "2026-09-05 (ce4b0af).  #276's figure appears only on 2026-09-10, "
            "after a flash whose build was not recorded, and no counting basis "
            "for it is evidenced.  The #276 script was not preserved, so the basis "
            "is unrecoverable and #292 is closed on that footing; 203 is the "
            "device-read figure"
        ),
    ),
    "cbm": RecordedCategorySet(
        generation="cbm",
        device="C64U (C64 Ultimate, 10.53.21.158)",
        firmware="1.1.0 (tag 1.1.0 = 7b628eb1, u64ii target)",
        date="2026-09-15",
        source=(
            "one bodyless GET /v1/configs, DeviceLock held, zero /Temp cost, "
            "n=1, taken by the supervisor and recorded in #287; item counts "
            "per category NOT read"
        ),
        categories=frozenset({
            "Audio Mixer", "Speaker Mixer", "SID Sockets Configuration",
            "UltiSID Configuration", "SID Addressing", "U64 Specific Settings",
            "C64 and Cartridge Settings", "SoftIEC Drive Settings",
            "Printer Settings", "Network Settings", "Ethernet Settings",
            "WiFi settings", "Tape Settings", "LED Strip Settings",
            "Keyboard Lighting", "Drive A Settings", "Drive B Settings",
            "Data Streams", "Modem Settings", "User Interface Settings",
        }),
        item_counts=_counted(
            {
                "Audio Mixer": 20, "Speaker Mixer": 11,
                "SID Sockets Configuration": 8, "UltiSID Configuration": 8,
                "SID Addressing": 8, "U64 Specific Settings": 22,
                "C64 and Cartridge Settings": 19, "SoftIEC Drive Settings": 3,
                "Printer Settings": 11, "Network Settings": 14,
                "Ethernet Settings": 5, "WiFi settings": 5, "Tape Settings": 1,
                "LED Strip Settings": 7, "Keyboard Lighting": 7,
                "Drive A Settings": 13, "Drive B Settings": 13,
                "Data Streams": 4, "Modem Settings": 16,
                "User Interface Settings": 6,
            },
            basis=("source-derived",),
            firmware="1.1.0 (tag 1.1.0 = 7b628eb1, u64ii target)",
            date="2026-09-15",
        ),
        totals={"covered": 157, "never_touch": 32, "neither": 12, "all": 201},
        counts_source=(
            "SOURCE-DERIVED ONLY, UNVERIFIED: the C64U's item lists have never "
            "been read on the device (a bodyless per-category GET costs zero "
            "/Temp attachments; the device was unreachable when last "
            "attempted).  Derived 2026-09-15 for #342 from firmware source at "
            "tag 1.1.0 (7b628eb1): each store's t_cfg_definition[] run through "
            "cc -E -P with the u64ii build flags from "
            "target/u64ii/riscv/ultimate/Makefile:226 (-DRISCV -DU64=2 "
            "-DUSB2513 -DOS -DCLOCK_FREQ=100000000 -DFP_SUPPORT=1 "
            "-DCOMMODORE=1; no DEVELOPER), counting the five REST-visible types "
            "STRING, STRFUNC, STRPASS, ENUM and VALUE.  Drive A and Drive B "
            "each copy the one c1541_config array (drive/c1541.cc:44-65; the "
            "constructor edits defaults, not the item list).  The same script "
            "reproduces the U64E record exactly at 7f6fcb51, per category.  "
            "The 194 quoted for this device before #342 was the same projection "
            "with Keyboard Lighting (7 items, u64/bling_board.cc:42-51) left "
            "out: that store calls cfg->hide(), which hides it from the "
            "on-device menu only -- emit_store and GET /v1/configs do not "
            "consult it, so it is REST-visible and counts.  The flags matter: "
            "without -DCOMMODORE=1 User Interface Settings has 8 items, not 6"
        ),
        residuals=(
            "This record's all-category total equals #276's U64E figure in the "
            "#292 residual by coincidence -- a different device, firmware and "
            "basis -- and bears on that residual not at all"
        ),
    ),
}

#: Categories a recorded device lists that are **in neither list**: not in
#: the covered set (so the entry reset never resets them) and not refused
#: as an argument either.  Recorded explicitly so "nobody has decided" is
#: a stated fact rather than an absence.  Moving one into
#: :data:`BASELINE_CATEGORIES` or :data:`BASELINE_NEVER_TOUCH` is an owner
#: decision (#227), not an edit to make because a test went green.
BASELINE_UNCLASSIFIED_CATEGORIES: dict[str, str] = {
    "UltiSID Configuration": (
        "both generations; outside the #227 covered set and never reviewed "
        "for it.  No firmware reading recorded here"
    ),
    "Data Streams": (
        "both generations; outside the #227 covered set and never reviewed "
        "for it.  No firmware reading recorded here"
    ),
}

#: Per never-touch store, per generation: whether the store exists there
#: and what its reason was read against.  Every value starts with
#: ``"present"`` or ``"absent"``, and ``tests/test_entry_baseline.py``
#: checks that word against :data:`BASELINE_RECORDED_CATEGORY_SETS`.  All
#: five reasons in :data:`BASELINE_NEVER_TOUCH` were written from the 3.15
#: line (``~/Documents/1541u-315preview``); on the C64U only *presence* is
#: measured, and a reason has been re-read against the 1.1.0 source only
#: where its value says so.
BASELINE_NEVER_TOUCH_BY_GENERATION: dict[str, dict[str, str]] = {
    "Ethernet Settings": {
        "ultimate": "present; reason read from the 3.15 line",
        "cbm": (
            "present (measured 2026-09-15); reason read from the 3.15 line -- "
            "its note that 1.1.0 calls dhcp_stop() unconditionally is the "
            "only part stated for this line"
        ),
    },
    "Network Settings": {
        "ultimate": "present; reason read from the 3.15 line",
        "cbm": (
            "present (measured 2026-09-15); reason read from the 3.15 line, "
            "not re-read against 1.1.0"
        ),
    },
    "WiFi settings": {
        "ultimate": "present; reason read from the 3.15 line",
        "cbm": (
            "present (measured 2026-09-15); the device-loss reason is C64U "
            "owner testimony (2026-09-11), its code path read from 3.15"
        ),
    },
    "SID Sockets Configuration": {
        "ultimate": "present; reason read from the 3.15 line, measured U64E n=3",
        "cbm": (
            "present (measured 2026-09-15); registered at tag 1.1.0 "
            "software/u64/u64_config.cc:456, effectuate at :673 -- the "
            "power-off consequence was measured on the U64E only"
        ),
    },
    "Clock Settings": {
        "ultimate": "present; reason read at 7f6fcb51 (rtc_i2c.cc, rtc.cc)",
        "cbm": (
            "absent (measured 2026-09-15; source: tag 1.1.0 builds "
            "rtc_dummy.cc, target/u64ii/riscv/ultimate/Makefile:70) -- the "
            "entry protects nothing here and is kept for the U64E"
        ),
    },
}

#: Extra spelling guard for the three named stores: a caller-supplied
#: ``Ethernet Settings 2`` or ``WiFi Client Settings`` is refused too.
#:
#: **This is not a general network-store filter and must not be described
#: as one.**  It matches four substrings, so every plausibly network-named
#: store that does not contain them is accepted -- measured against the
#: live validator 2026-09-11: ``LAN Settings``, ``IP Configuration``,
#: ``TCP/IP``, ``Wireless``, ``ESP32 Settings`` and ``Modem Settings`` all
#: pass.  A store that must never be reset belongs in
#: :data:`BASELINE_NEVER_TOUCH` by name; this list only stops a near-miss
#: spelling of one already there.
#:
#: There is already one accepted store that touches the network stack, and
#: it is in :data:`BASELINE_CATEGORIES` on purpose: ``Modem Settings``.
#: ``Modem::effectuate_settings`` ends at ``software/io/acia/modem.cc:889-890``
#: with ``listenerSocket->Start(newPort)`` -- it binds a listener on a
#: separate port.  It does **not** call ``dhcp_stop``/``netif_set_addr``,
#: does not touch the interface, and does not disturb the REST path, so it
#: is not the device-loss shape the never-touch list exists for.  Reviewed
#: and kept 2026-09-11; the point of recording it here is that "the marker
#: scan keeps network stores out of the covered set" is false, and the
#: covered set is the thing to check when widening it.
_EXCLUDED_MARKERS: tuple[str, ...] = ("ethernet", "network", "wifi", "wi-fi")


def baseline_default_for_generation(generation: str | None) -> bool:
    """The default for a device of this generation; unknown resolves off.

    Anything that is not a generation name this module has decided about
    -- ``None``, a non-string, a generation a future firmware line
    introduces -- is ``False``.  The failure mode of guessing ``True`` is a
    config reset on an unattended device reached over a link that may not
    come back; the failure mode of guessing ``False`` is a lane inheriting
    the previous lane's config.  Those are not comparable.
    """
    if not isinstance(generation, str):
        return False
    return BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION.get(generation, False)


def resolve_baseline_on_entry(
    generation: str | None = None, *, requested: bool | None = None
) -> tuple[bool, str]:
    """Resolve the entry-reset switch, with the reason, for the INFO line.

    Precedence, highest first:

    1. *requested* -- an explicit ``baseline_on_entry=`` from the caller.
    2. The environment: ``C64TEST_U64_BASELINE_ON_ENTRY`` wins, else
       :data:`BASELINE_ON_ENTRY_ENV`.  Same parser as
       ``HarnessConfig.from_env``, so one name opts both paths in or out.
    3. :func:`baseline_default_for_generation` for *generation*.

    Resolved at call time, not import time, so a long-lived process can
    flip the env var, and per acquire, so the generation is the one the
    device actually reported.

    :returns: ``(enabled, reason)`` -- the reason is a short phrase naming
        which of the three decided it, for the acquire-time log.  A default
        that varies by hardware has to say so out loud or it becomes "it
        worked on my device" a month later.
    """
    if requested is not None:
        return bool(requested), f"explicit baseline_on_entry={bool(requested)}"
    asked = resolve_baseline_on_entry_env()
    if asked is not None:
        return asked, f"{BASELINE_ON_ENTRY_ENV}={'on' if asked else 'off'}"
    enabled = baseline_default_for_generation(generation)
    return enabled, (
        f"default for generation {generation!r}"
        + ("" if enabled else " (only the 'ultimate' line defaults on)")
    )


def baseline_on_entry_enabled(generation: str | None = None) -> bool:
    """:func:`resolve_baseline_on_entry` without the reason.

    Called with no *generation* the answer is the environment's, or off --
    which is the conservative reading and the right one for any caller
    that does not have a device in hand.
    """
    enabled, _why = resolve_baseline_on_entry(generation)
    return enabled


# --------------------------------------------------------------------------- #
# Report / error                                                              #
# --------------------------------------------------------------------------- #

#: category -> item -> (value, default)
_ItemMap = dict[str, dict[str, tuple[Any, Any]]]


@dataclass
class BaselineReport:
    """What :func:`apply_factory_baseline` found and did.

    ``drifted``
        Items whose ``current`` differed from ``default`` *before* the
        reset — the previous lane's leftovers.  Logged, never a failure.
        ``category -> item -> (current_before, default)``.
    ``reset``
        Categories the firmware reports as reset, in request order.
        Empty on ``dry_run``.
    ``mismatched``
        Items still ``current != default`` *after* the reset.  Non-empty
        means the reset did not take; :func:`apply_factory_baseline`
        raises :class:`U64BaselineError` carrying this report.
        ``category -> item -> (current_after, default)``.
    ``skipped``
        Covered categories the device does not list (``GET /v1/configs``).
    ``unasserted``
        Per category, the items whose map carries no ``default`` key
        (preset-file / info types) — reported, not compared.
    ``detection_derived``
        Items the caller passed in ``exempt=`` (the detection-derived
        class: values the firmware measures rather than resets), as read
        after the reset, with their default — listed whatever they read,
        never compared, never PUT.
        ``category -> item -> (current_after, default)``.  Kept apart
        from ``mismatched`` on purpose: one is a detection result, the
        other a reset that did not take.
    ``dry_run``
        ``True`` when nothing was written.
    """

    drifted: _ItemMap = field(default_factory=dict)
    reset: tuple[str, ...] = ()
    mismatched: _ItemMap = field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    unasserted: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dry_run: bool = False
    detection_derived: _ItemMap = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """``True`` when nothing mismatched after the reset."""
        return not self.mismatched

    def drifted_items(self) -> list[tuple[str, str]]:
        """``(category, item)`` pairs that drifted before the reset."""
        return [(cat, item) for cat, items in self.drifted.items() for item in items]

    def mismatched_items(self) -> list[tuple[str, str]]:
        """``(category, item)`` pairs still off after the reset."""
        return [(cat, item) for cat, items in self.mismatched.items() for item in items]

    def detection_derived_items(self) -> list[tuple[str, str]]:
        """``(category, item)`` pairs reported as detection-derived."""
        return [(cat, item) for cat, items in self.detection_derived.items() for item in items]

    def summary(self) -> str:
        """One line for a log: counts, plus the mismatches by name."""
        parts = [
            f"{len(self.reset)} categor{'y' if len(self.reset) == 1 else 'ies'} reset",
            f"{len(self.drifted_items())} item(s) drifted before",
            f"{len(self.mismatched_items())} still off after",
        ]
        if self.detection_derived:
            parts.append(
                f"{len(self.detection_derived_items())} detection-derived (not compared)"
            )
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped (absent): {', '.join(self.skipped)}")
        if self.dry_run:
            parts.append("dry run")
        return "; ".join(parts)


class U64BaselineError(Ultimate64Error):
    """The per-category reset did not take for at least one item.

    Raised only after every covered category has been reset and read, so
    the rest of the device is at baseline whatever this one item did.
    ``mismatched`` is ``category -> item -> (current, default)``;
    ``report`` is the full :class:`BaselineReport`.
    """

    def __init__(self, report: BaselineReport, *, message: str | None = None) -> None:
        self.report = report
        self.mismatched = report.mismatched
        if message is None:
            detail = "; ".join(
                f"{cat!r}/{item!r}: current={cur!r} default={default!r}"
                for cat, items in report.mismatched.items()
                for item, (cur, default) in items.items()
            )
            message = (
                f"reset did not take: {len(report.mismatched_items())} item(s) "
                f"still differ from the firmware default after the per-category "
                f"reset — {detail}.  If an item's value is derived from "
                f"detection or hardware state (never equals its default on this "
                f"device, stable across a power-cycle), it is not a failed reset: "
                f"exempt it for one run with apply_factory_baseline(..., "
                f"exempt=[(category, item)]); if its whole store is a hardware "
                f"store (a reset there acts on the hardware, as the SID socket "
                f"store and the RTC do), it belongs in BASELINE_NEVER_TOUCH in "
                f"ultimate64_baseline.py with its reason and the measurement "
                f"(pinned by tests/test_entry_baseline.py).  Items exempt in this "
                f"run: {len(report.detection_derived_items())}."
            )
        super().__init__(message)


# --------------------------------------------------------------------------- #
# The entry reset                                                             #
# --------------------------------------------------------------------------- #

def _validate_categories(categories: Iterable[str]) -> tuple[str, ...]:
    """Refuse globs and anything that could reach a network store.

    Checked before the first request so a bad list costs nothing on the
    wire.  The firmware route is a *pattern* match, so ``*`` is the global
    reset by another name and ``Network*`` would widen to the excluded
    stores; neither is ever sent.
    """
    out: list[str] = []
    never = {c.lower(): (c, why) for c, why in BASELINE_NEVER_TOUCH.items()}
    for cat in categories:
        if not isinstance(cat, str) or not cat.strip():
            raise ValueError(f"category must be a non-empty string, got {cat!r}")
        if "*" in cat or "?" in cat:
            raise ValueError(
                f"category {cat!r} is a pattern; the entry reset never sends a "
                f"glob (the firmware route would match every store it covers)"
            )
        folded = cat.lower()
        if folded in never:
            name, why = never[folded]
            raise ValueError(
                f"category {name!r} is never touched by the entry reset: {why} "
                f"(issue #227; never-touch set: {BASELINE_EXCLUDED_CATEGORIES!r})"
            )
        if any(m in folded for m in _EXCLUDED_MARKERS):
            raise ValueError(
                f"category {cat!r} is a near-miss spelling of a never-touch "
                f"network store (matched marker in {_EXCLUDED_MARKERS!r}); those "
                f"stores re-effectuate onto the live stack the device is "
                f"reached over — issue #227; never-touch set: "
                f"{BASELINE_EXCLUDED_CATEGORIES!r}.  Note this marker check is "
                f"a spelling guard, not a general network-store filter: a "
                f"differently named network store would be accepted, so widen "
                f"BASELINE_CATEGORIES by review, not by trusting this."
            )
        out.append(cat)
    if not out:
        raise ValueError("categories must not be empty")
    return tuple(out)


def _resolve_present(name: str, listed: list[str]) -> str | None:
    """Return the device's spelling of *name*, or ``None`` if not listed."""
    if name in listed:
        return name
    folded = [c for c in listed if c.lower() == name.lower()]
    return folded[0] if len(folded) == 1 else None


def _unwrap_category(resp: Any, category: str) -> dict[str, Any]:
    if not isinstance(resp, dict):
        raise Ultimate64Error(
            f"GET config category {category!r}: expected object, got "
            f"{type(resp).__name__}"
        )
    errors = resp.get("errors")
    if errors:
        raise Ultimate64Error(
            f"GET config category {category!r}: device reported errors {errors!r}"
        )
    inner = resp.get(category)
    if inner is None:
        # Firmware keys the response by its canonical name; tolerate case.
        for key, value in resp.items():
            if key != "errors" and isinstance(key, str) and key.lower() == category.lower():
                inner = value
                break
    if not isinstance(inner, dict):
        raise Ultimate64Error(
            f"GET config category {category!r}: category missing from response "
            f"(keys: {sorted(k for k in resp if isinstance(k, str))!r})"
        )
    return inner


def _differs(value: Any, default: Any) -> bool:
    """``current`` vs ``default`` as the firmware emits them.

    Both come from the same ``ConfigItem`` (``route_configs.cc``
    ``emit_store``): enums as the choice string, ranges as ints, free
    strings as strings.  A plain ``!=`` is the comparison; the ``str``
    fallback only papers over an int/str split between the category
    GET (bare values) and the item GET (the map) if a firmware ever
    emits them differently.
    """
    if value == default:
        return False
    return str(value) != str(default)


def _validate_exempt(exempt: Iterable[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for entry in exempt:
        if (not isinstance(entry, tuple) or len(entry) != 2
                or not all(isinstance(x, str) and x for x in entry)):
            raise ValueError(
                f"exempt entries are (category, item) string pairs, got {entry!r}"
            )
        _validate_categories((entry[0],))   # never a network store, never a glob
        out.add(entry)
    return frozenset(out)


def apply_factory_baseline(
    client: Ultimate64Client,
    *,
    categories: Iterable[str] = BASELINE_CATEGORIES,
    dry_run: bool = False,
    exempt: Iterable[tuple[str, str]] = (),
) -> BaselineReport:
    """Reset the covered categories to factory default, then assert it.

    Every :data:`BASELINE_CATEGORIES` store is requested; those the device
    does not list are skipped with a log line.  Per category actually
    present: one category GET (the values before), one
    ``PUT /v1/configs/<category>:reset_to_default``, then one item GET per
    item for its ``current`` and ``default``.
    **The total request count is therefore device-dependent and has only
    been observed on the U64E** -- the C64U's category list was read once
    (2026-09-15, 20 categories) but its item lists have never been read, so
    any figure quoted for the Ultimate line does not carry over to the CBM
    line.  Both generations' per-category item counts, with their basis
    (device-read on the U64E, source-derived only on the C64U), are in
    :data:`BASELINE_RECORDED_CATEGORY_SETS`.  Every one of
    those requests carries **no body at all**, and ``attachment_writer``
    returns ``NULL`` for a body-less request before constructing any
    ``TempfileWriter`` -- an explicit zero-length branch
    (``software/api/routes.cc:40-46``); this route's writer slot is
    ``NULL`` regardless (``route_configs.cc:473``).  So the call creates no
    ``/Temp`` attachment, on any route binding.
    **The residual is closed by source (#436)**: the body gate was first
    read on the 3.15 line, and whether 1.1.0 carried the same five-line
    gate was for a while an inference rather than a read.  The C64U's own
    route table has since been read at tag ``1.1.0`` (``7b628eb1``), and
    **every PUT route binds NULL** there -- so no PUT can attach on 1.1.0
    whatever the body gate does, while the upload POSTs bind
    ``&attachment_writer``.  The direction of that former residual is kept
    on the record because the counting rule itself has not changed:
    counting POST-with-body only is the **permissive** side,
    not the conservative one: an uncounted attachment never advances
    ``_pending_temp_attachments``, so the budget is never reached, the
    hygiene pass never fires, and the counter reads zero while the device
    accumulates.  That direction is why the table was read rather than
    assumed.  The read establishes only that such a request *creates* an
    attachment; that accumulated attachments crash the firmware is the
    owner's account (CLAUDE.md), not something this citation shows.
    Pre-reset drift
    is logged at INFO per item; a post-reset mismatch raises
    :class:`U64BaselineError` after every category has been processed.

    Items in *exempt* are **not compared** (the detection-derived class:
    a value the firmware measures rather than resets); they are reported
    under ``report.detection_derived`` with current and default.  Their
    category is still reset.  Nothing is ever PUT by this function.  A
    store whose reset itself acts on hardware is not an exemption case
    but a :data:`BASELINE_NEVER_TOUCH` case, and is refused here.

    :param client: connected :class:`Ultimate64Client` — hold the device's
        ``DeviceLock`` (``create_manager(backend="u64")`` does, and runs
        this for you when opted in).  Without it the #194 unlocked-client
        notice is logged; nothing else changes.
    :param categories: the stores to reset; defaults to
        :data:`BASELINE_CATEGORIES`.  Globs and the network stores are
        refused with ``ValueError`` before any request.
    :param dry_run: read and report only — no reset is sent, ``reset`` is
        empty and ``mismatched`` is empty (nothing was asserted); the
        ``drifted`` map shows what a real run would have reset.
    :param exempt: ``(category, item)`` pairs to list instead of compare
        for this call — a one-off for a script on hardware with a
        detection-derived item in a covered store.  Refused for the
        never-touch stores and globs.
    :returns: :class:`BaselineReport`.
    :raises ValueError: a glob or an excluded category in *categories*
        or *exempt*.
    :raises U64BaselineError: an item still reads ``current != default``
        after its category was reset, or a listed category the firmware
        reports it did not reset.
    :raises Ultimate64Error: wire / protocol failures from the client.
    """
    wanted = _validate_categories(categories)
    exempt_pairs = _validate_exempt(exempt)

    host = getattr(client, "host", None)
    if _HAS_DEVICE_LOCK and isinstance(host, str) and host:
        _warn_unlocked_client(host, what="apply_factory_baseline", logger=_log)

    listed = [str(c) for c in client.list_configs()]

    drifted: _ItemMap = {}
    mismatched: _ItemMap = {}
    detection_derived: _ItemMap = {}
    unasserted: dict[str, tuple[str, ...]] = {}
    reset_done: list[str] = []
    skipped: list[str] = []
    not_reset: list[str] = []

    for requested in wanted:
        category = _resolve_present(requested, listed)
        if category is None:
            skipped.append(requested)
            _log.info(
                "entry baseline: category %r not on this device — skipped "
                "(present: %d categories)", requested, len(listed),
            )
            continue

        before = _unwrap_category(client.get_config_category(category), category)

        if not dry_run:
            names = client.reset_config_category_to_default(category)
            if not names:
                # Listed by /v1/configs but the reset matched nothing:
                # the store did not reset and a post-read would assert on
                # stale state.  Report it with the mismatches.
                not_reset.append(category)
                _log.warning(
                    "entry baseline: reset_to_default of %r matched no store "
                    "on the device", category,
                )
            else:
                reset_done.append(category)
                _log.debug("entry baseline: reset %r -> %r", category, names)

        no_default: list[str] = []
        for item in before:
            item_map = client.get_config_item(category, item)
            if not isinstance(item_map, dict) or "default" not in item_map:
                no_default.append(item)
                continue
            default = item_map["default"]
            current = item_map.get("current")
            if (category, item) in exempt_pairs or (requested, item) in exempt_pairs:
                # Caller-exempt (detection-derived class): what the firmware
                # measured, not what a reset produces.  Listed, never compared.
                detection_derived.setdefault(category, {})[item] = (current, default)
                continue
            if _differs(before[item], default):
                drifted.setdefault(category, {})[item] = (before[item], default)
            if dry_run:
                continue
            if _differs(current, default):
                mismatched.setdefault(category, {})[item] = (current, default)
        if no_default:
            unasserted[category] = tuple(no_default)

    for category, items in drifted.items():
        for item, (value, default) in items.items():
            _log.info(
                "entry baseline: inherited drift %r/%r: current=%r default=%r%s",
                category, item, value, default,
                " (reset)" if not dry_run else " (dry run, left as is)",
            )

    for category, items in detection_derived.items():
        for item, (value, default) in items.items():
            _log.debug(
                "entry baseline: detection-derived %r/%r: current=%r default=%r "
                "(not compared)", category, item, value, default,
            )

    report = BaselineReport(
        drifted=drifted,
        reset=tuple(reset_done),
        mismatched=mismatched,
        skipped=tuple(skipped),
        unasserted=unasserted,
        dry_run=dry_run,
        detection_derived=detection_derived,
    )
    _log.info("entry baseline: %s", report.summary())

    if not_reset:
        raise U64BaselineError(
            report,
            message=(
                "reset did not take: the firmware reports no store reset for "
                f"{not_reset!r} although /v1/configs lists them"
                + (f"; and {report.summary()}" if mismatched else "")
            ),
        )
    if mismatched:
        raise U64BaselineError(report)
    return report
