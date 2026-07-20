"""Cross-device turbo / CPU-speed contract tests (mocked Ultimate64Client).

Background (PR #131): ``CPU_SPEED_VALUES`` in ``ultimate64_schema`` is a
*superset* across device generations. The U64E (firmware 3.14) reports
``" 5"`` but not ``"64"``; the C64 Ultimate (firmware 1.1.0) reports
``"64"`` but not ``" 5"``. A speed that is valid on one generation but
absent on the other still passes the harness's *local* schema validation
(``cpu_speed_enum``) — the device only rejects it at *set* time, with an
HTTP 4xx.

These tests pin the resulting cross-device contract:

  * A foreign speed passes local validation and reaches the wire.
  * The firmware's 4xx propagates as :class:`Ultimate64Error` with its
    status preserved.
  * Because ``set_turbo_mhz`` writes ``CPU Speed`` *before*
    ``Turbo Control`` and the client's per-item loop aborts on the first
    failure, turbo is never left half-enabled.

The write-ordering assertions need visibility into the per-item PUT loop
that ``Ultimate64Client.set_config_items`` runs, so they use the
:class:`_OrderingClient` fake below rather than a bare ``MagicMock``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_U64_SPECIFIC,
    set_turbo_mhz,
)
from c64_test_harness.backends.ultimate64_schema import cpu_speed_enum


def _make_client() -> MagicMock:
    """Build a MagicMock that looks like an Ultimate64Client."""
    return MagicMock()


def _u64_specific(turbo: str = "Manual", cpu_speed: str = " 1") -> dict:
    """A ``get_config_category(CAT_U64_SPECIFIC)`` response shape."""
    return {
        "U64 Specific Settings": {
            "Turbo Control": turbo,
            "CPU Speed": cpu_speed,
            "System Mode": "NTSC",
        },
        "errors": [],
    }


class _OrderingClient:
    """Mirrors Ultimate64Client.set_config_items' per-item loop so intra-call
    ordering is observable.

    The real client iterates ``updates.items()`` calling ``set_config_item``
    per entry and does not catch exceptions, so a firmware 4xx on the first
    item aborts the batch before the rest are PUT. Recording every
    ``(item, value)`` in :attr:`item_calls` lets a test assert exactly which
    items reached the wire before the raise.
    """

    def __init__(
        self,
        reject_value: str | None = None,
        get_category_return: dict | None = None,
    ) -> None:
        self.item_calls: list[tuple[str, object]] = []
        self.category_gets: list[str] = []
        self._reject_value = reject_value
        self._get_category_return = get_category_return

    def set_config_items(self, category: str, updates: dict) -> None:
        for item, value in updates.items():
            self.set_config_item(category, item, value)

    def set_config_item(self, category: str, item: str, value: object) -> None:
        self.item_calls.append((item, value))
        if self._reject_value is not None and value == self._reject_value:
            raise Ultimate64Error(
                f"PUT /v1/configs returned HTTP 400: "
                f"CPU Speed value {value!r} not supported by this firmware",
                status=400,
                body=f'{{"errors":["Unknown value {value}"]}}',
            )

    def get_config_category(self, category: str) -> dict | None:
        self.category_gets.append(category)
        return self._get_category_return


# --------------------------------------------------------------------------- #
# A. Foreign-speed rejection at set time                                      #
# --------------------------------------------------------------------------- #

class TestForeignSpeedRejection:
    """A speed valid on the other device generation is rejected by firmware."""

    def test_foreign_speed_rejection_propagates_ultimate64error(self) -> None:
        """C64U firmware rejects "64"? No — the U64E does. set_turbo_mhz(64)
        against a firmware that lacks "64" surfaces the 4xx as Ultimate64Error
        with status 400 preserved."""
        client = _OrderingClient(reject_value="64")
        with pytest.raises(Ultimate64Error) as exc_info:
            set_turbo_mhz(client, 64)
        assert exc_info.value.status == 400

    def test_foreign_speed_rejection_aborts_before_turbo_control(self) -> None:
        """Load-bearing invariant: CPU Speed is PUT first and its rejection
        aborts the batch, so Turbo Control is never enabled — turbo is never
        left half-on."""
        client = _OrderingClient(reject_value="64")
        with pytest.raises(Ultimate64Error):
            set_turbo_mhz(client, 64)
        assert ("CPU Speed", "64") in client.item_calls
        assert not any(item == "Turbo Control" for item, _ in client.item_calls)

    def test_u64e_rejecting_speed5_symmetric(self) -> None:
        """The mirror direction: the C64 Ultimate drops " 5", so a firmware
        that lacks it rejects set_turbo_mhz(5) the same way, still before
        Turbo Control is touched."""
        client = _OrderingClient(reject_value=" 5")
        with pytest.raises(Ultimate64Error) as exc_info:
            set_turbo_mhz(client, 5)
        assert exc_info.value.status == 400
        assert ("CPU Speed", " 5") in client.item_calls
        assert not any(item == "Turbo Control" for item, _ in client.item_calls)


# --------------------------------------------------------------------------- #
# B. Local validation boundary                                                #
# --------------------------------------------------------------------------- #

class TestLocalValidationBoundary:
    """The superset schema accepts every device generation's speed locally;
    only unsupported/typed-wrong values raise before the network."""

    def test_speed_64_passes_local_validation(self) -> None:
        """"64" (C64 Ultimate-only) passes cpu_speed_enum and reaches the
        wire with CPU Speed first."""
        assert cpu_speed_enum(64) == "64"
        client = _make_client()
        set_turbo_mhz(client, 64)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC,
            {"CPU Speed": "64", "Turbo Control": "Manual"},
        )

    def test_speed_5_passes_local_validation(self) -> None:
        """" 5" (U64E-only, note the leading space) passes cpu_speed_enum and
        reaches the wire."""
        assert cpu_speed_enum(5) == " 5"
        client = _make_client()
        set_turbo_mhz(client, 5)
        client.set_config_items.assert_called_once_with(
            CAT_U64_SPECIFIC,
            {"CPU Speed": " 5", "Turbo Control": "Manual"},
        )

    @pytest.mark.parametrize("mhz", [96, 7, 0, -1])
    def test_unsupported_speeds_raise_valueerror_before_network(
        self, mhz: int
    ) -> None:
        """A speed absent from the superset raises ValueError locally and
        never touches the network."""
        client = _make_client()
        with pytest.raises(ValueError, match="Unsupported CPU speed"):
            set_turbo_mhz(client, mhz)
        client.set_config_items.assert_not_called()

    @pytest.mark.parametrize("bad", ["64", True])
    def test_bad_type_raises(self, bad: object) -> None:
        """A str (even one matching an enum) or a bool raises ValueError
        before any network call — bool is rejected explicitly since it is an
        int subclass."""
        client = _make_client()
        with pytest.raises(ValueError, match="must be int or None"):
            set_turbo_mhz(client, bad)  # type: ignore[arg-type]
        client.set_config_items.assert_not_called()


# --------------------------------------------------------------------------- #
# C. Extensibility placeholder — read-back verification                       #
# --------------------------------------------------------------------------- #

class TestReadbackVerification:
    """Documents an intended future contract, not yet implemented."""

    @pytest.mark.xfail(
        reason="read-back verification not yet implemented", strict=False
    )
    def test_readback_verifies_applied_speed(self) -> None:
        """Intended future behavior: after a successful set, a verifying
        variant GETs the U64 Specific category back and raises when the
        firmware's reported CPU Speed disagrees with what was written (e.g.
        a silent clamp). Here the device accepts the "64" write but reports
        " 1", so the verifying call must raise. No such variant exists today,
        so this xfails at import."""
        client = _OrderingClient(
            get_category_return=_u64_specific(turbo="Manual", cpu_speed=" 1")
        )
        from c64_test_harness.backends.ultimate64_helpers import (  # noqa: F401
            set_turbo_mhz_verified,
        )

        with pytest.raises(Ultimate64Error):
            set_turbo_mhz_verified(client, 64)
        assert CAT_U64_SPECIFIC in client.category_gets
