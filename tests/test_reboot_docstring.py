"""``Ultimate64Client.reboot``'s docstring matches ``docs/uci_networking.md`` (#299).

fdd9f2a's docstring said a reboot "leaves the REU and the Command Interface
slot **disabled**", as the reason ``enable_uci`` needs a reset and a settle.
Firmware source contradicts that on the normal path at tags ``1.1.0`` and
``7f6fcb51``: ``start_cartridge`` zeroes the enables, then -- unless an
external cartridge holds the bus -- calls ``set_cartridge(NULL)``, whose
``set_emulation_flags()`` restores them from config; the configured ``.crt``
can still zero them again through its ``prohibit`` mask.  ``#309`` corrected
the doc; this pins the docstring to the same account (read from source,
unmeasured).

The absence check is the doc pin's own ``_relapses`` (imported, so the two
cannot drift apart), and it is shown to fire on the exact paragraph master
carried.  #270 has since measured the Command Interface half on the U64E
(bce4535e); the REU half, the External/``.crt`` cases and the C64U are
still read from source.  What gets through it is listed in that module's docstring.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from c64_test_harness.backends.ultimate64_client import Ultimate64Client

from test_uci_networking_docs import _relapses

_DOC = Path(__file__).resolve().parent.parent / "docs" / "uci_networking.md"

#: The paragraph master carried (fdd9f2a), verbatim modulo line wrapping.
_MASTER_PARAGRAPH = (
    "What it *does* clear, which the \"survives\" list above can make "
    "easy to miss: ``start_cartridge`` zeroes ``C64_CARTRIDGE_TYPE``, "
    "``C64_REU_ENABLE``, ``C64_SAMPLER_ENABLE`` and "
    "``CMD_IF_SLOT_ENABLE`` (``c64.cc:852+``). So a reboot leaves the "
    "REU and the Command Interface slot **disabled** — which is why "
    "``enable_uci`` needs a ``reset()`` and a settle afterwards rather "
    "than working straight away."
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def doc() -> str:
    return _flat(inspect.getdoc(Ultimate64Client.reboot) or "")


# --------------------------------------------------------------------------- #
# The refuted claim is gone                                                   #
# --------------------------------------------------------------------------- #

def test_no_sentence_restates_the_unconditional_claim(doc):
    assert _relapses(doc) == []


def test_the_check_fires_on_masters_paragraph():
    """Vacuity guard: the absence check catches the text this PR removes."""
    assert _relapses(_flat(_MASTER_PARAGRAPH))


def test_the_refuted_cause_is_not_given_for_enable_uci(doc):
    assert "which is why ``enable_uci`` needs" not in doc
    assert "rather than working straight away" not in doc


def test_the_stale_line_reference_is_gone(doc):
    """``c64.cc:852+`` matches a later checkout, not tag 1.1.0 (#299)."""
    assert "c64.cc:852" not in doc


# --------------------------------------------------------------------------- #
# The source account is present and agrees with the doc                        #
# --------------------------------------------------------------------------- #

#: Phrases the corrected paragraph must carry.  Each is also required to
#: appear in docs/uci_networking.md, so the docstring cannot cite a trace the
#: doc does not.
SOURCE_TRACE = (
    "set_cartridge(NULL)",
    "set_emulation_flags()",
    "c64.cc:913",
    "c64.cc:923-924",
    "c64.cc:992",
    "c64.cc:1062-1068",
    "c64.cc:1056-1061",
    "7f6fcb51",
)


@pytest.mark.parametrize("phrase", SOURCE_TRACE)
def test_source_trace_present_in_docstring(doc, phrase):
    assert phrase in doc, phrase


@pytest.mark.parametrize("phrase", SOURCE_TRACE)
def test_source_trace_agrees_with_the_doc(phrase):
    assert phrase in _flat(_DOC.read_text()), phrase


def _exception_bullets(raw: str) -> list[str]:
    """The bullet items that follow "except in two cases", flattened.

    Scoped to the list, not the whole docstring: the qualifier words also
    appear in the sentence that introduces ``set_cartridge`` and in the
    bullet headings, so a whole-docstring substring check let a mutation
    that dropped a bullet's actual condition survive (R3/R4 in the #299
    mutation run).
    """
    _, sep, tail = raw.partition("except in")
    assert sep, "the exceptions sentence is missing"
    bullets: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("* "):
            bullets.append(stripped[2:])
        elif bullets and stripped and line.startswith("  "):
            bullets[-1] += " " + stripped
        elif bullets and not stripped:
            break
    return [_flat(b) for b in bullets]


@pytest.fixture(scope="module")
def raw() -> str:
    return inspect.getdoc(Ultimate64Client.reboot) or ""


def test_there_are_exactly_two_exceptions(raw):
    assert len(_exception_bullets(raw)) == 2, _exception_bullets(raw)


def test_one_exception_is_an_external_cartridge_on_the_bus(raw):
    hits = [b for b in _exception_bullets(raw)
            if "external cartridge" in b.lower() and "set_cartridge" in b]
    assert len(hits) == 1, _exception_bullets(raw)


def test_one_exception_is_the_crt_prohibit_mask(raw):
    hits = [b for b in _exception_bullets(raw)
            if "``prohibit``" in b and ".crt" in b]
    assert len(hits) == 1, _exception_bullets(raw)


@pytest.mark.parametrize("mangled, expected", [
    ("* **A cartridge holds the bus**: ``set_cartridge`` is skipped.", 0),
    ("* **An external cartridge holds the bus**: ``set_cartridge`` is skipped.", 1),
])
def test_bullet_scoping_can_fail(mangled, expected):
    """Vacuity guard: a bullet without its condition is not counted, even
    when the qualifier words appear elsewhere in the text."""
    text = (
        "when no external cartridge holds the bus, calls set_cartridge, "
        "except in two cases:\n\n" + mangled + "\n* **The .crt prohibits them.** x\n"
    )
    hits = [b for b in _exception_bullets(text)
            if "external cartridge" in b.lower() and "set_cartridge" in b]
    assert len(hits) == expected


def test_says_restored_from_config(doc):
    assert "restored from config" in doc or "restores them from config" in doc


def test_marks_it_unmeasured_and_links_the_doc(doc):
    assert "unmeasured" in doc.lower()
    assert "docs/uci_networking.md" in doc
    assert "#299" in doc


def test_the_enable_uci_requirement_is_not_restated_as_standing(doc):
    """#270 measured the enable live without a reset on the U64E (bce4535e)."""
    assert "still stands" not in doc
    assert "bce4535e" in doc and "#270" in doc


@pytest.mark.parametrize("phrase", (
    # PR #409 review round 1 (R5): the result, not just its citation.
    "was not reproduced on the U64E",
    "4/4 per arm",
    "run at +3 s",
    # Finding 5: what is measured, and what still is not.
    "The Command Interface half is measured on the U64E",
    "the REU half, the External and ``.crt`` cases and the C64U are **unmeasured**",
    # Finding 6.
    "is open (#422)",
))
def test_the_measurement_is_stated_with_its_scope(doc, phrase):
    assert phrase in doc, phrase


def test_the_enable_uci_observation_is_not_dropped(doc):
    """Removing the wrong cause must not remove the recorded observation."""
    assert "enable_uci" in doc and "reset()" in doc


# --------------------------------------------------------------------------- #
# Positive control for the fixture                                            #
# --------------------------------------------------------------------------- #

def test_the_docstring_fixture_reads_the_real_method(doc):
    assert doc.startswith("PUT /v1/machine:reboot")
    assert "Not a firmware reboot" in doc
