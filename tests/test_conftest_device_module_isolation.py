"""#522: conftest's per-device isolation must spare every module that drives a device.

``tests/conftest.py`` hides the machine's device alias map (#519) and resets
the process-wide ``/Temp`` ledgers around each test, but only for unit tests.
A module that drives a real device needs both. With the real map its lock
folds to the same ``uid-`` key as every other lane on that device, and the
kept ledger's count still describes the device it is driving. Before #522
only ``*_live.py`` modules were spared. ``test_blind_agent_*`` modules drive a
device under ``U64_HOST`` too (through ``UnifiedManager(backend="u64")``), yet
they got an empty map and locked the raw address. An upgraded ``*_live.py``
lane on the same device is then refused by the legacy-lockfile check instead
of queueing.

The conftest runs for real, in a subprocess pytest over generated modules, so
what is pinned is the fixtures' behaviour rather than a predicate's return
value. No device: ``U64_HOST`` is unset, and every live module skips its lock
guard.
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
_SUPPORT = ("conftest.py", "bridge_platform.py", "live_fixture_teardown.py")

_MODULE = '''
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
    temp_ledger_for("192.0.2.81").pending = 5


def test_2_still_counted():
    _record("pending_after", temp_ledger_for("192.0.2.81").pending)
'''


def _run(tmp_path: Path) -> dict:
    for name in _SUPPORT:
        shutil.copy(TESTS / name, tmp_path / name)
    for module in ("test_blind_agent_fake", "test_fake_live", "test_plain_fake"):
        (tmp_path / f"{module}.py").write_text(_MODULE)
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
         "-p", "no:randomly", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(out.read_text())


def test_device_driving_modules_keep_the_map_and_the_ledger_unit_tests_do_not(tmp_path):
    seen = _run(tmp_path)
    for module in ("test_blind_agent_fake", "test_fake_live"):
        assert seen[module]["key"] == "uid-601a96", (module, seen[module])
        assert seen[module]["pending_after"] == 5, (module, seen[module])
    assert seen["test_plain_fake"]["key"] == "192.0.2.81", seen["test_plain_fake"]
    assert seen["test_plain_fake"]["pending_after"] == 0, seen["test_plain_fake"]
