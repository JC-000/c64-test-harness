"""Safe SID slot remapping and SID-Addressing state coverage (issue #196.2).

Offline: the device is a stand-in that keeps a mutable copy of the
``SID Addressing`` category and records the order of every PUT, so the
ordering invariants are assertable without hardware.

Firmware references (Ultimate 3.15 pre-release tree,
``~/Documents/1541u-315preview``):

- ``software/u64/u64_config.cc:411`` — ``CFG_AUTO_MIRRORING`` is an
  ``en_dis`` enum whose **default is 1 (Enabled)**.
- ``software/u64/u64_config.cc:857-858`` — the enabled path calls
  ``auto_mirror()``.
- ``software/u64/u64_config.cc:2378-2430`` — ``auto_mirror`` clears
  decode mask bits A5..A9 wherever the in-range slots agree, so an
  address no slot occupies is answered by a mirror of one that does.
- ``software/api/route_configs.cc:26`` — ``CFG_TYPE_FUNC`` items (the
  ``Visual SID Address Editor``) are omitted from a category GET.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_CART,
    CAT_SID_ADDRESSING,
    CAT_U64_SPECIFIC,
    Ultimate64RestoreError,
    isolated_sid_addressing,
    restore_config_items,
    restore_state,
    set_sid_auto_mirroring,
    snapshot_state,
)
from c64_test_harness.backends.ultimate64_schema import (
    SID_ADDRESS_VALUES,
    SID_AUTO_MIRRORING_ITEM,
    SID_AUTO_MIRRORING_VALUES,
    SidSlot,
)

_MIRROR = SID_AUTO_MIRRORING_ITEM

#: The device's factory state: everything on $D400 with mirroring on.
_FACTORY = {
    "SID Socket 1 Address": "$D400",
    "SID Socket 2 Address": "$D400",
    "Ext DualSID Range Split": "Off",
    "UltiSID 1 Address": "$D400",
    "UltiSID 2 Address": "$D400",
    "UltiSID Range Split": "Off",
    "Paddle Override": "Enabled",
    _MIRROR: "Enabled",
}


class FakeU64:
    """Minimal Ultimate64Client stand-in for the SID Addressing category.

    Holds live state so a read-back sees the previous write, and records
    every PUT in order so the ordering invariants can be asserted.
    ``set_config_items`` fans out to ``set_config_item`` exactly as the
    real client does (one PUT per item, insertion order, no per-item
    error handling).
    """

    def __init__(self, category: dict[str, str] | None = None) -> None:
        self.state = {
            CAT_SID_ADDRESSING: dict(category or _FACTORY),
            CAT_U64_SPECIFIC: {
                "Turbo Control": "Enabled",
                "CPU Speed": " 1",
                "Badline Timing": "Enabled",
            },
            CAT_CART: {
                "RAM Expansion Unit": "Disabled",
                "REU Size": "2 MB",
                "Cartridge": "",
                "Bus Operation Mode": "Normal",
            },
        }
        self.puts: list[tuple[str, str, str]] = []
        self.reject: set[str] = set()
        self.frozen: set[str] = set()

    # --- reads -------------------------------------------------------
    def get_config_category(self, category: str) -> dict:
        return {category: dict(self.state[category]), "errors": []}

    def get_config_item(self, category: str, item: str) -> dict:
        # Item map, unwrapped from the REST envelope (issue #214).
        return {
            "current": self.state[category][item],
            "values": list(SID_ADDRESS_VALUES),
            "default": "$D400",
        }

    # --- writes ------------------------------------------------------
    def set_config_item(self, category: str, item: str, value) -> None:
        self.puts.append((category, item, value))
        if item in self.reject:
            raise Ultimate64Error(f"HTTP 400: '{value}' rejected for {item}")
        if item not in self.frozen:
            self.state[category][item] = value

    def set_config_items(self, category: str, updates: dict) -> None:
        for item, value in updates.items():
            self.set_config_item(category, item, value)

    def items_put(self, category: str = CAT_SID_ADDRESSING) -> list[str]:
        return [item for cat, item, _ in self.puts if cat == category]


def _addresses(client: FakeU64) -> dict[str, str]:
    inner = client.state[CAT_SID_ADDRESSING]
    return {k: v for k, v in inner.items() if k.endswith("Address")}


# --------------------------------------------------------------------------- #
# Auto Address Mirroring                                                      #
# --------------------------------------------------------------------------- #
class TestAutoMirroring:
    def test_the_device_default_is_enabled(self) -> None:
        """u64_config.cc:411 — the ``def`` column is 1. This is why a
        distinct address map is not sufficient on a fresh device."""
        assert _FACTORY[_MIRROR] == "Enabled"
        assert SID_AUTO_MIRRORING_VALUES == ("Disabled", "Enabled")

    def test_disable_writes_and_reads_back(self) -> None:
        client = FakeU64()
        set_sid_auto_mirroring(client, False)
        assert client.state[CAT_SID_ADDRESSING][_MIRROR] == "Disabled"
        assert (CAT_SID_ADDRESSING, _MIRROR, "Disabled") in client.puts

    def test_enable_writes_the_other_value(self) -> None:
        client = FakeU64({**_FACTORY, _MIRROR: "Disabled"})
        set_sid_auto_mirroring(client, True)
        assert client.state[CAT_SID_ADDRESSING][_MIRROR] == "Enabled"

    def test_a_write_that_does_not_take_raises(self) -> None:
        """Accepted-but-not-applied is the failure mode that leaves a run
        reading mirrors while looking correct."""
        client = FakeU64()
        client.frozen.add(_MIRROR)
        with pytest.raises(Ultimate64Error, match="cannot be trusted"):
            set_sid_auto_mirroring(client, False)


# --------------------------------------------------------------------------- #
# isolated_sid_addressing                                                     #
# --------------------------------------------------------------------------- #
class TestIsolatedSidAddressing:
    def test_mirroring_is_disabled_before_any_address_is_written(self) -> None:
        """Order matters: a widened old base over a new one is exactly
        the state this protects against."""
        client = FakeU64()
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
        ):
            written = client.items_put()
        assert written[0] == _MIRROR
        assert client.puts[0][2] == "Disabled"
        assert any(i.endswith("Address") for i in written)

    def test_every_slot_gets_its_own_base(self) -> None:
        """Including the two not under test: a second real SID left
        sharing a decode is written by every measurement."""
        client = FakeU64()
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
        ) as final:
            mapped = [a for a in final.values() if a != "Unmapped"]
            assert len(set(mapped)) == len(mapped) == 4
            assert final[SidSlot.SOCKET1] == "$D400"
            assert final[SidSlot.SOCKET2] == "$D420"

    def test_the_factory_state_would_otherwise_measure_one_chip_twice(
        self,
    ) -> None:
        """Sanity check on the premise: all four start on $D400."""
        client = FakeU64()
        assert len(set(_addresses(client).values())) == 1

    def test_unmapped_slots_are_left_unmapped_under_distinct(self) -> None:
        """An unmapped slot cannot alias anything (odd offset 0x01), and
        mapping it would put a chip on the bus that was not there."""
        client = FakeU64({
            **_FACTORY,
            "UltiSID 1 Address": "Unmapped",
            "UltiSID 2 Address": "Unmapped",
        })
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
        ) as final:
            assert final[SidSlot.ULTISID1] == "Unmapped"
            assert final[SidSlot.ULTISID2] == "Unmapped"

    def test_a_non_colliding_slot_is_not_moved(self) -> None:
        client = FakeU64({**_FACTORY, "UltiSID 2 Address": "$D700"})
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400"}
        ) as final:
            assert final[SidSlot.ULTISID2] == "$D700"

    def test_others_unmapped_is_the_strongest_isolation(self) -> None:
        client = FakeU64()
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400"}, others="unmapped"
        ) as final:
            assert final == {
                SidSlot.SOCKET1: "$D400",
                SidSlot.SOCKET2: "Unmapped",
                SidSlot.ULTISID1: "Unmapped",
                SidSlot.ULTISID2: "Unmapped",
            }

    def test_others_leave_refuses_a_residual_conflict(self) -> None:
        client = FakeU64()
        with pytest.raises(ValueError, match="two slots on one base"):
            with isolated_sid_addressing(
                client, {SidSlot.SOCKET1: "$D400"}, others="leave"
            ):
                pass

    def test_others_leave_accepts_an_already_clean_map(self) -> None:
        client = FakeU64({
            **_FACTORY,
            "SID Socket 2 Address": "$D420",
            "UltiSID 1 Address": "$D440",
            "UltiSID 2 Address": "$D460",
        })
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400"}, others="leave"
        ) as final:
            assert final[SidSlot.ULTISID1] == "$D440"

    def test_an_unknown_others_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'distinct', 'unmapped'"):
            with isolated_sid_addressing(
                FakeU64(), {SidSlot.SOCKET1: "$D400"}, others="whatever"
            ):
                pass

    def test_an_empty_mapping_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            with isolated_sid_addressing(FakeU64(), {}):
                pass

    def test_a_bad_address_is_refused_before_anything_is_written(self) -> None:
        client = FakeU64()
        with pytest.raises(ValueError):
            with isolated_sid_addressing(client, {SidSlot.SOCKET1: "$D410"}):
                pass
        assert client.puts == []

    def test_an_address_write_that_did_not_apply_is_refused(self) -> None:
        """The map is read back after the write, like mirroring is.

        Found on hardware (issue #204): with the address writes dropped,
        the slot under test decodes nowhere and the 6510 reads open bus
        at its base, while the helper -- which read back only mirroring
        -- reported a clean run.  A frozen item models an accepted-but-
        not-applied PUT.
        """
        client = FakeU64()
        client.frozen.add("SID Socket 2 Address")
        with pytest.raises(Ultimate64Error, match="SID Socket 2.*D420"):
            with isolated_sid_addressing(
                client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
            ):
                pytest.fail("body ran on an unverified map")
        # Restore still happened, mirroring last.
        assert client.state[CAT_SID_ADDRESSING] == _FACTORY
        assert client.items_put()[-1] == _MIRROR

    def test_the_read_back_covers_slots_that_were_not_written(self) -> None:
        """Every slot in the final map is checked, not only the ones PUT.

        A firmware side effect that moves a slot the helper did not touch
        (modelled here: writing one address item also rewrites another)
        leaves that slot decoding somewhere the map does not say.  A
        read-back restricted to the changed slots would miss it.
        """

        class SideEffectU64(FakeU64):
            def set_config_item(self, category, item, value):
                super().set_config_item(category, item, value)
                if item == "SID Socket 2 Address":
                    self.state[category]["UltiSID 1 Address"] = "$D4A0"

        client = SideEffectU64({
            **_FACTORY,
            "SID Socket 2 Address": "$D420",
            "UltiSID 1 Address": "$D440",
            "UltiSID 2 Address": "$D460",
        })
        with pytest.raises(Ultimate64Error, match="UltiSID 1.*D440.*D4A0"):
            with isolated_sid_addressing(
                client, {SidSlot.SOCKET2: "$D480"}, others="leave"
            ):
                pytest.fail("body ran on an unverified map")

    def test_the_read_back_passes_when_every_write_applied(self) -> None:
        client = FakeU64()
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
        ) as final:
            assert _addresses(client) == {
                f"{slot.value} Address": address
                for slot, address in final.items()
            }


# --------------------------------------------------------------------------- #
# Restore                                                                     #
# --------------------------------------------------------------------------- #
class TestIsolationRestore:
    def test_the_whole_category_comes_back(self) -> None:
        client = FakeU64()
        before = dict(client.state[CAT_SID_ADDRESSING])
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
        ):
            assert client.state[CAT_SID_ADDRESSING] != before
        assert client.state[CAT_SID_ADDRESSING] == before

    def test_mirroring_is_restored_last(self) -> None:
        """Put the widening back only once every base is back."""
        client = FakeU64()
        with isolated_sid_addressing(client, {SidSlot.SOCKET2: "$D420"}):
            mark = len(client.puts)
        restored = client.items_put()[mark:]
        assert restored[-1] == _MIRROR

    def test_restore_runs_when_the_body_raises(self) -> None:
        client = FakeU64()
        before = dict(client.state[CAT_SID_ADDRESSING])
        with pytest.raises(RuntimeError, match="boom"):
            with isolated_sid_addressing(client, {SidSlot.SOCKET2: "$D420"}):
                raise RuntimeError("boom")
        assert client.state[CAT_SID_ADDRESSING] == before

    def test_restore_false_leaves_the_device_remapped(self) -> None:
        client = FakeU64()
        with isolated_sid_addressing(
            client, {SidSlot.SOCKET2: "$D420"}, restore=False
        ):
            pass
        assert client.state[CAT_SID_ADDRESSING][_MIRROR] == "Disabled"
        assert client.state[CAT_SID_ADDRESSING]["SID Socket 2 Address"] == "$D420"

    def test_one_rejected_item_does_not_strand_the_others(self) -> None:
        """``set_config_items`` aborts the batch on the first failure and
        would leave the rest holding the test's values. The restore path
        must attempt all of them."""
        client = FakeU64()
        with pytest.raises(Ultimate64RestoreError):
            with isolated_sid_addressing(
                client, {SidSlot.SOCKET2: "$D420", SidSlot.ULTISID1: "$D440"}
            ):
                client.reject.add("SID Socket 1 Address")
        assert client.state[CAT_SID_ADDRESSING]["SID Socket 2 Address"] == "$D400"
        assert client.state[CAT_SID_ADDRESSING]["UltiSID 1 Address"] == "$D400"
        assert client.state[CAT_SID_ADDRESSING][_MIRROR] == "Enabled"


class TestRestoreConfigItems:
    def test_all_items_are_attempted_and_failures_aggregated(self) -> None:
        client = FakeU64()
        client.reject.add("Paddle Override")
        with pytest.raises(Ultimate64RestoreError) as exc:
            restore_config_items(
                client,
                CAT_SID_ADDRESSING,
                {
                    "Paddle Override": "Disabled",
                    "UltiSID 1 Address": "$D500",
                    _MIRROR: "Disabled",
                },
            )
        assert set(exc.value.failures) == {"Paddle Override"}
        assert client.state[CAT_SID_ADDRESSING]["UltiSID 1 Address"] == "$D500"
        assert client.state[CAT_SID_ADDRESSING][_MIRROR] == "Disabled"

    def test_a_clean_restore_raises_nothing(self) -> None:
        client = FakeU64()
        restore_config_items(
            client, CAT_SID_ADDRESSING, {"Paddle Override": "Disabled"}
        )
        assert client.state[CAT_SID_ADDRESSING]["Paddle Override"] == "Disabled"

    def test_the_error_names_every_failure(self) -> None:
        client = FakeU64()
        client.reject.update({"Paddle Override", _MIRROR})
        with pytest.raises(Ultimate64RestoreError) as exc:
            restore_config_items(
                client,
                CAT_SID_ADDRESSING,
                {"Paddle Override": "Disabled", _MIRROR: "Disabled"},
            )
        assert set(exc.value.failures) == {"Paddle Override", _MIRROR}
        assert "Paddle Override" in str(exc.value)


# --------------------------------------------------------------------------- #
# snapshot_state / restore_state now cover SID Addressing                     #
# --------------------------------------------------------------------------- #
class TestStateSnapshotCoversSidAddressing:
    def test_snapshot_captures_the_category(self) -> None:
        client = FakeU64()
        snap = snapshot_state(client)
        assert snap.sid_addressing == _FACTORY

    def test_mirroring_is_in_the_snapshot(self) -> None:
        """The item the issue says is the trap; without it a run that
        disabled mirroring silently changes every later measurement."""
        assert _MIRROR in snapshot_state(FakeU64()).sid_addressing

    def test_restore_puts_the_category_back(self) -> None:
        client = FakeU64()
        snap = snapshot_state(client)
        client.state[CAT_SID_ADDRESSING].update(
            {"SID Socket 1 Address": "$D700", _MIRROR: "Disabled"}
        )
        restore_state(client, snap)
        assert client.state[CAT_SID_ADDRESSING] == _FACTORY

    def test_restore_writes_mirroring_last(self) -> None:
        client = FakeU64()
        snap = snapshot_state(client)
        client.puts.clear()
        restore_state(client, snap)
        assert client.items_put()[-1] == _MIRROR

    def test_an_old_snapshot_without_the_field_still_restores(self) -> None:
        """Positional/older snapshots must keep working."""
        client = FakeU64()
        snap = snapshot_state(client)
        snap.sid_addressing = {}
        client.puts.clear()
        restore_state(client, snap)
        assert client.items_put() == []

    def test_the_func_item_is_never_snapshotted(self) -> None:
        """``Visual SID Address Editor`` is CFG_TYPE_FUNC and the
        firmware omits it from a GET; writing it back would be an error."""
        client = FakeU64()
        snap = snapshot_state(client)
        assert "Visual SID Address Editor" not in snap.sid_addressing

    def test_non_string_values_are_dropped(self) -> None:
        client = FakeU64()
        client.state[CAT_SID_ADDRESSING]["Some Numeric Item"] = 5
        snap = snapshot_state(client)
        assert "Some Numeric Item" not in snap.sid_addressing
