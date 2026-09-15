"""The retired /Temp model stays retired in the prose a reader takes rules from (#256).

#256 and #261 established, from the arithmetic and the owner's observation of
wedged devices, that the C64U failure is a **firmware crash** triggered by
``/Temp`` attachment accumulation, not ``/Temp`` filling up: the one recorded
wedge sat at ~5.8% of a 16 MiB RAM disk with 15 directory entries.  The docs
were swept, but nothing stopped the old wording coming back.  The existing pin
``tests/test_ultimate64_temp_gc.py::test_temp_gc_source_no_longer_states_the_3_mb_ramdisk``
guards the stale *figures* in one module only.

Three rules, over :data:`SCANNED`:

* **mechanism** -- a clause naming ``/Temp`` or the RAM disk together with
  "fill(s|ed|ing)", "exhaust..." or "full filesystem/disk" must negate it
  close by ("not", "no", "nothing", "nowhere near", "nor" within 60
  characters before) or call it the wrong model ("... is the wrong model",
  "... does not describe") within 40 characters after;
* **stale figure** -- "~3 MB", "31%", "945 KB" or "3 * 1024 * 1024" only in a
  paragraph that marks it stale or corrected;
* **fifteen as a bound** -- "15 uploads/cycles/attachments" in the same
  sentence as budget/under/below/safe/allowance/sit must say it is not one.

Heuristics over prose, so each has positive and negative controls, the
scanned set and the real mechanism clauses are floor-checked, and exemptions
are line-granular with a reason.

**Declared limits.**  A mechanism word more than 60 characters after its
negation, or across a colon, counts as unnegated (conservative).  A claim
worded without these words ("/Temp runs out of room") is not caught.
``.claude/agents/adversarial-reviewer.md`` is **not scanned**: it is an agent
definition, and its two retired phrasings (#256 audit) are left for the owner.
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
        _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_temp_gc.py",
        _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_client.py",
        _REPO / "tests" / "test_ultimate64_temp_hygiene.py",
    }
))

#: (file relative to the repo, exact flattened clause, reason).  Kept exact so an
#: exemption cannot absorb a new sentence.
EXEMPT: tuple[tuple[str, str, str], ...] = (
    (
        "docs/u64_recovery.md",
        "* The owner's operating theory is RAM-disk space exhaustion.",
        "attributes a theory to the owner; whether it is still the owner's view "
        "is for the owner to rule on, not for a docs sweep to rewrite (#256 audit)",
    ),
)

_SUBJECT = re.compile(r"/Temp|RAM[- ]disk", re.IGNORECASE)
_MECH_WORD = (
    r"(?:fill(?:s|ed|ing)?(?:\s+up)?|exhaust\w*|full\s+(?:filesystem|disk|RAM[- ]disk)"
    r"|(?:is|was|gets|got|became|becomes)\s+full)"
)
_MECH = re.compile(rf"\b{_MECH_WORD}\b", re.IGNORECASE)
_MECH_NEGATED = re.compile(
    rf"\b(?:not|no|never|nothing|nowhere|nor)\b[^.;:]{{0,60}}?\b{_MECH_WORD}"
    rf"|\b{_MECH_WORD}\W{{0,3}}[^.;]{{0,40}}?\b(?:is the wrong model|wrong model|does not describe)",
    re.IGNORECASE,
)
_STALE = re.compile(r"~?\s*\b3\s*MB\b|\b31\s*%|\b945\s*KB\b|3 \* 1024 \* 1024", re.IGNORECASE)
_STALE_MARKED = re.compile(r"\bstale\b|used to claim|#261|\bcorrected\b", re.IGNORECASE)
_FIFTEEN = re.compile(
    r"\b15\b[^.]{0,40}\b(?:upload|cycle|attachment)s?\b"
    r"|\b(?:upload|cycle|attachment)s?\b[^.]{0,40}\b15\b",
    re.IGNORECASE,
)
_BOUND = re.compile(r"\b(?:budget|under|below|allowance|safe|sit)\b", re.IGNORECASE)
_FIFTEEN_DISCLAIMED = re.compile(r"\b(?:not|nothing|no standing|for nothing)\b", re.IGNORECASE)


def _flat(text: str) -> str:
    text = (text.replace("``", "").replace("`", "").replace("**", "")
            .replace("#:", " ").replace("#", " "))
    return re.sub(r"\s+", " ", text).strip()


def _paragraphs(text: str) -> list[str]:
    return [_flat(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


def _clauses(paragraph: str) -> list[str]:
    return [c for c in re.split(r"(?<=[.!?;])\s+", paragraph) if c]


def _sentences(paragraph: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", paragraph) if s]


def retired_model_problems(text: str) -> list[tuple[str, str]]:
    """``(rule, excerpt)`` for every place *text* states the retired model."""
    out: list[tuple[str, str]] = []
    for paragraph in _paragraphs(text):
        for clause in _clauses(paragraph):
            if (_SUBJECT.search(clause) and _MECH.search(clause)
                    and not _MECH_NEGATED.search(clause)):
                out.append(("mechanism", clause))
        if _STALE.search(paragraph) and not _STALE_MARKED.search(paragraph):
            out.append(("stale figure", paragraph))
        for sentence in _sentences(paragraph):
            if (_FIFTEEN.search(sentence) and _BOUND.search(sentence)
                    and not _FIFTEEN_DISCLAIMED.search(sentence)):
                out.append(("fifteen as a bound", sentence))
    return out


def _exempt(path: Path, excerpt: str) -> bool:
    rel = str(path.relative_to(_REPO))
    return any(rel == f and excerpt == clause for f, clause, _ in EXEMPT)


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: str(p.relative_to(_REPO)))
def test_no_scanned_file_states_the_retired_temp_model(path: Path) -> None:
    found = [
        (rule, excerpt[:200]) for rule, excerpt in
        retired_model_problems(path.read_text(encoding="utf-8"))
        if not _exempt(path, excerpt)
    ]
    assert found == [], (
        f"{path.relative_to(_REPO)} states the retired /Temp model (#256): the "
        f"failure is a firmware crash triggered by accumulation, the RAM disk is "
        f"16 MiB, and ~15 uploads is not a budget: {found}"
    )


def test_every_exemption_is_still_needed() -> None:
    for rel, clause, reason in EXEMPT:
        assert reason, rel
        hits = [e for _, e in retired_model_problems((_REPO / rel).read_text(encoding="utf-8"))]
        assert clause in hits, f"dead exemption, remove it: {rel}: {clause!r}"


def test_the_scan_sees_the_real_corpus() -> None:
    """Vacuity guard: the files that carry the corrected model are scanned, and
    the mechanism rule meets real clauses there (all negated, hence passing)."""
    rel = {str(p.relative_to(_REPO)) for p in SCANNED}
    for expected in (
        "README.md",
        "docs/u64_recovery.md",
        ".claude/skills/c64-test/PATTERNS.md",
        ".claude/skills/c64-test/REFERENCE.md",
        ".claude/skills/c64-test/SKILL.md",
        "src/c64_test_harness/backends/ultimate64_temp_gc.py",
        "tests/test_ultimate64_temp_hygiene.py",
    ):
        assert expected in rel, f"{expected} is not scanned"
    candidates = [
        clause
        for path in SCANNED
        for paragraph in _paragraphs(path.read_text(encoding="utf-8"))
        for clause in _clauses(paragraph)
        if _SUBJECT.search(clause) and _MECH.search(clause)
    ]
    assert len(candidates) >= 8, f"only {len(candidates)} mechanism clauses met"
    assert any("wrong model" in c for c in candidates), "the corrected PATTERNS/REFERENCE wording is not reached"
    stale_paragraphs = [
        p for path in SCANNED
        for p in _paragraphs(path.read_text(encoding="utf-8"))
        if _STALE.search(p)
    ]
    assert stale_paragraphs, "no scanned paragraph cites the stale figure as stale; the marker rule meets nothing"


class TestTheRulesCanFail:
    @pytest.mark.parametrize("text, rule", [
        ("Once /Temp fills (~15 cycles of a 63 KB PRG) the device wedges.", "mechanism"),
        ("The health check finishes filling the /Temp that wedged it.", "mechanism"),
        ("A slow device is one approaching /Temp exhaustion.", "mechanism"),
        ("The RAM disk is exhausted after a few uploads.", "mechanism"),
        ("The RAM disk becomes full and the firmware stops.", "mechanism"),
        # A negation before a colon does not reach across it.
        ("No budget applies: a slow device is approaching /Temp exhaustion.", "mechanism"),
        ("The /Temp RAM disk is ~3 MB.", "stale figure"),
        ("The wedge happened at 31% of the RAM disk.", "stale figure"),
        ("The budget of 6 sits under the ~15 uploads that wedge a device.", "fifteen as a bound"),
        ("Keep uploads below 15 attachments to stay safe.", "fifteen as a bound"),
    ])
    def test_a_planted_retired_claim_is_flagged(self, text: str, rule: str) -> None:
        assert rule in {r for r, _ in retired_model_problems(text)}, text

    @pytest.mark.parametrize("text", [
        '"/Temp fills up" is the wrong model: the firmware crashes.',
        'Nothing was near exhaustion, and "/Temp filling" does not describe it.',
        "It is a firmware crash, not a full filesystem on /Temp.",
        "Neither free clusters nor directory slots on /Temp were anywhere near exhaustion.",
        "The \"3 * 1024 * 1024\" comment beside the linker symbols is stale.",
        "Treat 15 uploads as where one reproduction stopped, not a budget to sit under.",
        "The port pool is exhausted when workers exceed ports.",
        "Accumulating /Temp attachments crash the firmware.",
        # "15 uploads" with no bound word is a datapoint, not a budget.
        "A U64E once wedged after 15 uploads of a 63 KB PRG.",
    ])
    def test_the_corrected_model_passes(self, text: str) -> None:
        assert retired_model_problems(text) == [], text

    def test_an_exemption_is_exact(self) -> None:
        rel, clause, _ = EXEMPT[0]
        assert _exempt(_REPO / rel, clause)
        assert not _exempt(_REPO / rel, clause.replace("space exhaustion", "exhaustion"))
        assert not _exempt(_REPO / "README.md", clause)
