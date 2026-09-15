"""Docs pin: pytest runners lock per test, not per run, and say why (#324).

``docs/device_locking.md`` rule 1 says to hold the lock for the whole run.
The pytest runner scripts deliberately do not: ``tests/conftest.py``'s autouse
``device_lock_guard`` locks each live test, because an outer hold would defer
the lock-release ``/Temp`` drain -- the only one that catches a client a test
never closes, and it fires only on the outermost release -- to the end of the
run, and a client collected before then would never drain (``close()`` still
drains a leaking client).  Owner decision on #324 (2026-09-15): keep per-test
locking and document it plainly.

These tests fail if the exception disappears from any place that states the
rule, or if a whole-run restatement appears somewhere without it.  The
behaviour itself is pinned by
``tests/test_u64_runner_script_gates.py::test_the_pytest_runners_are_locked_by_conftest``.

The whole-run scan reads ``README.md``, ``docs/*.md`` and the skills' ``*.md``
(Python docstrings are pinned by the per-runner test instead).  It matches
"hold/held/keep ... lock ... the whole/entire run", the same with "for the
duration of / across / throughout the run", and "whole-run hold/lock".
**What still gets through:** a statement that does not name the lock within
three words of the verb and of the run phrase ("hold the device for the whole
run", "never release the lock between tests"), and any restatement followed
within ``_WINDOW`` characters by an unrelated ``#324`` mention.
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
        # Review round 1 (PR #406, finding 1): only the lock-release drain
        # is deferred; close() is not gated on the lock.
        "`close()` still drains a leaking client",
        "catches a client the test never closes",
        "held weakly",
        "never drain at all",
    ):
        assert needle in rule, needle
    for retired in ("into one drain", "every per-test"):
        assert retired not in rule, retired


@pytest.mark.parametrize("runner", _RUNNERS)
def test_each_pytest_runner_docstring_says_it_locks_per_test_and_why(runner):
    doc = _flat(ast.get_docstring(ast.parse((_ROOT / runner).read_text())) or "")
    assert "Locking is per test, not per run" in doc, runner
    assert "#324" in doc and "outermost release" in doc, runner
    assert "docs/device_locking.md" in doc, runner
    # Review round 1 (PR #406, finding 1).
    assert "``close()`` still drains a leaking client" in doc, runner
    assert "a client a test never closes" in doc, runner
    assert "would never drain" in doc, runner
    assert "every per-test drain" not in doc, runner


def test_the_skill_states_the_pytest_exception():
    text = _flat((_ROOT / ".claude" / "skills" / "c64-test" / "SKILL.md").read_text())
    assert "Pytest runs are the exception:" in text
    assert "#324" in text
    # Review round 1 (PR #406, finding 1).
    assert "`close()` still drains a leaking client" in text
    assert "a client a test never closes" in text
    assert "every per-test `/Temp` drain" not in text


#: A statement that the device lock should be held for a whole run.
#: Review round 1 (PR #406, finding 3) added "held"/"keep", "for the duration
#: of / across / throughout the run" and "whole-run hold/lock".
_WHOLE_RUN = re.compile(
    r"(?:hold\w*|held|keep\w*)\W+(?:\w+\W+){0,3}?(?:`?DeviceLock`?|lock)\W+"
    r"(?:\w+\W+){0,3}?"
    r"(?:(?:the\s+)?(?:whole|entire)\s+run"
    r"|(?:for\s+the\s+duration\s+of|across|throughout)\s+the\s+run)"
    r"|whole-run\s+(?:hold|lock)",
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


def _offenders(read=Path.read_text) -> dict[str, list[str]]:
    return {
        p.relative_to(_ROOT).as_posix(): hits
        for p in _prose_files()
        if (hits := _unqualified_whole_run_claims(read(p)))
    }


def test_no_whole_run_restatement_without_the_exception():
    assert _offenders() == {}


#: Review round 1 (PR #406, finding 2): the scan's reach must not shrink
#: silently.  There are 10 ``docs/*.md`` files at d446089.
_DOCS_FLOOR = 8


def test_the_scan_reads_the_readme_the_docs_directory_and_the_skill():
    rel = {p.relative_to(_ROOT).as_posix() for p in _prose_files()}
    assert "README.md" in rel
    docs = sorted(r for r in rel if r.startswith("docs/") and r.count("/") == 1)
    assert len(docs) >= _DOCS_FLOOR, docs
    assert ".claude/skills/c64-test/SKILL.md" in rel


def test_a_claim_planted_in_real_readme_and_docs_text_is_flagged():
    """Positive control through the real scan path, file set included."""
    targets = {_ROOT / "README.md", _ROOT / "docs" / "development.md"}

    def read(p: Path) -> str:
        text = p.read_text()
        return text + "\n\nHold the lock for the whole run.\n" if p in targets else text

    assert set(_offenders(read)) == {"README.md", "docs/development.md"}


@pytest.mark.parametrize("gap, flagged", [
    (1496, False),  # "#324" ends exactly at the window edge
    (1497, True),   # one character past it
    (5000, True),
])
def test_the_exception_must_fall_inside_the_window(gap, flagged):
    """Literal gaps, so a mutated ``_WINDOW`` cannot move the fixture with it."""
    claim = "Hold the lock for the whole run"
    text = claim + "." + "x" * (gap - 1) + "#324"
    assert bool(_unqualified_whole_run_claims(text)) is flagged


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
    # Review round 1 (PR #406, finding 3): paraphrases.
    ("hold the lock for the duration of the run", True),
    ("keep the `DeviceLock` held across the run", True),
    ("holding the device lock throughout the run", True),
    ("the runner takes a whole-run hold on the device", True),
    ("does **not** give whole-run exclusion", False),
    ("An outer hold would defer the drain to the end of the run.", False),
])
def test_the_scan_itself(line, flagged):
    assert bool(_unqualified_whole_run_claims(line)) is flagged
