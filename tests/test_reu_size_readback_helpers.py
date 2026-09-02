"""Unit tests for the restore/target helpers in ``test_reu_size_readback_live``.

The live module's flash-reload tests restore every item a
``configs:load_from_flash`` changed. Two properties must hold without a
device:

* ``_restore_category_items`` must keep going when one PUT is rejected
  (the firmware answers 400 for ``value=""`` on some items) and raise
  *after* the loop — otherwise ``Command Interface``, which the reload
  really disables on the device, can be left ``Disabled``.
* ``_pick_ram_target`` must return a size that differs from both the RAM
  value *and* the item default — if it happened to equal the flash value,
  the mechanism assertion would fail with the wrong diagnosis.
"""
from __future__ import annotations

import pytest

import test_reu_size_readback_live as live
from c64_test_harness.backends.ultimate64_client import Ultimate64Error


class _FakeClient:
    """Records ``set_config_item`` calls; raises on the items in *reject*."""

    def __init__(self, reject: set[str]) -> None:
        self.reject = reject
        self.calls: list[tuple[str, str, object]] = []

    def set_config_item(self, category: str, item: str, value: object) -> None:
        self.calls.append((category, item, value))
        if item in self.reject:
            raise Ultimate64Error(f"{item}: not a valid choice", status=400)


def test_restore_continues_past_a_rejected_put_and_raises_after():
    stock = {"A": "", "Command Interface": "Enabled", "REU Size": "512 KB"}
    now = {"A": "x", "Command Interface": "Disabled", "REU Size": "2 MB"}
    client = _FakeClient(reject={"A"})

    with pytest.raises(Ultimate64Error) as excinfo:
        live._restore_category_items(client, stock, now)

    put_items = {item for _cat, item, _val in client.calls}
    assert put_items == {"A", "Command Interface", "REU Size"}, (
        f"restore stopped early: only PUT {put_items!r}"
    )
    assert ("C64 and Cartridge Settings", "Command Interface", "Enabled") in client.calls
    assert ("C64 and Cartridge Settings", "REU Size", "512 KB") in client.calls
    assert "A" in str(excinfo.value)


def test_restore_is_a_no_op_when_nothing_differs():
    client = _FakeClient(reject=set())
    live._restore_category_items(client, {"A": "1"}, {"A": "1"})
    assert client.calls == []


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
