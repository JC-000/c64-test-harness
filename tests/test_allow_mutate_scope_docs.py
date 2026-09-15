"""The docs state ``U64_ALLOW_MUTATE``'s narrowed scope and never the wider one (#333).

Owner decision, 2026-09-15 (#333, option "narrow the docs"):
``U64_ALLOW_MUTATE`` covers **device config changes only**.  Resets, RAM
writes and stream start/stop are allowed on ``U64_HOST`` alone.  The docs
used to say suites that "mutate device state (reset, RAM writes, config
changes)" need the gate, a contract no scanner enforced.  With the narrower
contract, ``tests/test_live_mutation_gate.py``'s config-write check (#268) is
the whole enforcement of what the docs promise.

Some live-device modules still skip without the gate for a reset or a RAM
write.  That is stricter than the contract and allowed, and #333 changes no
module's gating.  What must not come back is a doc stating the wider rule.

Pinned two ways:

* **presence** -- ``docs/development.md`` and ``README.md`` state the narrow
  contract;
* **absence** -- no sentence in the corpus names ``U64_ALLOW_MUTATE``, a
  non-config state change (reset, reboot, RAM, memory writes, streams, "mutate
  device state") and a requirement word or shape (require, need, gate,
  additionally, guard, "skip unless/without", "must run/be set", "only
  with/when", "set ... before" -- the last four added in review round 1),
  unless it carries a narrowing phrase ("config changes only", "``U64_HOST``
  alone", "stricter than the contract", "not for the reset", ...).  A config
  reason is removed from the sentence before the terms are looked for, so
  "the reload changes RAM config" and "tests that write Data Streams config"
  are not resets or RAM writes.

The corpus is ``README.md``, ``docs/**/*.md``, ``.claude/skills/c64-test/*.md``
and the module docstring plus full-line ``#`` comments of every
``tests/test_*.py`` except this file.

**What still gets through:**

* a skip ``reason=`` string -- runtime text, not scanned, and describing
  that module's own (allowed) stricter gate;
* a synonym outside the term list ("pokes memory", "power-cycles", "DMA
  writes") or the requirement list ("is only run with", "opt in with");
* a wrong claim written in a sentence that also carries a narrowing phrase
  or a config reason that swallows the term;
* the gate named without its literal name ("the mutation gate") beside a
  reset;
* ``src/`` docstrings, inline trailing comments, and files outside the
  corpus above.

Nothing here proves the docs are right; it stops this specific regression.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
GATE = "U64_ALLOW_MUTATE"

#: Non-config state changes the wider contract used to put behind the gate.
_TERMS = re.compile(
    r"\breset|\breboot|\bRAM\b|\bmemory writes?\b|\bwrites? to (machine )?memory\b"
    r"|\bstream|\bmutat\w* (the )?(device|machine) state",
    re.I,
)
#: Words that make a sentence a requirement.
_REQUIRE = re.compile(
    r"\b(requires?|required|requiring|needs?|needed|gated?|gates|double-gated|additionally)\b"
    r"|\bguard(s|ed)?\b"
    r"|\bskip(s|ped)?\s+(unless|without)\b"
    r"|\bmust\s+(run|be\s+set)\b"
    r"|\bonly\s+(with|when)\b"
    r"|\bset\b[^.;]{0,60}?\bbefore\b",
    re.I,
)
#: A config write given as the reason; removed before the terms are looked for.
_CONFIG_REASON = re.compile(
    r"\b(writes?|wrote|writing|rewrites?|changes?|changing)\s+(\S+\s+){0,3}?"
    r"(config(uration)?|CPU speed)\b",
    re.I,
)
#: Phrases that state the narrow contract, or that a gate is stricter than it.
_NARROWING = re.compile(
    r"config(uration)? changes only|config writes only|U64_HOST alone"
    r"|stricter than the contract|beyond the contract|outside the contract"
    r"|\bnot (for|because of) (the |a )?(reset|reboot|RAM|stream)"
    r"|\bnot (gated|required)\b",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.;!?])\s+|\s\|\s")


def wider_contract_claims(text: str) -> list[str]:
    """Sentences in *text* that put a reset, RAM write or stream behind the gate.

    A blank line ends a sentence as well as ``. ; ! ?`` and a table-cell
    ``|``, so a heading, a code sample or the next comment block is never
    read as the same sentence.
    """
    claims = []
    for paragraph in re.split(r"\n\s*\n", text):
        plain = re.sub(r"\s+", " ", re.sub(r"[`*]", "", paragraph))
        for sentence in _SENTENCE.split(plain):
            if GATE not in sentence or _NARROWING.search(sentence):
                continue
            rest = _CONFIG_REASON.sub(" ", sentence)
            if _TERMS.search(rest) and _REQUIRE.search(rest):
                claims.append(sentence.strip())
    return claims


def _python_prose(path: Path) -> str:
    """Module docstring, then each run of consecutive full-line comments as a paragraph."""
    source = path.read_text()
    blocks: list[list[str]] = [[]]
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and not line.startswith("#!"):
            blocks[-1].append(stripped.lstrip("#").strip())
        elif blocks[-1]:
            blocks.append([])
    docstring = ast.get_docstring(ast.parse(source)) or ""
    return "\n\n".join([docstring, *("\n".join(b) for b in blocks if b)])


def corpus() -> dict[str, str]:
    """Label -> prose, for every file the pin reads."""
    docs = [
        _REPO / "README.md",
        *sorted((_REPO / "docs").rglob("*.md")),
        *sorted((_REPO / ".claude" / "skills" / "c64-test").glob("*.md")),
    ]
    out = {str(p.relative_to(_REPO)): p.read_text() for p in docs if p.is_file()}
    for path in sorted((_REPO / "tests").glob("test_*.py")):
        if path.resolve() != Path(__file__).resolve():
            out[str(path.relative_to(_REPO))] = _python_prose(path)
    return out


_CORPUS = corpus()


@pytest.mark.parametrize("label", sorted(_CORPUS))
def test_no_doc_restates_the_wider_contract(label: str) -> None:
    claims = wider_contract_claims(_CORPUS[label])
    assert not claims, (
        f"{label} says U64_ALLOW_MUTATE gates a reset, RAM write or stream. "
        "It covers device config changes only; those run on U64_HOST alone "
        "(owner decision 2026-09-15, #333). A module that gates them anyway is "
        "stricter than the contract -- say so. Sentences:\n  " + "\n  ".join(claims)
    )


def _folded(path: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*]", "", (_REPO / path).read_text()))


@pytest.mark.parametrize("path, phrases", [
    ("docs/development.md", (
        "U64_ALLOW_MUTATE=1 covers device config changes only",
        "Resets, RAM writes and stream start/stop are allowed on U64_HOST alone",
        "#333",
    )),
    ("README.md", (
        "U64_ALLOW_MUTATE=1 covers device config changes only",
        "resets, RAM writes and stream start/stop run on U64_HOST alone",
    )),
])
def test_the_narrow_contract_is_stated(path: str, phrases: tuple[str, ...]) -> None:
    text = _folded(path)
    missing = [p for p in phrases if p not in text]
    assert not missing, f"{path} no longer states the #333 contract: {missing}"


def test_the_corpus_reaches_every_kind_of_file() -> None:
    labels = set(_CORPUS)
    assert "README.md" in labels and "docs/development.md" in labels
    assert any(label.startswith(".claude/skills/c64-test/") for label in labels)
    assert "tests/test_u64_feature_parity_live.py" in labels
    assert "tests/test_live_mutation_gate.py" in labels
    assert GATE in _CORPUS["tests/test_u64_capabilities_live.py"], (
        "module docstrings are not being read"
    )


class TestThePinCanFail:
    """Retired sentences as they stood at 294d036 are caught; narrow ones are not."""

    @pytest.mark.parametrize("text", [
        # docs/development.md setup step 4
        "Suites that mutate device state (reset, RAM writes, config changes) "
        "additionally require `U64_ALLOW_MUTATE=1`; with either variable unset "
        "they skip cleanly.",
        # docs/development.md, Hardware and network live gates
        "Gates marked *mutate*\nalso need `U64_ALLOW_MUTATE=1` because they write "
        "device config or RAM\nand restore it.",
        # README.md, the live-test command block
        "# Ultimate 64 live tests (requires U64_HOST; suites that mutate device\n"
        "# state — reset, RAM writes, config changes — additionally require\n"
        "# U64_ALLOW_MUTATE=1)",
        # README.md, the UCI UDP probes
        "(plus their `UCI_UDP_LIVE=1` gate, and `U64_ALLOW_MUTATE=1` because they "
        "enable the Command Interface and reset the machine, #268)",
        # tests/test_u64_capabilities_live.py docstring
        "Everything here is **read-only** except the tests in ``TestSocketLifetime``,\n"
        "which reset the C64 and so additionally require ``U64_ALLOW_MUTATE``.",
        # tests/test_u64_feature_parity_live.py docstring
        "Double-gated by ``U64_HOST`` and ``U64_ALLOW_MUTATE`` (the suite resets\n"
        "the machine and writes to RAM) — e.g.:",
        # paraphrases
        "Stream start/stop needs U64_ALLOW_MUTATE.",
        "A test that reboots the C64 is gated on U64_ALLOW_MUTATE=1.",
        # review round 1 (#379): requirement shapes outside require/need/gate
        "The debug stream tests skip unless U64_ALLOW_MUTATE is set, because they "
        "start a stream.",
        "U64_ALLOW_MUTATE guards every state change: resets, RAM writes and streams.",
        "Anything that starts a stream must run with U64_ALLOW_MUTATE.",
        "Set U64_ALLOW_MUTATE before any test that writes RAM.",
        "Resets run only with U64_ALLOW_MUTATE=1.",
    ], ids=["dev-setup", "dev-mutate-marker", "readme-comment", "readme-uci",
            "capabilities-docstring", "feature-parity-docstring", "stream", "reboot",
            "r1-skip-unless", "r1-guards", "r1-must-run", "r1-set-before",
            "r1-only-with"])
    def test_a_retired_sentence_is_caught(self, text: str) -> None:
        assert wider_contract_claims(text)

    @pytest.mark.parametrize("text", [
        "`U64_ALLOW_MUTATE=1` covers device config changes only: suites that change "
        "device config additionally require it.",
        "Resets, RAM writes and stream start/stop are allowed on `U64_HOST` alone.",
        "The tests that write Data Streams config also need ``U64_ALLOW_MUTATE``.",
        "``U64_ALLOW_MUTATE=1`` — required: the reload changes RAM config",
        "the device-touching `reset(scope='cpu')` tests request `speed_baseline` and "
        "need `U64_ALLOW_MUTATE=1` for its CPU-speed writes, not for the reset",
        "This module also gates its resets on ``U64_ALLOW_MUTATE``, stricter than "
        "the contract.",
        "A reset needs no gate.",  # no gate named: not this pin's business
        "They also skip without U64_ALLOW_MUTATE because they reset the C64, "
        "stricter than the contract.",
        # corpus near-misses for the round-1 requirement shapes, verbatim
        # tests/test_u64_capabilities_live.py
        "which reset the C64.  They also skip without ``U64_ALLOW_MUTATE``: stricter\n"
        "than the contract, which covers config changes only (#333).",
        # tests/test_ultimate64_helpers_live.py
        "(turbo flip) runs only when ``U64_ALLOW_MUTATE`` is also set.",
        "Set U64_ALLOW_MUTATE before any test that writes device config.",
    ], ids=["narrow", "allowed", "streams-config", "ram-config", "not-for-the-reset",
            "stricter", "no-gate-named", "r1-skip-without-but-stricter",
            "r1-corpus-capabilities", "r1-corpus-helpers-only-when", "r1-set-before-config"])
    def test_a_narrow_sentence_is_not_caught(self, text: str) -> None:
        assert wider_contract_claims(text) == []
