"""Offline coverage for the SID restore fixture in the audio-capture module.

The fixture itself only ever runs against hardware, but where it writes --
and, just as much, where it must NOT write -- is where a silent mistake
would hide, and a mistake there leaves the bench dirty for every later
run. So it is driven here with a mock client.

Two stores, two rules (#447):

* ``SID Addressing`` is a selector store and a ``BASELINE_CATEGORIES``
  member, so each slot address goes back to the ``default`` the device
  reports -- not to the value read at entry, which can be a SIGKILLed
  predecessor's residue (the #334 baseline is ``current == default``).
* ``SID Sockets Configuration`` is in :data:`BASELINE_NEVER_TOUCH` and its
  enables go back to the **entry value**. Writing their default is not a
  tidy-up, it is a power cut: ``ConfigStore::reset`` sets ``SID Socket
  1/2 = Disabled`` and ``U64SidSockets::effectuate_settings``
  (``u64_config.cc:744-800``) then drops the PLD regulator bits, so the
  socketed SIDs go dark while the config read still looks clean (U64E,
  two 8580s, 2026-09-05, n=3), and detection only re-runs at boot or from
  the on-device menu. The store holds detection results, not reset
  products. ``test_socket_enables_go_back_to_the_entry_value_not_the_default``
  and its untouched-bench sibling are the pins for that.

The fixture is a generator function; these tests step it by hand rather
than going through pytest, which is what lets a mock stand in for the
device.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_ADDRESSING,
    CAT_SID_SOCKETS,
)

# The module is U64_HOST-gated at test level, but importing it is fine.
# Reached through the module rather than `from ... import`: a fixture
# bound to a module-level name here would be collected as a fixture of
# THIS module too, and it is autouse, so every test below would demand a
# live `u64_client`.
import test_u64_audio_capture_live as _live

#: The undecorated generator behind the fixture.
restore_sid_config = _live.restore_sid_config.__wrapped__

ADDRESS_ITEMS = (
    "SID Socket 1 Address",
    "SID Socket 2 Address",
    "UltiSID 1 Address",
    "UltiSID 2 Address",
)
SOCKET_ITEMS = ("SID Socket 1", "SID Socket 2")

#: The device's stock allocation: all four slots on $D400, which is also
#: the item default -- so a restore onto it grows an occupant set.
STOCK_ADDRESS = "$D400"
#: What the socket-enable items read on this bench, against a default of
#: "Disabled" (tests/test_entry_baseline.py records the divergence).
SOCKET_ENTRY = "Enabled"
SOCKET_DEFAULT = "Disabled"


def _client(
    addresses: dict[str, str] | None = None,
    sockets: dict[str, str] | None = None,
) -> MagicMock:
    """A device whose reads reflect a mutable in-memory config."""
    client = MagicMock()
    state = {
        CAT_SID_ADDRESSING: dict(
            addresses or {item: STOCK_ADDRESS for item in ADDRESS_ITEMS}
        ),
        CAT_SID_SOCKETS: dict(
            sockets or {item: SOCKET_ENTRY for item in SOCKET_ITEMS}
        ),
    }
    defaults = {
        CAT_SID_ADDRESSING: {item: STOCK_ADDRESS for item in ADDRESS_ITEMS},
        CAT_SID_SOCKETS: {item: SOCKET_DEFAULT for item in SOCKET_ITEMS},
    }

    def get_item(cat: str, item: str) -> dict:
        return {"current": state[cat][item], "default": defaults[cat][item]}

    def set_item(cat: str, item: str, value) -> None:
        state[cat][item] = value

    client.get_config_item.side_effect = get_item
    client.set_config_item.side_effect = set_item
    client._state = state
    return client


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


def _writes(client: MagicMock, category: str) -> list[tuple[str, object]]:
    return [
        (c.args[1], c.args[2])
        for c in client.set_config_item.call_args_list
        if c.args[0] == category
    ]


# --------------------------------------------------------------------------- #
# SID Addressing: back to the device default                                  #
# --------------------------------------------------------------------------- #

def test_addresses_go_back_to_their_device_default() -> None:
    """The exact leak that poisoned the bench: $D420 left behind."""
    client = _client()

    def body() -> None:
        client.set_config_item(CAT_SID_ADDRESSING, "SID Socket 2 Address", "$D420")

    _run(client, body)
    assert client._state[CAT_SID_ADDRESSING] == {
        item: STOCK_ADDRESS for item in ADDRESS_ITEMS
    }


def test_an_address_drifted_at_entry_is_corrected_not_reinstalled() -> None:
    """#447: the entry value can be a dead lane's residue.

    A snapshot-and-restore fixture would put ``$D520`` back and report
    success -- which is how a drifted ``SID Socket 2 Address`` once
    survived four independent read paths.
    """
    drifted = {item: STOCK_ADDRESS for item in ADDRESS_ITEMS}
    drifted["SID Socket 2 Address"] = "$D520"
    client = _client(addresses=drifted)

    _run(client, lambda: None)

    assert client._state[CAT_SID_ADDRESSING]["SID Socket 2 Address"] == STOCK_ADDRESS
    assert ("SID Socket 2 Address", "$D520") not in _writes(client, CAT_SID_ADDRESSING)


def test_restore_runs_after_a_test_raises_partway() -> None:
    """A test that dies mid-write is the case most likely to drift.

    Honest limitation: this does not independently falsify anything the
    sibling tests do not. pytest finalises a fixture by calling ``next``
    on it whether or not the test failed -- it never throws into the
    generator -- so the ``try/finally`` around the ``yield`` is
    defensive rather than load-bearing. The case is covered here because
    it is the one people assume is unhandled.
    """
    client = _client()

    def body() -> None:
        client.set_config_item(CAT_SID_ADDRESSING, "UltiSID 2 Address", "$D520")
        raise RuntimeError("capture blew up")

    with pytest.raises(RuntimeError):
        _run(client, body)
    assert client._state[CAT_SID_ADDRESSING]["UltiSID 2 Address"] == STOCK_ADDRESS


def test_restoring_every_slot_onto_the_stock_pile_is_not_refused() -> None:
    """All four defaults are $D400, so the restore grows an occupant set.

    The old fixture went through ``set_sid_address_map`` and needed an
    ``allow_conflicts`` override or the delta guard would have refused the
    whole restore while looking like it had done its job. Per-item
    ``set_config_item`` PUTs have no such guard -- this pins that the
    restore still lands on every slot.
    """
    client = _client()

    def body() -> None:
        client.set_config_item(CAT_SID_ADDRESSING, "SID Socket 2 Address", "$D520")

    _run(client, body)
    assert client._state[CAT_SID_ADDRESSING] == {
        item: STOCK_ADDRESS for item in ADDRESS_ITEMS
    }


# --------------------------------------------------------------------------- #
# SID Sockets Configuration: back to the ENTRY value -- never the default     #
# --------------------------------------------------------------------------- #

def test_socket_enables_go_back_to_the_entry_value_not_the_default() -> None:
    """Writing the default here cuts power to the socketed SIDs.

    ``SID Sockets Configuration`` is in ``BASELINE_NEVER_TOUCH``: its
    ``effectuate`` drops the PLD regulator bits, so a "restore" to
    ``Disabled`` powers the chips off with nothing in the report to say
    so, and detection never re-runs over REST.
    """
    client = _client()

    def body() -> None:
        client.set_config_item(CAT_SID_SOCKETS, "SID Socket 2", "Disabled")

    _run(client, body)

    assert client._state[CAT_SID_SOCKETS] == {
        item: SOCKET_ENTRY for item in SOCKET_ITEMS
    }
    restores = _writes(client, CAT_SID_SOCKETS)[1:]  # [0] is the body's own write
    assert restores == [(item, SOCKET_ENTRY) for item in SOCKET_ITEMS], restores


def test_the_socket_default_is_never_written_on_an_untouched_bench() -> None:
    """The autouse fixture runs after every test, read-only ones included.

    So a teardown that wrote the socket default unconditionally would
    power the SIDs off on a bench where no test went near them.
    """
    client = _client()
    _run(client, lambda: None)

    assert client._state[CAT_SID_SOCKETS] == {
        item: SOCKET_ENTRY for item in SOCKET_ITEMS
    }
    assert SOCKET_DEFAULT not in [
        value for _item, value in _writes(client, CAT_SID_SOCKETS)
    ]


def test_a_socket_reporting_no_current_value_refuses_to_start() -> None:
    """Exit could not put it back, so the run stops before the test body."""
    client = _client()
    client.get_config_item.side_effect = lambda cat, item: (
        {"default": SOCKET_DEFAULT}
        if cat == CAT_SID_SOCKETS
        else {"current": STOCK_ADDRESS, "default": STOCK_ADDRESS}
    )
    gen = restore_sid_config(client)
    with pytest.raises(RuntimeError, match="no current value"):
        next(gen)
    client.set_config_item.assert_not_called()


# --------------------------------------------------------------------------- #
# Shape of the restore                                                        #
# --------------------------------------------------------------------------- #

def test_restores_item_by_item_and_never_in_one_batch() -> None:
    """One bodyless PUT per item.

    ``set_config_items`` stops at the first rejection and leaves the rest
    holding the test's values, and ``set_config_items_batch`` is the POST
    that leaves a ``/Temp`` attachment on leak-prone firmware.
    """
    client = _client()
    _run(client, lambda: None)

    client.set_config_items.assert_not_called()
    client.set_config_items_batch.assert_not_called()
    assert _writes(client, CAT_SID_ADDRESSING) == [
        (item, STOCK_ADDRESS) for item in ADDRESS_ITEMS
    ]
    assert _writes(client, CAT_SID_SOCKETS) == [
        (item, SOCKET_ENTRY) for item in SOCKET_ITEMS
    ]


def test_an_address_reporting_no_default_refuses_to_start() -> None:
    """The old fixture logged and ran the test unprotected; a device that
    cannot be restored must stop the run, not quietly poison every later
    measurement on the bench."""
    client = _client()
    client.get_config_item.side_effect = lambda cat, item: (
        {"current": STOCK_ADDRESS}
        if cat == CAT_SID_ADDRESSING
        else {"current": SOCKET_ENTRY, "default": SOCKET_DEFAULT}
    )
    gen = restore_sid_config(client)
    with pytest.raises(RuntimeError, match="no default"):
        next(gen)
    client.set_config_item.assert_not_called()


def test_every_step_is_attempted_even_when_one_fails() -> None:
    """A rejected item must not leave the rest holding the test's values."""
    client = _client()
    rejected = "SID Socket 1 Address"

    def set_item(cat: str, item: str, value) -> None:
        if item == rejected:
            raise RuntimeError("firmware said no")
        client._state[cat][item] = value

    def body() -> None:
        for item in ADDRESS_ITEMS:
            client._state[CAT_SID_ADDRESSING][item] = "$D520"

    client.set_config_item.side_effect = set_item
    with pytest.raises(RuntimeError, match="SID config restore"):
        _run(client, body)

    for item in ADDRESS_ITEMS:
        if item != rejected:
            assert client._state[CAT_SID_ADDRESSING][item] == STOCK_ADDRESS
    assert client._state[CAT_SID_SOCKETS] == {
        item: SOCKET_ENTRY for item in SOCKET_ITEMS
    }
