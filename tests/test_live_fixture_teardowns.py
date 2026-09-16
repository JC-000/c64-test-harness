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

    # The entry-value reads the pre-#412 RR-Net fixtures made, so their red run
    # fails on the restored value rather than on a missing method.
    def get_config_value(self, category: str, item: str):
        entry = self.get_config_item(category, item)
        if "current" not in entry:  # as the real client
            from c64_test_harness import Ultimate64ProtocolError

            raise Ultimate64ProtocolError(
                f"config item {category!r}/{item!r} has no 'current' value: {entry!r}"
            )
        return entry["current"]

    def get_config_category(self, category: str) -> dict:
        """The real envelope: ``{category: {item: current, ...}, "errors": []}``.
        An item never written falls back to its entry value, as above."""
        client = self

        class _Items(dict):
            def __missing__(self, item: str):
                return client.get_config_value(category, item)

        items = _Items({
            item: entry["current"]
            for (cat, item), entry in self.config.items()
            if cat == category and "current" in entry
        })
        return {category: items, "errors": []}

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
    # #391: released the lock in a finally but never closed the client; left
    # out of #368 because another open PR owned the file then.
    Case("test_u64_turbo_bench_live.py", "client", "Ultimate64Client", lambda m: ["close"]),
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
        # chained from the first recorded failure, the close (#392 review, M17)
        assert isinstance(info.value.__cause__, RuntimeError)
        assert str(info.value.__cause__) == "FAKE 'close' failed"
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


class TestTurboBenchOriginalState:
    """``test_u64_turbo_bench_live.py::original_state``: the snapshot goes back
    on every exit, and a restore that fails is reported (#391)."""

    FILE = "test_u64_turbo_bench_live.py"
    RESTORE = [("restore", "SNAP")]

    def _gen(self, probe):
        module = _load_module(self.FILE)
        fake = _Fake(probe.journal, probe.fail)
        module.snapshot_state = lambda client: "SNAP"
        module.set_reu = lambda client, **_kw: fake._do("set_reu")
        module.restore_state = lambda client, snap: fake._do(("restore", snap))
        module.time = SimpleNamespace(sleep=lambda seconds: None)
        gen = _fixture_body(module, self.FILE, "original_state")(object())
        probe.generators.append(gen)
        return gen

    def test_normal_exit_restores(self, probe) -> None:
        gen = self._gen(probe)
        assert next(gen) == "SNAP"
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == self.RESTORE

    def test_an_exception_at_the_yield_still_restores_and_propagates(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == self.RESTORE

    def test_a_failed_restore_is_reported(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        probe.fail.add(("restore", "SNAP"))
        with pytest.raises(RuntimeError, match="original_state restore") as info:
            next(gen)
        assert str(info.value.__cause__) == "FAKE ('restore', 'SNAP') failed"

    def test_a_failed_restore_does_not_mask_the_test_body_exception(self, probe) -> None:
        gen = self._gen(probe)
        next(gen)
        probe.fail.add(("restore", "SNAP"))
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == self.RESTORE

    def test_a_failed_set_reu_still_restores(self, probe) -> None:
        gen = self._gen(probe)
        probe.fail.add("set_reu")
        with pytest.raises(RuntimeError, match="FAKE 'set_reu'"):
            next(gen)
        assert probe.journal == ["set_reu", *self.RESTORE]


class _FakeCapture(_Fake):
    def close(self) -> None:
        self._do("cap.close")


class TestFirstExchangeSession:
    """``test_first_exchange_live.py::session``: the capture is closed and
    ``Cartridge Preference`` put back on every exit, each step on its own, and
    a failed step is reported without masking the test body (#391)."""

    FILE = "test_first_exchange_live.py"

    def _gen(self, probe, *, linest: int = 0x80):
        from contextlib import contextmanager

        module = _load_module(self.FILE)
        journal, fail = probe.journal, probe.fail
        client = _FakeClient(journal, fail, probe.config)
        target = SimpleNamespace(
            transport=SimpleNamespace(client=client, reset=lambda: journal.append("reset"))
        )

        @contextmanager
        def scope(label, value):
            try:
                yield value
            finally:
                journal.append(f"{label} exit")

        manager = SimpleNamespace(instance=lambda: scope("instance", target))
        module.create_manager = lambda **_kw: scope("manager", manager)
        module.open_capture = lambda iface: _FakeCapture(journal, fail)
        module._host_addr = lambda iface: (bytes(6), bytes([10, 0, 0, 1]))
        module.time = SimpleNamespace(sleep=lambda seconds: None)
        module.wait_for_text = lambda *_a, **_kw: True
        module.load_code = lambda *_a, **_kw: None
        module.run_subroutine = lambda *_a, **_kw: None
        module._stat = lambda target: (linest, 0)
        gen = _fixture_body(module, self.FILE, "session")()
        probe.generators.append(gen)
        return module, gen

    @staticmethod
    def _teardown(module) -> list:
        return [
            "cap.close", ("config", module.CAT, module.ITEM, _default(module.ITEM)),
            "instance exit", "manager exit",
        ]

    def test_normal_exit_closes_restores_and_leaves_the_manager(self, probe) -> None:
        module, gen = self._gen(probe)
        next(gen)
        assert ("config", module.CAT, module.ITEM, "External") in probe.journal
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == self._teardown(module)

    def test_an_exception_at_the_yield_runs_every_step_and_propagates(self, probe) -> None:
        module, gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == self._teardown(module)

    def test_a_raising_close_still_restores_the_preference_and_is_reported(self, probe) -> None:
        module, gen = self._gen(probe)
        next(gen)
        probe.fail.add("cap.close")
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="session teardown") as info:
            next(gen)
        assert str(info.value.__cause__) == "FAKE 'cap.close' failed"
        assert _after(probe.journal, mark) == self._teardown(module)

    def test_a_raising_restore_is_reported(self, probe) -> None:
        module, gen = self._gen(probe)
        next(gen)
        probe.fail.add(("config", module.CAT, module.ITEM, _default(module.ITEM)))
        mark = len(probe.journal)
        with pytest.raises(RuntimeError, match="session teardown"):
            next(gen)
        assert _after(probe.journal, mark) == self._teardown(module)

    def test_a_raising_close_does_not_mask_the_test_body_exception(self, probe) -> None:
        module, gen = self._gen(probe)
        next(gen)
        probe.fail.add("cap.close")
        mark = len(probe.journal)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert _after(probe.journal, mark) == self._teardown(module)

    def test_a_failed_external_put_still_closes_and_restores(self, probe) -> None:
        module, gen = self._gen(probe)
        probe.fail.add(("config", module.CAT, module.ITEM, "External"))
        with pytest.raises(RuntimeError, match="FAKE"):
            next(gen)
        assert probe.journal[-4:] == self._teardown(module)

    def test_no_link_skips_and_still_tears_down(self, probe) -> None:
        module, gen = self._gen(probe, linest=0x00)
        with pytest.raises(pytest.skip.Exception, match="no 10BASE-T link"):
            next(gen)
        assert probe.journal[-4:] == self._teardown(module)


def _scope(journal: list, label: str, value):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        try:
            yield value
        finally:
            journal.append(f"{label} exit")

    return scope()


def _rrnet_manager(probe, module, transport) -> None:
    target = SimpleNamespace(transport=transport, client=transport.client)
    manager = SimpleNamespace(instance=lambda: _scope(probe.journal, "instance", target))
    module.create_manager = lambda **_kw: _scope(probe.journal, "manager", manager)
    module.time = SimpleNamespace(sleep=lambda seconds: None, monotonic=lambda: 0.0)


def _rrnet_fifo(probe):
    filename = "test_cs8900a_fifo_live.py"
    module = _load_module(filename)
    client = _FakeClient(probe.journal, probe.fail, probe.config)
    transport = SimpleNamespace(client=client, reset=lambda: probe.journal.append("reset"))
    _rrnet_manager(probe, module, transport)
    module._host_mac = lambda iface: bytes(6)
    module.wait_for_text = lambda *_a, **_kw: True
    module.load_code = lambda *_a, **_kw: None
    module.run_subroutine = lambda *_a, **_kw: None
    rxctl = module.RXCTL_IA_ONLY
    module.read_bytes = lambda *_a, **_kw: (
        b"\x0e\x63" + bytes([rxctl & 0xFF, rxctl >> 8]) + module.C64_MAC[:2]
    )
    module.open_capture = lambda iface: _FakeCapture(probe.journal, probe.fail)
    module._Bench = lambda *_a: SimpleNamespace()
    gen = _fixture_body(module, filename, "bench")()
    probe.generators.append(gen)
    return module, gen


def _rrnet_visibility(probe):
    filename = "test_run_prg_cartridge_visibility_live.py"
    module = _load_module(filename)
    client = _FakeClient(probe.journal, probe.fail, probe.config)
    transport = SimpleNamespace(client=client, reset=lambda: probe.journal.append("reset"))
    _rrnet_manager(probe, module, transport)

    def fresh(target, settle: float = 1.0) -> None:
        target.transport.client.set_config_item(module.CAT, module.ITEM, "External")

    module._fresh = fresh
    module._probe_at_ready = lambda target: module.IDENT + b"\x01"
    gen = _fixture_body(module, filename, "target")()
    probe.generators.append(gen)
    return module, gen


RRNET_FIXTURES = {
    "first_exchange::session": lambda probe: TestFirstExchangeSession()._gen(probe),
    "cs8900a_fifo::bench": _rrnet_fifo,
    "run_prg_cartridge_visibility::target": _rrnet_visibility,
}


@pytest.mark.parametrize("start", RRNET_FIXTURES.values(), ids=RRNET_FIXTURES.keys())
class TestRrnetPreferenceRestoresTheDefault:
    """#412: the three RR-Net fixtures that set ``Cartridge Preference =
    External`` put it back to the ``default`` the device reports, read before
    the ``External`` PUT -- not the value read at entry, which can be a
    SIGKILLed RR-Net lane's ``External`` (the #334 baseline rule)."""

    def test_normal_exit_writes_the_default_not_the_entry_value(self, probe, start) -> None:
        module, gen = start(probe)
        next(gen)
        assert ("config", module.CAT, module.ITEM, "External") in probe.journal
        with pytest.raises(StopIteration):
            next(gen)
        restores = [e for e in probe.journal if isinstance(e, tuple) and e[3] != "External"]
        assert restores == [("config", module.CAT, module.ITEM, _default(module.ITEM))]
        assert probe.config[(module.CAT, module.ITEM)]["current"] == _default(module.ITEM)
        assert probe.journal[-2:] == ["instance exit", "manager exit"]

    def test_an_exception_at_the_yield_still_writes_the_default(self, probe, start) -> None:
        module, gen = start(probe)
        next(gen)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert probe.config[(module.CAT, module.ITEM)]["current"] == _default(module.ITEM)
        assert probe.journal[-2:] == ["instance exit", "manager exit"]

    def test_a_failed_restore_is_reported(self, probe, start) -> None:
        module, gen = start(probe)
        next(gen)
        probe.fail.add(("config", module.CAT, module.ITEM, _default(module.ITEM)))
        with pytest.raises(RuntimeError, match="teardown") as info:
            next(gen)
        assert "FAKE" in str(info.value.__cause__)
        assert probe.journal[-2:] == ["instance exit", "manager exit"]

    @pytest.mark.parametrize("breakage", ["missing", "empty"])
    def test_no_default_refuses_before_the_external_put(self, probe, start, breakage) -> None:
        module, gen = start(probe)
        probe.config[(module.CAT, module.ITEM)] = (
            {"current": "External"} if breakage == "missing"
            else {"current": "External", "default": ""}
        )
        with pytest.raises(RuntimeError, match="no default"):
            next(gen)
        assert not [e for e in probe.journal if isinstance(e, tuple)], probe.journal
        assert probe.journal[-2:] == ["instance exit", "manager exit"]
        if module.__name__.endswith("first_exchange_live"):
            # session opens the capture before the manager: a refusal still
            # closes it (the plan read sits inside the try; #448 review R7)
            assert "cap.close" in probe.journal, probe.journal

    def test_a_failed_restore_does_not_mask_the_test_body_exception(
        self, probe, start
    ) -> None:
        """The failures are raised after the ``finally``, so an exception
        leaving the ``yield`` wins (#448 review R4/R4b)."""
        module, gen = start(probe)
        next(gen)
        probe.fail.add(("config", module.CAT, module.ITEM, _default(module.ITEM)))
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        assert ("config", module.CAT, module.ITEM, _default(module.ITEM)) in probe.journal
        assert probe.journal[-2:] == ["instance exit", "manager exit"]

    def test_a_drifted_entry_warns_naming_the_entry_value(self, probe, start, caplog) -> None:
        module, gen = start(probe)
        with caplog.at_level(logging.WARNING):
            next(gen)
        drift = [r.getMessage() for r in caplog.records if "drifted at entry" in r.getMessage()]
        assert len(drift) == 1, drift
        assert f"{module.CAT} / {module.ITEM} drifted at entry" in drift[0]
        assert repr(_entry(module.ITEM)) in drift[0]
        assert repr(_default(module.ITEM)) in drift[0]


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
            with pytest.raises(RuntimeError, match=r"teardown: .*FAKE") as info:
                next(gen)
            # chained from the step that failed (#392 review, M17)
            assert str(info.value.__cause__) == f"FAKE {failing!r} failed", failing
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

#: Calls that count as a teardown step inside a ``finally``.  A config restore
#: is one too (#391): ``cap.close()`` then ``client.set_config_item(...)`` in one
#: ``finally`` left ``Cartridge Preference`` unrestored when the close raised.
FINALLY_STEP_CALLS = frozenset(
    {"close", "release", "stop", "shutdown", "set_config_item", "restore_state"}
)


def _callee(call: ast.Call) -> str | None:
    """``x.close()`` -> ``close``; ``restore_state(c, s)`` -> ``restore_state``."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


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
                    isinstance(sub, ast.Call) and _callee(sub) in FINALLY_STEP_CALLS
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


def test_the_multi_step_finally_scan_sees_a_config_restore() -> None:
    """#391: the ``session`` shape -- a close then a config PUT -- and a bare
    ``restore_state`` call beside a close are both two steps."""
    source = (
        "def session():\n    try:\n        yield 1\n    finally:\n"
        "        cap.close()\n        client.set_config_item(CAT, ITEM, orig)\n"
        "def snapshot():\n    try:\n        yield 1\n    finally:\n"
        "        restore_state(client, snap)\n        client.close()\n"
        "def one_put():\n    try:\n        yield 1\n    finally:\n"
        "        client.set_config_item(CAT, ITEM, orig)\n"
    )
    assert multi_step_finally_bodies(source) == ["session:5", "snapshot:11"]


# --------------------------------------------------------------------------- #
# Structure: no live module keeps a bare post-yield release/close/shutdown   #
# --------------------------------------------------------------------------- #

#: Teardown calls that must not sit bare after a ``yield``.  A restore is one
#: (#391): a bare ``restore_state(client, snap)`` after the ``yield`` never ran
#: when the test body raised.
TEARDOWN_CALLS = frozenset({"release", "close", "shutdown", "restore_state", "set_config_item"})

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
                and _callee(node) in TEARDOWN_CALLS
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


def test_the_structure_scan_sees_a_bare_restore() -> None:
    """#391: the ``original_state`` shape -- a bare ``restore_state`` call, a
    plain function rather than a method -- and a bare config PUT after a
    ``yield`` are flagged; the same calls in a ``finally`` are not."""
    source = (
        "def original_state(client):\n    snap = snapshot_state(client)\n"
        "    yield snap\n    restore_state(client, snap)\n"
        "def put_back(client):\n    yield 1\n    client.set_config_item(C, I, V)\n"
        "def guarded(client):\n    try:\n        yield 1\n    finally:\n"
        "        restore_state(client, snap)\n"
    )
    assert bare_teardown_calls(source) == [
        "original_state:4 restore_state()", "put_back:7 client.set_config_item()",
    ]


# --------------------------------------------------------------------------- #
# #447: a live restore writes the device default, never the entry value       #
# --------------------------------------------------------------------------- #
#
# #412 fixed the three RR-Net ``Cartridge Preference`` fixtures; the sweep it
# asked for found the sites below still restoring a value read at entry.  The
# rule is the #334 baseline -- known state is ``current == default`` per item,
# so an entry value can be a SIGKILLed predecessor's residue and putting it
# back re-installs the drift.  One class per converted site, each driven
# offline against the module's own source through ``_fixture_body``.
#
# ``SID Detected Socket 1`` in ``test_sid_addressing_live.py`` is a named
# exemption and is deliberately NOT converted: it is a boot-probe measurement
# of the chip in the socket, so its schema ``default`` is not this bench's
# baseline.  The reason is recorded at that test.

_CAT_ADDRESSING = "SID Addressing"
_CAT_SOCKETS = "SID Sockets Configuration"
_CAT_CART = "C64 and Cartridge Settings"


# --------------------------------------------------------------------------- #
# The never-touch guard: no fixture restores a BASELINE_NEVER_TOUCH category  #
# to its "default"                                                            #
# --------------------------------------------------------------------------- #
#
# #447 review, blocker 1.  Converting a restore to the device default is right
# for a *selector* item, whose default is the #334 baseline.  It is wrong, and
# on one store dangerous, for a *measurement* store: writing ``SID Sockets
# Configuration`` back to its defaults sets ``SID Socket 1/2 = Disabled``, and
# ``U64SidSockets::effectuate_settings`` (u64_config.cc:744-800) then drops the
# PLD regulator bits -- the socketed SIDs are powered off while the config read
# stays clean, and detection only re-runs at boot or from the on-device menu.
# ``BASELINE_NEVER_TOUCH`` already names every such store, with its reason.
#
# This is a property over the whole live corpus rather than another per-site
# pin: a future fixture that adds one of those categories to a
# ``read_restore_defaults`` plan fails here, whether or not anyone writes a
# test for that fixture.

def _category_constants() -> dict[str, str]:
    """``CAT_*`` / ``CARTRIDGE_*`` name -> value, for resolving a Name key."""
    from c64_test_harness.backends import ultimate64_helpers as helpers

    return {
        name: value
        for name, value in vars(helpers).items()
        if isinstance(value, str) and name.startswith(("CAT_", "CARTRIDGE_"))
    }


def _constant_table(source: str) -> dict[str, str]:
    """Every name that resolves to a category string, for one module.

    The helpers' ``CAT_*`` constants overlaid with the module's **own**
    module-level string constants and aliases.  The live modules rarely hand
    a helpers name straight to ``read_restore_defaults``: they bind
    ``CAT = CARTRIDGE_SETTINGS_CATEGORY`` (alias),
    ``CAT = "C64 and Cartridge Settings"`` or ``_CAT_CART = "..."`` (local
    literal) first.  Resolving only the helpers names made this scan fail
    closed on four safe modules, which is a scanner defect, not a finding.
    Assignments are read in source order, so an alias sees what precedes it.
    """
    table = _category_constants()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            table[target.id] = value.value
        elif isinstance(value, ast.Name) and value.id in table:
            table[target.id] = table[value.id]
    return table


def restore_default_categories(source: str) -> list[tuple[str, str]]:
    """``(function, category)`` for every category handed to a default restore.

    Keys are read from the mapping literal passed to
    ``read_restore_defaults``; a literal string resolves to itself, and a name
    through :func:`_constant_table`.  Anything else resolves to an
    ``<unresolved ...>`` marker so the scan fails closed rather than passing a
    category it could not read.
    """
    consts = _constant_table(source)
    found: list[tuple[str, str]] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (
                isinstance(node, ast.Call)
                and _callee(node) == "read_restore_defaults"
            ):
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if not isinstance(arg, ast.Dict):
                    continue
                for key in arg.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.append((fn.name, key.value))
                    elif isinstance(key, ast.Name) and key.id in consts:
                        found.append((fn.name, consts[key.id]))
                    else:
                        found.append(
                            (fn.name, f"<unresolved {ast.unparse(key)}>")
                        )
    return found


@pytest.mark.parametrize("path", _scanned_modules(), ids=lambda p: p.name)
def test_no_default_restore_into_a_never_touch_category(path: Path) -> None:
    from c64_test_harness.backends.ultimate64_baseline import BASELINE_NEVER_TOUCH

    never = {name.casefold(): reason for name, reason in BASELINE_NEVER_TOUCH.items()}
    seen = restore_default_categories(path.read_text())

    unresolved = [f"{fn}: {cat}" for fn, cat in seen if cat.startswith("<unresolved")]
    assert not unresolved, (
        f"{path.name}: the never-touch scan could not resolve a category handed "
        "to read_restore_defaults, so it cannot prove the category is safe: "
        + ", ".join(unresolved)
    )

    offenders = [(fn, cat) for fn, cat in seen if cat.casefold() in never]
    assert not offenders, (
        f"{path.name}: a fixture restores a BASELINE_NEVER_TOUCH category to its "
        "'default', but that default is a reset product, not this bench's "
        "baseline -- for SID Sockets Configuration it powers the socketed SIDs "
        "off (#447). Restore the entry value and record the reason at the site: "
        + ", ".join(
            f"{fn} -> {cat} ({never[cat.casefold()][:80]}...)" for fn, cat in offenders
        )
    )


def test_the_never_touch_scan_resolves_every_key_form_and_can_fail() -> None:
    """Literal, helpers name, local alias and local literal all resolve.

    The last two are load-bearing: every RR-Net fixture binds
    ``CAT = CARTRIDGE_SETTINGS_CATEGORY`` and ``test_socketdma_live.py``
    binds ``_CAT_CART = "C64 and Cartridge Settings"``, so a scan that read
    only the helpers names reported four safe modules as unresolvable.
    """
    source = (
        "CAT = CARTRIDGE_SETTINGS_CATEGORY\n"
        "_CAT_CART = 'C64 and Cartridge Settings'\n"
        "def powers_the_sids_off(c):\n"
        "    plan = read_restore_defaults(c, {'SID Sockets Configuration': ['SID Socket 1']})\n"
        "def helpers_name(c):\n"
        "    plan = read_restore_defaults(c, {CAT_SID_ADDRESSING: ['UltiSID 1 Address']})\n"
        "def local_alias(c):\n"
        "    plan = read_restore_defaults(c, {CAT: ['Cartridge Preference']})\n"
        "def local_literal(c):\n"
        "    plan = read_restore_defaults(c, {_CAT_CART: ['REU Size']})\n"
        "def opaque(c):\n"
        "    plan = read_restore_defaults(c, {pick(): ['x']})\n"
    )
    assert restore_default_categories(source) == [
        ("powers_the_sids_off", "SID Sockets Configuration"),
        ("helpers_name", "SID Addressing"),
        ("local_alias", "C64 and Cartridge Settings"),
        ("local_literal", "C64 and Cartridge Settings"),
        ("opaque", "<unresolved pick()>"),
    ]

    from c64_test_harness.backends.ultimate64_baseline import BASELINE_NEVER_TOUCH

    # The category the scan exists to catch, and the two it must not.
    assert "SID Sockets Configuration" in BASELINE_NEVER_TOUCH
    assert "SID Addressing" not in BASELINE_NEVER_TOUCH
    assert "C64 and Cartridge Settings" not in BASELINE_NEVER_TOUCH


def _sid_items() -> tuple[list[str], str]:
    """The four slot-address items and the mirroring item, from the schema."""
    from c64_test_harness.backends.ultimate64_schema import (
        SID_AUTO_MIRRORING_ITEM,
        SID_SLOT_ADDRESS_ITEMS,
    )
    return list(SID_SLOT_ADDRESS_ITEMS.values()), SID_AUTO_MIRRORING_ITEM


def _prime(probe, category: str, items) -> None:
    """Give each item a distinct entry value and a distinct default."""
    for item in items:
        probe.config[(category, item)] = {
            "current": _entry(item), "default": _default(item)
        }


#: Real ``SID Addressing`` enum values.  The pre-#447 fixture restored through
#: ``set_sid_address_map``, which validates against the address enum, so
#: synthetic values made its red run fail on a schema refusal rather than on
#: the value it wrote.  With these the old code writes ``$D520`` successfully
#: and the red is a clean "wrote the entry value, wanted the default" diff.
#: ``$D400`` as the default is also the device's real stock allocation.
_SID_ENTRY_ADDRESS = "$D520"
_SID_DEFAULT_ADDRESS = "$D400"


def _prime_values(probe, category: str, items, current: str, default: str) -> None:
    for item in items:
        probe.config[(category, item)] = {"current": current, "default": default}


def _client(probe):
    client = _FakeClient(probe.journal, probe.fail, probe.config)
    probe.client = client
    return client


def _writes(category: str, items) -> list:
    return [("config", category, item, _default(item)) for item in items]


class TestSidAddressingFixtureRestoresDefaults:
    """``test_sid_addressing_live.py::restore_addressing`` (#447)."""

    NAME = "test_sid_addressing_live.py"

    def _gen(self, probe):
        items, _mirror = _sid_items()
        _prime_values(
            probe, _CAT_ADDRESSING, items, _SID_ENTRY_ADDRESS, _SID_DEFAULT_ADDRESS
        )
        module = _load_module(self.NAME)
        client = _client(probe)
        gen = _fixture_body(module, self.NAME, "restore_addressing")(client)
        probe.generators.append(gen)
        return items, client, gen

    def _current(self, client, item):
        return client.config[(_CAT_ADDRESSING, item)]["current"]

    def test_exit_writes_the_default_not_the_entry_map(self, probe) -> None:
        items, client, gen = self._gen(probe)
        assert next(gen) is client
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == [
            ("config", _CAT_ADDRESSING, item, _SID_DEFAULT_ADDRESS) for item in items
        ]
        for item in items:
            assert self._current(client, item) == _SID_DEFAULT_ADDRESS

    def test_an_exception_at_the_yield_still_writes_the_defaults(self, probe) -> None:
        items, client, gen = self._gen(probe)
        next(gen)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        for item in items:
            assert self._current(client, item) == _SID_DEFAULT_ADDRESS

    def test_a_failed_restore_is_reported_and_the_rest_still_run(self, probe) -> None:
        items, client, gen = self._gen(probe)
        next(gen)
        probe.fail.add(
            ("config", _CAT_ADDRESSING, items[0], _SID_DEFAULT_ADDRESS)
        )
        with pytest.raises(RuntimeError, match="restore_addressing teardown") as info:
            next(gen)
        assert "FAKE" in str(info.value.__cause__)
        for item in items[1:]:
            assert self._current(client, item) == _SID_DEFAULT_ADDRESS

    @pytest.mark.parametrize("breakage", ["missing", "empty"])
    def test_no_default_refuses_before_the_test_runs(self, probe, breakage) -> None:
        items, _mirror = _sid_items()
        _prime_values(
            probe, _CAT_ADDRESSING, items, _SID_ENTRY_ADDRESS, _SID_DEFAULT_ADDRESS
        )
        probe.config[(_CAT_ADDRESSING, items[0])] = (
            {"current": _SID_ENTRY_ADDRESS} if breakage == "missing"
            else {"current": _SID_ENTRY_ADDRESS, "default": ""}
        )
        module = _load_module(self.NAME)
        client = _client(probe)
        gen = _fixture_body(module, self.NAME, "restore_addressing")(client)
        probe.generators.append(gen)
        with pytest.raises(RuntimeError, match="no default"):
            next(gen)
        assert not [e for e in probe.journal if isinstance(e, tuple)], probe.journal

    def test_a_drifted_entry_warns_naming_what_exit_will_write(
        self, probe, caplog
    ) -> None:
        items, _client_, gen = self._gen(probe)
        with caplog.at_level(logging.WARNING):
            next(gen)
        drift = [
            r.getMessage() for r in caplog.records if "drifted at entry" in r.getMessage()
        ]
        assert len(drift) == len(items), drift
        assert repr(_SID_ENTRY_ADDRESS) in drift[0]
        assert repr(_SID_DEFAULT_ADDRESS) in drift[0]


class TestAudioCaptureFixtureRestoresDefaults:
    """``test_u64_audio_capture_live.py::restore_sid_config`` (#447).

    Two stores, two rules: ``SID Addressing`` goes back to the reported
    ``default``; the ``SID Sockets Configuration`` enables go back to the
    **entry value**, because that store is in ``BASELINE_NEVER_TOUCH`` and
    its default is a reset product that cuts power to the socketed SIDs.
    """

    NAME = "test_u64_audio_capture_live.py"
    SOCKETS = ["SID Socket 1", "SID Socket 2"]

    def _gen(self, probe):
        items, _mirror = _sid_items()
        _prime(probe, _CAT_ADDRESSING, items)
        _prime(probe, _CAT_SOCKETS, self.SOCKETS)
        module = _load_module(self.NAME)
        client = _client(probe)
        gen = _fixture_body(module, self.NAME, "restore_sid_config")(client)
        probe.generators.append(gen)
        return items, client, gen

    def _socket_entry_writes(self) -> list:
        return [
            ("config", _CAT_SOCKETS, item, _entry(item)) for item in self.SOCKETS
        ]

    def test_exit_writes_address_defaults_and_socket_entry_values(self, probe) -> None:
        items, _client_, gen = self._gen(probe)
        next(gen)
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == [
            *_writes(_CAT_ADDRESSING, items), *self._socket_entry_writes(),
        ]

    def test_the_socket_enable_default_is_never_written(self, probe) -> None:
        """Writing it drops the PLD regulator bits and powers the socketed
        SIDs off, with nothing in the config read to say so."""
        _items, client, gen = self._gen(probe)
        next(gen)
        with pytest.raises(StopIteration):
            next(gen)
        for item in self.SOCKETS:
            assert ("config", _CAT_SOCKETS, item, _default(item)) not in probe.journal
            assert client.config[(_CAT_SOCKETS, item)]["current"] == _entry(item)

    def test_an_exception_at_the_yield_still_restores(self, probe) -> None:
        items, client, gen = self._gen(probe)
        next(gen)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        for item in items:
            assert client.config[(_CAT_ADDRESSING, item)]["current"] == _default(item)
        for item in self.SOCKETS:
            assert client.config[(_CAT_SOCKETS, item)]["current"] == _entry(item)

    def test_an_address_without_a_default_refuses_to_start(self, probe) -> None:
        """The old fixture logged and yielded anyway; a device that cannot be
        put back must stop the run, not poison every later measurement."""
        items, _mirror = _sid_items()
        _prime(probe, _CAT_ADDRESSING, items)
        _prime(probe, _CAT_SOCKETS, self.SOCKETS)
        probe.config[(_CAT_ADDRESSING, items[0])] = {"current": _entry(items[0])}
        module = _load_module(self.NAME)
        client = _client(probe)
        gen = _fixture_body(module, self.NAME, "restore_sid_config")(client)
        probe.generators.append(gen)
        with pytest.raises(RuntimeError, match="no default"):
            next(gen)
        assert not [e for e in probe.journal if isinstance(e, tuple)], probe.journal

    def test_a_socket_without_a_current_value_refuses_to_start(self, probe) -> None:
        """The exemption reads ``current``, so that is what it refuses over."""
        items, _mirror = _sid_items()
        _prime(probe, _CAT_ADDRESSING, items)
        _prime(probe, _CAT_SOCKETS, self.SOCKETS)
        probe.config[(_CAT_SOCKETS, self.SOCKETS[0])] = {
            "default": _default(self.SOCKETS[0])
        }
        module = _load_module(self.NAME)
        client = _client(probe)
        gen = _fixture_body(module, self.NAME, "restore_sid_config")(client)
        probe.generators.append(gen)
        with pytest.raises(RuntimeError, match="no current value"):
            next(gen)
        assert not [e for e in probe.journal if isinstance(e, tuple)], probe.journal


class TestSidIsolationTargetRestoresDefaults:
    """``test_sid_addressing_isolation_live.py::target`` (#447)."""

    NAME = "test_sid_addressing_isolation_live.py"

    def _gen(self, probe):
        items, mirror = _sid_items()
        owned = [*items, mirror]
        _prime(probe, _CAT_ADDRESSING, owned)
        module = _load_module(self.NAME)
        client = _client(probe)
        tgt = SimpleNamespace(client=client, transport=object())

        class _Instance:
            def __enter__(self_inner):
                return tgt

            def __exit__(self_inner, *exc):
                probe.journal.append("instance exit")
                return False

        class _Manager:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                probe.journal.append("manager exit")
                return False

            def instance(self_inner):
                return _Instance()

        module.create_manager = lambda **_kw: _Manager()
        module.check_measurement_environment = lambda _c: None
        module.wait_for_text = lambda *_a, **_kw: "READY."
        module.time = SimpleNamespace(sleep=lambda _s: None)
        gen = _fixture_body(module, self.NAME, "target")()
        probe.generators.append(gen)
        return owned, client, tgt, gen

    def test_exit_writes_the_defaults_not_the_stock_snapshot(self, probe) -> None:
        owned, _client_, tgt, gen = self._gen(probe)
        assert next(gen) is tgt
        mark = len(probe.journal)
        with pytest.raises(StopIteration):
            next(gen)
        assert _after(probe.journal, mark) == [
            *_writes(_CAT_ADDRESSING, owned), "instance exit", "manager exit",
        ]

    def test_the_fixture_publishes_the_defaults_for_the_tests(self, probe) -> None:
        owned, _client_, tgt, gen = self._gen(probe)
        next(gen)
        assert tgt.sid_addressing_defaults == {i: _default(i) for i in owned}
        assert tgt.stock_sid_addressing != tgt.sid_addressing_defaults

    def test_a_restore_that_does_not_take_is_reported(self, probe) -> None:
        """The read-back verification is against the defaults now, so a PUT
        the device accepts and never applies is still caught."""
        owned, client, _tgt, gen = self._gen(probe)
        next(gen)
        item = owned[0]
        real = client.set_config_item

        def swallow(category, name, value):
            if name == item:
                client.journal.append(("config", category, name, value))
                return  # accepted, never applied
            real(category, name, value)

        client.set_config_item = swallow
        with pytest.raises(RuntimeError, match="SID Addressing teardown"):
            next(gen)

    def test_an_exception_at_the_yield_still_writes_the_defaults(self, probe) -> None:
        owned, client, _tgt, gen = self._gen(probe)
        next(gen)
        with pytest.raises(KeyError, match="test body"):
            gen.throw(KeyError("test body"))
        for item in owned:
            assert client.config[(_CAT_ADDRESSING, item)]["current"] == _default(item)


class TestAutoMirroringTestRestoresTheDefault:
    """``test_sid_addressing_isolation_live.py`` in-test ``finally`` (#447)."""

    NAME = "test_sid_addressing_isolation_live.py"

    def test_the_finally_writes_the_default_not_the_stock_value(self, probe) -> None:
        _items, mirror = _sid_items()
        _prime(probe, _CAT_ADDRESSING, [mirror])
        module = _load_module(self.NAME)
        client = _client(probe)
        module.set_sid_auto_mirroring = lambda c, on: c.set_config_item(
            _CAT_ADDRESSING, mirror, "Enabled" if on else "Disabled"
        )
        target = SimpleNamespace(
            client=client,
            stock_sid_addressing={mirror: _entry(mirror)},
            sid_addressing_defaults={mirror: _default(mirror)},
        )
        body = _fixture_body(
            module, self.NAME, "test_auto_mirroring_readback_reflects_the_write"
        )
        body(target)
        assert probe.journal[-1] == (
            "config", _CAT_ADDRESSING, mirror, _default(mirror)
        )
        assert client.config[(_CAT_ADDRESSING, mirror)]["current"] == _default(mirror)


class TestSocketDmaReuRestoresDefaults:
    """``test_socketdma_live.py`` REU ``finally`` blocks (#447)."""

    NAME = "test_socketdma_live.py"
    ITEMS = ["RAM Expansion Unit", "REU Size"]

    def _module(self, probe):
        _prime(probe, _CAT_CART, self.ITEMS)
        module = _load_module(self.NAME)
        client = _client(probe)
        client.get_info = lambda: {"product": "fake"}
        module.set_reu = lambda c, on, size=None: c.set_config_item(
            _CAT_CART, "REU Size", size
        )
        module.get_reu_config = lambda _c: (True, "512 KB")
        return module, client

    def test_the_contract_test_restores_the_defaults(self, probe) -> None:
        module, client = self._module(probe)
        body = _fixture_body(module, self.NAME, "test_set_reu_c64u_contract")
        body(client, lambda *_a: None)
        assert probe.journal[-2:] == _writes(_CAT_CART, self.ITEMS)
        for item in self.ITEMS:
            assert client.config[(_CAT_CART, item)]["current"] == _default(item)

    def _fidelity(self, module, client):
        def refuse(*_a, **_kw):
            from c64_test_harness import Ultimate64Error

            raise Ultimate64Error("connect refused")

        module.Ultimate64Transport = lambda **_kw: SimpleNamespace(
            close=lambda: client._do("transport.close"),
            socket_dma_reu_write=refuse,
        )
        return _fixture_body(module, self.NAME, "test_reuwrite_byte_fidelity")

    def test_the_fidelity_test_restores_the_defaults(self, probe) -> None:
        module, client = self._module(probe)
        body = self._fidelity(module, client)
        with pytest.raises(pytest.skip.Exception):
            body(client)
        assert probe.journal[-3:] == [
            "transport.close", *_writes(_CAT_CART, self.ITEMS),
        ]

    def test_a_raising_close_no_longer_skips_the_config_restore(self, probe) -> None:
        """The old ``finally`` ran ``transport.close()`` first and bare, so a
        close that raised left the REU enabled on a shared device."""
        module, client = self._module(probe)
        body = self._fidelity(module, client)
        probe.fail.add("transport.close")
        with pytest.raises(pytest.skip.Exception):
            body(client)
        for item in self.ITEMS:
            assert client.config[(_CAT_CART, item)]["current"] == _default(item)


class TestReuReadbackCartridgeRestoresTheDefault:
    """``test_reu_size_readback_live.py`` ``Cartridge`` ``finally`` (#447)."""

    NAME = "test_reu_size_readback_live.py"

    def test_a_crt_selected_at_entry_is_not_re_selected(self, probe) -> None:
        """Entry drift -- a ``.crt`` chosen by whatever ran last -- must not go
        back on: the item's own default (``""``) is the baseline."""
        probe.config[(_CAT_CART, "Cartridge")] = {
            "current": "game.crt", "default": "", "presets": [""]
        }
        probe.config[(_CAT_CART, "REU Size")] = {"current": "2 MB", "default": "2 MB"}
        probe.config[(_CAT_CART, "RAM Expansion Unit")] = {
            "current": "Enabled", "default": "Enabled"
        }
        module = _load_module(self.NAME)
        client = _client(probe)
        module.get_reu_config = lambda _c: (True, "2 MB")
        stock = {
            "Cartridge": "game.crt",
            "REU Size": "2 MB",
            "RAM Expansion Unit": "Enabled",
        }
        body = _fixture_body(
            module, self.NAME, "test_cartridge_write_does_not_move_reu_size"
        )
        body(client, stock, lambda *_a: None)
        assert client.config[(_CAT_CART, "Cartridge")]["current"] == ""
        assert ("config", _CAT_CART, "Cartridge", "game.crt") not in probe.journal
