"""#522: conftest's per-device isolation must spare every module that drives a device.

``tests/conftest.py`` hides the machine's device alias map (#519) and resets
the process-wide ``/Temp`` ledgers around each test, but only for unit tests.
A module that drives a real device needs both. With the real map its lock
folds to the same ``uid-`` key as every other lane on that device, and the
kept ledger's count still describes the device it is driving.

Before #522 only ``*_live.py`` modules were spared. ``test_blind_agent_*``,
``test_bridge_ping_tod.py`` and ``test_stress_smoke.py`` drive the device under
``U64_HOST`` too, yet they got an empty map and locked the raw address. An
upgraded ``*_live.py`` lane on the same device is then refused by the
legacy-lockfile check instead of queueing.

The conftest runs for real, in a subprocess pytest over generated modules, so
what is pinned is the fixtures' behaviour, not a predicate's return value.
Plain modules sort before, between and after the device modules, so a stale
cache or count left by either kind is seen by the other. No device:
``U64_HOST`` is unset, and every live module skips its lock guard.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import c64_test_harness

TESTS = Path(__file__).resolve().parent
_SUPPORT = (
    "conftest.py", "bridge_platform.py", "live_fixture_teardown.py",
    # conftest imports its device-module criterion from here (#375).
    "test_live_mutation_gate.py",
)

_RECORD = '''
import json, os
from c64_test_harness.backends.device_lock import normalize_device_host
from c64_test_harness.backends.ultimate64_temp_gc import temp_ledger_for

OUT = os.environ["ISOLATION_OUT"]


def _record(field, value):
    data = json.load(open(OUT)) if os.path.exists(OUT) else {}
    data.setdefault(__name__, {})[field] = value
    json.dump(data, open(OUT, "w"))


def test_1_spend():
    _record("key", normalize_device_host("192.0.2.81"))
    # 192.0.2.99 is in no alias group, so every module shares its ledger.
    _record("pending_before", temp_ledger_for("192.0.2.99").pending)
    temp_ledger_for("192.0.2.81").pending = 5
    temp_ledger_for("192.0.2.99").pending = 5


def test_2_still_counted():
    _record("pending_after", temp_ledger_for("192.0.2.81").pending)
    _record("pending_after_unmapped", temp_ledger_for("192.0.2.99").pending)


def test_3_leave_a_count():
    # What the next module inherits unless this module's exit reset runs.
    temp_ledger_for("192.0.2.99").pending = 7
'''

#: module name -> (header, drives a device?)
_MODULES = {
    # sorts first: leaves whatever its fixtures leave for the device modules
    "test_a_plain_fake": ("", False),
    # reads U64_HOST at import, like the real blind-agent modules
    "test_blind_agent_fake": ('U64_HOST = os.environ.get("U64_HOST", "")\n', True),
    # name looks like one, content does not: isolated
    "test_blindish_fake": ("", False),
    "test_fake_live": ("", True),
    # reads U64_HOST but supplies its own: a mocked unit test, isolated
    "test_mocked_host_fake": (
        "def test_0_own_host(monkeypatch):\n"
        '    monkeypatch.setenv("U64_HOST", "fake-host")\n'
        '    assert os.environ.get("U64_HOST") == "fake-host"\n',
        False,
    ),
    # reads U64_HOST inside a function with no _live/_blind_agent name
    "test_odd_name_device_fake": (
        'def _host():\n    return os.environ.get("U64_HOST", "")\n', True,
    ),
}


def _run(tmp_path: Path) -> dict:
    for name in _SUPPORT:
        shutil.copy(TESTS / name, tmp_path / name)
    for module, (header, _) in _MODULES.items():
        (tmp_path / f"{module}.py").write_text(_RECORD + "\n" + header)
    out = tmp_path / "out.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("U64_")}
    env.update(
        ISOLATION_OUT=str(out),
        C64_DEVICE_ALIASES="601a96=192.0.2.81",
        C64_DEVICE_ALIASES_FILE=str(tmp_path / "absent.toml"),
        # The package under test, not whatever the editable install points at.
        PYTHONPATH=str(Path(c64_test_harness.__file__).resolve().parents[1]),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", "--ignore", str(tmp_path / "test_live_mutation_gate.py"),
         *(str(tmp_path / f"{m}.py") for m in sorted(_MODULES))],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(out.read_text())


def test_device_driving_modules_keep_the_map_and_the_ledger_unit_tests_do_not(tmp_path):
    seen = _run(tmp_path)
    assert set(seen) == set(_MODULES), sorted(seen)
    for module, (_, device) in _MODULES.items():
        got = seen[module]
        # Every module follows a plain one (or starts the run), whose last
        # test leaves a count that its fixtures must reset on the way out.
        assert got["pending_before"] == 0, (module, got)
        if device:
            assert got["key"] == "uid-601a96", (module, got)
            assert got["pending_after"] == got["pending_after_unmapped"] == 5, (module, got)
        else:
            assert got["key"] == "192.0.2.81", (module, got)
            assert got["pending_after"] == got["pending_after_unmapped"] == 0, (module, got)


def test_every_module_the_mutation_scan_calls_a_device_module_is_spared():
    """The criterion is shared with the ``U64_ALLOW_MUTATE`` scan; if conftest
    ever grows its own list again, this names the module it missed."""
    from conftest import drives_a_device_file
    from test_live_mutation_gate import _scanned_modules

    scanned = _scanned_modules()
    assert len(scanned) >= 30, len(scanned)
    missed = [p.name for p in scanned if not drives_a_device_file(str(p))]
    assert missed == [], missed
    for name in ("test_blind_agent_probe.py", "test_bridge_ping_tod.py", "test_stress_smoke.py"):
        assert drives_a_device_file(str(TESTS / name)), name
    assert not drives_a_device_file(str(TESTS / "test_device_lock_advisory.py"))
