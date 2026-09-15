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
# Structure: no live module keeps a bare post-yield release/close/shutdown   #
# --------------------------------------------------------------------------- #

#: Teardown calls that must not sit bare after a ``yield``.
TEARDOWN_CALLS = frozenset({"release", "close", "shutdown"})

#: Live modules another lane owns while #334 is open; their bare shapes are
#: listed in the #334 follow-up issue, not fixed here.
OWNED_ELSEWHERE = {
    "test_u64_debug_stream_speed_live.py": "the speed-restore lane owns it",
}


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
    return [p for p in mods if p.name not in OWNED_ELSEWHERE]


@pytest.mark.parametrize("path", _scanned_modules(), ids=lambda p: p.name)
def test_no_bare_post_yield_teardown(path: Path) -> None:
    offenders = bare_teardown_calls(path.read_text())
    assert not offenders, (
        f"{path.name}: teardown calls after a yield and outside any finally, so a "
        "step that raises (or an exception at the yield) skips them -- the "
        "DeviceLock leaks or a client is never closed (#334): " + ", ".join(offenders)
    )


def test_owned_elsewhere_names_real_files() -> None:
    for name in OWNED_ELSEWHERE:
        assert (TESTS_DIR / name).exists(), f"stale OWNED_ELSEWHERE entry {name}"


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
