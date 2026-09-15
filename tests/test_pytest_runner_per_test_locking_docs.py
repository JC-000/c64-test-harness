"""Docs pin: pytest runners lock per test, not per run, and say why (#324).

``docs/device_locking.md`` rule 1 says to hold the lock for the whole run.
The pytest runner scripts deliberately do not: ``tests/conftest.py``'s autouse
``device_lock_guard`` locks each live test, because an outer whole-run hold
would defer every per-test ``/Temp`` drain (a lock-release callback that fires
only on the outermost release) to the end of the run.  Owner decision on #324
(2026-09-15): keep per-test locking and document it plainly.

These tests fail if the exception disappears from any place that states the
rule, or if a whole-run restatement appears somewhere without it.  The
behaviour itself is pinned by
``tests/test_u64_runner_script_gates.py::test_the_pytest_runners_are_locked_by_conftest``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_RUNNERS = [
    "scripts/run_all_u64_live.py",
    "scripts/run_sid_u64_live.py",
    "scripts/run_u64_parallel_locked.py",
]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_device_locking_rule_1_carries_the_pytest_exception_and_its_reason():
    text = _flat((_ROOT / "docs" / "device_locking.md").read_text())
    start = text.index("1. **Hold the lock for the whole run, not per call.**")
    end = text.index("2. **`run_prg` replaces the running program.**")
    rule = text[start:end]
    for needle in (
        "pytest runs lock per test, not per run",
        "does **not** give whole-run exclusion",
        "`hold_device_lock(host)`",
        "**outermost** release",
        "issue #324",
    ):
        assert needle in rule, needle


@pytest.mark.parametrize("runner", _RUNNERS)
def test_each_pytest_runner_docstring_says_it_locks_per_test_and_why(runner):
    doc = _flat(ast.get_docstring(ast.parse((_ROOT / runner).read_text())) or "")
    assert "Locking is per test, not per run" in doc, runner
    assert "#324" in doc and "outermost release" in doc, runner
    assert "docs/device_locking.md" in doc, runner


def test_the_skill_states_the_pytest_exception():
    text = _flat((_ROOT / ".claude" / "skills" / "c64-test" / "SKILL.md").read_text())
    assert "Pytest runs are the exception:" in text
    assert "#324" in text


#: A statement that the device lock should be held for a whole run.
_WHOLE_RUN = re.compile(
    r"hold\w*\W+(?:\w+\W+){0,3}?(?:`?DeviceLock`?|lock)\W+(?:\w+\W+){0,3}?"
    r"(?:the\s+)?(?:whole|entire)\s+run",
    re.IGNORECASE,
)
#: How far after such a statement the #324 exception must appear.
_WINDOW = 1500


def _unqualified_whole_run_claims(text: str) -> list[str]:
    flat = _flat(text)
    return [
        flat[m.start():m.end()]
        for m in _WHOLE_RUN.finditer(flat)
        if "#324" not in flat[m.start():m.end() + _WINDOW]
    ]


def _prose_files() -> list[Path]:
    files = [_ROOT / "README.md", *sorted((_ROOT / "docs").glob("*.md"))]
    files += sorted((_ROOT / ".claude" / "skills").rglob("*.md"))
    return [p for p in files if p.is_file()]


def test_no_whole_run_restatement_without_the_exception():
    offenders = {
        str(p.relative_to(_ROOT)): hits
        for p in _prose_files()
        if (hits := _unqualified_whole_run_claims(p.read_text()))
    }
    assert offenders == {}


def test_the_scan_sees_the_real_statements():
    """Vacuity guard: rule 1 and the skill bullet are both matched."""
    found = {
        str(p.relative_to(_ROOT))
        for p in _prose_files()
        if _WHOLE_RUN.search(_flat(p.read_text()))
    }
    assert {"docs/device_locking.md", ".claude/skills/c64-test/SKILL.md"} <= found, found


@pytest.mark.parametrize("line, flagged", [
    ("**Hold the lock for the whole run, not per call.**", True),
    ("Hold the `DeviceLock` across the whole run, hygiene included", True),
    ("holds the device lock for the entire run", True),
    ("Hold the lock for the whole run. Pytest runs lock per test (#324).", False),
    ("`capture_sid_u64()` owns the whole run", False),
])
def test_the_scan_itself(line, flagged):
    assert bool(_unqualified_whole_run_claims(line)) is flagged
