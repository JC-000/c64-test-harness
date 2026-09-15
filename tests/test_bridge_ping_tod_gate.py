"""``test_bridge_ping_tod.py`` writes device config, so it is gated on ``U64_ALLOW_MUTATE``.

``TestTodPrimitiveU64Live`` sweeps ``set_turbo_mhz`` and its ``u64_client``
fixture restores with ``restore_speed_defaults`` (#365): config PUTs on the
shared device.  The owner's #333 ruling is that config changes need
``U64_ALLOW_MUTATE``, but the #335 scanner in ``test_live_mutation_gate.py``
only globs ``test_*_live.py``, so this module was never scanned and was gated
on ``U64_HOST`` alone.

This runs that scanner's own classification on the module -- its helpers are
imported, not copied, so the rules stay the scanner's -- until the scanner's
glob covers non-``_live`` modules (tracked in the follow-up issue linked from
#367).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from test_live_mutation_gate import (
    GATE,
    config_writer_vocabulary,
    config_writes,
    gate_hint,
    gate_offence,
    gate_stores,
    ungated_tests_that_write,
)

MODULE = Path(__file__).parent / "test_bridge_ping_tod.py"


@pytest.fixture(scope="module")
def vocabulary() -> set[str]:
    return config_writer_vocabulary()


@pytest.fixture(scope="module")
def source() -> str:
    return MODULE.read_text()


def test_the_module_really_writes_device_config(source, vocabulary) -> None:
    """Non-vacuity: the gate checks below mean nothing if nothing writes."""
    writes = config_writes(ast.parse(source), vocabulary)
    assert {"set_turbo_mhz", "restore_speed_defaults"} <= writes, sorted(writes)


def test_the_module_is_gated_on_allow_mutate(source, vocabulary) -> None:
    offence = gate_offence(source, MODULE.name, vocabulary)
    assert not offence, (
        f"{MODULE.name} writes device config ({', '.join(sorted(offence))}) but "
        f"never skips when {GATE} is unset (#333, #268)." + gate_hint(ast.parse(source))
    )


def test_every_test_that_writes_config_is_gated_itself(source, vocabulary) -> None:
    tree = ast.parse(source)
    offenders = ungated_tests_that_write(tree, vocabulary)
    assert not offenders, (
        f"{MODULE.name}: not gated on {GATE}: " + "; ".join(offenders) + gate_hint(tree)
    )


def test_the_module_never_sets_the_gate_itself(source) -> None:
    assert not gate_stores(ast.parse(source))
