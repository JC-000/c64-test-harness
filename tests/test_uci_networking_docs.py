"""``docs/uci_networking.md`` states the ``enable_uci`` prerequisite honestly (#270).

The prerequisite -- ``enable_uci`` then ``client.reset()`` and a ~3 s
settle, or every routine times out at the sentinel -- is a recorded
observation from the live suites.  #270 proposed a cause for it:
``machine:reboot`` -> ``C64::start_cartridge(NULL)`` zeroes
``CMD_IF_SLOT_ENABLE``.  Firmware source does not support that as a cause.
At tag ``1.1.0`` (C64U) and ``7f6fcb51`` (U64E v3.15-85), ``start_cartridge``
zeroes the enable and then, when no external cartridge holds the bus,
calls ``set_cartridge(NULL)`` -> ``set_emulation_flags()``, which restores it
from config.  A REST config PUT reaches ``set_emulation_flags()`` as well,
through ``at_close_config`` -> ``effectuate`` -> ``effectuate_settings``.
So the observation stands and its cause is unexplained by source.  That is
what the doc must say (read from source, unmeasured; see #270 and #299).

Pinned three ways, the same shape as ``test_entry_baseline_docs.py``:

* the observation is still there (a docs change must not drop a
  requirement because its explanation fell through);
* the source trace is present, with its tags and the restoring call;
* the refuted cause is not restated as the reason.  Matched through a
  whitespace-folding normaliser, because prose wraps.

The absence pins are an enumeration plus one subject-scoped check (a
sentence that pairs the slot/REU enable with reboot and "disabled" and
does not also carry the restoring call).  A paraphrase that names none
of those can still get through; nothing here proves the doc is right.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
DOC = _REPO / "docs" / "uci_networking.md"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _prerequisite_section(text: str) -> str:
    """From the **Prerequisite:** paragraph to the next ``## `` heading."""
    start = text.index("**Prerequisite:**")
    end = text.index("\n## ", start)
    return text[start:end]


@pytest.fixture(scope="module")
def section() -> str:
    return _flat(_prerequisite_section(DOC.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def whole() -> str:
    return _flat(DOC.read_text(encoding="utf-8"))


#: The observation, which must survive any rewrite of its explanation.
OBSERVATION = (
    "client.reset()",
    "3 s settle",
    "times out at the sentinel",
)

#: The source trace.  Each entry names a step; tags are part of the claim.
SOURCE_TRACE = (
    "CMD_IF_SLOT_ENABLE",
    "set_emulation_flags()",
    "set_cartridge(NULL)",
    "ConfigureU64SystemBus()",
    "at_close_config",
    "`1.1.0`",
    "`7f6fcb51`",
    "c64.cc:326-327",
    "c64.cc:913",
    "c64.cc:923-924",
    "c64.cc:992",
)

#: The honesty clause: the requirement's cause is not established.
UNEXPLAINED = "not explained by firmware source"

#: Phrasings of the refuted cause, from #270's body and the ``reboot()``
#: docstring (fdd9f2a), which is where a reader would copy it from.
REFUTED_PHRASES = (
    "leaves the Command Interface slot disabled",
    "leaves the REU and the Command Interface slot disabled",
    "is the mechanism behind",
    "That is why enabling UCI and immediately driving it fails",
    "regardless of what the config items say",
    # The base doc's own causal gloss on the observation.  Source says the
    # config PUT applies the enable at once (at_close_config -> ... ->
    # set_emulation_flags), so "not live until reset" is an inference the
    # observation does not license.
    "registers do not go live until the next machine reset",
)


def _sentences(flat: str) -> list[str]:
    # Not on ':' -- house style introduces a claim with a colon, and
    # splitting there separates the subject from the claim (the same hole
    # test_entry_baseline_docs.py records).  Caught by the colon case in
    # TestThePinsCanFail on the first run.
    return re.split(r"(?<=[.;?!])\s+", flat)


def _restates_refuted_cause(flat: str) -> list[str]:
    """Sentences that say reboot leaves the enable disabled, unqualified."""
    hits = []
    for s in _sentences(flat):
        low = s.lower()
        if ("slot" in low or "cmd_if_slot_enable" in low or "reu" in low) \
                and ("reboot" in low or "start_cartridge" in low) \
                and "disabled" in low \
                and "set_cartridge" not in low and "external" not in low:
            hits.append(s)
    return hits


class TestTheObservationSurvives:
    @pytest.mark.parametrize("phrase", OBSERVATION)
    def test_present(self, section: str, phrase: str) -> None:
        assert phrase in section, f"prerequisite lost {phrase!r}"


class TestTheSourceTraceIsPresent:
    @pytest.mark.parametrize("phrase", SOURCE_TRACE)
    def test_present(self, section: str, phrase: str) -> None:
        assert phrase in section, f"source trace lost {phrase!r}"

    def test_says_the_cause_is_unexplained(self, section: str) -> None:
        assert UNEXPLAINED in section

    def test_links_the_docstring_contradiction(self, section: str) -> None:
        assert "#299" in section


class TestTheRefutedCauseIsNotTheReason:
    @pytest.mark.parametrize("phrase", REFUTED_PHRASES)
    def test_enumerated_phrase_absent(self, whole: str, phrase: str) -> None:
        assert phrase.lower() not in whole.lower(), phrase

    def test_no_sentence_restates_it(self, whole: str) -> None:
        assert not _restates_refuted_cause(whole)


class TestThePinsCanFail:
    """Vacuity guards: each absence check fires on a doc that relapses."""

    @pytest.mark.parametrize("phrase", REFUTED_PHRASES)
    def test_enumerated_phrase_is_detected(self, whole: str, phrase: str) -> None:
        relapsed = whole + " " + phrase + "."
        assert phrase.lower() in relapsed.lower()

    @pytest.mark.parametrize("claim", [
        "A reboot leaves the Command Interface slot disabled.",
        "machine:reboot runs start_cartridge, so the REU comes back disabled.",
        "After a reboot: CMD_IF_SLOT_ENABLE is disabled until reset.",
    ])
    def test_subject_scoped_check_fires(self, whole: str, claim: str) -> None:
        assert _restates_refuted_cause(whole + " " + claim)

    def test_subject_scoped_check_spares_the_qualified_statement(self) -> None:
        """Positive control: the exception, stated with its condition, passes."""
        ok = ("When an external cartridge holds the bus, start_cartridge skips "
              "set_cartridge and the slot stays disabled after a reboot.")
        assert not _restates_refuted_cause(ok)

    def test_normaliser_folds_a_wrapped_observation(self) -> None:
        """Prose reflows; a phrase split across lines must still be found.

        No pinned phrase in today's doc straddles a line break, so without
        this the normaliser could stop folding and every pin would still
        pass -- until the next reflow turned a present phrase into a
        false failure, or a relapsed one into a silent pass.
        """
        wrapped = "follow it with `client.reset()` and a 3 s\n  settle before"
        assert "3 s settle" in _flat(wrapped)
        assert "3 s settle" not in wrapped

    def test_wrapped_relapse_is_detected(self, whole: str) -> None:
        relapse = "\nleaves the Command Interface\n  slot disabled.\n"
        assert "leaves the command interface slot disabled" in _flat(
            whole + relapse).lower()
        assert _restates_refuted_cause(_flat(whole + " A reboot" + relapse))

    def test_section_extractor_is_bounded(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        sec = _prerequisite_section(text)
        assert sec.startswith("**Prerequisite:**")
        assert "## How the 6502 routine is dispatched" not in sec
