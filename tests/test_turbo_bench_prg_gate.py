"""The turbo-bench live module's PRG gate must be a decision, not an accident.

Issue #245. ``tests/test_u64_turbo_bench_live.py`` gated itself on

    _PRG_PATH = Path("/home/someone/c64-x25519/build/x25519.prg")
    pytest.mark.skipif(not _PRG_PATH.exists(), reason=f"{_PRG_PATH} not found")

an absolute path into another project's build tree on a Linux home
directory. On this macOS bench it can never exist, so the module — the
heaviest upload loop in the repository, 12 ``run_prg`` uploads per session
(4 turbo speeds x 3 vectors) — has been skipping silently and invisibly.
On a Linux checkout where the path happens to resolve it arms itself with
no other change, and whoever runs the live suite there gets 12 uploads they
did not plan. A guard that holds only on one developer's laptop is not a
guard.

The gate is now an explicit ``X25519_PRG`` environment variable, and this
module pins the three outcomes apart:

* unset -> skip, reason naming the variable (an intentional gate);
* set but unresolvable -> skip, reason naming the path (an environment
  defect, not a decision);
* set and resolvable -> armed with that path.

No device traffic: only the module's gate resolver is exercised.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


_LIVE_MODULE = Path(__file__).resolve().parent / "test_u64_turbo_bench_live.py"

#: The literal that made the skip an accident of ``$HOME``.
_HARD_CODED_PRG = "/home/someone/c64-x25519/build/x25519.prg"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_turbo_bench_live_under_test", _LIVE_MODULE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_hard_coded_prg_path_remains() -> None:
    source = _LIVE_MODULE.read_text()
    assert _HARD_CODED_PRG not in source, (
        "the live module still gates on a hard-coded path into another "
        "project's Linux build tree"
    )


def test_unset_env_var_skips_with_a_reason_that_names_the_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("X25519_PRG", raising=False)
    module = _load()

    path, reason = module._resolve_prg_path()

    assert path is None
    assert "X25519_PRG" in reason, (
        f"skip reason must name the variable the caller has to set: {reason!r}"
    )
    # The old reason read like a missing file; it must not now.
    assert "not found" not in reason
    # And the unset case must be reported *as* unset. Without this, a
    # resolver that quietly substitutes a default path passes every other
    # assertion here — it reports the default as missing, which names the
    # variable and avoids "not found", while having reintroduced exactly
    # the accident #245 is about (mutant D).
    assert "not set" in reason, (
        f"unset must be reported as unset, not as an unresolvable path: {reason!r}"
    )


def test_set_but_missing_file_skips_as_an_environment_defect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller who set the variable and got a silent skip deserves the why."""
    missing = tmp_path / "nowhere" / "x25519.prg"
    monkeypatch.setenv("X25519_PRG", str(missing))
    module = _load()

    path, reason = module._resolve_prg_path()

    assert path is None
    assert str(missing) in reason, (
        f"skip reason must name the path that did not resolve: {reason!r}"
    )
    assert "X25519_PRG" in reason


def test_resolvable_path_arms_the_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prg = tmp_path / "x25519.prg"
    prg.write_bytes(b"\x01\x08")
    monkeypatch.setenv("X25519_PRG", str(prg))
    module = _load()

    path, reason = module._resolve_prg_path()

    assert path == prg
    assert reason == ""


def test_module_level_gate_uses_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver is what the ``pytestmark`` skipif is actually built from.

    Without this, the resolver could be correct and unused — the module
    still gating on something else entirely.
    """
    monkeypatch.delenv("X25519_PRG", raising=False)
    module = _load()

    assert module._PRG_PATH is None
    reasons = [
        m.kwargs.get("reason", "")
        for m in module.pytestmark
        if m.name == "skipif"
    ]
    assert any("X25519_PRG" in r for r in reasons), (
        f"no module-level skipif carries the resolver's reason: {reasons!r}"
    )
