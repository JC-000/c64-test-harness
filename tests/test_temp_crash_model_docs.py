"""No count of uploads before an unpatched C64U crashes is kept in the docs (#256).

**Owner ruling, 2026-09-15.**  "When /Temp is filled then the device firmware
crashes. the c64 appears to continue to run but the rest api becomes
unresponsive and the local firmware menu switch no longer has an effect. This is
the outcome of the writemem garbage collection issue on unpatched firmware."
And, of the ~15-uploads figure: "this was a guess about the writemem exhaustion
issue. Now that we know the actual cause we should not maintain guesses about the
number of runs before an unpatched device crashes."

So "/Temp fills" is the mechanism and stays sayable.  This pin's job is the
retired **count**:

* **a count before the crash** -- a number (digits, or a written number from
  two upward, "a few", "several", "a dozen", "a handful") of uploads, runs,
  cycles or PRGs in a sentence that also has a crash word (wedge, crash,
  brick, dies, kills, survives) or a bound word (budget, allowance, limit,
  bound, threshold, safe, within, at most, up to, short of, under, below,
  between).  Or "a handful"/"a dozen"/"a few" placed beside a budget or
  limit.  A sentence escapes only if an explicit retirement phrase ("retired",
  "not a limit", "a budget for nothing", "no standing", "where one
  reproduction stopped", "do not size/cite/restate/maintain", "is a guess")
  sits within :data:`RETIRE_WINDOW` characters of the count.  A bare "not"
  anywhere in the sentence is **not** a retirement;
* **the stale RAM-disk figures** (#261): "3 MB"/"3 MiB", "31%", "945 KB",
  "3 * 1024 * 1024", unless "stale", "corrected", "used to claim" or "#261"
  sits within :data:`STALE_WINDOW` characters of the figure.

Scanned: README, ``docs/**/*.md``, the c64-test skill, the reviewer brief
(owner-approved), the temp-GC and client modules, and the hygiene test.  Every
exemption's reason must cite an issue number; there are none today.

**Harness units (review round 2).**  In the crash-word branch only,
attachments / POSTs / calls / requests / writes also count ("Fifteen POSTs
wedge it"), except as a *price*: followed by "per call/request/probe", or
preceded by a cost verb ("costs two attachments").  "per run", "a run",
"per client" and "an upload" are not prices.  A retirement does
not waive a count when "but" or "still" follows it in the same sentence.

**Declared limits, not design.**  The harness's own numbers pass because of
these limits, not because the rule understands them, and
:class:`TestTheDeclaredLimitsStayKnown` asserts each so it stays visible:
"a budget of 15 attachments" and "an upload count under 15" are **not**
caught (attachments are not a unit in the bound branch; the number after the
noun is not read), nor is "a few dozen uploads"; and "the budget is 6 uploads
per client" **is** flagged although it is a rate, not a crash count.
"One upload" is not a count (a single call is not a crash count, and the
corpus says "one PRG per iteration" often).  Also flagged although arguably
not a crash count: a retirement taken back by a following "but"/"still"
even when what follows is harmless ("... is not a budget, but a datapoint";
#386 drops the retirement escape altogether), and a harness count that
merely shares a sentence with a crash word ("The bench made 3 POSTs and
nothing crashed.", "Each of the 4 calls can wedge the runner if it is
already stuck.").  A number of
"cycles" beside a bound word is a /Temp count only in a paragraph about /Temp,
attachments or writemem, because "a budget denominated in 6502 cycles" is
ordinary prose elsewhere.  A count spelled some other way ("a score of runs")
is not caught.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

SCANNED: tuple[Path, ...] = tuple(sorted(
    {_REPO / "README.md"}
    | set((_REPO / "docs").rglob("*.md"))
    | set((_REPO / ".claude" / "skills" / "c64-test").glob("*.md"))
    | {
        _REPO / ".claude" / "agents" / "adversarial-reviewer.md",
        _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_temp_gc.py",
        _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_client.py",
        _REPO / "tests" / "test_ultimate64_temp_hygiene.py",
        # Its docstring once priced its upload loop against "a handful" (#382 round 3).
        _REPO / "tests" / "test_u64_turbo_bench_live.py",
    }
))

#: (file relative to the repo, exact flattened sentence, reason citing an issue).
EXEMPT: tuple[tuple[str, str, str], ...] = ()

RETIRE_WINDOW = 80
STALE_WINDOW = 120

_WRITTEN = (
    r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen"
    r"|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|a\s+hundred"
)
#: A number, then the unit directly -- only a size or a small set of
#: modifiers may sit between them ("63 KB PRG", "15 more uploads", "12
#: run_prg uploads").  Letting arbitrary words in matched "0 keeps a test run"
#: and "the 6502 between iterations".  Chip part numbers are not counts.
_NUM = (
    r"(?:(?:about|around|roughly|some|after)\s+)?"
    r"(?:(?!6502\b|6510\b|6526\b|6581\b|8580\b)\d+"
    rf"|{_WRITTEN}|a\s+handful(?:\s+of)?|a\s+dozen|dozens(?:\s+of)?|a\s+few|several)"
)
_MODIFIER = r"(?:\s+(?:KB|KiB|MB|more|consecutive|further|leaking|body-carrying))?"
_COUNT_RUNS = re.compile(
    rf"\b{_NUM}{_MODIFIER}\s+(?:run_prg\s+)?(?:uploads?|runs?|PRGs?|iterations?)\b",
    re.IGNORECASE,
)
_COUNT_CYCLES = re.compile(rf"\b{_NUM}{_MODIFIER}\s+cycles?\b", re.IGNORECASE)
#: Crash-word branch only: the harness's own units.
_HARNESS_MODIFIER = r"(?:\s+(?:attachment-creating|body-carrying|run_prg|KB|KiB|more|further|leaking))?"
_COUNT_HARNESS = re.compile(
    rf"\b{_NUM}{_HARNESS_MODIFIER}\s+(?:attachments?|POSTs?|calls?|requests?|writes?)\b",
    re.IGNORECASE,
)
#: A count is skipped only when it is a *price*: "two attachments per call",
#: "costs two attachments".  "per run", "a run", "per client" and "an upload"
#: are not prices -- "Fifteen POSTs per run wedge it" is a crash count
#: (review round 3; the earlier per|each|a|an + noun rule waved those through).
_PER_CALL = re.compile(r"\s+per\s+(?:call|request|probe)\b", re.IGNORECASE)
_COST_VERB_BEFORE = re.compile(
    r"\b(?:costs?|costing|leaves?|spends?|creates?)\s+(?:exactly\s+|only\s+)?$", re.IGNORECASE
)


def _is_a_price(sentence: str, match: re.Match[str]) -> bool:
    return bool(_PER_CALL.match(sentence, match.end())
                or _COST_VERB_BEFORE.search(sentence[:match.start()]))
_CRASH = re.compile(r"\b(?:wedg\w*|crash\w*|brick\w*|dies|die|kills?|survives?)\b", re.IGNORECASE)
_BOUND = re.compile(
    r"\b(?:budget|allowance|limit|bound|threshold|safe|within|between|under|below)\b"
    r"|\bat\s+most\b|\bup\s+to\b|\bshort\s+of\b",
    re.IGNORECASE,
)
_TEMP_PARAGRAPH = re.compile(r"/Temp|attachment|writemem|unpatched|leak-prone|#686", re.IGNORECASE)
#: The gap may cross a dot inside a word ("CLAUDE.md"), not a sentence end.
_IN_SENTENCE = r"(?:[^.;]|\.(?=\w))"
_HANDFUL_BOUND = re.compile(
    rf"\ba\s+(?:handful|dozen|few)\b{_IN_SENTENCE}{{0,90}}\b(?:budget|limit|allowance)\b"
    rf"|\b(?:budget|limit|allowance)\b{_IN_SENTENCE}{{0,90}}\ba\s+(?:handful|dozen|few)\b",
    re.IGNORECASE,
)
_RETIRES = re.compile(
    r"\bretired?\b|\bnot a (?:capacity|limit|budget|bound|measured budget)\b"
    r"|\bbudget for nothing\b|\bno standing\b|\bwhere (?:one|a single) reproduction stopped\b"
    r"|\bdo not (?:size|restate|maintain|cite)\b|\bis a guess\b|\bnot as a limit\b",
    re.IGNORECASE,
)
_TAKEN_BACK = re.compile(r"\b(?:but|still)\b", re.IGNORECASE)
_STALE = re.compile(r"\b3\s*Mi?B\b|\b31\s*%|\b945\s*KB\b|3 \* 1024 \* 1024", re.IGNORECASE)
_STALE_MARKER = re.compile(r"\bstale\b|used to claim|#261|\bcorrected\b", re.IGNORECASE)


def _flat(text: str) -> str:
    text = (text.replace("``", "").replace("`", "").replace("**", "")
            .replace("#:", " ").replace("# ", " "))
    return re.sub(r"\s+", " ", text).strip()


def _paragraphs(text: str) -> list[str]:
    return [_flat(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


def _sentences(paragraph: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", paragraph) if s]


def _retired_near(sentence: str, match: re.Match[str]) -> bool:
    """A retirement phrase that *starts* within the window after the count, or
    *ends* within the window before it."""
    return any(
        r.start() <= match.end() + RETIRE_WINDOW
        and r.end() >= match.start() - RETIRE_WINDOW
        # "retired as a hard rule but still a good budget" retires nothing.
        and not _TAKEN_BACK.search(sentence, r.end())
        for r in _RETIRES.finditer(sentence)
    )


def _states_a_count(sentence: str, paragraph: str) -> bool:
    handful = _HANDFUL_BOUND.search(sentence)
    if handful and not _retired_near(sentence, handful):
        return True
    for pattern, needs_temp in ((_COUNT_RUNS, False), (_COUNT_CYCLES, True)):
        for match in pattern.finditer(sentence):
            if _retired_near(sentence, match):
                continue
            if _CRASH.search(sentence):
                return True
            if _BOUND.search(sentence) and (not needs_temp or _TEMP_PARAGRAPH.search(paragraph)):
                return True
    if _CRASH.search(sentence):
        for match in _COUNT_HARNESS.finditer(sentence):
            if _is_a_price(sentence, match) or _retired_near(sentence, match):
                continue
            return True
    return False


def _stale_unmarked(paragraph: str) -> bool:
    for match in _STALE.finditer(paragraph):
        lo = max(0, match.start() - STALE_WINDOW)
        if not _STALE_MARKER.search(paragraph[lo:match.end() + STALE_WINDOW]):
            return True
    return False


def count_and_figure_problems(text: str) -> list[tuple[str, str]]:
    """``(rule, excerpt)`` for each retired count or unmarked stale figure in *text*."""
    out: list[tuple[str, str]] = []
    for paragraph in _paragraphs(text):
        for sentence in _sentences(paragraph):
            if _states_a_count(sentence, paragraph):
                out.append(("count before a crash", sentence))
        if _stale_unmarked(paragraph):
            out.append(("stale figure", paragraph))
    return out


def _exempt(path: Path, excerpt: str) -> bool:
    rel = str(path.relative_to(_REPO))
    return any(rel == f and excerpt == s for f, s, _ in EXEMPT)


def exemption_problems(exempt) -> list[str]:
    return [f"{f}: reason cites no issue: {reason!r}"
            for f, _, reason in exempt if not re.search(r"#\d+", reason)]


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: str(p.relative_to(_REPO)))
def test_no_scanned_file_keeps_a_crash_count_or_a_stale_figure(path: Path) -> None:
    found = [(rule, e[:220]) for rule, e in count_and_figure_problems(path.read_text(encoding="utf-8"))
             if not _exempt(path, e)]
    assert found == [], (
        f"{path.relative_to(_REPO)}: no count of uploads before an unpatched device "
        f"crashes is kept (owner, 2026-09-15), and the RAM disk is 16 MiB (#261): {found}"
    )


def test_every_exemption_cites_an_issue() -> None:
    assert exemption_problems(EXEMPT) == []
    assert exemption_problems((("docs/x.md", "sentence", "the owner said so"),))
    assert not exemption_problems((("docs/x.md", "sentence", "pending #256"),))


def test_the_scan_sees_the_real_corpus() -> None:
    """Vacuity guard: the files are scanned, and both rules meet real text."""
    rel = {str(p.relative_to(_REPO)) for p in SCANNED}
    for expected in (
        "README.md",
        "docs/u64_recovery.md",
        "docs/development.md",
        ".claude/skills/c64-test/PATTERNS.md",
        ".claude/skills/c64-test/REFERENCE.md",
        ".claude/skills/c64-test/SKILL.md",
        ".claude/agents/adversarial-reviewer.md",
        "src/c64_test_harness/backends/ultimate64_temp_gc.py",
        "tests/test_ultimate64_temp_hygiene.py",
        "tests/test_u64_turbo_bench_live.py",
    ):
        assert expected in rel, f"{expected} is not scanned"

    marked_figures = []
    for path in SCANNED:
        for paragraph in _paragraphs(path.read_text(encoding="utf-8")):
            if _STALE.search(paragraph):
                marked_figures.append(paragraph)
    assert len(marked_figures) >= 3, "the stale-figure rule meets too few real, marked figures"

    # Real count-shaped sentences that pass for the ordinary reason (no crash
    # or bound word), not because a retirement waives them.  Review round 2:
    # the previous floor counted retired figures, which this PR and #386 remove.
    patterns = (_REPO / ".claude" / "skills" / "c64-test" / "PATTERNS.md").read_text(encoding="utf-8")
    ordinary = [
        (s, p) for p in _paragraphs(patterns) for s in _sentences(p)
        if "twelve uploads in one session" in s
    ]
    assert ordinary, "the PATTERNS 'twelve uploads in one session' sentence is gone"
    for sentence, paragraph in ordinary:
        assert _COUNT_RUNS.search(sentence), "the count rule no longer reads it as a count"
        assert not _states_a_count(sentence, paragraph)
    unknown = [
        (s, p) for p in _paragraphs(patterns) for s in _sentences(p)
        if "how many uploads an unpatched device survives" in s
    ]
    assert unknown and all(_CRASH.search(s) and not _states_a_count(s, p) for s, p in unknown)
    rate = [
        (s, p) for p in _paragraphs(patterns) for s in _sentences(p)
        if "two attachments per call" in s
    ]
    assert rate, "the PATTERNS liveness_probe price sentence is gone"
    assert all(_COUNT_HARNESS.search(s) and _CRASH.search(s) and not _states_a_count(s, p)
               for s, p in rate), "the per-call price is read as a count before a crash"


#: The real /Temp sentence a count is planted beside (review round 3).
_PLANT_ANCHOR = "how many uploads an unpatched device survives"


def test_a_count_planted_into_a_real_file_is_flagged() -> None:
    """Corpus-independent positive control: the real text passes, and the same
    text with one count sentence **spliced into a real /Temp paragraph** does
    not.  Round 2 appended the plant as its own paragraph, so the control also
    passed on empty text (mutant P2); the anchor check makes that fail, and the
    splice puts the plant beside real budget and retirement vocabulary."""
    path = _REPO / ".claude" / "skills" / "c64-test" / "PATTERNS.md"
    text = path.read_text(encoding="utf-8")
    assert _PLANT_ANCHOR in text, "the PATTERNS /Temp sentence the plant is spliced after is gone"
    assert count_and_figure_problems(text) == []
    anchor_end = text.index(".", text.index(_PLANT_ANCHOR)) + 1
    for planted in ("Stay within 15 uploads.", "Fifteen POSTs wedge it.",
                    "The /Temp RAM disk is 3 MiB."):
        spliced = text[:anchor_end] + " " + planted + text[anchor_end:]
        assert spliced.count("\n\n") == text.count("\n\n"), "the plant opened a new paragraph"
        found = count_and_figure_problems(spliced)
        # The flag must be *about the planted sentence*, not merely non-empty.
        assert any(planted in excerpt for _, excerpt in found), (
            f"planted {planted!r} into PATTERNS.md was not flagged: {found}"
        )


class TestTheRulesCanFail:
    @pytest.mark.parametrize("text", [
        "~15 uploads wedge it.",
        "The C64U survives a budget of 15 uploads.",
        "After about a dozen runs the device crashes.",
        "after about a dozen runs the device crashes",
        "Treat a handful as the budget and do not approach it.",
        "One U64E wedged at ~15 cycles of a 63 KB PRG.",
        "Keep it below 15 PRGs to stay safe.",
        "It dies after 20 uploads.",
        "The device dies after fifteen uploads.",
        "A budget of 12 cycles of attachments protects /Temp.",
        # reviewer-2's escapes against the first head.
        "Our budget does not matter; keep under 15 uploads.",
        "Stay within 15 uploads.",
        "Plan on at most 15 uploads between GC passes.",
        "Stop short of fifteen uploads.",
        "Allow up to 15 uploads.",
        # A retirement that is not beside the count does not waive it.
        "Keep under 15 uploads; the figure that people have argued about for "
        "several review rounds and across three issues is retired.",
        # Review round 2 (variant B): harness units, and a retraction.
        "Fifteen POSTs wedge it.",
        "~15 calls to run_prg crash the firmware.",
        "Keep under 15 uploads, which is retired as a hard rule but still a good budget.",
        # Review round 3: "per run", "a run", "per client", "an upload" are not prices.
        "Fifteen POSTs per run wedge it.",
        "Six attachments a run crash the firmware within a session.",
        "15 writes per client crash it.",
        "Fifteen attachment-creating requests an upload wedge it.",
        "It wedges after 15 run_prg calls.",
        # Review round 3 (D2): the turbo bench docstring's old wording, a dot inside "CLAUDE.md".
        "That is 12 attachments against a budget the standing hardware-safety clause in "
        "CLAUDE.md says to treat as a handful.",
    ])
    def test_a_count_guess_is_flagged(self, text: str) -> None:
        assert "count before a crash" in {r for r, _ in count_and_figure_problems(text)}, text

    @pytest.mark.parametrize("text", [
        # The owner's model, which must stay sayable.
        "When /Temp fills, the firmware crashes.",
        "When /Temp is filled then the device firmware crashes.",
        "Accumulated attachments fill /Temp and crash unpatched firmware.",
        "Nobody knows how many uploads an unpatched device survives.",
        "A device approaching /Temp exhaustion is slow.",
        # A count that retires itself, beside the figure.
        "The retired guess of 15 uploads is not a budget.",
        "It wedged at ~15 cycles of a 63 KB PRG, a datapoint, not a limit.",
        # Counts that are not a count before a crash.
        "The bench U64E held zero attachments before and after fifteen run_prg uploads.",
        "Four mhz params times three vectors is twelve uploads in one session.",
        "The per-client budget is 6 attachment-creating calls.",
        "That budget is denominated in 6502 cycles, so it evaporates under warp.",
        "A wireguard soak loop runs one PRG per iteration until it crashes.",
        # Review round 2: harness numbers.
        "DEFAULT_LEAK_BUDGET is 6 attachments per client instance.",
        # Review round 3: prices, with a crash word in the sentence.
        "uci_socket_write costs two attachments when the payload exceeds the threshold, "
        "and a wedged device crashes anyway.",
        "liveness_probe() takes two attachments per call, so probing a suspected wedge spends budget.",
        # Review round 3 (H5): a retired harness-unit count.
        "The retired guess of 15 POSTs is not a budget.",
        "The retired guess that 15 POSTs wedge a device is not a budget.",
        "liveness_probe() costs two attachments per call, so probing a suspected wedge spends budget.",
    ])
    def test_the_owner_model_and_ordinary_counts_pass(self, text: str) -> None:
        assert count_and_figure_problems(text) == [], text

    def test_the_retire_window_is_what_decides(self) -> None:
        """The same sentence flips on the distance alone (reviewer-2's S1)."""
        near = "Keep under 15 uploads, a retired guess."
        far = "Keep under 15 uploads" + ", and so on" * 12 + ", a retired guess."
        assert len(far) - len("Keep under 15 uploads") > RETIRE_WINDOW + 20
        assert count_and_figure_problems(near) == []
        assert "count before a crash" in {r for r, _ in count_and_figure_problems(far)}

    @pytest.mark.parametrize("text", [
        "The /Temp RAM disk is ~3 MB.",
        "The RAM disk is 3 MiB.",
        "The wedge happened at 31% of the disk.",
        # A marker elsewhere in the paragraph is not a marker for this figure.
        "The RAM disk is 3 MB" + ", and so on" * 20 + ". The other comment is stale.",
    ])
    def test_a_stale_figure_is_flagged(self, text: str) -> None:
        assert "stale figure" in {r for r, _ in count_and_figure_problems(text)}, text

    def test_a_retraction_after_the_retirement_is_what_decides(self) -> None:
        retired = "Keep under 15 uploads, which is retired as a hard rule."
        taken_back = "Keep under 15 uploads, which is retired as a hard rule but still a good budget."
        assert count_and_figure_problems(retired) == []
        assert "count before a crash" in {r for r, _ in count_and_figure_problems(taken_back)}

    def test_a_stale_figure_marked_beside_it_passes(self) -> None:
        assert count_and_figure_problems(
            'The "3 * 1024 * 1024" comment beside the linker symbols is stale.'
        ) == []


class TestTheDeclaredLimitsStayKnown:
    """The module docstring's declared limits, asserted so each stays visible."""

    @pytest.mark.parametrize("text", [
        "Keep a budget of 15 attachments.",
        "Keep the upload count under 15.",
        "A few dozen uploads wedge it.",
    ])
    def test_a_declared_miss_is_not_caught(self, text: str) -> None:
        assert count_and_figure_problems(text) == [], (
            f"now caught: {text!r}; update the module docstring's declared limits"
        )

    @pytest.mark.parametrize("text", [
        "The budget is 6 uploads per client.",
        # Review round 3.
        "The ~15 uploads that wedged it are not a budget, but a datapoint.",
        "The bench made 3 POSTs and nothing crashed.",
        "Each of the 4 calls can wedge the runner if it is already stuck.",
    ])
    def test_a_declared_false_positive_is_still_flagged(self, text: str) -> None:
        assert count_and_figure_problems(text), (
            f"no longer flagged: {text!r}; update the module docstring's declared limits"
        )
