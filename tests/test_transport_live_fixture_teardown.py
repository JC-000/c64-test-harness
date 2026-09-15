"""``test_ultimate64_transport_live``'s fixtures, driven offline with fakes (#276).

The module's ``transport`` teardown used to be a bare post-``yield``
sequence: an exception arriving at the ``yield`` (``generator.throw``, a
``GeneratorExit`` from ``close()``) skipped ``set_speed(1)``, ``t.close()``
and ``lock.release()`` alike, and a raising ``close()`` orphaned the
``DeviceLock``.  This pins the repaired shape without a device:

* every teardown step is attempted whatever arrives at the ``yield``, and
  the lock is released **last**, even when a step raises;
* a failed restore is reported (raised after the lock is released), not
  swallowed;
* the CPU-speed tests reconcile at **entry** through the merged mechanism
  -- ``resolve_baseline_on_entry`` plus ``apply_factory_baseline`` -- which
  runs on the Ultimate line by default and **never** on a C64 Ultimate or an
  unidentified device, not even when ``U64_BASELINE_ON_ENTRY=1`` asks;
* every test in the module that writes CPU speed requests that fixture.

The fixture bodies are lifted from the module source (decorators stripped)
and executed in a private copy of the module's namespace, so the fakes
patched onto that copy are what they see.  pytest's fixture wrapper is not
used (#314), and the same extraction runs against any revision of the
module, which is how the red run against the pre-#276 module was taken.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from c64_test_harness import BASELINE_ON_ENTRY_ENV

MODULE_PATH = Path(__file__).parent / "test_ultimate64_transport_live.py"


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #

def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_teardown_probe_transport_live", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_body(module, name: str):
    """The module-level fixture *name* as a plain generator function."""
    tree = ast.parse(MODULE_PATH.read_text(), filename=str(MODULE_PATH))
    found = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]
    if not found:
        pytest.fail(f"{MODULE_PATH.name} defines no module-level fixture {name!r}")
    fn = found[0]
    fn.decorator_list = []
    fn.returns = None
    code = compile(
        ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
        str(MODULE_PATH), "exec",
    )
    exec(code, module.__dict__)  # noqa: S102 -- the module's own source
    return module.__dict__[name]


class _FakeLock:
    def __init__(self, journal: list, host: str) -> None:
        self.journal = journal
        journal.append(("lock", host))

    def acquire(self, timeout=None, **_kw) -> bool:
        self.journal.append("acquire")
        return True

    def release(self) -> None:
        self.journal.append("release")


class _FakeClient:
    def __init__(self, generation) -> None:
        self._generation = generation

    @property
    def capabilities(self):
        if isinstance(self._generation, BaseException):
            raise self._generation
        return SimpleNamespace(generation=self._generation)


class _FakeTransport:
    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.fail_close = False
        self.fail_speed = False
        self.client = _FakeClient("ultimate")

    def close(self) -> None:
        self.journal.append("close")
        if self.fail_close:
            raise RuntimeError("close failed")

    def set_speed(self, multiplier) -> None:
        self.journal.append(("set_speed", multiplier))
        if self.fail_speed:
            raise RuntimeError("set_speed failed")


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    for var in (BASELINE_ON_ENTRY_ENV, "C64TEST_" + BASELINE_ON_ENTRY_ENV):
        monkeypatch.delenv(var, raising=False)
    module = _load_module()
    journal: list = []
    state = SimpleNamespace(
        module=module,
        journal=journal,
        transport=_FakeTransport(journal),
        construct_error=None,
        generators=[],
    )

    def make_transport(**_kw):
        journal.append("construct")
        if state.construct_error is not None:
            raise state.construct_error
        return state.transport

    def fake_baseline(client, **_kw):
        journal.append("baseline")
        return SimpleNamespace(summary=lambda: "fake", drifted_items=lambda: [])

    module.DeviceLock = lambda host, **_kw: _FakeLock(journal, host)
    module.Ultimate64Transport = make_transport
    module.apply_factory_baseline = fake_baseline
    module._HOST = "device-under-test"
    module._PW = None
    module._ALLOW_MUTATE = True
    return state


def _after(journal: list, mark: int) -> list:
    return journal[mark:]


# --------------------------------------------------------------------------- #
# transport: lock, construct, close, release                                  #
# --------------------------------------------------------------------------- #

class TestTransportFixture:
    def _gen(self, probe):
        return _fixture_body(probe.module, "transport")()

    def test_normal_exit_closes_and_releases_the_lock_last(self, probe) -> None:
        gen = self._gen(probe)
        assert next(gen) is probe.transport
        with pytest.raises(StopIteration):
            next(gen)
        assert "close" in probe.journal
        assert probe.journal[-1] == "release"

    def test_an_exception_at_the_yield_still_closes_and_releases(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="test body"):
            gen.throw(RuntimeError("test body"))
        assert _after(probe.journal, mark) == ["close", "release"]

    def test_generator_close_still_closes_and_releases(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        gen.close()
        assert _after(probe.journal, mark) == ["close", "release"]

    def test_a_raising_close_still_releases_and_is_reported(self, probe) -> None:
        probe.transport.fail_close = True
        gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="close failed"):
            next(gen)
        assert _after(probe.journal, mark) == ["close", "release"]

    def test_a_raising_constructor_still_releases_the_lock(self, probe) -> None:
        probe.construct_error = OSError("unreachable host")
        gen = self._gen(probe)
        with pytest.raises(OSError, match="unreachable host"):
            next(gen)
        assert probe.journal[-1] == "release"


# --------------------------------------------------------------------------- #
# speed_baseline: gate, entry reconciliation, 1 MHz restore                   #
# --------------------------------------------------------------------------- #

class TestSpeedFixture:
    def _gen(self, probe):
        gen = _fixture_body(probe.module, "speed_baseline")(probe.transport)
        # Keep it referenced: a dropped generator is closed by the garbage
        # collector, and that close runs the exit restore before the assert.
        probe.generators.append(gen)
        return gen

    def test_skips_without_the_mutate_gate_and_writes_nothing(self, probe) -> None:
        probe.module._ALLOW_MUTATE = False
        with pytest.raises(pytest.skip.Exception):
            next(self._gen(probe))
        assert not any(
            e == "baseline" or (isinstance(e, tuple) and e[0] == "set_speed")
            for e in probe.journal
        )

    def test_the_ultimate_line_reconciles_then_sets_1_mhz(self, probe) -> None:
        next(self._gen(probe))
        assert probe.journal == ["baseline", ("set_speed", 1)]

    @pytest.mark.parametrize("env", [None, "1"])
    @pytest.mark.parametrize("generation", ["cbm", "unknown", None, "a-future-line"])
    def test_a_non_ultimate_device_is_never_reset_at_entry(
        self, probe, monkeypatch, generation, env
    ) -> None:
        if env is not None:
            monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, env)
        probe.transport.client = _FakeClient(generation)
        next(self._gen(probe))
        assert "baseline" not in probe.journal
        assert probe.journal == [("set_speed", 1)]

    def test_an_unreadable_generation_is_never_reset_at_entry(self, probe) -> None:
        probe.transport.client = _FakeClient(TimeoutError("probe timed out"))
        next(self._gen(probe))
        assert probe.journal == [("set_speed", 1)]

    def test_the_env_opt_out_skips_the_ultimate_reset(self, probe, monkeypatch) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "0")
        next(self._gen(probe))
        assert probe.journal == [("set_speed", 1)]

    def test_an_exception_at_the_yield_still_restores_1_mhz(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="test body"):
            gen.throw(RuntimeError("test body"))
        assert _after(probe.journal, mark) == [("set_speed", 1)]

    def test_a_failed_restore_is_reported_not_swallowed(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        probe.transport.fail_speed = True
        with pytest.raises(RuntimeError, match="set_speed failed"):
            next(gen)


# --------------------------------------------------------------------------- #
# Both fixtures together, as pytest finalises them                            #
# --------------------------------------------------------------------------- #

def test_an_exception_reaches_every_restore_and_the_lock_is_released_last(probe) -> None:
    outer = _fixture_body(probe.module, "transport")()
    t = next(outer)
    inner = _fixture_body(probe.module, "speed_baseline")(t)
    next(inner)
    mark = len(probe.journal)
    boom = RuntimeError("killed mid-test")
    with pytest.raises(RuntimeError, match="killed mid-test"):
        inner.throw(boom)
    with pytest.raises(RuntimeError, match="killed mid-test"):
        outer.throw(boom)
    assert _after(probe.journal, mark) == [("set_speed", 1), "close", "release"]


def test_a_failing_restore_does_not_skip_close_or_release(probe) -> None:
    outer = _fixture_body(probe.module, "transport")()
    t = next(outer)
    inner = _fixture_body(probe.module, "speed_baseline")(t)
    next(inner)
    probe.transport.fail_speed = True
    mark = len(probe.journal)
    with pytest.raises(RuntimeError, match="set_speed failed"):
        next(inner)
    with pytest.raises(StopIteration):
        next(outer)
    assert _after(probe.journal, mark) == [("set_speed", 1), "close", "release"]


# --------------------------------------------------------------------------- #
# Structure: every CPU-speed-writing test requests speed_baseline             #
# --------------------------------------------------------------------------- #

def _usefixtures(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for dec in getattr(node, "decorator_list", []):
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "usefixtures"
        ):
            names.update(
                a.value for a in dec.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            )
    return names


def speed_writers_without_the_fixture(source: str) -> tuple[list[str], int]:
    """(offending test names, number of speed-writing tests seen)."""
    tree = ast.parse(source)
    offenders: list[str] = []
    seen = 0

    def visit(node: ast.AST, inherited: set[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, inherited | _usefixtures(child))
            elif isinstance(child, ast.FunctionDef) and child.name.startswith("test"):
                writes = any(
                    isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "set_speed"
                    for c in ast.walk(child)
                )
                if not writes:
                    continue
                nonlocal seen
                seen += 1
                params = {a.arg for a in child.args.args}
                if "speed_baseline" not in (inherited | _usefixtures(child) | params):
                    offenders.append(child.name)

    visit(tree, set())
    return offenders, seen


def test_every_cpu_speed_test_requests_the_speed_fixture() -> None:
    offenders, seen = speed_writers_without_the_fixture(MODULE_PATH.read_text())
    assert seen >= 9, f"only {seen} speed-writing tests found; the scan is broken"
    assert not offenders, (
        "these tests write CPU speed without requesting speed_baseline, so "
        "they get neither entry reconciliation nor the 1 MHz restore: "
        + ", ".join(offenders)
    )


def test_the_structure_scan_can_fail() -> None:
    source = (
        "import pytest\n"
        "@pytest.mark.usefixtures('speed_baseline')\n"
        "class TestA:\n"
        "    def test_ok(self, transport):\n        transport.set_speed(4)\n"
        "class TestB:\n"
        "    def test_bad(self, transport):\n        transport.set_speed(4)\n"
        "    @pytest.mark.usefixtures('speed_baseline')\n"
        "    def test_marked(self, transport):\n        transport.set_speed(4)\n"
        "def test_param(speed_baseline, transport):\n    transport.set_speed(2)\n"
        "def test_read_only(transport):\n    transport.get_speed()\n"
    )
    assert speed_writers_without_the_fixture(source) == (["test_bad"], 4)
