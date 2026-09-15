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
The observation was not reproduced on bce4535e (#270), and its cause is
unexplained by source (#422).  That is
what the doc must say (read from source, unmeasured; see #270 and #299).

Pinned three ways, the same shape as ``test_entry_baseline_docs.py``:

* the observation is still there (a docs change must not drop a
  requirement because its explanation fell through);
* the source trace is present, with its tags and the restoring call;
* the refuted cause is not restated as the reason.  Matched through a
  whitespace-folding normaliser, because prose wraps.

The absence pins are an enumeration plus two sentence-level checks, both
reached through :func:`_relapses`:

* *reboot leaves it disabled* -- a sentence naming the enable (slot,
  ``CMD_IF_SLOT_ENABLE``, REU, Command Interface, UCI), reboot or
  ``start_cartridge``, and ``disabled`` or ``off``, without naming either
  real condition (``set_cartridge``, external cartridge, ``prohibit``);
* *the PUT waits for a reset* -- "only applied/takes effect at the next
  reset", "not applied/live until reset", "does not go live until".

Review round 1 (PR #309) found three plain paraphrases that walked through
the first version (E1-E3 in :class:`TestThePinsCanFail`); both checks were
widened for them.  **What still gets through**, concretely:

* a state word outside the list: "comes back inactive", "is cleared",
  "reverts to Disabled" spelt without ``disabled``, "you have to switch it
  on again";
* reboot not named: "after the C64 restarts the interface is off";
* a deferral without those shapes: "the reset is what makes the PUT
  stick", "the setting needs a reset to land";
* any relapse that also mentions ``external``, ``prohibit`` or
  ``set_cartridge`` in the same sentence -- those words waive the check,
  so a wrong claim written next to the qualifier passes.

Nothing here proves the doc is right; it stops these specific regressions.
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
    # The second exception (review round 1): a configured .crt whose
    # prohibit mask includes UCI zeroes the enable after it is restored.
    "CFG_C64_CART_CRT",
    # Review round 2: the .crt read-and-load is :962-964 at 1.1.0 (:961 is
    # the #ifndef); round 1 had it one line early.
    "c64.cc:962-964",
    "c64.cc:1062-1068",
    "CART_PROHIBIT_DFXX",
    "c64.cc:1056-1061",
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


#: What the enable is called.  Review round 1 added the two plain names:
#: "the Command Interface" and "UCI" passed a subject list that only knew
#: the slot, the register and the REU.
_SUBJECT = re.compile(r"\bslot\b|cmd_if_slot_enable|\breu\b|command interface|\buci\b")
#: What reboot is called.
_REBOOT = re.compile(r"reboot|start_cartridge")
#: What "left disabled" is called.  "off" is matched as a word so that
#: "power-on", "offset" and the like do not count.
_DISABLED = re.compile(r"\bdisabled\b|\bdisables?\b|\boff\b")
#: The two conditions under which source really does leave the enable at 0.
#: A sentence naming either is the qualified statement, not the relapse.
_QUALIFIERS = ("set_cartridge", "external", "prohibit")


def _restates_refuted_cause(flat: str) -> list[str]:
    """Sentences that say reboot leaves the enable disabled, unqualified."""
    hits = []
    for s in _sentences(flat):
        low = s.lower()
        if _SUBJECT.search(low) and _REBOOT.search(low) and _DISABLED.search(low) \
                and not any(q in low for q in _QUALIFIERS):
            hits.append(s)
    return hits


#: "The PUT only takes effect at the next reset": the deferral shape.  By
#: source a changed item is effectuated when the PUT closes, so any sentence
#: saying the setting waits for a reset is the inference this doc removed.
_DEFERRAL = re.compile(
    r"only (?:be )?(?:applied|applies|takes? effect|goes? live|active)"
    r"(?: \w+)? (?:at|on|after|by) the next (?:machine )?reset"
    r"|not (?:be )?(?:applied|live|active|effective|take effect|go live)"
    r" until(?: the next)?(?: machine)? reset"
    r"|(?:do|does) not go live until"
    # Review round 2 (F2): "takes effect only after a reset".
    r"|takes? effect only (?:at|on|after|by)(?: the next| a)?(?: machine)? reset"
    # PR #409 review round 1 (R4): "A reset is needed" survived every shape
    # above.  "no reset was needed" is the measured claim, not the relapse.
    r"|(?<!\bno )(?<!\bno machine )\breset (?:is|was) (?:still |typically |always )?"
    r"(?:needed|required)"
)


def _claims_deferred_apply(flat: str) -> list[str]:
    return [s for s in _sentences(flat) if _DEFERRAL.search(s.lower())]


def _c64u_claimed_measured(flat: str) -> list[str]:
    """Sentences naming the C64U as measured, without a whole-word negation."""
    return [s for s in _sentences(flat)
            if "C64U" in s and re.search(r"\bmeasured\b", s)
            and not re.search(r"\bnot\b|\bunmeasured\b", s.lower())]


def _relapses(flat: str) -> list[str]:
    """Every sentence-level absence check, as one entry point."""
    return _restates_refuted_cause(flat) + _claims_deferred_apply(flat)


#: The #270 measurement (U64E bce4535e, 2026-09-15): the enable is live with
#: no reset.  The doc must carry it with its conditions, and must stop
#: telling readers the reset is required.
MEASUREMENT = (
    "bce4535e",
    "2026-09-15",
    "n=4 per arm",
    "`Cartridge Preference` `Auto`",
    "without `reset()`",
    "not reproduced",
    # Re-verify round 3: the build the measurement ran on.
    "fw 3.15, `git_commit_hash` bce4535e",
    # PR #409 review round 1 (R3, finding 3): arm A's +0 is the identifier
    # read; its routine ran at +3 s.  Each arm's count is pinned.
    "`$DF1D` read `$C9` at +0",
    "`uci_probe`, run at +3 s with no reset, completed 4/4",
    "with `client.reset()` + 3 s, 4/4",
    "with `reboot()` + 5 s, 4/4",
    "the 3 s settle is not retired",
    # R7: the live suites' reset is kept deliberately.
    "The reset in the live suites is harmless and stays.",
    # Re-verify round (A13): the C64U remains source-read.
    "on it this is still source-read",
    # Finding 6: the open cause has an issue.
    "not established (#422)",
    # Finding 5: what arms B and C measured, and what they did not.
    "The Command Interface half of that account is measured on the U64E",
    "The REU half, the External and `.crt` cases, and the C64U are unmeasured.",
)

#: The timeout-reset trace in the dispatch section keeps #332's evidence grade
#: (``tests/test_uci_dispatch_timeout_docs.py`` pins the phrase).  #270 did
#: not run the timeout path, so no #270 measurement may be written there.
_DISPATCH_HEADING = "## How the 6502 routine is dispatched"


#: Present-tense requirement wording the measurement retires.
RETIRED_REQUIREMENT = (
    "Keep that sequence",
    "Nobody has done that",
    "drop the reset on a device and see which step fails",
    "It is a recorded observation",
    # Re-verify round 3 (P1): the blanket grade must not return beside the
    # arm-A/B/C measurement; the dispatch line is scoped to the timeout path.
    "This has not been measured on a device.",
)


class TestTheMeasurementIsRecorded:
    @pytest.mark.parametrize("phrase", MEASUREMENT)
    def test_present(self, section: str, phrase: str) -> None:
        assert phrase in section, f"#270 measurement lost {phrase!r}"

    def test_dispatch_section_carries_no_270_measurement(self) -> None:
        """#409 serial suite: the timeout trace is not what #270 measured."""
        text = DOC.read_text(encoding="utf-8")
        start = text.index(_DISPATCH_HEADING)
        dispatch = _flat(text[start:text.index("\n## ", start + 1)])
        assert "#270" not in dispatch and "bce4535e" not in dispatch
        assert "not been measured on a device" in dispatch

    @pytest.mark.parametrize("phrase", RETIRED_REQUIREMENT)
    def test_retired_requirement_absent(self, whole: str, phrase: str) -> None:
        assert phrase not in whole, phrase

    def test_c64u_is_said_to_be_unmeasured(self, section: str) -> None:
        """Non-vacuous (PR #409 R1): the disclaimer itself must be there."""
        assert [s for s in _sentences(section)
                if "C64U" in s and re.search(r"\bnot measured\b", s)]

    def test_c64u_is_not_claimed_measured(self, section: str) -> None:
        assert not _c64u_claimed_measured(section), _c64u_claimed_measured(section)


class TestEnableUciDocstring:
    @pytest.fixture(scope="class")
    def doc(self) -> str:
        import inspect

        from c64_test_harness.uci_network import enable_uci

        return _flat(inspect.getdoc(enable_uci) or "")

    def test_does_not_say_a_reset_is_needed(self, doc: str) -> None:
        assert "typically needed" not in doc
        assert not _relapses(doc)

    def test_does_not_say_reboot_reverts_it(self, doc: str) -> None:
        assert "reboot reverts" not in doc

    def test_cites_the_measurement(self, doc: str) -> None:
        assert "bce4535e" in doc and "#270" in doc and "docs/uci_networking.md" in doc

    #: PR #409 review round 1 (R2, R6, R8, finding 3): each carries a
    #: condition or a count that a rewrite could drop silently.
    @pytest.mark.parametrize("phrase", (
        "The C64U is not measured.",
        "survives ``reboot()``",
        "completed 4/4",
        "``$DF1D`` read ``$C9`` at +0",
        "run at +3 s with no reset",
        "keep a settle",
        # Re-verify round (A12): the premise the settle rests on.
        "A routine at +0 was not run",
    ))
    def test_carries(self, doc: str, phrase: str) -> None:
        assert phrase in doc, phrase

    def test_no_reset_claim_carries_its_conditions(self, doc: str) -> None:
        """Finding 2: the claim and its scope sit in one sentence."""
        claims = [s for s in _sentences(doc)
                  if re.search(r"\bno reset (?:is|was) needed", s.lower())]
        assert len(claims) == 1, claims
        for cond in ("U64E", "bce4535e", "Auto"):
            assert cond in claims[0], (cond, claims[0])


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
        """Both sentence-level checks, against the doc itself.

        Review round 1 follow-up: this called only the reboot check, so the
        deferral check ran in the vacuity tests but never on the doc -- E3
        inserted into the doc passed.
        """
        assert not _relapses(whole)


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

    @pytest.mark.parametrize("claim", [
        # Review round 1 (PR #309): plain paraphrases that passed the pin.
        "After `client.reboot()` the UCI registers come back off until the next reset.",
        "A reboot turns the Command Interface off again, so re-enable it afterwards.",
        "The config PUT is only applied at the next reset, which is why the reset is needed.",
        # Review round 2 (PR #309): two more natural paraphrases.
        "A reboot disables the Command Interface again.",
        "The Command Interface PUT takes effect only after a reset.",
        # PR #409 review round 1 (R4).
        "A reset is needed for $DF1C-$DF1F to answer.",
        "A machine reset is still required after enable_uci.",
    ], ids=["E1-uci-come-back-off", "E2-command-interface-turned-off",
            "E3-deferred-apply", "F1-reboot-disables", "F2-takes-effect-only-after",
            "R4-reset-is-needed", "R4-reset-is-still-required"])
    def test_plain_paraphrase_is_detected(self, whole: str, claim: str) -> None:
        assert _relapses(whole + " " + claim), claim

    @pytest.mark.parametrize("benign", [
        "After a reboot the UCI register offset is still $DF1C.",
        "A reboot of the C64 does not change the Command Interface's offset table.",
    ])
    def test_off_is_matched_as_a_word(self, benign: str) -> None:
        """Positive control: 'offset' is not 'off'.

        A substring match would flag these; the pin promises a word match.
        """
        assert not _relapses(benign)

    @pytest.mark.parametrize("benign", [
        "On the U64E no reset was needed for $DF1C-$DF1F to answer.",
        "No machine reset is needed on that build.",
    ])
    def test_measured_no_reset_is_not_a_deferral(self, benign: str) -> None:
        """Positive control for R4's shape: the negated claim passes."""
        assert not _claims_deferred_apply(benign)

    @pytest.mark.parametrize("claim", [
        "The C64U was measured too.",
        "The C64U was measured too, and on it this is still source-read.",
        "The C64U was measured nothing like the U64E.",
    ])
    def test_c64u_measured_claim_is_detected(self, claim: str) -> None:
        """R1: whole-word negation, and 'source-read' does not waive it."""
        assert _c64u_claimed_measured(claim), claim

    def test_qualified_external_cart_exception_passes(self) -> None:
        """E4: the exception, stated with its condition, is not a relapse."""
        ok = ("With Cartridge Preference External and a cartridge holding the "
              "bus, set_cartridge is skipped and the slot stays at 0.")
        assert not _relapses(ok)

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
