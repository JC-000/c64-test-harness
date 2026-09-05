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
* **Mechanism:** per-category ``PUT /v1/configs/{category}:reset_to_default``
  over the fixed covered set :data:`BASELINE_CATEGORIES`.  **Never** the
  global route (it iterates every store, network stores included, and a
  static-addressed device would flip to DHCP and be stranded) and
  **never** ``Ethernet Settings`` / ``Network Settings`` / the WiFi store
  (:data:`BASELINE_EXCLUDED_CATEGORIES`).  Categories the device does not
  list are skipped with a log line — the C64 Ultimate's set differs.
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
* **Opt-in** until two measurements land (whether any store's
  ``effectuate()`` pulses the C64 reset; the C64U WiFi store):
  ``U64_BASELINE_ON_ENTRY=1`` or ``HarnessConfig.u64_baseline_on_entry``
  (TOML ``[u64] baseline_on_entry = true``).  Off by default, and off
  means no requests at all.
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

from ..config import U64_BASELINE_ON_ENTRY_ENV, env_flag
from .ultimate64_client import Ultimate64Client, Ultimate64Error

try:
    from .device_lock import warn_unlocked_client as _warn_unlocked_client

    _HAS_DEVICE_LOCK = True
except ImportError:  # pragma: no cover
    _HAS_DEVICE_LOCK = False

_log = logging.getLogger(__name__)

__all__ = [
    "BASELINE_ON_ENTRY_ENV",
    "BASELINE_CATEGORIES",
    "BASELINE_EXCLUDED_CATEGORIES",
    "DETECTION_DERIVED_ITEMS",
    "BaselineReport",
    "U64BaselineError",
    "apply_factory_baseline",
    "baseline_on_entry_enabled",
]

#: The one environment switch, shared with ``HarnessConfig.from_env``
#: (which also honours its ``C64TEST_U64_BASELINE_ON_ENTRY`` convention
#: form, winning when both are set).  ``1`` / ``true`` / ``yes`` / ``on``,
#: case-insensitive.
BASELINE_ON_ENTRY_ENV = U64_BASELINE_ON_ENTRY_ENV

#: The stores the entry reset covers, by canonical firmware name (the
#: per-category route is a case-insensitive exact match for a name with
#: no glob characters).  Machine, SID, audio, drive, tape, printer, clock,
#: LED, modem and UI stores.  ``U64 Specific Settings`` is included even
#: though its ``effectuate()`` rewrites the CPU-speed registers
#: unconditionally (owner decision, #227; the C64U UCI hazard is
#: unestablished here).  Absent categories are skipped, not errors.
BASELINE_CATEGORIES: tuple[str, ...] = (
    "C64 and Cartridge Settings",
    "U64 Specific Settings",
    "SID Addressing",
    "SID Sockets Configuration",
    "Audio Mixer",
    "Drive A Settings",
    "Drive B Settings",
    "SoftIEC Drive Settings",
    "Tape Settings",
    "Printer Settings",
    "Clock Settings",
    "LED Strip Settings",
    "Modem Settings",
    "User Interface Settings",
)

#: Stores the entry reset must never reset, read or otherwise touch.
#: Resetting ``Ethernet Settings`` flips a static device to DHCP;
#: ``Network Settings`` reset blanks the password, hostname and service
#: flags; the C64U's WiFi store has not been read.  Any category whose
#: name contains one of :data:`_EXCLUDED_MARKERS` is refused too, so a
#: future firmware store named e.g. ``Ethernet Settings 2`` cannot slip
#: in through a caller-supplied ``categories``.
BASELINE_EXCLUDED_CATEGORIES: tuple[str, ...] = (
    "Ethernet Settings",
    "Network Settings",
    "WiFi settings",
)

_EXCLUDED_MARKERS: tuple[str, ...] = ("ethernet", "network", "wifi", "wi-fi")

#: ``(category, item)`` pairs whose value the firmware **derives from SID
#: detection at boot**.  They never equal ``default`` on a device with
#: SIDs fitted, so ``current == default`` is the wrong assertion for them:
#: the entry reset still resets their category, but they are reported
#: (``BaselineReport.detection_derived``, with current and default) and
#: **never compared and never PUT**.
#:
#: Measured on the U64E (fw 3.15 fork, two 8580s fitted) 2026-09-05, lock
#: held: these six read ``'8580'`` (default ``'None'``), ``'Enabled'``
#: (``'Disabled'``) and ``'22 nF'`` (``'470 pF'``), identical before and
#: after a physical power-cycle (n=1), while every other one of the 203
#: items equalled its default.  Whether a reset of the category moves
#: them transiently before the firmware re-detects is being measured;
#: exempting them from the comparison is correct either way.
#:
#: The set is pinned literally by ``tests/test_entry_baseline.py``: an
#: exemption masks a real failure, so it grows only with a measurement
#: of the same grade.  A one-off exemption for a script goes through
#: ``apply_factory_baseline(..., exempt=[...])`` instead.
DETECTION_DERIVED_ITEMS: frozenset[tuple[str, str]] = frozenset({
    ("SID Sockets Configuration", "SID Detected Socket 1"),
    ("SID Sockets Configuration", "SID Detected Socket 2"),
    ("SID Sockets Configuration", "SID Socket 1"),
    ("SID Sockets Configuration", "SID Socket 2"),
    ("SID Sockets Configuration", "SID Socket 1 Capacitors"),
    ("SID Sockets Configuration", "SID Socket 2 Capacitors"),
})


def baseline_on_entry_enabled() -> bool:
    """Whether :data:`BASELINE_ON_ENTRY_ENV` asks for the entry reset.

    Read at call time (not import time) so tests and long-lived processes
    can flip it.  Same parser as ``HarnessConfig.from_env``.
    """
    return env_flag(BASELINE_ON_ENTRY_ENV) is True


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
        Items in :data:`DETECTION_DERIVED_ITEMS` (or the caller's
        ``exempt=``) as read after the reset, with their default —
        listed whatever they read, never compared, never PUT.
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
                f"detection at boot (never equals its default on this hardware, "
                f"stable across a power-cycle), it is not a failed reset: exempt "
                f"it for one run with apply_factory_baseline(..., "
                f"exempt=[(category, item)]) and, with the measurement, add it to "
                f"DETECTION_DERIVED_ITEMS in ultimate64_baseline.py (pinned by "
                f"tests/test_entry_baseline.py).  Detection-derived items already "
                f"exempt: {len(report.detection_derived_items())}."
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
    excluded_folded = {c.lower() for c in BASELINE_EXCLUDED_CATEGORIES}
    for cat in categories:
        if not isinstance(cat, str) or not cat.strip():
            raise ValueError(f"category must be a non-empty string, got {cat!r}")
        if "*" in cat or "?" in cat:
            raise ValueError(
                f"category {cat!r} is a pattern; the entry reset never sends a "
                f"glob (the firmware route would match every store it covers)"
            )
        folded = cat.lower()
        if folded in excluded_folded or any(m in folded for m in _EXCLUDED_MARKERS):
            raise ValueError(
                f"category {cat!r} is a network store; the entry reset never "
                f"touches {BASELINE_EXCLUDED_CATEGORIES!r} (a reset there can "
                f"strand the device — issue #227)"
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

    Per category (present on the device): one category GET (the values
    before), one ``PUT /v1/configs/<category>:reset_to_default``, then one
    item GET per item for its ``current`` and ``default``.  Pre-reset drift
    is logged at INFO per item; a post-reset mismatch raises
    :class:`U64BaselineError` after every category has been processed.

    Items in :data:`DETECTION_DERIVED_ITEMS` — and in *exempt* — are
    **not compared** (their value is what the firmware detected, not what
    a reset produces); they are reported under
    ``report.detection_derived`` with current and default.  Their
    category is still reset.  Nothing is ever PUT by this function.

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
    :param exempt: extra ``(category, item)`` pairs to treat like
        :data:`DETECTION_DERIVED_ITEMS` for this call — a one-off for a
        script on hardware whose detection-derived items are not yet in
        the pinned set.  Refused for the network stores and globs.
    :returns: :class:`BaselineReport`.
    :raises ValueError: a glob or an excluded category in *categories*
        or *exempt*.
    :raises U64BaselineError: an item still reads ``current != default``
        after its category was reset, or a listed category the firmware
        reports it did not reset.
    :raises Ultimate64Error: wire / protocol failures from the client.
    """
    wanted = _validate_categories(categories)
    exempt_pairs = DETECTION_DERIVED_ITEMS | _validate_exempt(exempt)

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
                # Detection-derived: what the firmware found in the socket,
                # not what a reset produces.  Listed, never compared.
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
