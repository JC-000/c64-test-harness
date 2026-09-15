"""The dispatch section of ``docs/uci_networking.md`` states the timeout path (#332).

Since #332, ``_execute_uci_routine`` does not just raise on a timeout: it
resets the 6510 with ``transport.reset(scope="cpu")``, sleeps
``_TIMEOUT_RESET_SETTLE`` and only then raises ``TimeoutError``.  The doc's
"How the 6502 routine is dispatched" section is the host-sequence reference,
so a reader who only has it must learn that a timeout costs a reset.

Pinned against the code's own constant, so a settle change that forgets the
doc fails here.  This checks the section mentions the step; it cannot check
the prose is right.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from c64_test_harness import uci_network as u

DOC = Path(__file__).resolve().parent.parent / "docs" / "uci_networking.md"


@pytest.fixture(scope="module")
def section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## How the 6502 routine is dispatched")
    end = text.index("\n## ", start + 1)
    return " ".join(text[start:end].split())


REQUIRED = [
    'transport.reset(scope="cpu")',
    # The full endpoint: the bare word also appears in the source trace, so
    # it would survive the endpoint being dropped from the step itself.
    "`PUT /v1/machine:reset`",
    "TimeoutError",
    # The claim, not the word: "costs one `/Temp` attachment" is a
    # hardware-safety statement and must not pass (review round 1, N4).
    "no `/Temp` attachment",
    # The citations with their lines, not the bare refs: the side-effects
    # bullet also names 7f6fcb51, so a bare pin survives the source trace
    # losing its citation.
    "tag `1.1.0` (`c64.cc:593-601`",
    "at `7f6fcb51` (`c64.cc:612-620`",
    # The evidence grade of the source trace (review round 1, N7).
    "not been measured on a device",
]


@pytest.mark.parametrize("phrase", REQUIRED)
def test_section_names_the_timeout_step(section: str, phrase: str) -> None:
    assert phrase in section, f"dispatch section lost {phrase!r}"


def test_section_states_the_settle_the_code_uses(section: str) -> None:
    # Bound to the constant's name: a bare "3 s" also appears in the
    # "borrowed" sentence and would survive a wrong figure in the step.
    settle = u._TIMEOUT_RESET_SETTLE
    shown = f"`_TIMEOUT_RESET_SETTLE` ({settle:g} s)"
    assert shown in section, f"settle {shown!r} not stated"


def test_settle_is_not_presented_as_measured(section: str) -> None:
    assert re.search(r"borrowed|not measured|unmeasured", section)


def test_does_not_claim_the_reset_clears_a_state_bit_wedge(section: str) -> None:
    """``machine:reset`` does not clear a UCI STATE-bit wedge (#112)."""
    assert "#112" in section
    sentences = re.split(r"(?<=[.;])\s+", section)
    # Exempt only a negation that governs the verb: any "not" elsewhere in
    # the sentence let "clears ... which is not otherwise possible" through
    # (review round 1, N1).
    negated_verb = re.compile(
        r"\b(?:does not|doesn't|do not|don't|will not|won't|never|cannot"
        r"|can't|can not)\s+clear\b"
    )
    affirmative = [
        s for s in sentences
        if "STATE" in s and re.search(r"\bclears?\b", s)
        and not negated_verb.search(s)
    ]
    assert not affirmative, affirmative
