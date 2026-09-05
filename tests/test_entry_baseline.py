"""Reset-on-entry to the factory-default baseline (issue #227), mocked.

The contract under test (owner's decisions, #227):

* The baseline is the firmware's own factory default: every item whose
  map carries a ``default`` key must read ``current == default`` at entry.
  There is no harness-owned table.
* Mechanism: per-category ``PUT /v1/configs/{category}:reset_to_default``
  for a fixed covered set.  **Never** the global route, **never**
  ``Ethernet Settings`` / ``Network Settings`` / the WiFi store.
* Ordering: **reset, then assert.**  A mismatch *before* the reset is the
  normal state of a shared device (another lane's leftovers) and is
  logged per item as inherited drift; a mismatch *after* the reset means
  the reset did not take and is a hard failure naming the item.
* Opt-in: ``U64_BASELINE_ON_ENTRY=1`` / ``HarnessConfig.u64_baseline_on_entry``.
  Off by default -> no requests at all.
* The entry reset runs inside the ``DeviceLock`` in the manager path.  The
  standalone callable relies on the #194 unlocked-client notice; it adds
  no second mechanism.

The fake below is modelled on ``tests/test_sid_isolation.py``'s stateful
``FakeU64`` (reads see earlier writes; a ``frozen`` item accepts the reset
and does not move — the #204 shape) and on the #214 item-map contract.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import device_lock as lock_mod
from c64_test_harness.backends.device_lock import (
    UNLOCKED_NOTICE_PHRASE,
    UNLOCKED_WARNING_ENV,
    _reset_advisory_state,
)
from c64_test_harness.backends.ultimate64_baseline import (
    BASELINE_CATEGORIES,
    BASELINE_EXCLUDED_CATEGORIES,
    BASELINE_ON_ENTRY_ENV,
    BaselineReport,
    U64BaselineError,
    apply_factory_baseline,
    baseline_on_entry_enabled,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    CARTRIDGE_PREFERENCE_ITEM,
    CAT_CART,
    CAT_U64_SPECIFIC,
)
from c64_test_harness.backends.unified_manager import (
    UnifiedManager,
    _LockedU64Manager,
)

_BASELINE_LOGGER = "c64_test_harness.backends.ultimate64_baseline"
HOST = "192.0.2.81"

GLOBAL_RESET_PATH = "/v1/configs:reset_to_default"

# (current, default) per item; default None = the item carries no default
# key (preset / info types) and is reported, never asserted.
_FACTORY: dict[str, dict[str, tuple[Any, Any]]] = {
    CAT_CART: {
        "RAM Expansion Unit": ("Disabled", "Disabled"),
        "REU Size": ("2 MB", "2 MB"),
        "Cartridge": ("", None),
        CARTRIDGE_PREFERENCE_ITEM: ("Auto", "Auto"),
        "Command Interface": ("Disabled", "Disabled"),
    },
    CAT_U64_SPECIFIC: {
        "Turbo Control": ("Off", "Off"),
        "CPU Speed": (" 1", " 1"),
        "Badline Timing": ("Enabled", "Enabled"),
    },
    "SID Addressing": {
        "Auto Address Mirroring": ("Enabled", "Enabled"),
    },
    "Audio Mixer": {"Vol UltiSid 1": ("0 dB", "0 dB")},
    "Drive A Settings": {"Drive": ("Enabled", "Enabled"), "Drive Bus ID": (8, 8)},
    "Drive B Settings": {"Drive": ("Disabled", "Disabled")},
    "SoftIEC Drive Settings": {"IEC Drive and Printer": ("Disabled", "Disabled")},
    "Tape Settings": {"Datasette Playback Clock": ("PAL", "PAL")},
    "Printer Settings": {"IEC printer": ("Disabled", "Disabled")},
    "Clock Settings": {"Correction": (0, 0)},
    "LED Strip Settings": {"LED Strip Length": (40, 40)},
    "Modem Settings": {"ACIA": ("Disabled", "Disabled")},
    "User Interface Settings": {"Menu Theme": ("Default", "Default")},
    "SID Sockets Configuration": {"SID Socket 1": ("Enabled", "Enabled")},
    # The stores the entry reset must never touch.
    "Ethernet Settings": {"Use DHCP": ("Enabled", "Enabled")},
    "Network Settings": {"Ultimate DMA Service": ("Enabled", "Enabled")},
    "WiFi settings": {"WiFi": ("Enabled", "Enabled")},
}


class FakeBaselineU64:
    """Stateful Ultimate64Client stand-in for the config-reset surface.

    * ``state``  — category -> item -> current value (reads see writes).
    * ``defaults`` — category -> item -> default (``None`` = no default key).
    * ``frozen`` — items that accept a reset/PUT and do not move (#204).
    * ``requests`` — every state-changing call in order, as
      ``(kind, category)`` where kind is ``"reset"`` / ``"put"`` /
      ``"global-reset"``.
    """

    host = HOST

    def __init__(
        self,
        factory: dict[str, dict[str, tuple[Any, Any]]] | None = None,
        *,
        drift: dict[str, dict[str, Any]] | None = None,
        frozen: set[str] | None = None,
        absent: set[str] | None = None,
    ) -> None:
        factory = factory or _FACTORY
        self.state: dict[str, dict[str, Any]] = {
            cat: {item: cur for item, (cur, _d) in items.items()}
            for cat, items in factory.items()
            if cat not in (absent or set())
        }
        self.defaults: dict[str, dict[str, Any]] = {
            cat: {item: d for item, (_c, d) in items.items()}
            for cat, items in factory.items()
            if cat not in (absent or set())
        }
        for cat, items in (drift or {}).items():
            self.state[cat].update(items)
        self.frozen: set[str] = set(frozen or ())
        self.requests: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str | None]] = []

    # --- reads -------------------------------------------------------
    def list_configs(self) -> list[str]:
        return list(self.state)

    def get_config_category(self, category: str) -> dict:
        self.gets.append((category, None))
        return {category: dict(self.state[category]), "errors": []}

    def get_config_item(self, category: str, item: str) -> dict:
        self.gets.append((category, item))
        item_map: dict[str, Any] = {"current": self.state[category][item]}
        default = self.defaults[category][item]
        if default is None:
            item_map["presets"] = [""]
        else:
            item_map["default"] = default
            item_map["values"] = [default, self.state[category][item]]
        return item_map

    # --- writes ------------------------------------------------------
    def reset_config_category_to_default(self, category: str) -> list[str]:
        self.requests.append(("reset", category))
        if category not in self.state:
            return []
        for item, default in self.defaults[category].items():
            if default is not None and item not in self.frozen:
                self.state[category][item] = default
        return [category]

    def reset_config_to_default(self) -> None:
        self.requests.append(("global-reset", "*"))
        for cat in self.state:
            self.reset_config_category_to_default(cat)

    def set_config_item(self, category: str, item: str, value: Any) -> None:
        self.requests.append(("put", category))
        if item not in self.frozen:
            self.state[category][item] = value

    def set_config_items(self, category: str, updates: dict) -> None:
        for item, value in updates.items():
            self.set_config_item(category, item, value)

    # --- assertions helpers -----------------------------------------
    def mismatches(self, categories=BASELINE_CATEGORIES) -> dict[tuple[str, str], tuple[Any, Any]]:
        out = {}
        for cat in categories:
            if cat not in self.state:
                continue
            for item, default in self.defaults[cat].items():
                if default is not None and self.state[cat][item] != default:
                    out[(cat, item)] = (self.state[cat][item], default)
        return out


_DRIFT = {
    CAT_CART: {"REU Size": "16 MB", "RAM Expansion Unit": "Enabled",
               CARTRIDGE_PREFERENCE_ITEM: "External"},
    CAT_U64_SPECIFIC: {"CPU Speed": "48", "Turbo Control": "Manual"},
}


# --------------------------------------------------------------------------- #
# The covered / excluded sets                                                  #
# --------------------------------------------------------------------------- #

class TestCoveredSet:
    def test_the_fourteen_covered_categories(self) -> None:
        assert set(BASELINE_CATEGORIES) == {
            "C64 and Cartridge Settings", "U64 Specific Settings",
            "SID Addressing", "SID Sockets Configuration", "Audio Mixer",
            "Drive A Settings", "Drive B Settings", "SoftIEC Drive Settings",
            "Tape Settings", "Printer Settings", "Clock Settings",
            "LED Strip Settings", "Modem Settings", "User Interface Settings",
        }

    def test_the_network_stores_are_excluded(self) -> None:
        assert {"Ethernet Settings", "Network Settings", "WiFi settings"} <= set(
            BASELINE_EXCLUDED_CATEGORIES
        )
        assert not set(BASELINE_CATEGORIES) & set(BASELINE_EXCLUDED_CATEGORIES)

    def test_no_covered_name_is_a_glob(self) -> None:
        """The firmware route takes a *pattern*; a wildcard in a covered
        name could widen to a network store."""
        for cat in BASELINE_CATEGORIES:
            assert "*" not in cat and "?" not in cat


# --------------------------------------------------------------------------- #
# Entry after a simulated kill                                                #
# --------------------------------------------------------------------------- #

class TestEntryAfterKill:
    def test_entry_without_restore_on_exit_ends_at_defaults(self) -> None:
        """The previous lane drifted five items and never restored (killed).
        The next entry still ends with ``current == default`` everywhere."""
        client = FakeBaselineU64(drift=_DRIFT)
        assert client.mismatches(), "precondition: the device is drifted"
        report = apply_factory_baseline(client)
        assert client.mismatches() == {}
        assert isinstance(report, BaselineReport)
        assert report.ok

    def test_report_names_every_drifted_item(self) -> None:
        client = FakeBaselineU64(drift=_DRIFT)
        report = apply_factory_baseline(client)
        assert set(report.drifted_items()) == {
            (CAT_CART, "REU Size"),
            (CAT_CART, "RAM Expansion Unit"),
            (CAT_CART, CARTRIDGE_PREFERENCE_ITEM),
            (CAT_U64_SPECIFIC, "CPU Speed"),
            (CAT_U64_SPECIFIC, "Turbo Control"),
        }
        assert report.drifted[CAT_CART]["REU Size"] == ("16 MB", "2 MB")

    def test_every_covered_category_present_is_reset(self) -> None:
        client = FakeBaselineU64()
        report = apply_factory_baseline(client)
        resets = [cat for kind, cat in client.requests if kind == "reset"]
        assert set(resets) == set(BASELINE_CATEGORIES)
        assert set(report.reset) == set(BASELINE_CATEGORIES)
        assert len(resets) == len(BASELINE_CATEGORIES), "one reset per category"

    def test_nothing_drifted_is_still_reset_and_reported_clean(self) -> None:
        client = FakeBaselineU64()
        report = apply_factory_baseline(client)
        assert report.drifted == {}
        assert report.mismatched == {}
        assert report.ok


# --------------------------------------------------------------------------- #
# Reset, then assert                                                          #
# --------------------------------------------------------------------------- #

class TestOrdering:
    def test_pre_reset_drift_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeBaselineU64(drift=_DRIFT)
        with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
            apply_factory_baseline(client)   # must not raise
        drift_lines = [
            r for r in caplog.records
            if "inherited drift" in r.getMessage() and r.levelno == logging.INFO
        ]
        assert drift_lines, "inherited drift must be logged at INFO"
        text = "\n".join(r.getMessage() for r in drift_lines)
        for item in ("REU Size", "CPU Speed", CARTRIDGE_PREFERENCE_ITEM):
            assert item in text
        assert "16 MB" in text and "2 MB" in text, "old and default values are named"
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_reset_precedes_every_post_read(self) -> None:
        """Structural: for each category the reset request lands before
        the item reads whose ``current`` is asserted."""
        client = FakeBaselineU64(drift=_DRIFT)
        events: list[tuple[str, str]] = []
        real_reset = client.reset_config_category_to_default
        real_item = client.get_config_item

        def _reset(cat):
            events.append(("reset", cat))
            return real_reset(cat)

        def _item(cat, item):
            events.append(("item", cat))
            return real_item(cat, item)

        client.reset_config_category_to_default = _reset  # type: ignore[method-assign]
        client.get_config_item = _item  # type: ignore[method-assign]
        apply_factory_baseline(client)
        for cat in BASELINE_CATEGORIES:
            kinds = [k for k, c in events if c == cat]
            assert kinds and kinds[0] == "reset", f"{cat}: reads before the reset: {kinds}"
            assert "item" in kinds

    def test_a_frozen_item_fails_post_reset_by_name(self) -> None:
        """The #204 shape: the reset is accepted and the item does not move."""
        client = FakeBaselineU64(drift=_DRIFT, frozen={"REU Size"})
        with pytest.raises(U64BaselineError) as ei:
            apply_factory_baseline(client)
        msg = str(ei.value)
        assert "reset did not take" in msg
        assert CAT_CART in msg and "REU Size" in msg
        assert "16 MB" in msg and "2 MB" in msg, "current and default are named"
        assert ei.value.mismatched == {CAT_CART: {"REU Size": ("16 MB", "2 MB")}}
        # The rest was still reset: only the frozen item mismatches.
        assert client.mismatches() == {(CAT_CART, "REU Size"): ("16 MB", "2 MB")}

    def test_frozen_failure_is_raised_after_every_category(self) -> None:
        """A failing category does not stop the others being reset — the
        error is raised once, at the end, with the full mismatch map."""
        client = FakeBaselineU64(drift=_DRIFT, frozen={"REU Size", "CPU Speed"})
        with pytest.raises(U64BaselineError) as ei:
            apply_factory_baseline(client)
        resets = {cat for kind, cat in client.requests if kind == "reset"}
        assert resets == set(BASELINE_CATEGORIES)
        assert set(ei.value.mismatched) == {CAT_CART, CAT_U64_SPECIFIC}
        assert ei.value.report.reset and set(ei.value.report.reset) == set(BASELINE_CATEGORIES)

    def test_post_reset_mismatch_is_not_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeBaselineU64(drift=_DRIFT, frozen={"REU Size"})
        with caplog.at_level(logging.DEBUG, logger=_BASELINE_LOGGER):
            with pytest.raises(U64BaselineError):
                apply_factory_baseline(client)


# --------------------------------------------------------------------------- #
# Never the global route, never the network stores                            #
# --------------------------------------------------------------------------- #

class TestExclusions:
    def test_excluded_categories_are_never_requested(self) -> None:
        client = FakeBaselineU64(drift={
            "Ethernet Settings": {"Use DHCP": "Disabled"},
            "Network Settings": {"Ultimate DMA Service": "Disabled"},
            "WiFi settings": {"WiFi": "Disabled"},
            **_DRIFT,
        })
        before = {cat: dict(client.state[cat]) for cat in BASELINE_EXCLUDED_CATEGORIES}
        apply_factory_baseline(client)
        touched = {cat for _kind, cat in client.requests}
        assert not touched & set(BASELINE_EXCLUDED_CATEGORIES)
        assert all(client.state[cat] == before[cat] for cat in before), (
            "the excluded stores must be byte-identical before/after"
        )
        assert not any(cat in BASELINE_EXCLUDED_CATEGORIES for cat, _i in client.gets), (
            "not even read: the entry path has no business in the network stores"
        )

    def test_global_route_is_never_used(self) -> None:
        client = FakeBaselineU64(drift=_DRIFT)
        apply_factory_baseline(client)
        assert ("global-reset", "*") not in client.requests

    @pytest.mark.parametrize("bad", ["Ethernet Settings", "Network Settings", "WiFi settings",
                                     "ethernet settings", "Network*"])
    def test_passing_an_excluded_or_glob_category_is_refused_before_any_request(
        self, bad: str
    ) -> None:
        client = FakeBaselineU64()
        with pytest.raises(ValueError, match="never"):
            apply_factory_baseline(client, categories=(CAT_CART, bad))
        assert client.requests == [] and client.gets == []

    @pytest.mark.parametrize("pattern", ["*", "Drive ? Settings", "SID*"])
    def test_a_glob_category_is_refused(self, pattern: str) -> None:
        """The firmware route matches globs; ``*`` would be the global reset
        by another name."""
        client = FakeBaselineU64()
        with pytest.raises(ValueError, match="pattern"):
            apply_factory_baseline(client, categories=(pattern,))
        assert client.requests == []

    def test_wire_level_the_entry_path_cannot_emit_the_global_route(self) -> None:
        """Structural, on the real client: record every request the entry
        path emits and assert the global route and the network stores are
        absent from the wire."""
        client = Ultimate64Client(HOST, write_mem_query_threshold=48, warn_unlocked=False)
        wire: list[tuple[str, str]] = []

        def _request(method, path, *, body=None, content_type=None, query=None):
            wire.append((method, path))
            if path == "/v1/configs":
                return 200, json.dumps({"categories": list(_FACTORY), "errors": []}).encode()
            if path.endswith(":reset_to_default"):
                cat = _decode_category(path[len("/v1/configs/"):-len(":reset_to_default")])
                return 200, json.dumps({"reset": [cat], "errors": []}).encode()
            parts = path[len("/v1/configs/"):].split("/")
            cat = _decode_category(parts[0])
            if len(parts) == 1:
                return 200, json.dumps({cat: {
                    item: cur for item, (cur, _d) in _FACTORY[cat].items()}, "errors": []}).encode()
            item = _decode_category(parts[1])
            cur, default = _FACTORY[cat][item]
            item_map = {"current": cur}
            if default is not None:
                item_map["default"] = default
                item_map["values"] = [default]
            else:
                item_map["presets"] = [""]
            return 200, json.dumps({cat: {item: item_map}, "errors": []}).encode()

        with patch.object(client, "_request", side_effect=_request):
            report = apply_factory_baseline(client)
        assert report.ok
        paths = [p for _m, p in wire]
        assert GLOBAL_RESET_PATH not in paths
        assert not any("Ethernet" in p or "Network" in p or "WiFi" in p for p in paths)
        puts = [p for m, p in wire if m != "GET"]
        assert puts and all(p.endswith(":reset_to_default") for p in puts)
        assert len(puts) == len(BASELINE_CATEGORIES)


def _decode_category(segment: str) -> str:
    import urllib.parse
    return urllib.parse.unquote(segment)


# --------------------------------------------------------------------------- #
# Categories missing on the device / items without a default                  #
# --------------------------------------------------------------------------- #

class TestDeviceShape:
    def test_missing_categories_are_skipped_with_a_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The C64U's category set differs; absence is a skip, not an error."""
        absent = {"LED Strip Settings", "SID Sockets Configuration"}
        client = FakeBaselineU64(absent=absent)
        with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
            report = apply_factory_baseline(client)
        assert set(report.skipped) == absent
        assert not any(cat in absent for _k, cat in client.requests)
        text = "\n".join(r.getMessage() for r in caplog.records)
        for cat in absent:
            assert cat in text and "skipp" in text
        assert report.ok

    def test_items_without_a_default_are_reported_not_asserted(self) -> None:
        client = FakeBaselineU64(drift={CAT_CART: {"Cartridge": "foo.crt"}})
        report = apply_factory_baseline(client)   # no raise: no default to assert
        assert report.unasserted[CAT_CART] == ("Cartridge",)
        assert (CAT_CART, "Cartridge") not in report.drifted_items()
        assert report.ok

    def test_a_category_the_firmware_did_not_reset_is_a_failure(self) -> None:
        """Listed by ``/v1/configs`` but the reset answered an empty list:
        the store did not match — report it rather than assert on stale
        reads."""
        client = FakeBaselineU64()
        real = client.reset_config_category_to_default
        client.reset_config_category_to_default = (  # type: ignore[method-assign]
            lambda cat: [] if cat == "Tape Settings" else real(cat)
        )
        with pytest.raises(U64BaselineError, match="Tape Settings"):
            apply_factory_baseline(client)

    def test_int_valued_items_compare_as_values(self) -> None:
        client = FakeBaselineU64(drift={"Drive A Settings": {"Drive Bus ID": 9}})
        report = apply_factory_baseline(client)
        assert report.drifted["Drive A Settings"]["Drive Bus ID"] == (9, 8)
        assert client.state["Drive A Settings"]["Drive Bus ID"] == 8


# --------------------------------------------------------------------------- #
# dry_run                                                                     #
# --------------------------------------------------------------------------- #

class TestDryRun:
    def test_dry_run_makes_no_state_changing_request(self) -> None:
        client = FakeBaselineU64(drift=_DRIFT)
        report = apply_factory_baseline(client, dry_run=True)
        assert client.requests == []
        assert client.mismatches(), "nothing was reset"
        assert report.dry_run is True
        assert report.reset == ()
        assert set(report.drifted_items()) == {
            (CAT_CART, "REU Size"), (CAT_CART, "RAM Expansion Unit"),
            (CAT_CART, CARTRIDGE_PREFERENCE_ITEM),
            (CAT_U64_SPECIFIC, "CPU Speed"), (CAT_U64_SPECIFIC, "Turbo Control"),
        }
        assert report.mismatched == {}, "nothing was asserted"


# --------------------------------------------------------------------------- #
# The unlocked notice (#194) — no second mechanism                            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def default_lock_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "locks"
    d.mkdir()
    monkeypatch.setattr(lock_mod, "_default_lock_dir", lambda create=True: d)
    monkeypatch.delenv(UNLOCKED_WARNING_ENV, raising=False)
    _reset_advisory_state()
    yield d
    _reset_advisory_state()


class TestUnlockedNotice:
    def test_calling_without_the_lock_logs_the_194_notice(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeBaselineU64()
        with caplog.at_level(logging.WARNING, logger=_BASELINE_LOGGER):
            apply_factory_baseline(client)
        notices = [r for r in caplog.records if UNLOCKED_NOTICE_PHRASE in r.getMessage()]
        assert len(notices) == 1
        assert "apply_factory_baseline" in notices[0].getMessage()
        assert HOST in notices[0].getMessage()

    def test_silent_when_this_process_holds_the_lock(
        self, default_lock_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        lock = lock_mod.DeviceLock(HOST, lock_dir=default_lock_dir)
        assert lock.acquire(timeout=1.0)
        try:
            with caplog.at_level(logging.WARNING, logger=_BASELINE_LOGGER):
                apply_factory_baseline(FakeBaselineU64())
        finally:
            lock.release()
        assert not [r for r in caplog.records if UNLOCKED_NOTICE_PHRASE in r.getMessage()]


# --------------------------------------------------------------------------- #
# Opt-in                                                                      #
# --------------------------------------------------------------------------- #

class TestOptIn:
    def test_env_name(self) -> None:
        assert BASELINE_ON_ENTRY_ENV == "U64_BASELINE_ON_ENTRY"

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("0", False), ("", False), ("false", False), ("no", False),
    ])
    def test_env_parsing(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, value)
        assert baseline_on_entry_enabled() is expected

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BASELINE_ON_ENTRY_ENV, raising=False)
        assert baseline_on_entry_enabled() is False


def _mock_instance(client: Any, host: str = HOST) -> MagicMock:
    inst = MagicMock()
    inst.pid = None
    inst.device.host = host
    inst.transport = MagicMock()
    inst.transport.client = client
    return inst


class TestManagerPath:
    """``_LockedU64Manager.acquire`` runs the entry reset inside the lock."""

    def _locked(self, MockDeviceLock: MagicMock, order: list[str]) -> MagicMock:
        lock = MagicMock()
        lock.acquire_or_raise.side_effect = lambda **kw: order.append("lock")
        lock.release.side_effect = lambda: order.append("unlock")
        MockDeviceLock.return_value = lock
        return lock

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_off_by_default_no_requests(self, MockDeviceLock: MagicMock) -> None:
        order: list[str] = []
        self._locked(MockDeviceLock, order)
        client = FakeBaselineU64(drift=_DRIFT)
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        mgr = _LockedU64Manager(inner)
        mgr.acquire()
        assert client.requests == [] and client.gets == []
        assert client.mismatches(), "the drift is untouched when opted out"

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_opted_in_resets_after_the_lock_and_before_handing_out(
        self, MockDeviceLock: MagicMock
    ) -> None:
        order: list[str] = []
        self._locked(MockDeviceLock, order)
        client = FakeBaselineU64(drift=_DRIFT)
        real_reset = client.reset_config_category_to_default

        def _reset(cat):
            order.append("reset")
            return real_reset(cat)

        client.reset_config_category_to_default = _reset  # type: ignore[method-assign]
        inner = MagicMock()
        inst = _mock_instance(client)
        inner.acquire.return_value = inst
        mgr = _LockedU64Manager(inner, baseline_on_entry=True)
        got = mgr.acquire()
        assert got is inst
        assert order[0] == "lock", "the reset must run INSIDE the lock"
        assert order.count("reset") == len(BASELINE_CATEGORIES)
        assert "unlock" not in order
        assert client.mismatches() == {}

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_entry_reset_failure_releases_lock_and_device(
        self, MockDeviceLock: MagicMock
    ) -> None:
        order: list[str] = []
        self._locked(MockDeviceLock, order)
        client = FakeBaselineU64(drift=_DRIFT, frozen={"REU Size"})
        inner = MagicMock()
        inst = _mock_instance(client)
        inner.acquire.return_value = inst
        mgr = _LockedU64Manager(inner, baseline_on_entry=True)
        with pytest.raises(U64BaselineError, match="REU Size"):
            mgr.acquire()
        assert order[-1] == "unlock"
        inner.release.assert_called_once_with(inst)
        assert mgr._locks == {}

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_release_after_a_baselined_acquire(self, MockDeviceLock: MagicMock) -> None:
        order: list[str] = []
        lock = self._locked(MockDeviceLock, order)
        client = FakeBaselineU64()
        inner = MagicMock()
        inst = _mock_instance(client)
        inner.acquire.return_value = inst
        mgr = _LockedU64Manager(inner, baseline_on_entry=True)
        mgr.release(mgr.acquire())
        lock.release.assert_called_once()
        inner.release.assert_called_once_with(inst)

    def test_manager_refuses_the_entry_reset_without_the_device_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No DeviceLock available -> a bare client would run the reset
        unlocked.  The manager path refuses rather than degrading."""
        monkeypatch.setattr(
            "c64_test_harness.backends.unified_manager._HAS_DEVICE_LOCK", False
        )
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager", create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            with pytest.raises(RuntimeError, match="DeviceLock"):
                UnifiedManager(backend="u64", u64_hosts=[HOST], baseline_on_entry=True)

    def test_env_opts_the_manager_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        with patch("c64_test_harness.backends.unified_manager._LockedU64Manager") as Locked:
            UnifiedManager(backend="u64", u64_hosts=[HOST])
        assert Locked.call_args.kwargs["baseline_on_entry"] is True

    def test_env_unset_leaves_the_manager_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BASELINE_ON_ENTRY_ENV, raising=False)
        with patch("c64_test_harness.backends.unified_manager._LockedU64Manager") as Locked:
            UnifiedManager(backend="u64", u64_hosts=[HOST])
        assert Locked.call_args.kwargs["baseline_on_entry"] is False

    def test_explicit_false_beats_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        with patch("c64_test_harness.backends.unified_manager._LockedU64Manager") as Locked:
            UnifiedManager(backend="u64", u64_hosts=[HOST], baseline_on_entry=False)
        assert Locked.call_args.kwargs["baseline_on_entry"] is False

    def test_vice_backend_ignores_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        with patch("c64_test_harness.backends.unified_manager.ViceInstanceManager"):
            mgr = UnifiedManager(backend="vice", baseline_on_entry=True)
        assert mgr.backend == "vice"


class TestPackageSurface:
    def test_exported_from_the_package_root(self) -> None:
        import c64_test_harness as pkg

        for name in ("apply_factory_baseline", "BaselineReport", "U64BaselineError",
                     "BASELINE_CATEGORIES", "BASELINE_EXCLUDED_CATEGORIES",
                     "BASELINE_ON_ENTRY_ENV"):
            assert name in pkg.__all__
            assert getattr(pkg, name) is not None

    def test_baseline_error_is_an_ultimate64_error(self) -> None:
        from c64_test_harness.backends.ultimate64_client import Ultimate64Error

        assert issubclass(U64BaselineError, Ultimate64Error)
