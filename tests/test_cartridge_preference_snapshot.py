"""``Cartridge Preference`` — named constants and snapshot coverage (issue #221).

Two asks split out of #214:

1. ``"C64 and Cartridge Settings"`` / ``"Cartridge Preference"`` were string
   literals at every call site (the RR-Net recipe, the #217 re-PUT in
   ``execute.py``, the live tests).  ``CARTRIDGE_SETTINGS_CATEGORY`` and
   ``CARTRIDGE_PREFERENCE_ITEM`` now live next to the other category names
   in ``ultimate64_helpers`` and every in-repo caller uses them.
2. ``snapshot_state``/``restore_state`` did not carry the item, so a hardware
   RR-Net run that set ``External`` had to put ``Auto`` back by hand.  The
   item is memory-only (it survives ``machine:reboot``, measured 3/3 on
   2026-09-05; only a firmware boot reloads flash) and leaks between lanes
   on a shared device — exactly what #214 was about.

The round-trip test here is the red test the issue asks for: before the
fix ``restore_state`` never wrote the item, so the read-back after
restore stayed ``External``.
"""

from __future__ import annotations

from typing import Any

import pytest

from c64_test_harness.backends.ultimate64_helpers import (
    CAT_CART,
    CAT_SID_ADDRESSING,
    CAT_U64_SPECIFIC,
    U64StateSnapshot,
    restore_state,
    snapshot_state,
)

_CAT = "C64 and Cartridge Settings"
_ITEM = "Cartridge Preference"


class _StatefulU64:
    """Ultimate64Client stand-in whose reads see its own earlier writes.

    Same shape as ``tests/test_sid_isolation.py``'s ``FakeU64``: category
    GETs return bare values under the category key, item GETs return the
    item map (issue #214), ``set_config_items`` fans out one PUT per item
    in insertion order exactly as the real client does.
    """

    def __init__(self, preference: str = "Auto") -> None:
        self.state: dict[str, dict[str, Any]] = {
            CAT_U64_SPECIFIC: {
                "Turbo Control": "Off",
                "CPU Speed": " 1",
                "Badline Timing": "Enabled",
            },
            CAT_CART: {
                "RAM Expansion Unit": "Disabled",
                "REU Size": "2 MB",
                "Cartridge": "",
                "Bus Operation Mode": "Quiet",
                _ITEM: preference,
            },
            CAT_SID_ADDRESSING: {
                "SID Socket 1 Address": "$D400",
                "Auto Address Mirroring": "Enabled",
            },
        }
        self.puts: list[tuple[str, str, Any]] = []

    def get_config_category(self, category: str) -> dict:
        return {category: dict(self.state[category]), "errors": []}

    def get_config_item(self, category: str, item: str) -> dict:
        if item == "Cartridge":
            # U64E fw 3.15 shape: a .crt chooser with no presets.
            return {"current": "", "presets": [""], "default": ""}
        return {"current": self.state[category][item], "default": "Auto"}

    def set_config_item(self, category: str, item: str, value: Any) -> None:
        self.puts.append((category, item, value))
        self.state[category][item] = value

    def set_config_items(self, category: str, updates: dict) -> None:
        for item, value in updates.items():
            self.set_config_item(category, item, value)


# --------------------------------------------------------------------------- #
# Snapshot / restore round trip (the #221 red test)                           #
# --------------------------------------------------------------------------- #

def test_restore_puts_cartridge_preference_back() -> None:
    """snapshot -> set External -> restore leaves the read-back at the original.

    Before #221 ``restore_state`` never wrote the item: the read-back after
    restore stayed ``External`` and the next lane inherited it.
    """
    client = _StatefulU64(preference="Auto")
    snap = snapshot_state(client)
    client.set_config_item(_CAT, _ITEM, "External")
    assert client.state[CAT_CART][_ITEM] == "External"
    restore_state(client, snap)
    assert client.state[CAT_CART][_ITEM] == "Auto", (
        "restore_state left Cartridge Preference at "
        f"{client.state[CAT_CART][_ITEM]!r}; it must write the snapshotted value"
    )
    assert (_CAT, _ITEM, "Auto") in client.puts


def test_snapshot_carries_the_preference() -> None:
    client = _StatefulU64(preference="External")
    snap = snapshot_state(client)
    assert snap.cartridge_preference == "External"


def test_restore_writes_a_non_default_preference_too() -> None:
    """A lane that *wants* External back after a sub-step gets it: the
    snapshotted value is restored whatever it is, not just ``Auto``."""
    client = _StatefulU64(preference="External")
    snap = snapshot_state(client)
    client.set_config_item(_CAT, _ITEM, "Auto")
    restore_state(client, snap)
    assert client.state[CAT_CART][_ITEM] == "External"


def test_restore_skips_an_empty_preference() -> None:
    """A snapshot taken before the field existed (or from a device that
    does not expose the item) restores everything else and never PUTs
    ``""`` — the firmware answers HTTP 400 to that."""
    client = _StatefulU64(preference="Auto")
    client.set_config_item(_CAT, _ITEM, "External")
    client.puts.clear()
    snap = U64StateSnapshot(
        turbo_control="Off",
        cpu_speed=" 1",
        reu_enabled="Disabled",
        reu_size="2 MB",
        cartridge="",
    )
    assert snap.cartridge_preference == ""
    restore_state(client, snap)
    assert not any(item == _ITEM for _c, item, _v in client.puts)
    assert client.state[CAT_CART][_ITEM] == "External"


def test_preference_is_written_after_the_cartridge_items() -> None:
    """The ``Cartridge``-first ordering invariant of the cart batch is
    untouched: the preference PUT is appended, not inserted ahead."""
    client = _StatefulU64(preference="Auto")
    snap = snapshot_state(client)
    client.set_config_item(_CAT, _ITEM, "External")
    client.puts.clear()
    restore_state(client, snap)
    cart_items = [item for cat, item, _v in client.puts if cat == CAT_CART]
    assert cart_items[-1] == _ITEM
    assert cart_items.index("RAM Expansion Unit") < cart_items.index(_ITEM)


# --------------------------------------------------------------------------- #
# Named constants                                                             #
# --------------------------------------------------------------------------- #

def test_constants_name_the_firmware_strings() -> None:
    from c64_test_harness.backends.ultimate64_helpers import (
        CARTRIDGE_PREFERENCE_ITEM,
        CARTRIDGE_SETTINGS_CATEGORY,
    )

    assert CARTRIDGE_SETTINGS_CATEGORY == _CAT
    assert CARTRIDGE_PREFERENCE_ITEM == _ITEM
    assert CARTRIDGE_SETTINGS_CATEGORY == CAT_CART


def test_constants_are_exported_from_the_package_root() -> None:
    import c64_test_harness as pkg

    assert pkg.CARTRIDGE_SETTINGS_CATEGORY == _CAT
    assert pkg.CARTRIDGE_PREFERENCE_ITEM == _ITEM
    assert "CARTRIDGE_SETTINGS_CATEGORY" in pkg.__all__
    assert "CARTRIDGE_PREFERENCE_ITEM" in pkg.__all__


def test_execute_re_put_uses_the_same_constants() -> None:
    """The #217 re-PUT in ``execute.py`` addresses the item through the
    helpers' constants, so the two cannot drift apart."""
    from c64_test_harness import execute as _execute
    from c64_test_harness.backends.ultimate64_helpers import (
        CARTRIDGE_PREFERENCE_ITEM,
        CARTRIDGE_SETTINGS_CATEGORY,
    )

    assert _execute._CARTRIDGE_CATEGORY is CARTRIDGE_SETTINGS_CATEGORY
    assert _execute._CARTRIDGE_PREFERENCE_ITEM is CARTRIDGE_PREFERENCE_ITEM


@pytest.mark.parametrize("name", ["CARTRIDGE_SETTINGS_CATEGORY", "CARTRIDGE_PREFERENCE_ITEM"])
def test_constants_are_in_helpers_all(name: str) -> None:
    from c64_test_harness.backends import ultimate64_helpers as helpers

    assert name in helpers.__all__
