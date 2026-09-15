"""``restore_speed_defaults`` and the two live fixtures that now use it (#365).

Neither ``set_turbo_mhz`` form puts ``U64 Specific Settings`` back at
``current == default``: ``set_turbo_mhz(client, None)`` leaves ``CPU Speed``
behind (#360) and ``set_turbo_mhz(client, 1)`` writes ``Turbo Control =
Manual``.  This pins, without a device:

* the helper reads both defaults before writing anything, writes ``CPU Speed``
  then ``Turbo Control`` one item at a time, attempts both when one is
  rejected, and refuses an item with no usable default;
* on a real ``Ultimate64Client`` every request it issues is bodyless, so it
  costs zero ``/Temp`` attachments (the client's own
  ``_creates_temp_attachment`` rule is the judge);
* ``tests/test_bridge_ping_tod.py``'s ``u64_client`` restores before releasing
  the lock on every exit path, and releases even when the restore raises;
* ``tests/test_u64_debug_stream_speed_live.py``'s ``original_state`` restores
  both speed items to default and the stream mode to its entry value, attempts
  both, and raises a failure instead of swallowing it.

The fake device's defaults are deliberately not the factory ``" 1"``/``"Off"``
and its entry values differ from both, so a restore that hardcodes 1 MHz,
writes the entry value back, or skips an item cannot pass.

The fixtures are lifted from their modules' source with ``ast`` and executed
against fakes, so neither live module is imported (``test_bridge_ping_tod``
probes the host's BPF nodes at import).
"""
from __future__ import annotations

import __future__
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

import c64_test_harness
from c64_test_harness.backends import ultimate64_helpers
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64ProtocolError,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_U64_SPECIFIC,
    Ultimate64RestoreError,
    restore_speed_defaults,
)

TESTS = Path(__file__).parent
BRIDGE_TOD = TESTS / "test_bridge_ping_tod.py"
DEBUG_STREAM = TESTS / "test_u64_debug_stream_speed_live.py"

#: Non-factory defaults; entry values differ from both them and the factory.
_DEFAULTS = {"CPU Speed": " 3", "Turbo Control": "U64 Turbo Registers"}
_ENTRY = {"CPU Speed": "48", "Turbo Control": "Manual"}


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #

class _FakeClient:
    """Just enough of ``Ultimate64Client`` for the speed restore."""

    def __init__(self, journal: list | None = None) -> None:
        self.journal = journal if journal is not None else []
        self.fail_items: set[str] = set()
        self.store = {
            item: {"current": _ENTRY[item], "default": _DEFAULTS[item]}
            for item in _DEFAULTS
        }

    def get_config_item(self, category, item):
        assert category == CAT_U64_SPECIFIC
        self.journal.append(("get", item))
        return dict(self.store[item])

    def set_config_item(self, category, item, value) -> None:
        assert category == CAT_U64_SPECIFIC
        self.journal.append(("put", item, value))
        if item in self.fail_items:
            raise Ultimate64Error(f"PUT {item} rejected")
        self.store[item]["current"] = value

    def set_config_items(self, *_a, **_kw) -> None:
        raise AssertionError("restore must write one item at a time")

    def set_config_items_batch(self, *_a, **_kw) -> None:
        raise AssertionError("POST /v1/configs carries a body -- a /Temp attachment")


def _currents(client: _FakeClient) -> dict:
    return {item: entry["current"] for item, entry in client.store.items()}


# --------------------------------------------------------------------------- #
# The helper                                                                  #
# --------------------------------------------------------------------------- #

class TestRestoreSpeedDefaults:
    def test_the_fake_is_falsifiable(self) -> None:
        assert _DEFAULTS["CPU Speed"] not in (" 1", _ENTRY["CPU Speed"])
        assert _DEFAULTS["Turbo Control"] not in ("Off", _ENTRY["Turbo Control"])

    def test_reads_both_then_writes_cpu_speed_then_turbo_control(self) -> None:
        client = _FakeClient()
        written = restore_speed_defaults(client)
        assert client.journal == [
            ("get", "CPU Speed"),
            ("get", "Turbo Control"),
            ("put", "CPU Speed", _DEFAULTS["CPU Speed"]),
            ("put", "Turbo Control", _DEFAULTS["Turbo Control"]),
        ]
        assert written == _DEFAULTS
        assert list(written) == ["CPU Speed", "Turbo Control"]
        assert _currents(client) == _DEFAULTS

    def test_a_clean_entry_is_still_written(self) -> None:
        """The U64E path: the entry baseline left ``current == default`` and the
        drift comes later, from the speed change being restored.  Reading the
        store *after* that change is the helper's whole job, so a restore that
        skips items whose current already equals default is wrong only when
        the store was clean at read time -- which is this case."""
        client = _FakeClient()
        for entry in client.store.values():
            entry["current"] = entry["default"]
        written = restore_speed_defaults(client)
        assert written == _DEFAULTS
        assert [e for e in client.journal if e[0] == "put"] == [
            ("put", "CPU Speed", _DEFAULTS["CPU Speed"]),
            ("put", "Turbo Control", _DEFAULTS["Turbo Control"]),
        ]

    def test_only_one_item_drifted_still_writes_both(self) -> None:
        client = _FakeClient()
        client.store["Turbo Control"]["current"] = _DEFAULTS["Turbo Control"]
        restore_speed_defaults(client)
        assert [e[1] for e in client.journal if e[0] == "put"] == ["CPU Speed", "Turbo Control"]
        assert _currents(client) == _DEFAULTS

    @pytest.mark.parametrize("item", ["CPU Speed", "Turbo Control"])
    @pytest.mark.parametrize("breakage", ["missing", "empty", "not-a-string"])
    def test_an_unusable_default_refuses_before_any_write(self, item, breakage) -> None:
        client = _FakeClient()
        entry = client.store[item]
        if breakage == "missing":
            del entry["default"]
        elif breakage == "empty":
            entry["default"] = ""
        else:
            entry["default"] = 1
        with pytest.raises(Ultimate64ProtocolError, match=f"{item}: no usable default"):
            restore_speed_defaults(client)
        assert not [e for e in client.journal if e[0] == "put"]
        assert _currents(client) == _ENTRY

    def test_a_non_map_item_refuses(self, monkeypatch) -> None:
        client = _FakeClient()
        monkeypatch.setattr(client, "get_config_item", lambda *_a: [" 1"])
        with pytest.raises(Ultimate64ProtocolError, match="no usable default"):
            restore_speed_defaults(client)
        assert _currents(client) == _ENTRY

    def test_a_rejected_cpu_speed_write_still_restores_turbo_control(self) -> None:
        client = _FakeClient()
        client.fail_items = {"CPU Speed"}
        with pytest.raises(Ultimate64RestoreError) as info:
            restore_speed_defaults(client)
        assert set(info.value.failures) == {"CPU Speed"}
        assert ("put", "Turbo Control", _DEFAULTS["Turbo Control"]) in client.journal
        assert client.store["Turbo Control"]["current"] == _DEFAULTS["Turbo Control"]

    def test_both_rejected_are_both_reported(self) -> None:
        client = _FakeClient()
        client.fail_items = {"CPU Speed", "Turbo Control"}
        with pytest.raises(Ultimate64RestoreError) as info:
            restore_speed_defaults(client)
        assert set(info.value.failures) == {"CPU Speed", "Turbo Control"}

    def test_on_a_real_client_every_request_is_bodyless(self, monkeypatch) -> None:
        client = Ultimate64Client(
            "192.0.2.64", write_mem_query_threshold=128,
            warn_unlocked=False, temp_hygiene=False,
        )
        store = {item: {"current": _ENTRY[item], "default": _DEFAULTS[item]} for item in _DEFAULTS}
        sent: list[tuple] = []

        def wire(method, path, *, body=None, content_type=None, query=None):
            sent.append((method, unquote(path), body, dict(query or {})))
            if method == "GET":
                item = unquote(path).rsplit("/", 1)[-1]
                envelope = {CAT_U64_SPECIFIC: {item: store[item]}, "errors": []}
                return 200, json.dumps(envelope).encode()
            return 200, b""

        monkeypatch.setattr(client, "_request_uncounted", wire)
        monkeypatch.setattr(client, "_check_device_lock", lambda *_a, **_kw: None)
        restore_speed_defaults(client)

        cat = f"/v1/configs/{CAT_U64_SPECIFIC}"
        assert [(m, p) for m, p, _b, _q in sent] == [
            ("GET", f"{cat}/CPU Speed"),
            ("GET", f"{cat}/Turbo Control"),
            ("PUT", f"{cat}/CPU Speed"),
            ("PUT", f"{cat}/Turbo Control"),
        ]
        assert [q.get("value") for m, _p, _b, q in sent if m == "PUT"] == [
            _DEFAULTS["CPU Speed"], _DEFAULTS["Turbo Control"],
        ]
        assert all(b is None for _m, _p, b, _q in sent)
        assert not any(
            Ultimate64Client._creates_temp_attachment(m, b) for m, _p, b, _q in sent
        )
        assert client._pending_temp_attachments == 0

    def test_exported_beside_set_turbo_mhz(self) -> None:
        assert "restore_speed_defaults" in ultimate64_helpers.__all__
        assert "restore_speed_defaults" in c64_test_harness.__all__
        assert c64_test_harness.restore_speed_defaults is restore_speed_defaults
        assert c64_test_harness.set_turbo_mhz is ultimate64_helpers.set_turbo_mhz


# --------------------------------------------------------------------------- #
# Fixture extraction                                                          #
# --------------------------------------------------------------------------- #

def _fixture(path: Path, name: str, namespace: dict, cls: str | None = None):
    """The fixture *name* from *path* as a plain generator function."""
    tree = ast.parse(path.read_text(), filename=str(path))
    scope = tree.body
    if cls is not None:
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
        if not classes:
            pytest.fail(f"{path.name} defines no class {cls!r}")
        scope = classes[0].body
    found = [n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name]
    if not found:
        pytest.fail(f"{path.name} defines no fixture {name!r}")
    fn = found[0]
    fn.decorator_list = []
    code = compile(
        ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
        str(path), "exec",
        flags=__future__.annotations.compiler_flag, dont_inherit=True,
    )
    exec(code, namespace)  # noqa: S102 -- the repo's own test source
    return namespace[name]


# --------------------------------------------------------------------------- #
# test_bridge_ping_tod.py :: TestTodPrimitiveU64Live.u64_client               #
# --------------------------------------------------------------------------- #

@pytest.fixture
def tod(monkeypatch) -> SimpleNamespace:
    journal: list = []
    state = SimpleNamespace(
        journal=journal, acquire=True, construct_error=None,
        restore_error=None, client=None, generators=[],
    )

    class Lock:
        def __init__(self, host, **_kw) -> None:
            journal.append(("lock", host))

        def acquire(self, timeout=None, **_kw) -> bool:
            journal.append("acquire")
            return state.acquire

        def release(self) -> None:
            journal.append("release")

    def make_client(**_kw):
        journal.append("construct")
        if state.construct_error is not None:
            raise state.construct_error
        state.client = SimpleNamespace(name="client")
        return state.client

    def make_transport(**_kw):
        return SimpleNamespace(name="transport")

    def restore(client):
        journal.append(("restore", client))
        if state.restore_error is not None:
            raise state.restore_error
        return dict(_DEFAULTS)

    import c64_test_harness.backends.device_lock as device_lock
    import c64_test_harness.backends.ultimate64 as ultimate64
    import c64_test_harness.backends.ultimate64_client as ultimate64_client

    monkeypatch.setattr(device_lock, "DeviceLock", Lock)
    monkeypatch.setattr(ultimate64, "Ultimate64Transport", make_transport)
    monkeypatch.setattr(ultimate64_client, "Ultimate64Client", make_client)
    monkeypatch.setattr(ultimate64_helpers, "restore_speed_defaults", restore)
    import os

    namespace = {"os": os, "pytest": pytest, "_U64_HOST": "device-under-test"}
    body = _fixture(BRIDGE_TOD, "u64_client", namespace, cls="TestTodPrimitiveU64Live")

    def start():
        gen = body(None)
        state.generators.append(gen)
        return gen

    state.start = start
    return state


class TestTodFixture:
    def test_normal_exit_restores_then_releases(self, tod) -> None:
        gen = tod.start()
        client, _transport = next(gen)
        mark = len(tod.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert tod.journal[mark:] == [("restore", client), "release"]

    def test_an_exception_in_the_test_still_restores_then_releases(self, tod) -> None:
        gen = tod.start()
        client, _transport = next(gen)
        mark = len(tod.journal)
        with pytest.raises(RuntimeError, match="test body"):
            gen.throw(RuntimeError("test body"))
        assert tod.journal[mark:] == [("restore", client), "release"]

    def test_generator_close_still_restores_then_releases(self, tod) -> None:
        gen = tod.start()
        client, _transport = next(gen)
        mark = len(tod.journal)
        gen.close()
        assert tod.journal[mark:] == [("restore", client), "release"]

    def test_a_raising_restore_still_releases_and_is_reported(self, tod) -> None:
        gen = tod.start()
        client, _transport = next(gen)
        tod.restore_error = Ultimate64RestoreError(CAT_U64_SPECIFIC, {"CPU Speed": OSError("x")})
        mark = len(tod.journal)
        with pytest.raises(Ultimate64RestoreError):
            next(gen)
        assert tod.journal[mark:] == [("restore", client), "release"]

    def test_a_raising_restore_after_a_failed_test_still_releases(self, tod) -> None:
        gen = tod.start()
        next(gen)
        tod.restore_error = Ultimate64ProtocolError("no usable default")
        with pytest.raises(Ultimate64ProtocolError):
            gen.throw(RuntimeError("test body"))
        assert tod.journal[-1] == "release"

    def test_a_raising_constructor_releases_without_a_restore(self, tod) -> None:
        tod.construct_error = OSError("unreachable host")
        gen = tod.start()
        with pytest.raises(OSError, match="unreachable host"):
            next(gen)
        assert tod.journal[-1] == "release"
        assert not [e for e in tod.journal if isinstance(e, tuple) and e[0] == "restore"]

    def test_an_unacquired_lock_skips_without_a_restore(self, tod) -> None:
        tod.acquire = False
        gen = tod.start()
        with pytest.raises(pytest.skip.Exception):
            next(gen)
        assert not [e for e in tod.journal if isinstance(e, tuple) and e[0] == "restore"]


# --------------------------------------------------------------------------- #
# test_u64_debug_stream_speed_live.py :: original_state                       #
# --------------------------------------------------------------------------- #

_ENTRY_MODE = "mode-at-entry"


@pytest.fixture
def stream() -> SimpleNamespace:
    journal: list = []
    state = SimpleNamespace(journal=journal, restore_error=None, mode_error=None, generators=[])
    client = SimpleNamespace(name="client")
    state.client = client

    def restore(c):
        journal.append(("restore", c))
        if state.restore_error is not None:
            raise state.restore_error
        return dict(_DEFAULTS)

    def set_mode(c, mode):
        journal.append(("mode", mode))
        if mode == _ENTRY_MODE and state.mode_error is not None:
            raise state.mode_error

    namespace = {
        "restore_speed_defaults": restore,
        "get_debug_stream_mode": lambda c: _ENTRY_MODE,
        "set_debug_stream_mode": set_mode,
        "DEBUG_MODE_6510": "6510 Only",
        "time": SimpleNamespace(sleep=lambda _s: None),
        "SETTLE_SECONDS": 0.0,
        "logger": logging.getLogger("test_speed_restore_defaults.stream"),
    }
    body = _fixture(DEBUG_STREAM, "original_state", namespace)

    def start():
        gen = body(client)
        state.generators.append(gen)
        return gen

    state.start = start
    return state


class TestDebugStreamFixture:
    def test_normal_exit_restores_speed_defaults_and_the_entry_mode(self, stream) -> None:
        gen = stream.start()
        next(gen)
        assert stream.journal == [("mode", "6510 Only")]
        with pytest.raises(StopIteration):
            next(gen)
        assert stream.journal[1:] == [("restore", stream.client), ("mode", _ENTRY_MODE)]

    def test_an_exception_in_the_test_still_restores_both(self, stream) -> None:
        gen = stream.start()
        next(gen)
        with pytest.raises(RuntimeError, match="test body"):
            gen.throw(RuntimeError("test body"))
        assert stream.journal[1:] == [("restore", stream.client), ("mode", _ENTRY_MODE)]

    def test_a_failed_speed_restore_still_restores_the_mode_and_is_raised(self, stream) -> None:
        gen = stream.start()
        next(gen)
        stream.restore_error = Ultimate64RestoreError(CAT_U64_SPECIFIC, {"CPU Speed": OSError("x")})
        with pytest.raises(Ultimate64RestoreError):
            next(gen)
        assert stream.journal[1:] == [("restore", stream.client), ("mode", _ENTRY_MODE)]

    def test_a_failed_mode_restore_is_raised(self, stream) -> None:
        gen = stream.start()
        next(gen)
        stream.mode_error = Ultimate64Error("mode PUT rejected")
        with pytest.raises(Ultimate64Error, match="mode PUT rejected"):
            next(gen)
        assert ("restore", stream.client) in stream.journal

    def test_when_both_fail_the_speed_restore_failure_is_raised(self, stream) -> None:
        gen = stream.start()
        next(gen)
        stream.restore_error = Ultimate64ProtocolError("no usable default")
        stream.mode_error = Ultimate64Error("mode PUT rejected")
        with pytest.raises(Ultimate64ProtocolError, match="no usable default"):
            next(gen)
        assert stream.journal[-1] == ("mode", _ENTRY_MODE)
