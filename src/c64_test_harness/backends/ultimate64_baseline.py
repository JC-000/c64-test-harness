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
  SIDs off) and the RTC (the next PUT writes the clock chip); each entry
  carries its reason.  Categories the device does not list are skipped
  with a log line — the C64 Ultimate's set differs.
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
  **The residual, and its direction**: ``routes.cc`` was read on the 3.15
  line (``v3.15-84-g871ad034``) and whether 1.1.0 carries the same
  five-line gate is an **inference, not a read**.  Note which way that
  error runs -- counting POST-with-body only is the **permissive** side,
  not the conservative one: an uncounted attachment never advances
  ``_pending_temp_attachments``, so the budget is never reached, the
  hygiene pass never fires, and the counter reads zero while the device
  accumulates.  That is the gap to close, not the margin to rely on.  One
  cheap live check closes it: ``/Temp`` count, one ``reset_to_default``
  PUT, count again, next time somebody is at that bench.  But ``/Temp`` was not the
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
from typing import Any, Iterable

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
    "BaselineReport",
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
#: printer, LED, modem and UI stores — twelve.  ``U64 Specific Settings``
#: is included even though its ``effectuate()`` rewrites the CPU-speed
#: registers unconditionally (owner decision, #227; the C64U UCI hazard
#: is unestablished here).  Absent categories are skipped, not errors.
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
)

#: Stores the entry reset must never reset, read, PUT or otherwise touch,
#: each with the reason it is here (firmware citations are to the
#: 1541ultimate ``v3.15`` line, ``~/Documents/1541u-315preview``).  Passing
#: one as a category or in ``exempt=`` raises ``ValueError`` carrying the
#: reason, before any request.  Pinned literally by
#: ``tests/test_entry_baseline.py``.
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
        "the RTC (Year..Seconds, defaults 2015-10-13 16:52:55, rtc.cc:26-34). "
        "RtcConfigStore::effectuate is empty, so a reset only sets RAM + "
        "staleEffect and shows neither drift nor mismatch -- the RAM items "
        "already read the 2015 defaults before any reset, because "
        "at_open_config fills them from the chip only when the on-device "
        "menu opens (measured U64E 2026-09-05) -- while arming the rollback: "
        "the next PUT to the category runs at_close_config (rtc.cc:350-409), "
        "which writes every item, 2015 included, to the RTC chip"
    ),
}

#: The never-touch names as a tuple (the pre-review name; kept as the
#: public alias of :data:`BASELINE_NEVER_TOUCH`'s keys).
BASELINE_EXCLUDED_CATEGORIES: tuple[str, ...] = tuple(BASELINE_NEVER_TOUCH)

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

    Twelve categories are requested; those the device does not list are
    skipped silently.  Per category actually present: one category GET (the
    values before), one ``PUT /v1/configs/<category>:reset_to_default``,
    then one item GET per item for its ``current`` and ``default``.
    **The total request count is therefore device-dependent and has only
    been observed on the U64E** -- the C64U's category and item lists have
    never been read, so any figure quoted for the Ultimate line (such as
    the one in ``PATTERNS.md``) does not carry over to the CBM line.  Every one of
    those requests carries **no body at all**, and ``attachment_writer``
    returns ``NULL`` for a body-less request before constructing any
    ``TempfileWriter`` -- an explicit zero-length branch
    (``software/api/routes.cc:40-46``); this route's writer slot is
    ``NULL`` regardless (``route_configs.cc:473``).  So the call creates no
    ``/Temp`` attachment, on any route binding.
    **The residual, and its direction**: ``routes.cc`` was read on the 3.15
    line (``v3.15-84-g871ad034``) and whether 1.1.0 carries the same
    five-line gate is an **inference, not a read**.  Note which way that
    error runs -- counting POST-with-body only is the **permissive** side,
    not the conservative one: an uncounted attachment never advances
    ``_pending_temp_attachments``, so the budget is never reached, the
    hygiene pass never fires, and the counter reads zero while the device
    accumulates.  That is the gap to close, not the margin to rely on.  Pre-reset drift
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
