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


@pytest.fixture(autouse=True)
def _mutate_gate(monkeypatch):
    """Set ``U64_ALLOW_MUTATE``: without it exit writes nothing at all, so
    every test of *what* exit writes needs the gate.  The gate-less tests
    remove it themselves."""
    monkeypatch.setenv("U64_ALLOW_MUTATE", "1")


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


def _drifted_client() -> MagicMock:
    drifted = {item: STOCK_ADDRESS for item in ADDRESS_ITEMS}
    drifted["SID Socket 2 Address"] = "$D520"
    return _client(addresses=drifted)


def test_an_address_drifted_at_entry_is_corrected_under_the_mutate_gate(
    monkeypatch,
) -> None:
    """#447: the entry value can be a dead lane's residue.

    A snapshot-and-restore fixture would put ``$D520`` back and report
    success -- which is how a drifted ``SID Socket 2 Address`` once
    survived four independent read paths.
    """
    monkeypatch.setenv("U64_ALLOW_MUTATE", "1")
    client = _drifted_client()

    _run(client, lambda: None)

    assert client._state[CAT_SID_ADDRESSING]["SID Socket 2 Address"] == STOCK_ADDRESS
    assert _writes(client, CAT_SID_ADDRESSING) == [("SID Socket 2 Address", STOCK_ADDRESS)]


def test_drift_present_at_entry_is_only_warned_without_the_mutate_gate(
    monkeypatch, caplog,
) -> None:
    """Correcting inherited drift is a config write, and a read-only test
    running on ``U64_HOST`` alone was never allowed one."""
    monkeypatch.delenv("U64_ALLOW_MUTATE", raising=False)
    client = _drifted_client()

    with caplog.at_level("WARNING"):
        _run(client, lambda: None)

    client.set_config_item.assert_not_called()
    assert client._state[CAT_SID_ADDRESSING]["SID Socket 2 Address"] == "$D520"
    assert any("without U64_ALLOW_MUTATE" in r.getMessage() for r in caplog.records)


def test_an_out_of_band_change_is_not_written_without_the_mutate_gate(
    monkeypatch, caplog,
) -> None:
    """Without the gate the module's config-writing tests are skipped, so a
    change during a read-only test is not the test's own -- another lane,
    a reboot, the on-device menu -- and putting the entry value back into
    the never-touch socket store would be a write nobody allowed."""
    monkeypatch.delenv("U64_ALLOW_MUTATE", raising=False)
    client = _client()

    def body() -> None:  # out of band: not through the client under test
        client._state[CAT_SID_SOCKETS]["SID Socket 2"] = SOCKET_DEFAULT
        client._state[CAT_SID_ADDRESSING]["UltiSID 1 Address"] = "$D520"

    with caplog.at_level("WARNING"):
        _run(client, body)

    client.set_config_item.assert_not_called()
    assert client._state[CAT_SID_SOCKETS]["SID Socket 2"] == SOCKET_DEFAULT
    assert sum(
        "without U64_ALLOW_MUTATE" in r.getMessage() for r in caplog.records
    ) == 2


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
    assert restores == [("SID Socket 2", SOCKET_ENTRY)], restores


@pytest.mark.parametrize("mutate", [False, True], ids=["read-only", "mutate-gate"])
def test_nothing_is_written_on_an_untouched_bench(monkeypatch, mutate) -> None:
    """The autouse fixture runs after every test, read-only ones included.

    Those run on ``U64_HOST`` alone, without ``U64_ALLOW_MUTATE``, so a
    teardown that wrote anything there would be a config write the
    operator never allowed -- and the firmware has no same-value
    short-circuit, so even a PUT of the value already held re-runs the
    store's ``effectuate_settings``, which for ``SID Sockets
    Configuration`` is the socket power path.  With the gate set it is
    still a diff: nothing moved, nothing is written.
    """
    if mutate:
        monkeypatch.setenv("U64_ALLOW_MUTATE", "1")
    else:
        monkeypatch.delenv("U64_ALLOW_MUTATE", raising=False)
    client = _client()
    _run(client, lambda: None)

    client.set_config_item.assert_not_called()
    assert client._state[CAT_SID_SOCKETS] == {
        item: SOCKET_ENTRY for item in SOCKET_ITEMS
    }


@pytest.mark.parametrize("entry", [
    {"default": SOCKET_DEFAULT},
    {"current": "", "default": SOCKET_DEFAULT},
], ids=["missing", "empty"])
def test_a_socket_reporting_no_current_value_refuses_to_start(entry) -> None:
    """Exit could not put it back, so the run stops before the test body.

    An empty ``current`` is refused like a missing one: the enables are
    ``Enabled``/``Disabled`` enums, so ``""`` is not a value exit could
    write back.
    """
    client = _client()
    client.get_config_item.side_effect = lambda cat, item: (
        dict(entry)
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

    def body() -> None:
        for item in ADDRESS_ITEMS:
            client._state[CAT_SID_ADDRESSING][item] = "$D520"
        for item in SOCKET_ITEMS:
            client._state[CAT_SID_SOCKETS][item] = SOCKET_DEFAULT

    _run(client, body)

    client.set_config_items.assert_not_called()
    client.set_config_items_batch.assert_not_called()
    assert _writes(client, CAT_SID_ADDRESSING) == [
        (item, STOCK_ADDRESS) for item in ADDRESS_ITEMS
    ]
    assert _writes(client, CAT_SID_SOCKETS) == [
        (item, SOCKET_ENTRY) for item in SOCKET_ITEMS
    ]


#: The envelopes ``get_config_item`` answers with an absent item or category
#: on stock firmware -- HTTP 200 with no category key, or an empty map.
_ABSENT_ENVELOPES = {
    "UltiSID 1 Address": {"errors": []},
    "UltiSID 2 Address": {CAT_SID_ADDRESSING: {}, "errors": []},
}


def test_an_item_the_device_does_not_expose_is_left_out(caplog) -> None:
    """The C64U may not have every item: skip it, do not error every test."""
    from c64_test_harness.backends.ultimate64_client import (
        Ultimate64Error,
        Ultimate64ProtocolError,
    )

    client = _client()
    read = client.get_config_item.side_effect

    def get_item(cat: str, item: str) -> dict:
        if item in _ABSENT_ENVELOPES:
            raise Ultimate64ProtocolError(f"no {item!r}")
        if item == "SID Socket 2":
            raise Ultimate64Error("not found", status=404)
        return read(cat, item)

    client.get_config_item.side_effect = get_item
    client.get_config_item_raw.side_effect = (
        lambda cat, item: _ABSENT_ENVELOPES[item]
    )

    def body() -> None:
        client._state[CAT_SID_ADDRESSING]["SID Socket 1 Address"] = "$D520"

    with caplog.at_level("WARNING"):
        _run(client, body)

    assert client.set_config_item.call_args_list == [
        ((CAT_SID_ADDRESSING, "SID Socket 1 Address", STOCK_ADDRESS),)
    ]
    assert sum("not exposed" in r.getMessage() for r in caplog.records) == 3


def _raise_invalid_json(cat: str, item: str):
    from c64_test_harness.backends.ultimate64_client import Ultimate64ProtocolError

    raise Ultimate64ProtocolError("invalid JSON from device")


@pytest.mark.parametrize("raw", [
    _raise_invalid_json,
    lambda cat, item: {"errors": ["Could not read item"]},
    lambda cat, item: {cat: {item: "not a map"}, "errors": []},
    lambda cat, item: {cat: "not a map", "errors": []},
    lambda cat, item: ["not", "an", "envelope"],
    # present under the firmware's case-insensitive name match
    lambda cat, item: {cat.upper(): {item.lower(): {"current": 1}}, "errors": []},
], ids=["invalid-json", "errors-array", "item-not-a-map", "category-not-a-map",
        "not-a-dict", "present-in-another-case"])
def test_a_protocol_error_that_is_not_absence_stops_the_start(raw) -> None:
    """Only the absent shapes are skipped.  Counting every
    ``Ultimate64ProtocolError`` as absence let a device answering garbage
    empty the plan and start the test unprotected."""
    from c64_test_harness.backends.ultimate64_client import Ultimate64ProtocolError

    client = _client()

    def get_item(cat: str, item: str):
        raise Ultimate64ProtocolError("unreadable")

    client.get_config_item.side_effect = get_item
    client.get_config_item_raw.side_effect = raw
    gen = restore_sid_config(client)
    with pytest.raises(Ultimate64ProtocolError):
        next(gen)
    client.set_config_item.assert_not_called()


def test_an_unreadable_item_that_is_not_absent_stops_the_start() -> None:
    """Only "not exposed" is skipped; any other failure is not guessed at."""
    from c64_test_harness.backends.ultimate64_client import Ultimate64Error

    client = _client()
    client.get_config_item.side_effect = Ultimate64Error("HTTP 500", status=500)
    gen = restore_sid_config(client)
    with pytest.raises(Ultimate64Error, match="500"):
        next(gen)
    client.set_config_item.assert_not_called()


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
