"""The c64-test skill states the UCI + ``Cartridge Preference = External`` pitfall (#393).

Measured in #359 (U64E, fw 3.15, paired ABBAAB, n=3 per arm): with
``Cartridge Preference`` = External the UCI identifier is absent from ``$DF1D``
after ``enable_uci`` + ``reset()`` + settle, and every routine timed out, while
``Command Interface`` still read Enabled; with Auto the identifier read ``$C9``
and every routine completed.  PR #394 turned the timeout into
``UCIInterfaceAbsentError``.  The skill's RR-Net recipe *sets* External, so an
agent following it and then running UCI hits exactly this.

Pinned per file, on the paragraph or list item that names the error (or, for
PATTERNS.md, on the "Hardware RR-Net on the U64" section), because a token
anywhere in a 1500-line file proves nothing:

* **SKILL.md** pitfall: the cause (External / an external cartridge holding
  the bus), the error, the remedy (Auto, then ``reset()`` and a settle), that
  RR-Net and UCI runs cannot share a session without it, #359, and that
  ``$C9`` does not rule out a UCI STATE-bit wedge (``uci_wedge_probe``);
* **REFERENCE.md** ``uci_network`` section: the class (a ``UCIError``), both
  attributes, the per-routine ``$DF1D`` read and its zero ``/Temp`` cost, and
  that ``uci_probe`` does not pre-check;
* **PATTERNS.md** RR-Net section: putting Auto back before any UCI use, naming
  the error.

And one absence rule over the three files: no sentence says the identifier
check (or ``$C9``) *rules out*, *proves the absence of*, or *clears* a wedge,
or that ``$C9`` means the UCI is *healthy*.  The check reads only the
identifier register; a STATE-bit wedge lives in ``$DF1C`` (``uci_wedge_probe``).

**Declared limits:** the presence pins are token checks inside the located
unit, so a unit that carries every token but garbles the claim passes; the
absence rule matches only the verb phrases listed in :data:`_OVERCLAIM`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SKILL_DIR = _REPO / ".claude" / "skills" / "c64-test"
FILES = {name: _SKILL_DIR / name for name in ("SKILL.md", "REFERENCE.md", "PATTERNS.md")}

ERROR = "UCIInterfaceAbsentError"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", "")).strip()


def _units(text: str) -> list[str]:
    """Paragraphs, with each Markdown list item its own unit."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        items = re.split(r"\n(?=\s*(?:[-*]|\d+\.)\s)", para)
        out.extend(_flat(i) for i in items if i.strip())
    return out


def _units_naming(text: str, token: str) -> list[str]:
    return [u for u in _units(text) if token in u]


def _section(text: str, heading: str) -> str:
    """From a ``###`` heading to the next ``##``/``###`` heading."""
    start = text.index(heading)
    nxt = re.search(r"\n#{2,3} ", text[start + len(heading):])
    end = start + len(heading) + nxt.start() if nxt else len(text)
    return text[start:end]


#: (file, unit locator, tokens the unit must carry).
SKILL_TOKENS = ("External", "Auto", "reset()", "settle", "RR-Net", "#359",
                "Command Interface", "STATE-bit", "uci_wedge_probe", "$C9")
REFERENCE_TOKENS = ("UCIError", "identifier", "cartridge_preference", "$DF1D",
                    "bodyless", "/Temp", "uci_probe")
PATTERNS_RRNET_HEADING = "### Hardware RR-Net on the U64"
PATTERNS_TOKENS = (ERROR, "Auto", "UCI", "reset()")


def missing_tokens(unit_texts: list[str], tokens: tuple[str, ...]) -> list[str]:
    """Tokens no single unit carries in full: the claim must be in one place."""
    best: list[str] = list(tokens)
    for unit in unit_texts:
        miss = [t for t in tokens if t not in unit]
        if len(miss) < len(best):
            best = miss
    return best


_OVERCLAIM = re.compile(
    r"(?:\$?C9|identifier(?: check)?)[^.;]{0,80}?"
    # "does not rule out" / "never clears" / "doesn't exclude" are the correct limit.
    r"(?<!not )(?<!never )(?<!n't )"
    r"\b(?:rules? out|proves? (?:there is )?no|clears?|excludes?)\b[^.;]{0,40}?wedge"
    r"|\$?C9[^.;]{0,40}?\bmeans?\b[^.;]{0,30}?\bhealthy\b",
    re.IGNORECASE,
)


def overclaims(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", _flat(text)) if _OVERCLAIM.search(s)]


def test_the_skill_files_exist() -> None:
    for name, path in FILES.items():
        assert path.is_file(), name


def test_skill_md_states_the_pitfall_in_one_item() -> None:
    units = _units_naming(FILES["SKILL.md"].read_text(encoding="utf-8"), ERROR)
    assert units, f"SKILL.md names no {ERROR}"
    assert missing_tokens(units, SKILL_TOKENS) == []


def test_reference_md_documents_the_error_in_the_uci_section() -> None:
    text = FILES["REFERENCE.md"].read_text(encoding="utf-8")
    section = text[text.index("## Module: uci_network"):]
    section = section[:section.index("\n## ", 1)] if "\n## " in section[1:] else section
    units = _units_naming(section, ERROR)
    assert units, f"REFERENCE.md's uci_network section names no {ERROR}"
    assert missing_tokens(units, REFERENCE_TOKENS) == []


def test_patterns_rrnet_section_requires_auto_before_uci() -> None:
    section = _section(FILES["PATTERNS.md"].read_text(encoding="utf-8"), PATTERNS_RRNET_HEADING)
    units = _units_naming(section, ERROR)
    assert units, f"PATTERNS.md's RR-Net section names no {ERROR}"
    assert missing_tokens(units, PATTERNS_TOKENS) == []


@pytest.mark.parametrize("name", list(FILES))
def test_no_file_says_the_identifier_rules_out_a_wedge(name: str) -> None:
    assert overclaims(FILES[name].read_text(encoding="utf-8")) == []


class TestThePinCanFail:
    def test_a_token_split_across_units_is_missing(self) -> None:
        units = ["UCIInterfaceAbsentError: set Auto.", "Then reset() and settle."]
        assert missing_tokens(units, ("Auto", "reset()")) == ["reset()"]
        assert missing_tokens(["Auto, then reset()."], ("Auto", "reset()")) == []

    def test_list_items_are_separate_units(self) -> None:
        text = "- one UCIInterfaceAbsentError\n- two Auto\n\nthree"
        assert _units(text) == ["- one UCIInterfaceAbsentError", "- two Auto", "three"]

    def test_the_section_stops_at_the_next_heading(self) -> None:
        text = "### Hardware RR-Net on the U64\nin\n### Next\nout"
        assert "out" not in _section(text, "### Hardware RR-Net on the U64")
        assert "in" in _section(text, "### Hardware RR-Net on the U64")

    @pytest.mark.parametrize("text", [
        "A $C9 at $DF1D rules out a STATE-bit wedge.",
        "The identifier check proves there is no wedge.",
        "Reading C9 means the UCI is healthy.",
        "The identifier check clears a wedged device.",
    ])
    def test_an_overclaim_is_flagged(self, text: str) -> None:
        assert overclaims(text), text

    @pytest.mark.parametrize("text", [
        "A $C9 at $DF1D does not rule out a UCI STATE-bit wedge.",
        "$C9 only means the slot is on the bus; run uci_wedge_probe for the STATE bits.",
        "The identifier check costs one bodyless GET.",
    ])
    def test_the_correct_limit_is_not_an_overclaim(self, text: str) -> None:
        assert overclaims(text) == [], text
