"""The x25519 bench routines never land on harness scratch, for any main_loop (#477).

Both routines now sit at a fixed page with main_loop's low byte, so the
one-byte hijack cannot be fetched torn.  That moves them with the build: this
checks every low byte against every non-transient ``HARNESS_SCRATCH`` span,
for the script's benchmark routine and the live module's trampoline.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from c64_test_harness.memory_policy import harness_scratch_regions

_TESTS = Path(__file__).resolve().parent
_SCRIPT = _TESTS.parent / "scripts" / "bench_x25519_u64_turbo.py"
_LIVE = _TESTS / "test_u64_turbo_bench_live.py"

#: Stand-in call targets; only the routine's own span is checked.
_CALLS = {"vic_blank": 0x17E1, "bench_cycles_start": 0x177D, "x25519_base": 0x1741,
          "bench_cycles_stop": 0x17AC, "vic_unblank": 0x17EA}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _overlaps(start: int, length: int) -> list[str]:
    end = start + length
    return [str(r) for r in harness_scratch_regions()
            if start < r.end and r.start < end]


def test_script_bench_routine_avoids_harness_scratch() -> None:
    module = _load(_SCRIPT, "bench_x25519_scratch_check")
    hits = {}
    for lo in range(256):
        base = module._bench_sub_addr(0x0800 | lo)
        code = module._build_bench_subroutine(base, _CALLS)
        if found := _overlaps(base, len(code)):
            hits[f"${base:04X}+{len(code)}"] = found
    assert hits == {}


def test_live_module_trampoline_avoids_harness_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prg = tmp_path / "x25519.prg"
    prg.write_bytes(b"\x01\x08")
    monkeypatch.setenv("X25519_PRG", str(prg))
    module = _load(_LIVE, "turbo_bench_scratch_check")
    hits = {}
    for lo in range(256):
        tramp = module._trampoline_addr(0x0800 | lo)
        monkeypatch.setattr(module, "TRAMPOLINE", tramp)
        code = module._trampoline_code(0x148E)
        if found := _overlaps(tramp, len(code)):
            hits[f"${tramp:04X}+{len(code)}"] = found
    assert hits == {}
