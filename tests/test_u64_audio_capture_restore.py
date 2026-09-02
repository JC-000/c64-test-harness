"""Offline coverage for the SID restore fixture in the audio-capture module.

The fixture itself only ever runs against hardware, but its logic -- diff
the snapshot, restore only what moved, override the delta guard while
doing so -- is where a silent mistake would hide, and a mistake there
leaves the bench dirty for every later run. So it is driven here with a
mock client.

The fixture is a generator function; these tests step it by hand rather
than going through pytest, which is what lets a mock stand in for the
device.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Error
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_ADDRESSING,
    CAT_SID_SOCKETS,
)
from c64_test_harness.backends.ultimate64_schema import SidSlot

# The module is U64_HOST-gated at test level, but importing it is fine.
# Reached through the module rather than `from ... import`: a fixture
# bound to a module-level name here would be collected as a fixture of
# THIS module too, and it is autouse, so every test below would demand a
# live `u64_client`.
import test_u64_audio_capture_live as _live

#: The undecorated generator behind the fixture.
restore_sid_config = _live.restore_sid_config.__wrapped__


def _client(addresses: dict[str, str], sockets: dict[str, str]) -> MagicMock:
    """A device whose reads reflect a mutable in-memory config."""
    client = MagicMock()
    state = {CAT_SID_ADDRESSING: dict(addresses), CAT_SID_SOCKETS: dict(sockets)}

    def get_category(cat: str) -> dict:
        return {cat: dict(state[cat]), "errors": []}

    def set_items(cat: str, updates: dict) -> None:
        state[cat].update(updates)

    client.get_config_category.side_effect = get_category
    client.set_config_items.side_effect = set_items
    # What a real client raises when the probe fails; the address-choice
    # probe catches this and falls back to the schema superset.
    client.get_config_item.side_effect = Ultimate64Error("no probe")
    client._state = state
    return client


STOCK_ADDRESSES = {
    "SID Socket 1 Address": "$D400",
    "SID Socket 2 Address": "$D400",
    "UltiSID 1 Address": "$D400",
    "UltiSID 2 Address": "$D400",
}
STOCK_SOCKETS = {"SID Socket 1": "Enabled", "SID Socket 2": "Enabled"}


def _run(client: MagicMock, body) -> None:
    """Drive the fixture around *body*, which mutates the fake device."""
    gen = restore_sid_config(client)
    next(gen)
    try:
        body()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_restores_an_address_the_test_moved() -> None:
    """The exact leak that poisoned the bench: $D420 left behind."""
    client = _client(STOCK_ADDRESSES, STOCK_SOCKETS)

    def body() -> None:
        client.set_config_items(
            CAT_SID_ADDRESSING, {"SID Socket 2 Address": "$D420"}
        )

    _run(client, body)
    assert client._state[CAT_SID_ADDRESSING] == STOCK_ADDRESSES


def test_restore_runs_after_a_test_raises_partway() -> None:
    """A test that dies mid-write is the case most likely to drift.

    Honest limitation: this does not independently falsify anything the
    sibling tests do not. pytest finalises a fixture by calling ``next``
    on it whether or not the test failed -- it never throws into the
    generator -- so the ``try/finally`` around the ``yield`` is
    defensive rather than load-bearing, and a mutation removing it
    changes nothing observable. The case is covered here because it is
    the one people assume is unhandled, not because the assertion is
    unique.
    """
    client = _client(STOCK_ADDRESSES, STOCK_SOCKETS)

    def body() -> None:
        client.set_config_items(
            CAT_SID_ADDRESSING, {"UltiSID 2 Address": "$D520"}
        )
        raise RuntimeError("capture blew up")

    with pytest.raises(RuntimeError):
        _run(client, body)
    assert client._state[CAT_SID_ADDRESSING] == STOCK_ADDRESSES


def test_restoring_onto_the_stock_pile_is_not_blocked() -> None:
    """Putting a slot back on $D400 grows an occupant set.

    Without the ``allow_conflicts`` override the delta guard would
    correctly refuse the restore, and the fixture would leave the device
    drifted while looking like it had done its job.
    """
    client = _client(STOCK_ADDRESSES, STOCK_SOCKETS)

    def body() -> None:
        client.set_config_items(
            CAT_SID_ADDRESSING, {"SID Socket 2 Address": "$D520"}
        )

    _run(client, body)
    assert client._state[CAT_SID_ADDRESSING]["SID Socket 2 Address"] == "$D400"


def test_restores_a_socket_enable_state() -> None:
    client = _client(STOCK_ADDRESSES, STOCK_SOCKETS)

    def body() -> None:
        client.set_config_items(CAT_SID_SOCKETS, {"SID Socket 2": "Disabled"})

    _run(client, body)
    assert client._state[CAT_SID_SOCKETS]["SID Socket 2"] == "Enabled"


def test_writes_nothing_when_the_test_changed_nothing() -> None:
    """Read-only tests pay two GETs and no writes."""
    client = _client(STOCK_ADDRESSES, STOCK_SOCKETS)
    _run(client, lambda: None)
    assert client.set_config_items.call_count == 0


def test_restores_only_the_slots_that_moved_in_one_request() -> None:
    """Two slots move, two do not.

    Moves two so the batching is actually observable -- with a single
    moved slot, "one request" holds however the restore is written.
    """
    client = _client(
        {
            "SID Socket 1 Address": "$D400",
            "SID Socket 2 Address": "$D420",
            "UltiSID 1 Address": "$D500",
            "UltiSID 2 Address": "Unmapped",
        },
        STOCK_SOCKETS,
    )

    def body() -> None:
        client.set_config_items(
            CAT_SID_ADDRESSING,
            {"UltiSID 1 Address": "$D540", "SID Socket 2 Address": "$D460"},
        )

    _run(client, body)

    addressing_calls = [
        c for c in client.set_config_items.call_args_list
        if c.args[0] == CAT_SID_ADDRESSING
    ]
    restore_calls = addressing_calls[1:]  # [0] is the body's own write
    assert len(restore_calls) == 1, (
        f"expected one batched restore, got {len(restore_calls)}: "
        f"{[c.args[1] for c in restore_calls]}"
    )
    assert restore_calls[0].args[1] == {
        "SID Socket 2 Address": "$D420",
        "UltiSID 1 Address": "$D500",
    }, "restore should name exactly the slots that moved, at their old values"


def test_a_failed_snapshot_lets_the_test_run_unprotected() -> None:
    """A probe failure must not turn into a spurious test error."""
    client = MagicMock()
    client.get_config_category.side_effect = Ultimate64Error("unreachable")
    gen = restore_sid_config(client)
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
