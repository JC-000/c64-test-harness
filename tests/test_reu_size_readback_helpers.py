"""Unit tests for the restore/target helpers in ``test_reu_size_readback_live``.

The live module's flash-reload tests restore every item a
``configs:load_from_flash`` changed. The properties that must hold without
a device:

* ``_restore_category_items`` puts each item of ``C64 and Cartridge
  Settings`` back to the ``default`` the device reports, not to the value
  read at entry (#469).  The store is a ``BASELINE_CATEGORIES`` member and
  every one of its items is a selector (``c64_config[]``, 1541ultimate
  ``software/io/c64/c64.cc:72``, identical item set at ``bce4535e`` and
  ``1.1.0``), so the #334 baseline is ``current == default`` per item and an
  entry value off its default is drift -- a snapshot restore would put it
  straight back.
* It PUTs only what differs, keeps going when one PUT is rejected (the
  firmware answers 400 for ``value=""`` on some items) and raises *after*
  the loop, so one bad item cannot leave the rest un-restored.
* ``_pick_ram_target`` must return a size that differs from both the RAM
  value *and* the item default -- if it happened to equal the flash value,
  the mechanism assertion would fail with the wrong diagnosis.
"""
from __future__ import annotations

import pytest

import test_reu_size_readback_live as live
from c64_test_harness.backends.ultimate64_client import Ultimate64Error

#: Real defaults (``c64_config[]`` at bce4535e) for the items used below.
DEFAULTS = {
    "Cartridge": "",
    "REU Size": "2 MB",
    "RAM Expansion Unit": "Disabled",
    "Command Interface": "Disabled",
    "Fast Reset": "Disabled",
}


class _FakeClient:
    """Reports *defaults*; records ``set_config_item``; rejects *reject*."""

    def __init__(
        self,
        reject: set[str] = frozenset(),
        defaults: dict | None = None,
        unreadable: set[str] = frozenset(),
    ) -> None:
        self.reject = reject
        self.defaults = dict(DEFAULTS if defaults is None else defaults)
        self.unreadable = unreadable
        self.calls: list[tuple[str, str, object]] = []

    def get_config_item(self, category: str, item: str) -> dict:
        assert category == live.CAT_CART, category
        if item in self.unreadable:
            raise Ultimate64Error(f"{item}: HTTP 500", status=500)
        entry = {"current": "unused"}
        if self.defaults.get(item) is not None:
            entry["default"] = self.defaults[item]
        return entry

    def set_config_item(self, category: str, item: str, value: object) -> None:
        self.calls.append((category, item, value))
        if item in self.reject:
            raise Ultimate64Error(f"{item}: not a valid choice", status=400)


def test_items_go_back_to_their_default_not_the_entry_snapshot():
    """The bench's lanes leave ``REU Size`` at 512 KB and ``Command
    Interface`` Enabled while the defaults are 2 MB / Disabled; the entry
    snapshot is that drift, so it is not what goes back."""
    # entry snapshot: {"REU Size": "512 KB", "Command Interface": "Enabled"}
    now = {"REU Size": "1 MB", "Command Interface": "Disabled"}
    client = _FakeClient()

    live._restore_category_items(client, now)

    assert client.calls == [(live.CAT_CART, "REU Size", "2 MB")]


def test_drift_present_at_entry_is_corrected():
    """Unchanged since entry is not the same as at baseline."""
    now = {"Fast Reset": "Enabled", "REU Size": "2 MB"}  # as at entry
    client = _FakeClient()

    live._restore_category_items(client, now)

    assert client.calls == [(live.CAT_CART, "Fast Reset", "Disabled")]


def test_restore_continues_past_a_rejected_put_and_raises_after():
    now = {"Cartridge": "x.crt", "RAM Expansion Unit": "Enabled", "REU Size": "1 MB"}
    client = _FakeClient(reject={"Cartridge"})

    with pytest.raises(Ultimate64Error) as excinfo:
        live._restore_category_items(client, now)

    assert sorted(client.calls) == [
        (live.CAT_CART, "Cartridge", ""),
        (live.CAT_CART, "RAM Expansion Unit", "Disabled"),
        (live.CAT_CART, "REU Size", "2 MB"),
    ]
    assert "Cartridge" in str(excinfo.value)


@pytest.mark.parametrize("broken", ["no-default", "unreadable"])
def test_an_item_without_a_readable_default_is_reported_and_the_rest_restored(
    broken,
):
    if broken == "no-default":
        client = _FakeClient(defaults={**DEFAULTS, "Command Interface": None})
    else:
        client = _FakeClient(unreadable={"Command Interface"})
    with pytest.raises(Ultimate64Error, match="Command Interface"):
        live._restore_category_items(
            client, {"Command Interface": "Enabled", "REU Size": "1 MB"}
        )
    assert client.calls == [(live.CAT_CART, "REU Size", "2 MB")]


def test_an_empty_default_is_a_real_default():
    """``Cartridge``'s default is ``""`` (no ``.crt``); it is written, not
    refused as missing."""
    client = _FakeClient()
    live._restore_category_items(client, {"Cartridge": "stale.crt"})
    assert client.calls == [(live.CAT_CART, "Cartridge", "")]


def test_restore_is_a_no_op_when_everything_is_at_its_default():
    client = _FakeClient()
    live._restore_category_items(client, {"REU Size": "2 MB", "Cartridge": ""})
    assert client.calls == []


def test_the_residue_is_what_is_still_off_its_default():
    client = _FakeClient()
    now = {"REU Size": "2 MB", "Fast Reset": "Enabled", "Cartridge": ""}
    assert live._restore_residue(client, now) == {"Fast Reset": ("Disabled", "Enabled")}


@pytest.mark.parametrize(
    "stock_size, default_size",
    [
        ("512 KB", "2 MB"),   # the bench as measured
        ("512 KB", "1 MB"),   # flash holds the first candidate
        ("1 MB", "4 MB"),     # flash holds the second candidate
        ("1 MB", "1 MB"),     # RAM == flash (freshly booted bench)
        ("2 MB", None),       # default unknown
    ],
)
def test_pick_ram_target_avoids_stock_and_default(stock_size, default_size):
    target = live._pick_ram_target(stock_size, default_size)
    assert target in live.REU_SIZE_VALUES
    assert target != stock_size
    assert target != default_size
