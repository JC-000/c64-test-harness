"""The docs say what the entry reset actually defaults to (#285, closing #266).

PR #285 made the U64 entry baseline resolve **per device generation at
acquire time** instead of being globally opt-in, and shipped with no
documentation change at all.  Every doc and skill file went on describing
the old behaviour, and the ``c64-test`` skill is what an agent reads
*before* authoring a test — so the stale prose was not merely out of date,
it was actively teaching a false default.

Prose drifts back.  A sentence is only pinned if a test fails when it
returns, so each claim site is pinned three ways:

* the corrected claim must be present, verbatim, in every file that states
  the default at all;
* each *enumerated* phrasing of the superseded claim must be absent from
  all of them; and
* no sentence anywhere in these files may pair the entry reset with
  "opt-in"/"off by default", or claim the C64U defaults on.

The second bullet is what catches an appended correction: a presence-only
pin passes against a file that says both things at once, and this repo's
standing lesson is that an appended correction competes with the claim
above it and loses on position.

**What this file does not promise.**  The enumerated list is a
*regression* pin — one entry per phrasing this lane actually deleted — and
an enumeration can never be exhaustive.  Two mutations proved it: an
adversarial reviewer appended "The entry baseline is opt-in and off by
default: set U64_BASELINE_ON_ENTRY=1 to enable it." to ``SKILL.md`` and
"On the C64U the entry reset defaults on." to ``README.md``, and both
walked through a set that had already caught six other mutations.  The
third bullet exists for that class and is written against the *subject*
rather than against remembered wording.

It is still a heuristic over prose, and the residual is worth naming
precisely, because the first cut of it got this wrong.  Two of the three
holes found in it were **mechanical, not novel**: a claim introduced by a
colon (the sentence splitter cut between the subject and the claim), and a
claim in the second clause of a sentence whose first clause contained a
negation (the waiver applied to the whole sentence).  Both are house
style here, both are now cases in
:class:`TestTheSubjectScopedPinsCanFail`, and both would have been found
by probing rather than by reading.  Assume the next hole is of that kind
too.

One known relapse form is **still uncaught**, deliberately: a paraphrase
that names neither the switch nor the shape, such as "The baseline reset
must be requested; nothing happens unless you ask."  Catching it needs
either a much wider subject list, which fires on unrelated prose, or
semantics these pins do not have.  Nothing here proves the docs are
correct — only that these specific regressions cannot return silently.

The claim itself is derived from
:data:`~c64_test_harness.backends.ultimate64_baseline.BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION`
rather than restated here, so flipping a generation's default in the code
fails this file instead of silently un-documenting itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from c64_test_harness.backends.ultimate64_baseline import (
    BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION,
    baseline_default_for_generation,
)

_REPO = Path(__file__).resolve().parent.parent

#: Every file in this repo that states, or is read as stating, what a lane
#: gets when nobody set the switch.  The skill files are first on purpose:
#: an agent reads them before writing a test.
LANE_DOCS: dict[str, Path] = {
    "README.md": _REPO / "README.md",
    "docs/development.md": _REPO / "docs" / "development.md",
    "skill/SKILL.md": _REPO / ".claude" / "skills" / "c64-test" / "SKILL.md",
    "skill/PATTERNS.md": _REPO / ".claude" / "skills" / "c64-test" / "PATTERNS.md",
    "skill/REFERENCE.md": _REPO / ".claude" / "skills" / "c64-test" / "REFERENCE.md",
}

#: The corrected claim, as one contiguous phrase.  Pinned as a sentence and
#: not as a bag of tokens: "the U64E" and "the C64U" both appear in prose
#: that makes no claim about the default at all, and a token pair would
#: pass against it.
DEFAULT_CLAIM = "on for the U64E, off for the C64U, off for an unreadable generation"

#: Phrasings of the superseded opt-in claim.  Every entry had a real
#: referent that this lane deleted; none is a straw man, and none should be
#: removed without re-establishing that first.
#:
#: **Provenance, with its scope** — a claim without one is this repo's
#: recurring bug, and it has already cost this lane two round trips.  Each
#: phrase was live text in the **working tree this lane started from**: the
#: uncommitted draft sitting on top of commit ``5483ef7``, *not* that
#: commit's own content.  The distinction is measurable and was measured —
#: this file fails 18 times against a clean checkout of ``5483ef7`` and
#: failed 25 times against that working tree, and the gap is exactly the
#: phrases that existed only in the draft.  So a reader who checks out the
#: parent commit and finds nothing will conclude these pins are dead.  They
#: are not; they are looking at the wrong tree.
#:
#: **And do not check with a plain grep.**  These are matched *through*
#: :func:`_flat`, which folds whitespace, so a phrase that straddles a line
#: break in its source file is contiguous to the pin and invisible to
#: ``grep``.  At least one is: ``that store has never been read`` was
#: wrapped mid-phrase in the draft ``docs/development.md`` ("…over WiFi and
#: that\n  store has never been read…") and returns zero raw hits there,
#: which is precisely how it was once mistaken for a pin with no referent.
#: Verify with the normaliser or not at all.
#:
#: Each phrase is also distinctive enough to name exactly one referent —
#: bare "opt-in" and bare "off by default" are not, because both describe
#: unrelated things in these files (the ``turbo_safe`` keyword, the
#: ``MemoryPolicy``, FTP File Service on 1.1.0).
STALE_PHRASES = (
    "Known state on entry (opt-in",
    "Opt-in until two measurements land",
    "Default is off, and off means no requests",
    "unset/off means no requests",
    "Unset everywhere means off",
    "Why it is off",
    "lanes; it is off by default",
    "(default) asks the environment",
    "the manager asks the environment",
    "Opt in and the manager",
)

#: Retracted readings that lived in the same paragraphs.  Both are refuted
#: in ``BASELINE_NEVER_TOUCH`` itself: the device is already on DHCP and
#: those static fields are unused factory defaults (measured U64E
#: 2026-09-10), and the WiFi store's reason is now owner testimony, not an
#: absence of information.
RETRACTED_PHRASES = (
    "flips a static-addressed device to DHCP",
    "a static-addressed device flips to DHCP",
    "has never been read, so a reset there is unassessed",
    "that store has never been read",
    "the WiFi store (unread on the C64U)",
)


def _flat(text: str) -> str:
    """Markdown emphasis and line wrapping removed, nothing else.

    The pins are verbatim phrases, so they have to survive a paragraph
    being rewrapped or a word being bolded — and must survive *nothing*
    else.  Case, hyphens, commas and word order are all semantic here
    (``not the conservative one`` is one negation away from inverting a
    safety claim), so this deliberately does not fold any of them.
    """
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", ""))


def _doc(name: str) -> str:
    return _flat(LANE_DOCS[name].read_text(encoding="utf-8"))


def _blocks(name: str) -> list[str]:
    """The file as flattened prose blocks — paragraphs, bullets, headings.

    Flattening the *whole* file first, as :func:`_doc` does, is right for a
    verbatim phrase pin and wrong for anything that reasons about a
    sentence: it welds a heading to the paragraph under it and a paragraph
    to the list under that, so an "opt-in" in one and a subject term three
    items later land in the same "sentence".  That is not hypothetical —
    it was the first thing the subject-scoped pins below did.

    A block ends at a blank line and at every heading, list item,
    blockquote and table row, so a hard-wrapped paragraph stays whole while
    a list stays separate.

    One thing it does **not** do, stated because the obvious reading of the
    line above is stronger than the code: a heading *starts* a block but
    does not close the following line into its own, so
    ``"## Heading\nText."`` comes back welded as one block.  In these five
    files every heading is followed by a blank line, so it never arises;
    and the error widens a block rather than narrowing it, which costs a
    false positive rather than hiding a relapse.  Pinned either way by
    :class:`TestTheSubjectScopedPinsCanFail`.
    """
    out: list[str] = []
    cur: list[str] = []
    for line in LANE_DOCS[name].read_text(encoding="utf-8").splitlines():
        starts_block = line.lstrip().startswith(("#", "-", "*", ">", "|", "1."))
        if not line.strip() or starts_block:
            if cur:
                out.append(_flat(" ".join(cur)))
                cur = []
            if line.strip():
                cur = [line]
            continue
        cur.append(line)
    if cur:
        out.append(_flat(" ".join(cur)))
    return [b for b in out if b.strip()]


# --------------------------------------------------------------------------- #
# The claim, and the claim it replaced                                        #
# --------------------------------------------------------------------------- #

class TestTheDefaultIsDocumented:
    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    def test_every_lane_doc_states_the_per_generation_default(self, name: str) -> None:
        assert DEFAULT_CLAIM in _doc(name), (
            f"{name}: does not state the resolved default verbatim "
            f"({DEFAULT_CLAIM!r}). Since #285 the entry reset is NOT opt-in; a "
            f"file that describes the switch without saying so teaches the "
            f"wrong default to whoever reads it next."
        )

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    @pytest.mark.parametrize("phrase", STALE_PHRASES)
    def test_no_lane_doc_still_describes_it_as_opt_in(
        self, name: str, phrase: str
    ) -> None:
        assert phrase not in _doc(name), (
            f"{name}: still carries the pre-#285 opt-in claim {phrase!r}. "
            f"Correct by deleting the superseded sentence, not by appending a "
            f"note beside it."
        )

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    @pytest.mark.parametrize("phrase", RETRACTED_PHRASES)
    def test_no_lane_doc_repeats_a_retracted_never_touch_reason(
        self, name: str, phrase: str
    ) -> None:
        assert phrase not in _doc(name), (
            f"{name}: repeats {phrase!r}, a reading BASELINE_NEVER_TOUCH itself "
            f"records as retracted."
        )

    # -- the open-ended half: written against the subject, not the wording --

    #: Ways of naming the thing whose default this lane is about.  A
    #: sentence has to be *about* the entry reset before the pins below
    #: care what it says — "opt-in" describes several unrelated things in
    #: these files (the ``turbo_safe`` keyword, ``MemoryPolicy``, the live
    #: suites, FTP File Service on 1.1.0) and flagging those would make the
    #: guard useless within a week.
    _SUBJECT = (
        "entry baseline", "entry reset", "baseline_on_entry",
        "U64_BASELINE_ON_ENTRY", "apply_factory_baseline",
        "reset-on-entry", "baseline on entry",
    )
    #: The superseded shape, by meaning rather than by remembered wording.
    _OPT_IN = (
        "opt-in", "opt in", "off by default", "defaults off",
        "default is off", "disabled by default", "turn it on",
    )
    #: Cues that negate an opt-in phrase.  Saying the reset is *not*
    #: opt-in is the corrected claim, not a relapse, and several such
    #: sentences are load-bearing — but the waiver is applied **per match**,
    #: within :data:`_NEGATION_WINDOW` characters before the phrase, never
    #: to the whole sentence.  Sentence-wide was a hole: "The entry reset is
    #: not a flag you turn on for VICE, but on the U64 it is off by default"
    #: waived itself on its first clause and smuggled the relapse in on its
    #: second.
    #:
    #: The cues are matched on **word boundaries**.  Plain substring
    #: matching waived "Note the entry reset is opt-in." on the "not"
    #: inside "Note", and would do the same for "nor" inside "minor",
    #: "ignore" or "norm".  ``n't`` is deliberately not boundary-anchored —
    #: it is a suffix.  The boundary is evaluated against the **whole**
    #: sentence, not against a slice of it: see :meth:`_unnegated_opt_in`,
    #: where slicing reopened this same trap at the window edge.
    _NEGATED = ("not", "n't", "no longer", "never", "nor")

    #: How far back to look for a negation cue.  Wide enough for "is not
    #: opt-in" and "do not opt in", narrow enough that a negation in a
    #: previous clause does not reach the phrase.
    _NEGATION_WINDOW = 24
    #: A claim that the C64U — the one generation that defaults off, for a
    #: device-loss reason — defaults on.  Directly contradicts DEFAULT_CLAIM
    #: rather than merely predating it, so it can sit beside the corrected
    #: sentence and be caught by nothing else here.
    _CBM = ("C64U", "C64 Ultimate", "cbm", "CBM line")
    _DEFAULTS_ON = ("defaults on", "default on", "on by default", "defaults to on")

    @staticmethod
    def _sentences(name: str) -> list[str]:
        """Every sentence of every block — the unit a claim is made in.

        Splits on ``.!?`` only.  An earlier version split on ``:`` too and
        that was a hole, not a refinement: colon-introduced phrasing is the
        house style here ("Env ``U64_BASELINE_ON_ENTRY``: off by default"),
        and splitting there puts the subject in one fragment and the claim
        in the next, so neither fragment is both about the entry reset and
        wrong about it.

        **That class is narrowed, not closed**, and the remaining seams are
        the same shape arriving from the other side: a claim whose subject
        is a pronoun in the *previous* sentence ("The entry reset works
        well.  It is off by default.") and an abbreviation whose full stop
        splits the subject from the claim ("The entry reset (e.g. on the
        U64E) is off by default.", ", i.e. the #227 step,").  Both survive
        and are left uncaught deliberately: carrying a subject across a
        sentence boundary needs pronoun resolution, and the false-positive
        cost of that is worse than the seam.  Version strings are *not* a
        seam — ``1.1.0``, ``v3.15-85`` and ``routes.cc:40-46`` all stay
        whole, because the split needs ``[.!?]`` followed by whitespace.
        """
        return [
            sent
            for block in _blocks(name)
            for sent in re.split(r"(?<=[.!?])\s+", block)
            if sent.strip()
        ]

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    def test_no_sentence_calls_the_entry_reset_opt_in(self, name: str) -> None:
        """Catches a reintroduction the enumerated list never saw.

        Subject-scoped so it does not fire on the many unrelated opt-ins in
        these files, and the negation waiver is scoped to the individual
        match rather than to the sentence around it.
        """
        for sentence in self._sentences(name):
            for phrase in self._unnegated_opt_in(sentence):
                raise AssertionError(
                    f"{name}: this sentence calls the entry reset {phrase!r}, "
                    f"which it has not been since #285 — {sentence.strip()!r}"
                )

    @classmethod
    def _unnegated_opt_in(cls, sentence: str) -> list[str]:
        """Opt-in phrases in *sentence*, minus the negated ones.

        Extracted so the two positive controls can exercise the decision
        directly instead of asserting on file contents — the pin above is a
        loop over this.
        """
        low = sentence.lower()
        if not any(t.lower() in low for t in cls._SUBJECT):
            return []
        negation = cls._negation_re()
        hits: list[str] = []
        for term in cls._OPT_IN:
            start = low.find(term)
            while start != -1:
                # Bounds on the WHOLE string, never a slice.  ``re``
                # evaluates ``\b`` at *pos* against the preceding character
                # of the string it was given, so slicing the window out
                # first presents a word cut by the window boundary as a
                # fresh word: "Cannot: the entry reset is opt-in." sliced
                # to "not: the entry reset is " waived itself on the tail
                # of "Cannot", and "The minor: …" on the tail of "minor".
                # That is the substring trap the word boundaries exist to
                # close, surviving at exactly one offset — which is why the
                # same words a few characters earlier were caught and this
                # looked fixed.
                window = max(0, start - cls._NEGATION_WINDOW)
                if negation is None or not negation.search(low, window, start):
                    hits.append(term)
                    break
                start = low.find(term, start + 1)
        return hits

    @classmethod
    def _negation_re(cls) -> "re.Pattern[str] | None":
        """:data:`_NEGATED` as a word-boundary pattern, or ``None`` if empty.

        ``None`` means "no waiver at all", which is what an emptied
        :data:`_NEGATED` should mean — the pin then flags everything, and
        ``test_the_opt_in_pin_leaves_correct_prose_alone`` says so.
        """
        if not cls._NEGATED:
            return None
        parts = [
            re.escape(cue) if "'" in cue else rf"\b{re.escape(cue)}\b"
            for cue in cls._NEGATED
        ]
        return re.compile("|".join(parts))

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    def test_no_sentence_claims_the_c64u_defaults_on(self, name: str) -> None:
        """The contradiction case: a claim that can sit *beside* the
        corrected sentence, so no presence or absence pin above sees it."""
        for sentence in self._sentences(name):
            low = sentence.lower()
            if not any(t.lower() in low for t in self._CBM):
                continue
            if not any(t in low for t in self._DEFAULTS_ON):
                continue
            assert any(t.lower() in low for t in self._SUBJECT) is False, (
                f"{name}: claims the C64U defaults ON for the entry reset. It "
                f"defaults OFF, for the WiFi-rejoin reason — {sentence.strip()!r}"
            )

    def test_the_claim_matches_the_code_it_describes(self) -> None:
        """The doc sentence is checked against the table, not just spelled.

        Flipping a generation's default in the code without touching the
        docs fails here — which is the failure #285 shipped with.
        """
        assert BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION == {
            "ultimate": True, "cbm": False, "unknown": False,
        }
        assert baseline_default_for_generation("ultimate") is True
        assert baseline_default_for_generation("cbm") is False
        assert baseline_default_for_generation("unknown") is False

    def test_the_c64u_off_carries_its_reason_where_the_reason_belongs(self) -> None:
        """Naming the default without its reason is how it gets "tidied up".

        The three explainer files carry the mechanism; the two reference
        files (REFERENCE.md, SKILL.md) carry the fact and a pointer.
        """
        for name in ("README.md", "docs/development.md", "skill/PATTERNS.md"):
            flat = _doc(name)
            assert "reconnection after a power cycle is known unreliable" in flat, (
                f"{name}: states the C64U default without the WiFi-rejoin reason "
                f"it rests on"
            )


# --------------------------------------------------------------------------- #
# The instrument                                                              #
# --------------------------------------------------------------------------- #

class TestTheCitationsResolve:
    """Citations by line range rot silently; these are cited by name.

    Both lane docs point at the fixture whose SIGKILLed teardown is the
    #276 incident.  They used to cite it as ``:51-57``, which is exact —
    and stays exact only by luck: another lane has that file open, and a
    line range cannot be wrong in a way any test notices.  Citing the
    fixture by name removes the luck, but only if the name is checked, so
    it is checked here.

    Deliberately *not* asserted: the shape of the fixture's teardown.  The
    docs describe it as a bare post-``yield`` sequence, which is true today,
    but pinning that would make another lane's improvement to that fixture
    fail this lane's test.  A rename or a move is the drift worth catching.
    """

    _CITED = _REPO / "tests" / "test_ultimate64_transport_live.py"

    def test_the_cited_module_exists(self) -> None:
        assert self._CITED.is_file(), f"{self._CITED} is cited by two lane docs"

    def test_the_cited_fixture_is_still_called_transport(self) -> None:
        import ast

        tree = ast.parse(self._CITED.read_text(encoding="utf-8"))
        fixtures = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any("fixture" in ast.dump(d) for d in node.decorator_list)
        }
        assert "transport" in fixtures, (
            f"the docs cite a module-scoped 'transport' fixture in "
            f"{self._CITED.name}; found {sorted(fixtures)}"
        )

    @pytest.mark.parametrize("name", ["docs/development.md", "skill/PATTERNS.md"])
    def test_neither_doc_cites_it_by_line_range_any_more(self, name: str) -> None:
        flat = _doc(name)
        assert ":51-57" not in flat, (
            f"{name}: back to a line-range citation into a file another lane "
            f"has open, where nothing would report the drift"
        )
        # `_flat` strips backticks, so the markup-bearing spelling can never
        # appear here; asserting it as an alternative would be a dead branch.
        assert "transport fixture" in flat


class TestTheWithdrawnItemCount:
    """The ``~150 items`` figure traced to one sentence in a live test's
    docstring, with no n and no date, eleven lines from a figure carrying
    both.  It was withdrawn rather than re-derived: the covered-category
    count cannot be got from the measured 201 without a device, because
    the five never-touch stores are excluded and nobody has counted the
    remainder.  Pinned so it does not come back as a round number.
    """

    #: Cues that mark a mention of the figure as a *withdrawal* of it
    #: rather than an assertion of it.  A bare absence pin was the first
    #: attempt and it was wrong in a way worth recording: it forbade the
    #: string outright, which forbids naming the figure in the sentence
    #: that retracts it — and an unnamed retraction is unfindable by the
    #: reader who met "~150" somewhere else and came looking.
    _WITHDRAWAL = ("withdrawn", "was an estimate", "used to assert")

    @classmethod
    def _asserted_occurrences(cls, text: str) -> list[str]:
        """Occurrences of the figure whose own sentence does not retract it.

        Scoped to the sentence, not to a character window.  A 400-character
        window was the first attempt and it failed its own job: reasserting
        the figure a line above the paragraph that withdraws it left the
        withdrawal inside the window, so the relapse read as a retraction.
        The retraction has to be in the sentence making the claim, which is
        also the only place a reader would see it.
        """
        out: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", _flat(text)):
            if "~150 items" not in sentence:
                continue
            if not any(cue in sentence for cue in cls._WITHDRAWAL):
                out.append(sentence)
        return out

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    def test_no_lane_doc_asserts_an_uncounted_item_figure(self, name: str) -> None:
        bad = self._asserted_occurrences(LANE_DOCS[name].read_text(encoding="utf-8"))
        assert not bad, f"{name}: asserts ~150 items without withdrawing it — {bad!r}"

    def test_the_live_test_docstring_no_longer_asserts_it_either(self) -> None:
        """The origin, not just the copy — otherwise the next doc sweep
        reads the docstring, trusts it, and reintroduces the figure."""
        origin = (_REPO / "tests" / "test_entry_baseline_live.py").read_text(
            encoding="utf-8"
        )
        assert not self._asserted_occurrences(origin)

    def test_the_withdrawal_pin_is_not_satisfied_by_any_mention(self) -> None:
        """Positive control.  A cue-window pin degenerates into "the string
        may appear anywhere" if the window is wide enough or the cue list
        loose enough, and nothing else here would notice."""
        assert self._asserted_occurrences("The reset touches ~150 items on the U64E.")
        assert not self._asserted_occurrences(
            "The ~150 items figure was an estimate and is withdrawn."
        )
        # And the case that defeated the character-window version: a fresh
        # assertion standing next to, but outside, the retraction.
        assert self._asserted_occurrences(
            "The reset touches ~150 items on the U64E. "
            "An earlier figure was an estimate and is withdrawn."
        )

    def test_patterns_keeps_the_measured_figure_with_its_scope(self) -> None:
        flat = _doc("skill/PATTERNS.md")
        assert "201 items over every category the U64E lists" in flat
        assert "uncounted subset" in flat


class TestTheSubjectScopedPinsCanFail:
    """Positive controls for the two heuristic pins, and for ``_blocks``.

    The enumerated pins have had a positive control since they were
    written; these did not, and they need one more, because they are
    heuristics over prose rather than literal substrings.  A reviewer
    proved the point with two one-token edits — ``_NEGATED = ("", …)``,
    which waives every match, and ``starts_block = False``, which makes
    ``_blocks`` return the whole file as one block.  Both turned the guard
    into a no-op and **both left the suite green**.  A check that can be
    silently disabled is not a gate, so the decision is exercised here
    directly rather than being inferred from five files that currently
    happen to be clean.

    Every relapse below is a form that has actually been tried against
    this lane: R1/R2 are the two that walked past the enumerated list, and
    R3/R4 are the two that walked past the first cut of these pins.  They
    are cases, not illustrations — a future edit that reopens either hole
    fails here.
    """

    _PIN = TestTheDefaultIsDocumented

    # -- relapses that must be flagged -----------------------------------

    @pytest.mark.parametrize("label,sentence", [
        ("R1 enumerated-list escapee",
         "The entry baseline is opt-in and off by default, so set "
         "U64_BASELINE_ON_ENTRY=1 to enable it."),
        ("R5 defaults-off plus an invitation",
         "The entry baseline defaults off; turn it on with the env var."),
        ("R6 hyphenated subject",
         "Reset-on-entry is disabled by default."),
        # R3: the colon form.  Splitting sentences on ":" put the subject in
        # one fragment and the claim in the next, so neither was both about
        # the entry reset and wrong about it.  Colon-introduced phrasing is
        # the house style in development.md's own gate table.
        ("R3 colon-introduced claim",
         "Env U64_BASELINE_ON_ENTRY: off by default."),
        # R4: a negation in an EARLIER clause used to waive the whole
        # sentence, so the relapse rode in on the second clause.
        ("R4 negation in a different clause",
         "The entry reset is not a flag you turn on for VICE, but on the "
         "U64 it is off by default."),
        # A negation cue hiding inside an ordinary word.  Substring matching
        # waived this on the "not" in "Note"; "nor" inside "minor",
        # "ignore" and "norm" is the same trap.
        ("a negation cue inside an ordinary word",
         "Note the entry reset is opt-in."),
        # The same trap at the window edge.  Both of these are tuned so the
        # enclosing word straddles the 24-character boundary exactly: slice
        # the window out and "Cannot" presents its tail as the word "not",
        # "minor" as "nor".  Offsets matter, so do not reflow these.
        ("a cue whose word is cut by the window boundary",
         "Cannot: the entry reset is opt-in."),
        ("the same, on the tail of 'minor'",
         "The minor: the entry reset is opt-in."),
    ])
    def test_the_opt_in_pin_flags_a_planted_relapse(
        self, label, sentence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Planted into a file and run through the **whole** pin.

        Deliberately not a call to ``_unnegated_opt_in`` with the sentence
        in hand: two of these relapses are defeated upstream of that
        helper, in ``_blocks`` and ``_sentences``, so a control that starts
        at the helper cannot see them.  A first cut of this test did
        exactly that and a mutation restoring the ``:`` split walked past
        it — the case looked like a regression pin for R3 and pinned only
        the half of the mechanism that never failed.
        """
        doc = tmp_path / "planted.md"
        doc.write_text(f"Some unrelated prose.\n\n{sentence}\n", encoding="utf-8")
        monkeypatch.setitem(LANE_DOCS, "planted", doc)
        with pytest.raises(AssertionError, match="calls the entry reset"):
            self._PIN().test_no_sentence_calls_the_entry_reset_opt_in("planted")

    # -- corrected prose that must NOT be flagged ------------------------

    @pytest.mark.parametrize("label,sentence", [
        # THE ONE THAT EXERCISES THE WAIVER.  Subject, opt-in term and
        # negation all present, so this is the only case here that reaches
        # the negation check at all — every other one is clean for an
        # unrelated reason (no opt-in term, or no subject term).  Without
        # it the waiver is dead code: `_NEGATED = ()` and
        # `_NEGATION_WINDOW = 0` both left the suite green.  A future
        # author will write this sentence; without the waiver it is a false
        # positive, so do not "simplify" the waiver away on the strength of
        # a green suite that no longer includes this line.
        ("negated, and it reaches the waiver",
         "The entry reset is not opt-in."),
        ("the negation is the corrected claim",
         "The entry reset is not a flag you turn on."),
        ("negated, spelled the other way",
         "You do not opt in — a U64E lane already gets this."),
        ("an unrelated opt-in with no subject term",
         "Two opt-in U64 suites, both also gated on U64_HOST."),
        ("MemoryPolicy's opt-in, also unrelated",
         "Opt in: the cheapest signal is MemoryPolicy.from_prg(prg)."),
    ])
    def test_the_opt_in_pin_leaves_correct_prose_alone(
        self, label, sentence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other direction, through the same whole-pin path.  A pin that
        flags everything is as useless as one that flags nothing, and it is
        the shape that gets deleted by the next person who trips over it."""
        doc = tmp_path / "planted.md"
        doc.write_text(f"Some unrelated prose.\n\n{sentence}\n", encoding="utf-8")
        monkeypatch.setitem(LANE_DOCS, "planted", doc)
        self._PIN().test_no_sentence_calls_the_entry_reset_opt_in("planted")

    # -- the contradiction pin -------------------------------------------

    def test_the_c64u_pin_flags_a_planted_contradiction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc = tmp_path / "planted.md"
        doc.write_text(
            "Some prose.\n\nOn the C64U the entry reset defaults on.\n",
            encoding="utf-8",
        )
        monkeypatch.setitem(LANE_DOCS, "planted", doc)
        with pytest.raises(AssertionError, match="defaults ON"):
            self._PIN().test_no_sentence_claims_the_c64u_defaults_on("planted")

    def test_the_c64u_pin_leaves_the_corrected_claim_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_CLAIM names the C64U and the word "on" in one sentence.
        If the contradiction pin fired on it, every lane doc would be red
        and the pin would be removed rather than fixed."""
        doc = tmp_path / "planted.md"
        doc.write_text(
            f"The entry reset resolves per generation: {DEFAULT_CLAIM}.\n",
            encoding="utf-8",
        )
        monkeypatch.setitem(LANE_DOCS, "planted", doc)
        self._PIN().test_no_sentence_claims_the_c64u_defaults_on("planted")

    # -- the parser underneath both --------------------------------------

    def test_blocks_splits_a_paragraph_from_a_following_bullet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutation that survived: with ``starts_block = False`` the
        file comes back as one block, and every subject term in it reaches
        every claim in it."""
        doc = tmp_path / "planted.md"
        doc.write_text(
            "A paragraph about the entry reset.\n"
            "- A bullet that is opt-in for unrelated reasons.\n",
            encoding="utf-8",
        )
        monkeypatch.setitem(LANE_DOCS, "planted", doc)
        blocks = _blocks("planted")
        assert len(blocks) == 2, blocks
        assert "entry reset" in blocks[0] and "opt-in" not in blocks[0]
        assert "opt-in" in blocks[1] and "entry reset" not in blocks[1]

    def test_blocks_keeps_a_hard_wrapped_paragraph_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The opposite error: splitting per line would cut a claim in half
        and lose it, which is the failure mode a stricter splitter has."""
        doc = tmp_path / "planted.md"
        doc.write_text(
            "The entry reset is\nsomething this sentence\nwraps across.\n",
            encoding="utf-8",
        )
        monkeypatch.setitem(LANE_DOCS, "planted", doc)
        blocks = _blocks("planted")
        assert blocks == ["The entry reset is something this sentence wraps across."]


class TestTheNormaliser:
    """``_flat`` is load-bearing for every pin above, so it is pinned too.

    A verbatim assertion is only as narrow as the normaliser underneath it:
    if ``_flat`` ever strips something semantic, every pin widens
    *invisibly* — they still read as exact phrases while matching text that
    no longer says the same thing.  Both directions are covered, because
    checking only that reflowed text still matches would pass for a
    ``_flat`` that deleted everything.
    """

    @pytest.mark.parametrize("label,mangle", [
        ("reflowed across lines", lambda p: p.replace(", off for", ",\n  off for")),
        ("emphasis added", lambda p: p.replace("U64E", "**U64E**")),
        ("code markup added", lambda p: p.replace("C64U", "`C64U`")),
        ("whitespace doubled", lambda p: p.replace(" ", "  ")),
    ])
    def test_formatting_changes_do_not_break_a_pin(self, label, mangle) -> None:
        mangled = mangle(DEFAULT_CLAIM)
        assert mangled != DEFAULT_CLAIM, f"{label}: the mangle did nothing"
        assert DEFAULT_CLAIM in _flat(mangled), label

    @pytest.mark.parametrize("label,mangle", [
        ("generations swapped — inverts the claim",
         lambda p: p.replace("on for the U64E, off for the C64U",
                             "on for the C64U, off for the U64E")),
        ("unknown flipped to on",
         lambda p: p.replace("off for an unreadable", "on for an unreadable")),
        ("case changed", lambda p: p.replace("off for the C64U", "Off for the C64U")),
        ("comma removed", lambda p: p.replace("U64E, off", "U64E off")),
    ])
    def test_semantic_changes_still_fail_a_pin(self, label, mangle) -> None:
        mangled = mangle(DEFAULT_CLAIM)
        assert mangled != DEFAULT_CLAIM, f"{label}: the mangle did nothing"
        assert DEFAULT_CLAIM not in _flat(mangled), (
            f"_flat absorbed a semantic difference ({label}) — every pin built "
            f"on it is wider than it looks"
        )

    def test_it_removes_only_markup_and_whitespace_runs(self) -> None:
        assert _flat("a`b") == "ab"
        assert _flat("a*b") == "ab"
        assert _flat("a \n\t b") == "a b"

    @pytest.mark.parametrize("text", ["a-b", "a,b", "AbC", "1.1.0", "#285",
                                      "u64_config.cc:744-800"])
    def test_it_preserves_everything_else(self, text: str) -> None:
        assert _flat(text) == text

    def test_a_stale_phrase_is_detectable_through_the_normaliser(self) -> None:
        """Positive control for the absence pins.

        An absence assertion passes trivially against a haystack the
        instrument cannot see into.  This proves the instrument can see a
        stale phrase when one is actually there — so a green absence pin is
        evidence and not a dead gauge.
        """
        for phrase in STALE_PHRASES + RETRACTED_PHRASES:
            haystack = _flat(f"prose before\n**{phrase}** and prose after")
            assert phrase in haystack, phrase

    def test_a_phrase_wrapped_mid_sentence_is_still_matched(self) -> None:
        """The grep trap, made executable.

        A phrase that straddles a line break in a hard-wrapped markdown file
        returns zero raw ``grep`` hits while matching this file's pins
        perfectly — which is how ``that store has never been read`` was once
        mistaken for a pin with no referent.  Asserting both halves here
        means the next reader who reaches for ``grep`` has a test telling
        them why it disagrees, instead of a comment they can talk
        themselves out of.
        """
        for phrase in STALE_PHRASES + RETRACTED_PHRASES:
            words = phrase.split()
            if len(words) < 3:
                continue
            mid = len(words) // 2
            wrapped = " ".join(words[:mid]) + "\n  " + " ".join(words[mid:])
            assert phrase not in wrapped, (
                f"{phrase!r}: the wrap did nothing — this case proves nothing"
            )
            assert phrase in _flat(wrapped), (
                f"{phrase!r} is lost when its source file wraps it; the pin "
                f"would silently stop protecting anything"
            )
