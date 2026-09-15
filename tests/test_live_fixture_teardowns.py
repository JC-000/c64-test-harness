"""Live fixtures' teardowns, driven offline with recording fakes (#334).

#276 repaired a bare post-``yield`` teardown in the transport module; the
same shape sat in other live fixtures.  A bare sequence --
``client.reset()``, ``client.close()``, ``lock.release()`` -- stops at the
first step that raises, so a raising ``close()`` orphans the ``DeviceLock``,
and an exception arriving at the ``yield`` skips every step.  This pins the
repaired shape without a device:

* every teardown step is attempted even when an earlier one raises, and
  the lock is released **last**;
* a failed step is reported (raised after the release), not swallowed;
* an exception at the ``yield`` (``throw``, ``close()``) still runs every
  step and still propagates;
* the two restore fixtures put each item they own back to the ``default``
  the device reports -- one per-item PUT each -- and refuse to start when
  an item reports none.

The fixture bodies are lifted from each module's source (decorators
stripped) and executed in a private copy of that module's namespace, so the
fakes patched onto the copy are what they see; pytest's fixture wrapper is
not used (#314).  The same extraction runs against any revision of a
module, which is how the red run against the pre-#334 modules was taken.

A structural scan at the end refuses the bare shape in every live module.
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

TESTS_DIR = Path(__file__).parent


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #

def _load_module(filename: str):
    path = TESTS_DIR / filename
    spec = importlib.util.spec_from_file_location(f"_teardown_probe_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A module defining a dataclass under ``from __future__ import
    # annotations`` looks itself up in sys.modules while it executes
    # (test_u64_debug_stream_speed_live.py does).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_body(module, filename: str, name: str):
    """The module-level fixture *name* as a plain function."""
    path = TESTS_DIR / filename
    tree = ast.parse(path.read_text(), filename=str(path))
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if not found:
        pytest.fail(f"{filename} defines no module-level fixture {name!r}")
    fn = found[0]
    fn.decorator_list = []
    fn.returns = None
    code = compile(
        ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
        str(path), "exec",
    )
    exec(code, module.__dict__)  # noqa: S102 -- the module's own source
    return module.__dict__[name]


class _Fake:
    """Shared journal and failure injection: a key in ``fail`` raises."""

    def __init__(self, journal: list, fail: set) -> None:
        self.journal = journal
        self.fail = fail

    def _do(self, key) -> None:
        self.journal.append(key)
        if key in self.fail:
            raise RuntimeError(f"FAKE {key!r} failed")


class _FakeLock(_Fake):
    def acquire(self, timeout=None, **_kw) -> bool:
        return True

    def acquire_or_raise(self, timeout=None, **_kw) -> None:
        return None

    def release(self) -> None:
        self._do("release")


def _default(item: str) -> str:
    return f"default of {item}"


def _entry(item: str) -> str:
    return f"entry value of {item}"


class _FakeClient(_Fake):
    """No ``set_config_items`` / ``set_config_items_batch``: per-item PUTs only."""

    def __init__(self, journal: list, fail: set, config: dict) -> None:
        super().__init__(journal, fail)
        self.config = config

    def get_config_item(self, category: str, item: str) -> dict:
        return dict(self.config.get(
            (category, item), {"current": _entry(item), "default": _default(item)}
        ))

    def set_config_item(self, category: str, item: str, value) -> None:
        self._do(("config", category, item, value))
        self.config.setdefault((category, item), {"default": _default(item)})
        self.config[(category, item)]["current"] = value

    def reset(self) -> None:
        self._do("reset")

    def close(self) -> None:
        self._do("close")


@pytest.fixture
def probe():
    journal: list = []
    return SimpleNamespace(
        journal=journal, fail=set(), config={}, construct_error=None, generators=[]
    )


def _patch(probe, module, ctor_name: str) -> None:
    def construct(*_a, **_kw):
        probe.journal.append("construct")
        if probe.construct_error is not None:
            raise probe.construct_error
        probe.client = _FakeClient(probe.journal, probe.fail, probe.config)
        return probe.client

    module.DeviceLock = lambda *_a, **_kw: _FakeLock(probe.journal, probe.fail)
    setattr(module, ctor_name, construct)
    if hasattr(module, "_HOST"):
        module._HOST = "device-under-test"  # several fixtures assert a host


# --------------------------------------------------------------------------- #
# The corpus: bare post-yield lock/close teardowns found at 294d036           #
# --------------------------------------------------------------------------- #

def _restores(category_items: list[tuple[str, dict]]) -> list:
    return [
        ("config", category, item, _default(item))
        for category, items in category_items
        for item in items
    ]


def _chromatic_items(module) -> list:
    addressing: dict = {}
    ultisid: dict = {}
    for config in module.SID_CONFIGS:
        addressing.update(dict.fromkeys(config["addressing"]))
        ultisid.update(dict.fromkeys(config["ultisid_settings"] or {}))
    return _restores([("SID Addressing", addressing), ("UltiSID Configuration", ultisid)])


def _multi_sid_items(module) -> list:
    return _restores([
        ("SID Addressing", module.QUAD_ADDRESSING),
        ("Audio Mixer", module.QUAD_PANNING),
    ])


@dataclass(frozen=True)
class Case:
    filename: str
    fixture: str
    ctor: str
    #: Journal entries between the yield and the release, from the module.
    steps: Callable[[object], list]

    @property
    def id(self) -> str:
        return f"{self.filename.removeprefix('test_').removesuffix('.py')}::{self.fixture}"


CASES = [
    Case("test_chromatic_capture_live.py", "u64_client", "Ultimate64Client",
         lambda m: [*_chromatic_items(m), "reset", "close"]),
    Case("test_multi_sid_parallel_live.py", "u64_client", "Ultimate64Client",
         lambda m: [*_multi_sid_items(m), "reset", "close"]),
    Case("test_u64_audio_capture_live.py", "u64_client", "Ultimate64Client",
         lambda m: ["reset", "close"]),
    Case("test_u64_streams_live.py", "client", "Ultimate64Client",
         lambda m: ["close"]),
    Case("test_u64_capabilities_live.py", "client", "Ultimate64Client",
         lambda m: ["close"]),
    Case("test_ultimate64_client_live.py", "client", "Ultimate64Client",
         lambda m: ["close"]),
    Case("test_ultimate64_helpers_live.py", "client", "Ultimate64Client",
         lambda m: ["close"]),
    Case("test_ultimate64_recovery_live.py", "client", "Ultimate64Client",
         lambda m: ["close"]),
    Case("test_u64_feature_parity_live.py", "transport", "Ultimate64Transport",
         lambda m: ["close"]),
    # #368: the bare shape owned by another lane while #334 was open, and the
    # fixtures that released the lock in a finally but never closed the client
    *(
        Case(filename, "client", "Ultimate64Client", lambda m: ["close"])
        for filename in (
            "test_u64_debug_stream_speed_live.py",
            "test_entry_baseline_live.py",
            "test_flash_baseline_live.py",
            "test_reu_size_readback_live.py",
            "test_socketdma_live.py",
            "test_temp_gc_live.py",
            "test_turbo_contract_live.py",
            "test_ultimate64_client_writemem_live.py",
        )
    ),
]


def _start(probe, case: Case):
    module = _load_module(case.filename)
    _patch(probe, module, case.ctor)
    gen = _fixture_body(module, case.filename, case.fixture)()
    probe.generators.append(gen)  # a dropped generator is closed by the GC
    yielded = next(gen)
    return module, gen, yielded


def _after(journal: list, mark: int) -> list:
    return journal[mark:]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
class TestLockedClientFixtures:
    def test_normal_exit_runs_every_step_then_releases(self, probe, case) -> None:
        module, gen, yielded = _start(probe, case)
        assert yielded is probe.client
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_a_raising_step_skips_nothing_and_is_reported(self, probe, case) -> None:
        module = _load_module(case.filename)
        expected = case.steps(module)
        for failing in expected:
            run = SimpleNamespace(
                journal=[], fail={failing}, config={}, construct_error=None, generators=[]
            )
            _module, gen, _ = _start(run, case)
            mark = len(run.journal)
            with pytest.raises(RuntimeError, match="FAKE"):
                next(gen)
            assert _after(run.journal, mark) == [*expected, "release"], failing

    def test_every_step_raising_still_releases(self, probe, case) -> None:
        module, gen, _ = _start(probe, case)
        probe.fail.update(case.steps(module))
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="FAKE"):
            next(gen)
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_an_exception_at_the_yield_runs_every_step_and_propagates(
        self, probe, case
    ) -> None:
        module, gen, _ = _start(probe, case)
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_a_failing_step_does_not_mask_the_test_body_exception(
        self, probe, case
    ) -> None:
        module, gen, _ = _start(probe, case)
        probe.fail.add("close")
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_generator_close_runs_every_step(self, probe, case) -> None:
        module, gen, _ = _start(probe, case)
        mark = len(probe.journal)
        gen.close()
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_a_raising_release_does_not_mask_the_test_body_exception(
        self, probe, case
    ) -> None:
        """#368 (b): a release that raises is logged and reported, never a
        replacement for the exception already leaving the ``yield``."""
        module, gen, _ = _start(probe, case)
        probe.fail.add("release")
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_a_raising_release_is_reported_with_the_step_failures(
        self, probe, case
    ) -> None:
        module, gen, _ = _start(probe, case)
        probe.fail.update({"close", "release"})
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="teardown") as info:
            next(gen)
        assert "close" in str(info.value) and "release" in str(info.value)
        assert _after(probe.journal, mark) == [*case.steps(module), "release"]

    def test_a_raising_constructor_still_releases(self, probe, case) -> None:
        module = _load_module(case.filename)
        _patch(probe, module, case.ctor)
        probe.construct_error = OSError("unreachable host")
        gen = _fixture_body(module, case.filename, case.fixture)()
        with pytest.raises(OSError, match="unreachable host"):
            next(gen)
        assert probe.journal == ["construct", "release"]


# --------------------------------------------------------------------------- #
# The two restore fixtures: defaults read at entry, per-item PUTs             #
# --------------------------------------------------------------------------- #

RESTORE_CASES = [c for c in CASES if c.fixture == "u64_client" and "audio" not in c.filename]


@pytest.mark.parametrize("case", RESTORE_CASES, ids=lambda c: c.id)
class TestRestoreToDefault:
    def test_the_fakes_defaults_are_not_the_old_hard_coded_values(self, case) -> None:
        """Guard: the pre-#334 hard-coded restore values cannot match the journal."""
        old = {"$D400", "$D420", "8580 Lo", "6581", "Enabled", "Left 3", "Right 3", "Center"}
        restores = [s for s in case.steps(_load_module(case.filename)) if isinstance(s, tuple)]
        assert restores, "no restore steps derived from the module"
        assert not {value for *_x, value in restores} & old

    def test_items_drifted_by_the_test_end_at_their_default(self, probe, case) -> None:
        module, gen, client = _start(probe, case)
        restores = [s for s in case.steps(module) if isinstance(s, tuple)]
        for _op, category, item, _value in restores:  # what the tests do
            client.set_config_item(category, item, "moved by the test")
        with pytest.raises(StopIteration):
            next(gen)
        assert {
            (cat, item): probe.config[(cat, item)]["current"]
            for _op, cat, item, _v in restores
        } == {(cat, item): _default(item) for _op, cat, item, _v in restores}

    @pytest.mark.parametrize("breakage", ["missing", "empty"])
    def test_an_item_without_a_default_refuses_to_start_and_still_releases(
        self, probe, case, breakage
    ) -> None:
        module = _load_module(case.filename)
        _patch(probe, module, case.ctor)
        _op, category, item, _v = [s for s in case.steps(module) if isinstance(s, tuple)][-1]
        probe.config[(category, item)] = (
            {"current": "x"} if breakage == "missing" else {"current": "x", "default": ""}
        )
        gen = _fixture_body(module, case.filename, case.fixture)()
        with pytest.raises(RuntimeError, match="no default"):
            next(gen)
        assert probe.journal == ["construct", "close", "release"]

    def test_a_drifted_entry_warns_that_exit_writes_the_default(
        self, probe, case, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            module, _gen, _ = _start(probe, case)
        drift = [r.getMessage() for r in caplog.records if "drifted at entry" in r.getMessage()]
        restores = [s for s in case.steps(module) if isinstance(s, tuple)]
        assert len(drift) == len(restores), drift
        for (_op, category, item, _v), message in zip(restores, drift):
            assert f"{category} / {item} drifted at entry" in message
            assert repr(_entry(item)) in message and repr(_default(item)) in message
            assert "written to its default at exit" in message

    def test_a_clean_entry_does_not_warn(self, probe, case, caplog) -> None:
        module = _load_module(case.filename)
        for _op, category, item, _v in [s for s in case.steps(module) if isinstance(s, tuple)]:
            probe.config[(category, item)] = {"current": _default(item), "default": _default(item)}
        with caplog.at_level(logging.WARNING):
            _start(probe, case)
        assert not [r for r in caplog.records if "drifted at entry" in r.getMessage()]


# --------------------------------------------------------------------------- #
# Split fixtures: test_uci_turbo_live, the blind-agent modules                #
# --------------------------------------------------------------------------- #

class TestUciTurboFixtures:
    FILE = "test_uci_turbo_live.py"

    def test_device_lock_releases_when_the_yield_raises(self, probe) -> None:
        module = _load_module(self.FILE)
        _patch(probe, module, "Ultimate64Client")
        module._HOST = "device-under-test"  # the fixture asserts a host
        gen = _fixture_body(module, self.FILE, "device_lock")()
        next(gen)
        with pytest.raises(KeyError):
            gen.throw(KeyError("test body"))
        assert probe.journal == ["release"]

    @pytest.mark.parametrize("fixture, ctor", [
        ("client", "Ultimate64Client"), ("transport", "Ultimate64Transport"),
    ])
    def test_the_client_and_transport_are_closed_on_every_exit(
        self, probe, fixture, ctor
    ) -> None:
        module = _load_module(self.FILE)
        _patch(probe, module, ctor)
        body = _fixture_body(module, self.FILE, fixture)
        for finish in ("next", "throw"):
            del probe.journal[:]
            gen = body(object())
            assert next(gen) is probe.client
            if finish == "next":
                with pytest.raises(StopIteration):
                    next(gen)
            else:
                with pytest.raises(KeyError):
                    gen.throw(KeyError("test body"))
            assert probe.journal == ["construct", "close"], finish


class TestUciEnabledRestore:
    """``test_uci_turbo_live.py::uci_enabled``: disable then restore, every exit (#368)."""

    FILE = "test_uci_turbo_live.py"

    def _gen(self, probe, original_uci: bool):
        module = _load_module(self.FILE)
        fake = _Fake(probe.journal, probe.fail)
        module.snapshot_state = lambda client: "SNAP"
        module.get_uci_enabled = lambda client: original_uci
        module.enable_uci = lambda client: fake._do("enable")
        module.disable_uci = lambda client: fake._do("disable")
        module.restore_state = lambda client, snap: fake._do(("restore", snap))
        module.time = SimpleNamespace(sleep=lambda seconds: None)
        client = SimpleNamespace(reset=lambda: fake._do("reset"))
        gen = _fixture_body(module, self.FILE, "uci_enabled")(client)
        probe.generators.append(gen)
        return gen

    @staticmethod
    def _restore(original_uci: bool) -> list:
        return ([] if original_uci else ["disable"]) + [("restore", "SNAP")]

    @pytest.mark.parametrize("original_uci", [False, True])
    def test_normal_exit_restores(self, probe, original_uci) -> None:
        gen = self._gen(probe, original_uci)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == self._restore(original_uci)

    @pytest.mark.parametrize("original_uci", [False, True])
    def test_an_exception_at_the_yield_still_restores(self, probe, original_uci) -> None:
        gen = self._gen(probe, original_uci)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == self._restore(original_uci)

    def test_a_failed_disable_is_reported_and_restore_still_runs(self, probe) -> None:
        gen = self._gen(probe, False)
        next(gen)
        probe.fail.add("disable")
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="uci_enabled restore"):
            next(gen)
        assert _after(probe.journal, mark) == self._restore(False)

    def test_a_failed_restore_is_reported(self, probe) -> None:
        gen = self._gen(probe, True)
        next(gen)
        probe.fail.add(("restore", "SNAP"))
        with pytest.raises(RuntimeError, match="uci_enabled restore"):
            next(gen)

    def test_a_failed_enable_still_restores(self, probe) -> None:
        gen = self._gen(probe, False)
        probe.fail.add("enable")
        with pytest.raises(RuntimeError, match="FAKE"):
            next(gen)
        assert probe.journal == ["enable", *self._restore(False)]


class TestRestoreDrifted:
    """``test_entry_baseline_live.py::restore_drifted``: every item, every exit (#368)."""

    FILE = "test_entry_baseline_live.py"

    def _gen(self, probe):
        module = _load_module(self.FILE)
        fake = _Fake(probe.journal, probe.fail)
        items = list(module._DRIFTED_BY_THIS_MODULE)
        assert len(items) >= 2, "the fixture needs two items to show independence"
        stock = {cat: {} for cat, _item in items}
        for cat, item in items:
            stock[cat][item] = f"stock {item}"
        client = SimpleNamespace(
            get_config_value=lambda cat, item: "drifted",
            set_config_item=lambda cat, item, value: fake._do(("set", item, value)),
        )
        gen = _fixture_body(module, self.FILE, "restore_drifted")(client, stock)
        probe.generators.append(gen)
        sets = [("set", item, f"stock {item}") for _cat, item in items]
        return module, gen, sets

    def test_normal_exit_puts_every_item_back(self, probe) -> None:
        _module, gen, sets = self._gen(probe)
        next(gen)
        with pytest.raises(StopIteration):
            next(gen)
        assert probe.journal == sets

    def test_an_exception_at_the_yield_still_puts_items_back(self, probe) -> None:
        _module, gen, sets = self._gen(probe)
        next(gen)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert probe.journal == sets

    def test_any_failing_put_skips_no_later_item_and_is_reported(self, probe) -> None:
        module, gen, sets = self._gen(probe)
        next(gen)
        probe.fail.add(sets[0])  # a RuntimeError, not an Ultimate64Error
        with pytest.raises(module.Ultimate64Error, match="could not be put back"):
            next(gen)
        assert probe.journal == sets


class _FakeManager(_Fake):
    def __init__(self, journal, fail) -> None:
        super().__init__(journal, fail)
        self.target = SimpleNamespace(transport=object())

    def acquire(self):
        return self.target

    def release(self, target) -> None:
        assert target is self.target
        self._do("release")

    def shutdown(self) -> None:
        self._do("shutdown")


class TestBlindAgentFixtures:
    def test_screen_target_shuts_down_even_when_release_raises(
        self, probe, monkeypatch
    ) -> None:
        import c64_test_harness.backends.unified_manager as um

        manager = _FakeManager(probe.journal, {"release"})
        monkeypatch.setattr(um, "UnifiedManager", lambda **_kw: manager)
        monkeypatch.setenv("U64_HOST", "device-under-test")
        name = "test_blind_agent_screen.py"
        gen = _fixture_body(_load_module(name), name, "target")()
        assert next(gen) is manager.target
        with pytest.raises(RuntimeError, match="FAKE"):
            next(gen)
        assert probe.journal == ["release", "shutdown"]

    def test_screen_target_releases_and_shuts_down_when_the_yield_raises(
        self, probe, monkeypatch
    ) -> None:
        import c64_test_harness.backends.unified_manager as um

        manager = _FakeManager(probe.journal, set())
        monkeypatch.setattr(um, "UnifiedManager", lambda **_kw: manager)
        monkeypatch.setenv("U64_HOST", "device-under-test")
        name = "test_blind_agent_screen.py"
        gen = _fixture_body(_load_module(name), name, "target")()
        next(gen)
        with pytest.raises(KeyError):
            gen.throw(KeyError("test body"))
        assert probe.journal == ["release", "shutdown"]

    @pytest.mark.parametrize("fixture, expected", [
        ("manager", ["shutdown"]), ("transport", ["release"]),
    ])
    def test_memory_fixtures_tear_down_when_the_yield_raises(
        self, probe, monkeypatch, fixture, expected
    ) -> None:
        name = "test_blind_agent_memory.py"
        module = _load_module(name)
        manager = _FakeManager(probe.journal, set())
        module.UnifiedManager = lambda **_kw: manager
        monkeypatch.setenv("U64_HOST", "device-under-test")
        body = _fixture_body(module, name, fixture)
        gen = body() if fixture == "manager" else body(manager)
        next(gen)
        with pytest.raises(KeyError):
            gen.throw(KeyError("test body"))
        assert probe.journal == expected


# --------------------------------------------------------------------------- #
# VICE and bridge fixtures: every teardown step, each on its own (#368)       #
# --------------------------------------------------------------------------- #

_PORTS = (6001, 6002)


class _FakeAllocator(_Fake):
    def __init__(self, journal, fail) -> None:
        super().__init__(journal, fail)
        self._next = iter(_PORTS)

    def allocate(self) -> int:
        return next(self._next)

    def take_socket(self, port):
        return None

    def release(self, port) -> None:
        self._do(("release", port))


class _FakeVice(_Fake):
    def __init__(self, journal, fail, port) -> None:
        super().__init__(journal, fail)
        self.port = port

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self._do(("stop", self.port))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.journal.append(("exit", self.port))
        return False


class _FakeEndpoint(_Fake):
    def __init__(self, journal, fail, name) -> None:
        super().__init__(journal, fail)
        self.name = name

    def settimeout(self, seconds) -> None:
        pass

    def close(self) -> None:
        self._do(("close", self.name))


@dataclass(frozen=True)
class ViceCase:
    filename: str
    fixture: str
    #: The teardown journal, in order; ``exit`` is the ``with`` leaving, not a step.
    teardown: tuple

    @property
    def id(self) -> str:
        return f"{self.filename.removesuffix('.py')}::{self.fixture}"


_P, _Q = _PORTS
_WITH_VICE = (("close", _P), ("release", _P), ("exit", _P))
_ONE_VICE = (("close", _P), ("stop", _P), ("release", _P))
_TWO_VICE = (
    ("close", _P), ("close", _Q), ("stop", _P), ("stop", _Q),
    ("release", _P), ("release", _Q),
)

VICE_CASES = [
    ViceCase("conftest.py", "binary_transport", _WITH_VICE),
    ViceCase("test_vice_binary.py", "binary_transport", _WITH_VICE),
    ViceCase("test_snapshot.py", "vice_transport", _WITH_VICE),
    ViceCase("test_vice_wire_format_live.py", "raw_monitor",
             (("close", "sock"), ("stop", 7001))),
    ViceCase("test_rrnet_udp_send_live.py", "single_vice_with_rrnet", _ONE_VICE),
    ViceCase("test_ethernet.py", "vice_ethernet", _ONE_VICE),
    ViceCase("conftest.py", "bridge_vice_pair", _TWO_VICE),
    ViceCase("test_ethernet_bridge.py", "vice_bridge_pair", _TWO_VICE),
]


def _vice_gen(probe, case: ViceCase, monkeypatch):
    """The fixture with every VICE, port and bridge collaborator faked."""
    import bridge_platform
    import c64_test_harness.ethernet as ethernet

    module = _load_module(case.filename)
    j, f = probe.journal, probe.fail
    vice = lambda config: _FakeVice(j, f, config.port)  # noqa: E731
    def connect(port, proc=None, **_kw):
        if getattr(probe, "connect_fail", None) == port:
            raise RuntimeError("FAKE connect")
        return _FakeEndpoint(j, f, port)

    def create_connection(addr, timeout=None):
        if getattr(probe, "connect_fail", None) == "sock":
            raise RuntimeError("FAKE connect")
        return _FakeEndpoint(j, f, "sock")

    noop = lambda *_a, **_kw: object()  # noqa: E731
    fakes = {
        "PortAllocator": lambda **_kw: _FakeAllocator(j, f),
        "ViceConfig": lambda **kw: SimpleNamespace(**kw),
        "ViceProcess": vice,
        "start_vice_or_skip": vice,
        "connect_binary_transport": connect,
        "_connect_vice": connect,
        "require_vice_or_skip": noop,
        "_bridge_wait_ready": noop,
        "_bridge_init_cs8900a": noop,
        "_wait_for_ready": noop,
        "_init_cs8900a": noop,
        "set_cs8900a_mac": noop,
        "binary_wait_for_text": noop,
        "free_port": lambda: 7001,
        "socket": SimpleNamespace(create_connection=create_connection),
        "shutil": SimpleNamespace(which=lambda name: "/usr/bin/" + name),
        "time": SimpleNamespace(sleep=lambda seconds: None, monotonic=__import__("time").monotonic),
        "IFACE_A": "if-a", "IFACE_B": "if-b", "ETHERNET_DRIVER": "fake",
    }
    for name, value in fakes.items():
        setattr(module, name, value)
    monkeypatch.setattr(bridge_platform, "iface_present", lambda name: True)
    monkeypatch.setattr(ethernet, "set_cs8900a_mac", noop)
    gen = _fixture_body(module, case.filename, case.fixture)()
    probe.generators.append(gen)
    return gen


def _steps_of(case: ViceCase) -> list:
    return [key for key in case.teardown if key[0] != "exit"]


@pytest.mark.parametrize("case", VICE_CASES, ids=lambda c: c.id)
class TestViceFixtures:
    def test_normal_exit_runs_every_step_in_order(self, probe, case, monkeypatch) -> None:
        gen = _vice_gen(probe, case, monkeypatch)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert tuple(_after(probe.journal, mark)) == case.teardown

    def test_an_exception_at_the_yield_runs_every_step_and_propagates(
        self, probe, case, monkeypatch
    ) -> None:
        gen = _vice_gen(probe, case, monkeypatch)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert tuple(_after(probe.journal, mark)) == case.teardown

    def test_a_raising_step_skips_nothing_and_is_reported(
        self, probe, case, monkeypatch
    ) -> None:
        for failing in _steps_of(case):
            run = SimpleNamespace(journal=[], fail={failing}, generators=[])
            gen = _vice_gen(run, case, monkeypatch)
            next(gen)
            mark = len(run.journal)
            with pytest.raises(RuntimeError, match=r"teardown: .*FAKE"):
                next(gen)
            assert tuple(_after(run.journal, mark)) == case.teardown, failing

    def test_a_resource_never_created_is_skipped_not_failed(
        self, probe, case, monkeypatch, caplog
    ) -> None:
        """The last connection fails during setup: everything already created is
        torn down, the missing one is skipped (not called on ``None``), and no
        teardown step is reported as failed (kills H2)."""
        closes = [key for key in case.teardown if key[0] == "close"]
        probe.connect_fail = closes[-1][1]
        gen = _vice_gen(probe, case, monkeypatch)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match="FAKE connect"):
                next(gen)
        assert tuple(probe.journal) == tuple(k for k in case.teardown if k != closes[-1])
        assert not [r for r in caplog.records if "teardown step" in r.getMessage()]


# --------------------------------------------------------------------------- #
# Structure: no fixture's finally runs two teardown calls in a row (#368)     #
# --------------------------------------------------------------------------- #

#: Calls that count as a teardown step inside a ``finally``.
FINALLY_STEP_CALLS = frozenset({"close", "release", "stop", "shutdown"})


def multi_step_finally_bodies(source: str) -> list[str]:
    """``fn:line`` for each generator ``finally`` with two or more bare teardown calls.

    A statement counts when it is not itself a ``try`` and contains a call to
    one of :data:`FINALLY_STEP_CALLS` -- so ``if vice is not None:
    vice.stop()`` counts, while a step list handed to ``attempt_steps`` (bound
    methods, not calls) and a nested ``try``/``finally`` do not.
    """
    offenders: list[str] = []
    for fn in ast.parse(source).body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            steps = [
                stmt for stmt in node.finalbody
                if not isinstance(stmt, ast.Try)
                and any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in FINALLY_STEP_CALLS
                    for sub in ast.walk(stmt)
                )
            ]
            if len(steps) >= 2:
                offenders.append(f"{fn.name}:{node.finalbody[0].lineno}")
    return offenders


def _finally_scanned() -> list[Path]:
    mods = sorted(p for p in TESTS_DIR.glob("*.py") if p.name != Path(__file__).name)
    assert len(mods) >= 100, "the test tree is missing -- the scan looks in the wrong place"
    return mods


@pytest.mark.parametrize("path", _finally_scanned(), ids=lambda p: p.name)
def test_no_multi_step_finally(path: Path) -> None:
    offenders = multi_step_finally_bodies(path.read_text())
    assert not offenders, (
        f"{path.name}: a fixture's finally runs several teardown calls in a row, so "
        "the first that raises skips the rest (#368); hand them to attempt_steps: "
        + ", ".join(offenders)
    )


def test_the_multi_step_finally_scan_can_fail() -> None:
    source = (
        "def two():\n    try:\n        yield 1\n    finally:\n"
        "        t.close()\n        a.release(p)\n"
        "def guarded():\n    try:\n        yield 1\n    finally:\n"
        "        failures = attempt_steps([('t.close()', t.close), ('r', a.release)])\n"
        "def conditional():\n    try:\n        yield 1\n    finally:\n"
        "        if v is not None:\n            v.stop()\n        a.release(p)\n"
        "def nested():\n    try:\n        yield 1\n    finally:\n"
        "        try:\n            t.close()\n        finally:\n            lock.release()\n"
        "def not_a_generator():\n    try:\n        pass\n    finally:\n"
        "        t.close()\n        a.release(p)\n"
    )
    assert multi_step_finally_bodies(source) == ["two:5", "conditional:16"]


# --------------------------------------------------------------------------- #
# Structure: no live module keeps a bare post-yield release/close/shutdown   #
# --------------------------------------------------------------------------- #

#: Teardown calls that must not sit bare after a ``yield``.
TEARDOWN_CALLS = frozenset({"release", "close", "shutdown"})

def bare_teardown_calls(source: str) -> list[str]:
    """``fn:line call`` for each teardown call after a ``yield`` outside a ``finally``."""
    tree = ast.parse(source)
    offenders: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        yields = [n.lineno for n in ast.walk(fn) if isinstance(n, (ast.Yield, ast.YieldFrom))]
        if not yields:
            continue
        protected = {
            id(sub)
            for node in ast.walk(fn) if isinstance(node, ast.Try)
            for stmt in node.finalbody for sub in ast.walk(stmt)
        }
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in TEARDOWN_CALLS
                and node.lineno > min(yields)
                and id(node) not in protected
            ):
                offenders.append(f"{fn.name}:{node.lineno} {ast.unparse(node.func)}()")
    return offenders


def _scanned_modules() -> list[Path]:
    mods = sorted(
        {*TESTS_DIR.glob("test_*_live.py"), *TESTS_DIR.glob("test_blind_agent_*.py")}
    )
    assert len(mods) >= 40, "the live corpus is missing -- the scan looks in the wrong place"
    return mods


@pytest.mark.parametrize("path", _scanned_modules(), ids=lambda p: p.name)
def test_no_bare_post_yield_teardown(path: Path) -> None:
    offenders = bare_teardown_calls(path.read_text())
    assert not offenders, (
        f"{path.name}: teardown calls after a yield and outside any finally, so a "
        "step that raises (or an exception at the yield) skips them -- the "
        "DeviceLock leaks or a client is never closed (#334): " + ", ".join(offenders)
    )


def test_the_structure_scan_can_fail() -> None:
    source = (
        "def bare():\n    lock = L()\n    yield 1\n    c.close()\n    lock.release()\n"
        "def guarded():\n    try:\n        yield 1\n    finally:\n"
        "        try:\n            c.close()\n        finally:\n            lock.release()\n"
        "def before_yield():\n    c.close()\n    yield 1\n"
        "def not_a_generator():\n    lock.release()\n"
        "def half():\n    try:\n        yield 1\n    finally:\n        c.close()\n"
        "    mgr.shutdown()\n"
    )
    assert bare_teardown_calls(source) == [
        "bare:4 c.close()", "bare:5 lock.release()", "half:24 mgr.shutdown()",
    ]
