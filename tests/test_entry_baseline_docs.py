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


def _scanned_tokens(records=None) -> tuple[str, ...]:
    """Item-count figures the docs scan looks for.

    Derived from the table, so a remeasurement that changes a total is
    rescanned without anyone editing this file: every per-record total of
    100 or more (smaller totals such as 12 or 40 collide with ordinary
    prose), plus :data:`TestTheItemCountCarriesItsScope._NEVER`.
    """
    from c64_test_harness.backends.ultimate64_baseline import (
        BASELINE_RECORDED_CATEGORY_SETS,
    )

    records = BASELINE_RECORDED_CATEGORY_SETS if records is None else records
    figures = {str(v) for r in records.values() for v in r.totals.values() if v >= 100}
    return tuple(sorted(figures | set(TestTheItemCountCarriesItsScope._NEVER)))


def _table_figures(records=None) -> frozenset[str]:
    from c64_test_harness.backends.ultimate64_baseline import (
        BASELINE_RECORDED_CATEGORY_SETS,
    )

    records = BASELINE_RECORDED_CATEGORY_SETS if records is None else records
    return frozenset(str(v) for r in records.values() for v in r.totals.values()
                     if v >= 100)


def _token_re(tokens: tuple[str, ...]) -> re.Pattern[str]:
    #: Standalone only: not part of a longer number, a version (``3.151``),
    #: a line range (``145-150``), a path, a hex literal or an issue
    #: reference (``#150``, ``#214``).
    return re.compile(
        r"(?<![\w#.$/-])(" + "|".join(map(re.escape, tokens)) + r")(?!\w|\.\d|-\d)"
    )


class TestTheItemCountCarriesItsScope:
    """Item counts live in one table; the docs cite it by name (#342).

    ``~150 items`` circulated for months with no device and no date, was
    withdrawn on that ground, was then measured on the U64E and turned out
    to be accurate -- and the figure still moved four times in three days
    (#286, #288, #292), because it lived only in prose and every copy lost
    some of its scope.  #342 moved every count into
    ``BASELINE_RECORDED_CATEGORY_SETS``: per generation, per category, with
    basis, firmware and date, and totals asserted against the lists by
    ``tests/test_entry_baseline.py``.  The #288 pins here keep their
    protection by pointing at that table:

    * **No doc restates a table figure.**  The scanned figures are derived
      from the table's totals (:func:`_scanned_tokens`), so a remeasurement
      is rescanned automatically, plus three figures that are always wrong
      in a doc: 150 (withdrawn), 194 (the C64U projection that left out
      ``Keyboard Lighting``) and 214 (a live listing that includes the
      monitor-bookmarks store).
    * **The one historical figure the docs still quote carries its own
      scope**: #276's 201, the U64E, 2026-09-10, in the incident record.
      It must name the device and **its own** date in its own sentence,
      and share that sentence with no C64U alias -- the table's C64U
      all-category total is the same number by coincidence, and that
      coincidence is exactly the misreading this rule exists to stop.
    * **The count sites cite the table** (the vacuity floor: a doc with the
      figures deleted and nothing in their place passes every scan).
    * **The table keeps the #288 evidence**: the source-derivation
      citation and the #292 residual are pinned on the record's own text.

    The per-category arithmetic that used to be parsed out of the live
    docstring is now asserted on the table itself
    (``TestRecordedItemCounts`` in ``tests/test_entry_baseline.py``).

    What it does not promise: a count spelled in words, a figure below 100,
    or a per-category number.

    **Declared limit: the "respectively" construction.**  A figure takes
    the date inside its own clause (split at ``,`` ``;`` ``—``), and
    otherwise the date whose *nearer edge* is closest; two figures and two
    dates in one clause bind wrongly.  Write one clause per figure.  An
    exact distance tie between two *different* dates is flagged.
    """

    #: Device naming, in the same sentence as the number.
    _DEVICE = ("U64E", "Ultimate 64 Elite")
    #: Every alias the repo uses for the C64U line, in any case and with a
    #: space, a hyphen or nothing between "C64" and "Ultimate" (#317).
    #: ``cbm`` is the harness's generation name and :func:`_flat` strips the
    #: backticks around it, so the case-sensitive tuple this replaced let
    #: "the cbm generation", "C64 ultimate" and "C64-Ultimate" through.
    _OTHER_DEVICE = re.compile(
        r"\bcbm\b|c64[\s-]?ultimate|commodore 64 ultimate|\bc64u\b", re.IGNORECASE
    )

    #: The historical figure prose may still quote, bound to the one date it
    #: was recorded on (#276).  A closed table on purpose.
    _SCOPE = {"201": "2026-09-10"}
    #: Figures that are never a stated item count in a doc.
    _NEVER = ("150", "194", "214")

    _DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

    #: Known non-count occurrences.  Each entry's group 1 is the exempted
    #: token.  Add to this only with a referent, and never a bare token.
    #: The two whole-sentence ``"~150"`` anchors that lived here until #342
    #: went with their sentences; both sentences are now scan cases that
    #: must be flagged.
    _ALLOWED = (
        re.compile(r"~(150) ms\b"),
    )

    #: Vacuity floor for the incident figure: the scoped #276 clause in the
    #: incident paragraph of each site.
    _FLOOR = {"skill/PATTERNS.md": 1, "docs/development.md": 1}

    #: Where the count used to be stated, and must now be cited by name.
    _LIVE_DOCSTRINGS = ("test_entry_baseline_live.py", "test_entry_baseline_table_live.py")

    @classmethod
    def _tokens(cls) -> tuple[str, ...]:
        return _scanned_tokens()

    @staticmethod
    def _live_docstring(name: str = "test_entry_baseline_live.py") -> str:
        import ast

        src = (_REPO / "tests" / name).read_text(encoding="utf-8")
        return ast.get_docstring(ast.parse(src)) or ""

    @classmethod
    def _count_occurrences(cls, text: str) -> list[tuple[str, str, int]]:
        """``(token, sentence, offset-in-sentence)`` for every scanned count.

        Sentences split on ``:`` as well as ``.!?`` -- this pin *requires*
        an accompaniment, so a smaller unit is stricter.
        """
        token_re = _token_re(cls._tokens())
        out: list[tuple[str, str, int]] = []
        for sentence in re.split(r"(?<=[.!?:])\s+", _flat(text)):
            allowed = {
                m.span(1) for pat in cls._ALLOWED for m in pat.finditer(sentence)
            }
            for m in token_re.finditer(sentence):
                if m.span(1) in allowed:
                    continue
                out.append((m.group(1), sentence, m.start(1)))
        return out

    @classmethod
    def _unscoped(cls, text: str) -> list[str]:
        """One message per count occurrence a doc may not state as written."""
        bad: list[str] = []
        table = _table_figures()
        for token, sentence, pos in cls._count_occurrences(text):
            want = cls._SCOPE.get(token)
            if want is None:
                if token in table:
                    bad.append(
                        f"{token} restates a figure BASELINE_RECORDED_CATEGORY_SETS "
                        f"owns -- cite the table by name: {sentence!r}"
                    )
                else:
                    bad.append(f"{token} is not a measured item count: {sentence!r}")
                continue
            if not any(d in sentence for d in cls._DEVICE):
                bad.append(f"{token} names no device: {sentence!r}")
                continue
            if cls._OTHER_DEVICE.search(sentence):
                bad.append(f"{token} shares its sentence with the C64U, whose "
                           f"item counts have never been read: {sentence!r}")
                continue
            dates = list(cls._DATE_RE.finditer(sentence))
            if not dates:
                bad.append(f"{token} carries no date: {sentence!r}")
                continue
            end = pos + len(token)
            lo = max((m.end() for m in re.finditer(r"[,;—]", sentence[:pos])),
                     default=0)
            nxt = re.search(r"[,;—]", sentence[end:])
            hi = end + nxt.start() if nxt else len(sentence)
            local = [d for d in dates if d.start() >= lo and d.end() <= hi]
            pool = local or dates
            ranked = sorted(
                ((pos - d.end()) if d.end() <= pos else (d.start() - end), d.group())
                for d in pool
            )
            if (len(ranked) > 1 and ranked[0][0] == ranked[1][0]
                    and ranked[0][1] != ranked[1][1]):
                bad.append(f"{token} is equidistant from {ranked[0][1]} and "
                           f"{ranked[1][1]}: {sentence!r}")
                continue
            nearest = ranked[0][1]
            if nearest != want:
                bad.append(
                    f"{token} is dated {nearest}, but was measured {want}: "
                    f"{sentence!r}"
                )
        return bad

    # -- the scanned figures follow the table --------------------------------

    def test_the_scanned_figures_follow_the_table(self) -> None:
        import dataclasses

        from c64_test_harness.backends.ultimate64_baseline import (
            BASELINE_RECORDED_CATEGORY_SETS as recs,
        )

        tokens = set(_scanned_tokens())
        figures = _table_figures()
        # Vacuity guard: both records' covered and all-category totals.
        assert len(figures) >= 4 and figures <= tokens, (sorted(figures), sorted(tokens))
        assert set(self._NEVER) <= tokens
        # Positive control: a remeasured total is scanned without editing here.
        moved = {**recs, "cbm": dataclasses.replace(
            recs["cbm"], totals={**recs["cbm"].totals, "all": 999})}
        assert "999" in _scanned_tokens(moved)

    # -- docs cite, never restate ------------------------------------------

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    def test_no_lane_doc_restates_a_table_figure(self, name: str) -> None:
        bad = self._unscoped(LANE_DOCS[name].read_text(encoding="utf-8"))
        assert not bad, f"{name}: item count stated instead of cited — {bad!r}"

    @pytest.mark.parametrize("name", _LIVE_DOCSTRINGS)
    def test_no_live_docstring_restates_a_table_figure(self, name: str) -> None:
        """The origin, not just the copy: a figure here is what the copy
        downstream inherits."""
        doc = self._live_docstring(name)
        assert doc, f"{name}: no module docstring"
        assert not self._unscoped(doc), self._unscoped(doc)

    @pytest.mark.parametrize("site", sorted(_FLOOR))
    def test_the_incident_sites_have_not_gone_quiet(self, site: str) -> None:
        found = self._count_occurrences(LANE_DOCS[site].read_text(encoding="utf-8"))
        assert len(found) >= self._FLOOR[site], (
            f"{site}: {len(found)} count occurrences, floor {self._FLOOR[site]} "
            f"— the scoped #276 clause was deleted; found {[t for t, _, _ in found]}"
        )

    @staticmethod
    def _cost_bullet() -> str:
        text = LANE_DOCS["skill/PATTERNS.md"].read_text(encoding="utf-8")
        bullets = [line for line in text.splitlines() if line.startswith("- Cost:")]
        assert len(bullets) == 1, f"PATTERNS.md: {len(bullets)} '- Cost:' bullets"
        return _flat(bullets[0])

    def test_patterns_cost_bullet_cites_the_table_with_its_scope(self) -> None:
        flat = self._cost_bullet()
        for token in ("BASELINE_RECORDED_CATEGORY_SETS", "device-read",
                      "source-derived only", "#292", "#342",
                      "tests/test_entry_baseline_table_live.py"):
            assert token in flat, f"PATTERNS Cost bullet lacks {token!r}"
        # The C64U's category list HAS been read (2026-09-15, #287); only its
        # item lists have not.  The superseded wording must not come back.
        whole = _doc("skill/PATTERNS.md")
        assert "category and item lists have never been read" not in whole
        assert "category list was read once (2026-09-15, 20 categories" in flat
        assert "but its item lists never have" in flat

    @pytest.mark.parametrize("where", ["docs/development.md", *_LIVE_DOCSTRINGS])
    def test_the_other_count_sites_cite_the_table(self, where: str) -> None:
        text = (LANE_DOCS[where].read_text(encoding="utf-8")
                if where in LANE_DOCS else self._live_docstring(where))
        assert "BASELINE_RECORDED_CATEGORY_SETS" in _flat(text), where

    # -- the table keeps the #288 evidence ---------------------------------

    @staticmethod
    def _u64e_record():
        from c64_test_harness.backends.ultimate64_baseline import (
            BASELINE_RECORDED_CATEGORY_SETS,
        )

        return BASELINE_RECORDED_CATEGORY_SETS["ultimate"]

    def test_the_source_derivation_is_cited_beside_the_device_read(self) -> None:
        """Two instruments, both named, and the citation placed before the
        residual so it cannot be read as covering #276's figure."""
        rec = self._u64e_record()
        errs = self._citation_errors(rec.counts_source + " " + rec.residuals)
        assert not errs, errs
        assert "device-read" in rec.counts_source

    _CITATION = "reproduced from firmware source at 7f6fcb51 (v3.15-85)"

    @classmethod
    def _citation_errors(cls, text: str, *, must_name: tuple[str, ...] = ()) -> list[str]:
        """The citation must not claim the one figure source did NOT reproduce.

        Source gives 203, #276 recorded 201 — that disagreement is the
        residual.  So the citation sentence may not contain 201, must come
        before the residual, and must name the figures it covers when asked.
        """
        flat = _flat(text)
        sents = [s for s in re.split(r"(?<=[.!?:])\s+", flat) if cls._CITATION in s]
        if not sents:
            return ["the source-derivation citation is gone"]
        errs: list[str] = []
        for s in sents:
            nums = set(re.findall(r"(?<![\w#.$/-])\d+(?!\w|\.\d|-\d)", s))
            if "201" in nums:
                errs.append(f"claims #276's 201 was reproduced: {s!r}")
            missing = [n for n in must_name if n not in nums]
            if missing:
                errs.append(f"does not name the figures it covers {missing}: {s!r}")
        residual = flat.find("Unexplained residual")
        if residual != -1 and flat.find(cls._CITATION) > residual:
            errs.append("the citation follows the residual, so it covers 201")
        return errs

    @pytest.mark.parametrize("label,text,must_name", [
        ("the round-3 overclaim: bullet-wide scope, figures unnamed",
         "Cost — 151 items on the U64E 2026-09-12 (read-only; every figure in "
         "this bullet was also reproduced from firmware source at 7f6fcb51 "
         "(v3.15-85) in review). Unexplained residual: #276 records \"201 "
         "items compared\".", ("151", "40", "12", "203")),
        ("201 named as reproduced",
         "The 151, 40, 12, 201 and 203 figures were also reproduced from "
         "firmware source at 7f6fcb51 (v3.15-85).", ()),
        ("placed after the residual",
         "Unexplained residual: two counts differ. Every figure above was "
         "reproduced from firmware source at 7f6fcb51 (v3.15-85).", ()),
        ("citation gone", "Counted on the device only.", ()),
    ])
    def test_the_citation_check_can_fail(self, label, text, must_name) -> None:
        assert self._citation_errors(text, must_name=must_name), label

    # -- the residual -------------------------------------------------------

    #: Words that turn "nobody has established why" into its opposite when
    #: they share its sentence.  Only that sentence is checked.
    _CAUSAL_RE = re.compile(
        r"\b(because|due to|caused by|as a result|since|owing to|explained by|"
        r"results? from)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _residual_errors(cls, text: str) -> list[str]:
        flat = _flat(text)
        sentences = re.split(r"(?<=[.!?:])\s+", flat)
        errs: list[str] = []
        # Both counts in ONE sentence, so the comparison survives as one.
        if not [s for s in sentences if "201" in s and "203" in s]:
            errs.append("the 201-vs-203 comparison is gone")
        # #276's own words and what it did not record travel together.
        if ('"201 items compared" (category scope and firmware build not '
                'recorded)') not in flat:
            errs.append("#276 is no longer quoted as '201 items compared' with "
                        "its unrecorded category scope and firmware build")
        if "#292" not in flat:
            errs.append("the residual no longer links its follow-up issue #292")
        why = [s for s in sentences if "nobody has established why" in s.lower()]
        if not why:
            errs.append("'nobody has established why' is gone")
        for s in why:
            m = cls._CAUSAL_RE.search(s)
            if m:
                errs.append(f"asserts a cause ({m.group()!r}) beside it: {s!r}")
        return errs

    def test_the_residual_is_kept_in_the_u64e_record(self) -> None:
        """#276's 201 (U64E, 2026-09-10) against the record's 203 (#292).

        The failure mode guarded is the tidy one: dropping a figure,
        averaging them, or asserting a cause.  #342 moved the residual into
        the record; it did not resolve it.
        """
        residuals = self._u64e_record().residuals
        assert not self._residual_errors(residuals), self._residual_errors(residuals)

    def test_the_residual_guard_can_fail(self) -> None:
        flat = _flat(self._u64e_record().residuals)
        assert not self._residual_errors(flat)
        cause = flat.replace(
            "Nobody has established why",
            "Two items appeared because the device state changed, though "
            "nobody has established why",
        )
        assert cause != flat and self._residual_errors(cause)
        unquoted = flat.replace("201 items compared", "201 all-category items")
        assert unquoted != flat and self._residual_errors(unquoted)
        dropped = flat.replace("of 203", "")
        assert dropped != flat and self._residual_errors(dropped)

    def test_the_residual_records_the_292_investigation(self) -> None:
        """#292's 2026-09-15 findings travel with the residual.

        The fresh read after the owner's power-cycle, the counting basis it
        ruled out (every item carries ``default``), the build the device
        reported, and the arithmetic candidate -- graded inferred, never
        stated as the cause.
        """
        flat = _flat(self._u64e_record().residuals)
        for token in (
            "2026-09-15",
            "power-cycle",
            "every one of the 203 items carries both current and default",
            "bce4535e",
            "v3.15-132",
            "tree diff 7f6fcb51 -> bce4535e (not a linear range)",
            "the device read is the authority",
            "203 was also recorded on 2026-09-05 (ce4b0af)",
            "no counting basis for it is evidenced",
            "unrecoverable",
        ):
            assert token in flat, token
        # Review round 1 (#401): picking two items of 203 to reach 201 was
        # number-fitting on a false premise (ce4b0af exempted six, and
        # be99a1a removed the exemption the same day).  Deleted, not caveated.
        assert "SID Detected Socket" not in flat
        assert "arithmetic candidate" not in flat

    def test_the_record_source_names_the_running_build(self) -> None:
        """#401 round 1: 7f6fcb51 is where the counts were derived, not what
        the device runs; the source field may not present it as the latter."""
        src = _flat(self._u64e_record().source)
        assert "comes from the flash record, not from that read" not in src
        for token in ("v3.15-85 (7f6fcb51)", "bce4535e (v3.15-132)",
                      "derived at 7f6fcb51"):
            assert token in src, token

    # -- positive controls for the scan itself -------------------------------

    @pytest.mark.parametrize("label,text", [
        ("a table figure, fully scoped",
         "151 items across the covered categories, measured on the U64E (fw "
         "3.15) 2026-09-12."),
        ("a table figure as a noun", "151 is what makes those seconds interpretable."),
        ("an all-category figure without 'items'",
         "for 203 across all 19 categories the device lists."),
        ("a C64U table figure", "157 covered items on the C64U, 2026-09-15."),
        ("201 bare", "The reset compared 201 items."),
        ("201 scope in the neighbouring sentence",
         "#276 compared 201 items. Measured on the U64E 2026-09-10."),
        ("201 scope above a colon lead-in",
         "Measured on the U64E 2026-09-10: 201 items compared."),
        ("201 device but no date", "201 items compared on the U64E."),
        ("201 date but no device", "201 items compared, 2026-09-10."),
        ("201 with the other measurement's date", "201 items compared, U64E, 2026-09-12."),
        ("the C64U named beside 201",
         "The C64U counts 201 items on 2026-09-10, unlike the U64E."),
        ("the C64 Ultimate named beside 201",
         "The C64 Ultimate and the U64E both list 201 items, 2026-09-10."),
        ("the Commodore 64 Ultimate named beside 201",
         "The Commodore 64 Ultimate lists 201 items too, like the U64E, 2026-09-10."),
        ("the CBM line named beside 201",
         "On the CBM line the U64E's 201 items carry over, 2026-09-10."),
        ("an unbroken tie between two different dates",
         "2026-09-10 201 2026-09-12 on the U64E."),
        ("the withdrawn figure, even scoped", "150 items on the U64E, 2026-09-12."),
        ("the withdrawn figure unquoted", "The reset touches ~150 items."),
        ("the withdrawn figure quoted but stated as a count",
         'The entry reset touches "~150" items on every device.'),
        ("the stale C64U projection", "194 items across 20 categories, U64E, 2026-09-10."),
        ("the planted all-category figure",
         "214 across all 19 categories on the U64E, 2026-09-12."),
        ("the retired PATTERNS mention of the withdrawn figure",
         'So the covered figure and the all-category figure differ by scope, '
         'not by disagreement — the "~150" that circulated for months was '
         'accurate and was withdrawn only because nobody had measured it.'),
        ("the retired live-docstring mention of the withdrawn figure",
         'The earlier "~150" in this docstring had neither, was quoted '
         'downstream as though it were a measurement, and was withdrawn for '
         'that reason before anybody counted.'),
    ])
    def test_the_scan_flags(self, label, text) -> None:
        assert self._unscoped(text), label

    @pytest.mark.parametrize("label,text", [
        ("the incident, correctly scoped",
         "#276 compared 201 items on the U64E on 2026-09-10."),
        ("two clauses, the date before its figure",
         "On 2026-09-12 the table was written, and on 2026-09-10 the U64E "
         "compared 201 items."),
        ("a citation by name",
         "Per-category counts are in BASELINE_RECORDED_CATEGORY_SETS."),
        ("a timing", "16 KiB in ~150 ms instead of >6 s."),
        ("issue references", "See #150, #194 and #214 for that."),
        ("versions and ranges", "fw 3.151, machine.c:145-150, 0x151, 1.203, 157-160."),
    ])
    def test_the_scan_leaves_non_counts_alone(self, label, text) -> None:
        assert not self._unscoped(text), (label, self._unscoped(text))
        assert not self._count_occurrences(text) or label in (
            "the incident, correctly scoped",
            "two clauses, the date before its figure",
        ), label

    # Tuned offsets, do not reflow: 201's own date ends 1 character before
    # it and the other starts 7 after it, while measured from each date's
    # START the other one is nearer (10 vs 11).  No comma, so the
    # own-clause preference cannot rescue it.
    @pytest.mark.parametrize("label,text", [
        ("edge distance, not start distance",
         "U64E read 2026-09-10 201 items 2026-09-12."),
    ])
    def test_the_scan_binds_by_nearer_edge(self, label, text) -> None:
        assert not self._unscoped(text), (label, self._unscoped(text))


class TestTheCoveredSetIsNotCountedInWords:
    """Name ``BASELINE_CATEGORIES``; do not count it (#310).

    Adding the two C64U-only stores made "the twelve covered categories"
    false in five places at once -- README, REFERENCE, PATTERNS (twice) and
    the module's own docstring -- and nothing failed.  A count in words is
    the same defect as a count in digits: a copy of the list's size that
    does not move when the list does.  Scanned: the lane docs and the
    ``ultimate64_baseline`` module source.

    **Declared limit:** the scan looks forward from the number word, so a
    count placed *after* the list it counts, in a separate sentence, is not
    caught.  The module comment at 86596dc had exactly that shape ("...
    modem and UI stores — twelve.") and was removed by hand; it is not a
    case here because this scan cannot see it.
    """

    _NUMBER_WORDS = ("ten", "eleven", "twelve", "thirteen", "fourteen",
                     "fifteen", "sixteen")
    #: A number word followed, inside one sentence and within 60 characters,
    #: by something that names the covered set.
    _WORDED = re.compile(
        r"\b(?:" + "|".join(_NUMBER_WORDS) + r")\b[^.;:]{0,60}?"
        r"(?:\bcovered\b|BASELINE_CATEGORIES|categories are requested|machine/SID)",
        re.IGNORECASE,
    )

    @classmethod
    def _worded(cls, text: str) -> list[str]:
        return [m.group(0) for m in cls._WORDED.finditer(_flat(text))]

    @pytest.mark.parametrize("name", sorted(LANE_DOCS))
    def test_no_lane_doc_counts_the_covered_set_in_words(self, name: str) -> None:
        found = self._worded(LANE_DOCS[name].read_text(encoding="utf-8"))
        assert not found, f"{name}: {found!r} -- name BASELINE_CATEGORIES instead"

    def test_the_module_does_not_count_its_own_covered_set(self) -> None:
        src = (_REPO / "src" / "c64_test_harness" / "backends"
               / "ultimate64_baseline.py").read_text(encoding="utf-8")
        assert not self._worded(src), self._worded(src)

    @pytest.mark.parametrize("text", [
        # Every phrasing #310 had to remove, verbatim from 86596dc.
        "151 items across the twelve covered categories, measured on the U64E.",
        "over the twelve machine/SID-addressing/audio/drive/tape/printer/LED/"
        "modem/UI stores (`BASELINE_CATEGORIES`)",
        "`BASELINE_CATEGORIES` — the twelve covered stores: `C64 and Cartridge "
        "Settings`",
        "(`PUT /v1/configs/<category>:reset_to_default`, the twelve "
        "`BASELINE_CATEGORIES`)",
        "Twelve categories are requested; those the device does not list are "
        "skipped silently.",
        "The fourteen covered stores.",
    ])
    def test_the_scan_flags(self, text: str) -> None:
        assert self._worded(text), text

    @pytest.mark.parametrize("text", [
        "Twelve is the peak before a device wedges.",
        "route_machine.cc has twelve routes. The covered set is unrelated.",
        "over the covered stores in `BASELINE_CATEGORIES`",
    ])
    def test_the_scan_leaves_other_counts_alone(self, text: str) -> None:
        assert not self._worded(text), text


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


def test_reference_uci_socket_write_prices_by_grade():
    """#318 review round 1: the REFERENCE.md ``uci_socket_write`` bullet was
    rewritten for #294 (the transport chunks unless post-safe), and restoring
    master's unqualified "each call costs one attachment" survived. Scoped to
    that one bullet -- other lanes edit the skill files concurrently."""
    from pathlib import Path

    ref = Path(__file__).resolve().parents[1] / ".claude/skills/c64-test/REFERENCE.md"
    bullets = [ln for ln in ref.read_text(encoding="utf-8").splitlines()
               if ln.startswith("- `uci_socket_write(")]
    # Vacuity guard: exactly the one bullet this is about.
    assert len(bullets) == 1, bullets
    bullet = bullets[0]
    assert "SOCKET_WRITE_MAX_BYTES" in bullet
    assert "writemem_post_safe is True" in bullet
    assert "each call costs one `/Temp` attachment" not in bullet
    # Review round 2 (#318): "leak-prone or unknown grade" as a keyword let
    # the wrong cost on the right grade through (V2: "costs **one** /Temp
    # attachment"). Pin the claim as one phrase, markup stripped.
    claim = bullet.replace("*", "").replace("`", "").lower()
    assert ("on a leak-prone or unknown grade (the c64u) a call costs no "
            "/temp attachment") in claim
    # The post-safe sentence says "one POST", so this cannot collide.
    assert "costs one /temp attachment" not in claim


#: #317: every spelling of the C64U line a count sentence may use.  ``cbm``
#: is written in backticks in the docs, and ``_flat`` strips them.
_OTHER_DEVICE_VARIANTS = {
    "cbm": "the cbm generation",
    "`cbm`": "the `cbm` generation",
    "CBM line": "the CBM line",
    "C64 ultimate": "the C64 ultimate",
    "C64-Ultimate": "the C64-Ultimate",
    "C64 Ultimate": "the C64 Ultimate",
    "c64u": "the c64u",
    "C64U": "the C64U",
    "Commodore 64 ultimate": "the Commodore 64 ultimate",
}

#: The base sentence each variant is planted into.  It must pass alone, so
#: the variant is the only thing a flag can be about.
_OTHER_DEVICE_BASE = "The U64E counted 201 items on 2026-09-10"


def test_the_other_device_matcher_meets_the_real_corpus() -> None:
    """Vacuity guard for #317: the new matcher is exercised by real docs.

    Two facts, both measured on the lane docs when this was written: real
    count sentences reach the other-device check (so it runs on the corpus,
    not only on the planted controls below), and the corpus itself writes
    the generation name in lower case, a spelling the old case-sensitive
    tuple did not see.
    """
    pin = TestTheItemCountCarriesItsScope
    reached = [
        (name, token, sentence)
        for name, path in LANE_DOCS.items()
        for token, sentence, _ in pin._count_occurrences(path.read_text(encoding="utf-8"))
        if token in pin._SCOPE and any(dev in sentence for dev in pin._DEVICE)
    ]
    assert reached, "no real count sentence reaches the other-device check"

    spellings = {
        m.group(0)
        for path in LANE_DOCS.values()
        for m in pin._OTHER_DEVICE.finditer(_flat(path.read_text(encoding="utf-8")))
    }
    # The lower-case generation name the old tuple missed ...
    assert "cbm" in spellings, (
        f"no lane doc writes `cbm`; the matcher meets nothing the old tuple did not: "
        f"{sorted(spellings)}"
    )
    # ... and capitalised product names, which the lower-case pattern reaches
    # only through IGNORECASE.  A case-sensitive pattern still matches the
    # literal "cbm", so without this the guard did not see case at all
    # (mutation O1 survived it).
    assert any(s != s.lower() for s in spellings), (
        f"the matcher found no capitalised spelling in the lane docs: {sorted(spellings)}"
    )


def test_the_other_device_base_sentence_passes() -> None:
    assert TestTheItemCountCarriesItsScope._unscoped(_OTHER_DEVICE_BASE + ".") == []


@pytest.mark.parametrize("variant", list(_OTHER_DEVICE_VARIANTS))
def test_the_count_scan_flags_every_spelling_of_the_other_device(variant: str) -> None:
    sentence = f"{_OTHER_DEVICE_BASE}, unlike {_OTHER_DEVICE_VARIANTS[variant]}."
    bad = TestTheItemCountCarriesItsScope._unscoped(sentence)
    assert any("shares its sentence with the C64U" in b for b in bad), (variant, bad)


@pytest.mark.parametrize("sentence", [
    # ``cbm`` inside a longer word is not the generation name.
    f"{_OTHER_DEVICE_BASE}, logged with opencbm.",
    # The U64E's own product name contains "Ultimate".
    f"{_OTHER_DEVICE_BASE} on the Ultimate 64 Elite.",
])
def test_the_count_scan_leaves_near_misses_alone(sentence: str) -> None:
    assert TestTheItemCountCarriesItsScope._unscoped(sentence) == []
