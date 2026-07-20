"""Cross-device REU-enable contract tests (mocked Ultimate64Client).

Background: :func:`set_reu` used to unconditionally write
``Cartridge: "REU"`` when enabling the REU. On the U64 Elite (firmware
3.14) that write is REQUIRED — the ``Cartridge`` preset is what exposes
the expansion to the C64. On the new C64 Ultimate (firmware 1.1.0) the
``Cartridge`` item reports ``presets: [""]``; ``"REU"`` is only a
mirrored display value and PUTting it back is rejected with HTTP 400
("not a valid choice"). Worse, the old updates-dict ordering wrote
``RAM Expansion Unit: "Enabled"`` *before* the fallible ``Cartridge``
write, so a C64U was left half-mutated when the write raised.

These tests pin the resulting cross-device contract:

  * :func:`set_reu` probes the ``Cartridge`` presets once and, when
    including the ``Cartridge`` write, orders it FIRST so a firmware
    rejection aborts before the REU is half-enabled.
  * On a C64U (presets ``[""]``) the ``Cartridge`` write is omitted.
  * An inconclusive probe (probe GET raises) preserves legacy U64E
    behavior: ``Cartridge`` is written and written first.
  * :func:`restore_state` applies the same gate to a snapshotted
    ``Cartridge`` value.

The ordering assertions need visibility into the per-item PUT loop that
``Ultimate64Client.set_config_items`` runs, so they use the
:class:`_OrderingClient` fake below (mirroring the pattern in
``test_turbo_cross_device.py``) rather than a bare ``MagicMock``. This
fake additionally serves a configurable ``get_config_item`` response and
records item-level call order across both categories.
"""
from __future__ import annotations

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_CART,
    U64StateSnapshot,
    restore_state,
    set_reu,
)

_ITEM_CARTRIDGE = "Cartridge"
_ITEM_REU_ENABLED = "RAM Expansion Unit"
_ITEM_REU_SIZE = "REU Size"


def _cart_item_response(presets: list[str], current: str = "REU") -> dict:
    """A ``get_config_item(CAT_CART, "Cartridge")`` response shape.

    Mirrors the live-verified structure: the item map is nested under the
    category name, carrying ``current`` / ``presets`` / ``default``.
    """
    return {
        CAT_CART: {
            _ITEM_CARTRIDGE: {
                "current": current,
                "presets": presets,
                "default": "",
            }
        },
        "errors": [],
    }


# U64 Elite firmware 3.14: "REU" is a real, settable preset.
_U64E_CART_RESPONSE = _cart_item_response(presets=["", "REU", "Action Replay"])
# C64 Ultimate firmware 1.1.0: only the empty preset is settable.
_C64U_CART_RESPONSE = _cart_item_response(presets=[""])


class _OrderingClient:
    """Mirrors Ultimate64Client.set_config_items' per-item loop so intra-call
    ordering is observable, and serves a configurable get_config_item probe.

    The real client iterates ``updates.items()`` calling ``set_config_item``
    per entry and does not catch exceptions, so a firmware 4xx on the first
    item aborts the batch before the rest are PUT. Recording every
    ``(item, value)`` in :attr:`item_calls` lets a test assert exactly which
    items reached the wire before a raise, and in what order.

    :param cart_item_response: What ``get_config_item`` returns for the
        ``Cartridge`` probe. ``None`` returns ``None``.
    :param probe_raises: When set, ``get_config_item`` raises this instead
        of returning — used to exercise the inconclusive-probe path.
    :param reject_value: When a ``set_config_item`` value equals this, the
        fake raises :class:`Ultimate64Error` (HTTP 400) — the firmware
        rejection path.
    """

    def __init__(
        self,
        cart_item_response: dict | None = None,
        probe_raises: Exception | None = None,
        reject_value: str | None = None,
    ) -> None:
        self.item_calls: list[tuple[str, object]] = []
        self.item_probe_calls: list[tuple[str, str]] = []
        self._cart_item_response = cart_item_response
        self._probe_raises = probe_raises
        self._reject_value = reject_value

    def get_config_item(self, category: str, item: str) -> dict | None:
        self.item_probe_calls.append((category, item))
        if self._probe_raises is not None:
            raise self._probe_raises
        return self._cart_item_response

    def set_config_items(self, category: str, updates: dict) -> None:
        for item, value in updates.items():
            self.set_config_item(category, item, value)

    def set_config_item(self, category: str, item: str, value: object) -> None:
        self.item_calls.append((item, value))
        if self._reject_value is not None and value == self._reject_value:
            raise Ultimate64Error(
                f"PUT /v1/configs returned HTTP 400: "
                f"{value!r} is not a valid choice for {item}",
                status=400,
                body=f'{{"errors":["{value} not a valid choice"]}}',
            )

    def _items(self) -> list[str]:
        """Item names that reached set_config_item, in call order."""
        return [item for item, _ in self.item_calls]


# --------------------------------------------------------------------------- #
# A. set_reu(enabled=True) — cross-generation dispatch                        #
# --------------------------------------------------------------------------- #

class TestSetReuEnable:
    def test_u64e_shape_writes_cartridge_first(self) -> None:
        """U64E presets include "REU": all three items applied, Cartridge
        ordered first, then RAM Expansion Unit, then REU Size."""
        client = _OrderingClient(cart_item_response=_U64E_CART_RESPONSE)
        set_reu(client, True, size=16)
        assert client._items() == [
            _ITEM_CARTRIDGE,
            _ITEM_REU_ENABLED,
            _ITEM_REU_SIZE,
        ]
        assert (_ITEM_CARTRIDGE, "REU") in client.item_calls
        # Exactly one probe, against the Cartridge item.
        assert client.item_probe_calls == [(CAT_CART, _ITEM_CARTRIDGE)]

    def test_c64u_shape_omits_cartridge(self) -> None:
        """C64U presets are [""]: no Cartridge call; RAM Expansion Unit and
        REU Size applied."""
        client = _OrderingClient(cart_item_response=_C64U_CART_RESPONSE)
        set_reu(client, True, size=16)
        assert client._items() == [_ITEM_REU_ENABLED, _ITEM_REU_SIZE]
        assert not any(item == _ITEM_CARTRIDGE for item, _ in client.item_calls)
        assert client.item_probe_calls == [(CAT_CART, _ITEM_CARTRIDGE)]

    def test_probe_raises_falls_back_to_legacy_cartridge_first(self) -> None:
        """An inconclusive probe (GET raises Ultimate64Error) preserves legacy
        U64E behavior: Cartridge is written, and written first."""
        client = _OrderingClient(
            probe_raises=Ultimate64Error("probe boom", status=500, body="")
        )
        set_reu(client, True, size=16)
        assert client._items() == [
            _ITEM_CARTRIDGE,
            _ITEM_REU_ENABLED,
            _ITEM_REU_SIZE,
        ]

    def test_cartridge_rejection_never_half_applies(self) -> None:
        """Load-bearing invariant: on a U64E-shaped device whose firmware
        rejects the Cartridge write with 400, the error propagates and
        RAM Expansion Unit is NEVER written — no half-apply."""
        client = _OrderingClient(
            cart_item_response=_U64E_CART_RESPONSE,
            reject_value="REU",
        )
        with pytest.raises(Ultimate64Error) as exc_info:
            set_reu(client, True, size=16)
        assert exc_info.value.status == 400
        assert (_ITEM_CARTRIDGE, "REU") in client.item_calls
        assert not any(
            item == _ITEM_REU_ENABLED for item, _ in client.item_calls
        )
        assert not any(item == _ITEM_REU_SIZE for item, _ in client.item_calls)

    def test_disable_does_not_probe(self) -> None:
        """enabled=False: no probe GET, no Cartridge write, a single
        Disabled write."""
        client = _OrderingClient(cart_item_response=_U64E_CART_RESPONSE)
        set_reu(client, False)
        assert client.item_probe_calls == []
        assert client.item_calls == [(_ITEM_REU_ENABLED, "Disabled")]


# --------------------------------------------------------------------------- #
# B. restore_state — same cross-generation gate on the Cartridge field        #
# --------------------------------------------------------------------------- #

def _snapshot(cartridge: str = "REU") -> U64StateSnapshot:
    return U64StateSnapshot(
        turbo_control="Off",
        cpu_speed=" 1",
        reu_enabled="Enabled",
        reu_size="16 MB",
        cartridge=cartridge,
    )


class TestRestoreStateCartridge:
    def test_c64u_shape_skips_mirrored_cartridge(self) -> None:
        """C64U presets [""]: a snapshotted mirrored Cartridge="REU" is not
        written back, but the other fields still restore."""
        client = _OrderingClient(cart_item_response=_C64U_CART_RESPONSE)
        restore_state(client, _snapshot(cartridge="REU"))
        assert not any(item == _ITEM_CARTRIDGE for item, _ in client.item_calls)
        cart_items = {
            item for item, _ in client.item_calls if item != "CPU Speed"
        }
        assert _ITEM_REU_ENABLED in cart_items
        assert _ITEM_REU_SIZE in cart_items
        assert client.item_probe_calls == [(CAT_CART, _ITEM_CARTRIDGE)]

    def test_u64e_shape_writes_cartridge(self) -> None:
        """U64E presets include "REU": the snapshotted Cartridge value is
        restored."""
        client = _OrderingClient(cart_item_response=_U64E_CART_RESPONSE)
        restore_state(client, _snapshot(cartridge="REU"))
        assert (_ITEM_CARTRIDGE, "REU") in client.item_calls

    def test_empty_cartridge_skips_write_without_probing(self) -> None:
        """An empty snapshotted cartridge is skipped by the existing guard and
        does not even probe (short-circuit before the presets check)."""
        client = _OrderingClient(cart_item_response=_U64E_CART_RESPONSE)
        restore_state(client, _snapshot(cartridge=""))
        assert not any(item == _ITEM_CARTRIDGE for item, _ in client.item_calls)
        assert client.item_probe_calls == []
