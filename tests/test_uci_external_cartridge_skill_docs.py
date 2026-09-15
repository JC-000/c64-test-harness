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

**Phrases, not only tokens.**  The remedy ("Auto, then reset() and ...
settle"), the RR-Net/UCI session sentence, the STATE-bit limit and the
REFERENCE attributes are pinned as regexes: in the first mutation round each
survived as a token check, because the unit repeats "reset()", "settle",
"RR-Net" and ``cartridge_preference`` elsewhere (mutants D2, D4, D6, P6).

**Declared limits:** the remaining presence pins are token checks inside the
located unit, so a unit that carries those tokens but garbles the claim
passes; the absence rule matches only the verb phrases in :data:`_OVERCLAIM`,
plus a ``reset()``/``reboot()`` "clears" in a sentence that names the STATE-bit
wedge (``STATE-bit``, ``uci_wedge_probe`` or ``$DF1C``), and "the reset is
required/needed" or "needs a reset()" in a sentence that names Auto or
``Cartridge Preference``.  Round 2 adds phrase needs for the evidence grades:
SKILL's "unmeasured; the reset is a precaution, not a known requirement" and
its source-read note on the external-cartridge cause, and PATTERNS's "(the
measured sequence; see SKILL 15)".  The review-round-1
phrase needs (#359 result, C64U unmeasured, the harness leaves the preference
alone, ``Command Interface`` still Enabled, reset/reboot do not clear the
wedge, zero ``/Temp``, raises before writing, ``uci_probe`` does not pre-check,
"still yours") pin the exact current wording: a rewording must update them.
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


#: The remedy as one phrase: the preference back to Auto, *then* the reset and settle.
REMEDY = re.compile(r"Auto\W{0,3},?\s+then reset\(\) and (?:a )?(?:~?\d+ s )?settle")
SHARING = re.compile(r"RR-Net runs and UCI runs cannot share a device session")
WEDGE_LIMIT = re.compile(r"does not rule out a (?:UCI )?STATE-bit wedge[^.]*uci_wedge_probe")
ATTRIBUTES = re.compile(r"Attributes: identifier \([^)]*\) and cartridge_preference \(")

# Review round 1 (#413): each of these facts survived an outright reversal as a
# token check (the unit keeps "/Temp", "Command Interface", "uci_probe", ...).
RESULT_359 = re.compile(r"Auto gave identifier \$C9 and a completed routine 3/3; "
                        r"External gave neither, 0/3")
NOT_MEASURED_C64U = re.compile(r"Not measured on the C64U\.")
SKILL_NO_CHANGE = re.compile(r"The harness does not change the preference for you")
STILL_ENABLED = re.compile(r"Command Interface still reads Enabled")
DO_NOT_CLEAR = re.compile(r"reset\(\)/reboot\(\) do not clear it")
ZERO_TEMP = re.compile(r"one bodyless GET, so zero /Temp cost")
BEFORE_WRITTEN = re.compile(r"raises before anything is written or typed")
REF_NEVER_CHANGES = re.compile(r"the harness never changes it for you")
NO_PRECHECK = re.compile(r"uci_probe does not pre-check \(check_identifier=False\)")
PATTERNS_RESULT = re.compile(r"0/3 on External against 3/3 on Auto")
STILL_YOURS = re.compile(r"the reset and settle are still yours")
# Review round 2 (#413, reviewer-3 S7/S8/S9/P3b): the evidence grades.
PRECAUTION = re.compile(r"puts the slot back on the bus — unmeasured; "
                        r"the reset is a precaution, not a known requirement")
SOURCE_READ_CAUSE = re.compile(r"\(source-read, c64\.cc ConfigureU64SystemBus: under Auto only a "
                               r"cartridge the detect lines see; #359's RR-Net under Auto did not\)")
MEASURED_SEQUENCE = re.compile(r"then reset\(\) and settle \(the measured sequence; see SKILL 15\)")

#: What the unit that names the error must carry: a literal token or a phrase.
SKILL_TOKENS = ("External", "#359", "Command Interface", "$C9",
                REMEDY, SHARING, WEDGE_LIMIT, RESULT_359, NOT_MEASURED_C64U,
                SKILL_NO_CHANGE, STILL_ENABLED, DO_NOT_CLEAR, PRECAUTION, SOURCE_READ_CAUSE)
REFERENCE_TOKENS = ("UCIError", "$DF1D", "bodyless", "/Temp", "uci_probe", ATTRIBUTES,
                    ZERO_TEMP, BEFORE_WRITTEN, REF_NEVER_CHANGES, NO_PRECHECK)
PATTERNS_RRNET_HEADING = "### Hardware RR-Net on the U64"
PATTERNS_TOKENS = (ERROR, "UCI", REMEDY, STILL_ENABLED, PATTERNS_RESULT, STILL_YOURS,
                   MEASURED_SEQUENCE)


def _has(unit: str, need) -> bool:
    return bool(need.search(unit)) if isinstance(need, re.Pattern) else need in unit


def _label(need) -> str:
    return need.pattern if isinstance(need, re.Pattern) else need


def missing_tokens(unit_texts: list[str], tokens: tuple) -> list[str]:
    """Needs no single unit carries in full: the claim must be in one place."""
    best: list[str] = [_label(t) for t in tokens]
    for unit in unit_texts:
        miss = [_label(t) for t in tokens if not _has(unit, t)]
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

#: A reset verb that clears the STATE-bit wedge.  Gated on a STATE-bit subject in
#: the same sentence, because "A soft reset() clears it" (a hung 6510) and
#: "reboot() clears REU/DMA stuck state" are true elsewhere in PATTERNS/REFERENCE.
_WEDGE_SUBJECT = re.compile(r"STATE-bit|uci_wedge_probe|\$DF1C", re.IGNORECASE)
_RESET_CLEARS = re.compile(
    r"\b(?:reset|reboot)\(\)(?:/(?:reset|reboot)\(\))? (?:also |will |does )?clears?\b",
    re.IGNORECASE,
)


#: The reset after the preference PUT stated as a requirement (review round 2):
#: #359 measured PUT + reset + settle, and by source the PUT alone moves the bus.
#: Gated on the preference being named in the sentence.
_PREFERENCE_SUBJECT = re.compile(r"\bAuto\b|Cartridge Preference", re.IGNORECASE)
_RESET_REQUIRED = re.compile(
    r"\b(?:the )?reset(?:\(\))? (?:is|are|was) (?:still |always )?(?:required|needed|necessary|mandatory)\b"
    r"|\b(?:requires|needs) (?:a )?reset\(\)",
    re.IGNORECASE,
)


def overclaims(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", _flat(text))
            if _OVERCLAIM.search(s) or (_WEDGE_SUBJECT.search(s) and _RESET_CLEARS.search(s))
            or (_PREFERENCE_SUBJECT.search(s) and _RESET_REQUIRED.search(s))]


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


def _real_units() -> dict[str, tuple[list[str], tuple]]:
    ref = FILES["REFERENCE.md"].read_text(encoding="utf-8")
    ref = ref[ref.index("## Module: uci_network"):]
    return {
        "SKILL.md": (_units_naming(FILES["SKILL.md"].read_text(encoding="utf-8"), ERROR),
                     SKILL_TOKENS),
        "REFERENCE.md": (_units_naming(ref, ERROR), REFERENCE_TOKENS),
        "PATTERNS.md": (_units_naming(_section(FILES["PATTERNS.md"].read_text(encoding="utf-8"),
                                               PATTERNS_RRNET_HEADING), ERROR), PATTERNS_TOKENS),
    }


#: Every phrase need, by file.  Stripping it from the real unit must make the
#: pin report it: a need list that silently drops a phrase fails here (P6).
_PHRASE_NEEDS = [("SKILL.md", REMEDY), ("SKILL.md", SHARING), ("SKILL.md", WEDGE_LIMIT),
                 ("SKILL.md", RESULT_359), ("SKILL.md", NOT_MEASURED_C64U),
                 ("SKILL.md", SKILL_NO_CHANGE), ("SKILL.md", STILL_ENABLED),
                 ("SKILL.md", DO_NOT_CLEAR), ("SKILL.md", PRECAUTION),
                 ("SKILL.md", SOURCE_READ_CAUSE),
                 ("REFERENCE.md", ATTRIBUTES), ("REFERENCE.md", ZERO_TEMP),
                 ("REFERENCE.md", BEFORE_WRITTEN), ("REFERENCE.md", REF_NEVER_CHANGES),
                 ("REFERENCE.md", NO_PRECHECK),
                 ("PATTERNS.md", REMEDY), ("PATTERNS.md", STILL_ENABLED),
                 ("PATTERNS.md", PATTERNS_RESULT), ("PATTERNS.md", STILL_YOURS),
                 ("PATTERNS.md", MEASURED_SEQUENCE)]


@pytest.mark.parametrize("name,need", _PHRASE_NEEDS, ids=lambda v: getattr(v, "pattern", v)[:24])
def test_stripping_a_phrase_from_the_real_unit_is_reported(name: str, need: re.Pattern[str]) -> None:
    units, tokens = _real_units()[name]
    assert units, name
    stripped = [need.sub("", u) for u in units]
    # The mangle must have done something, or this proves nothing.
    assert stripped != units, f"{need.pattern!r} matched nothing in {name}"
    assert need.pattern in missing_tokens(stripped, tokens), (
        f"{name}: removing {need.pattern!r} is not reported; is it still in the need list?"
    )


def test_every_phrase_need_has_a_strip_case() -> None:
    # A phrase need with no strip case is never shown to be reportable (T3).
    for name, (_units_, tokens) in _real_units().items():
        for need in tokens:
            if isinstance(need, re.Pattern):
                assert (name, need) in _PHRASE_NEEDS, f"{name}: no strip case for {need.pattern!r}"


@pytest.mark.parametrize("name", list(FILES))
def test_no_file_says_the_identifier_rules_out_a_wedge(name: str) -> None:
    assert overclaims(FILES[name].read_text(encoding="utf-8")) == []


class TestThePinCanFail:
    def test_a_token_split_across_units_is_missing(self) -> None:
        units = ["UCIInterfaceAbsentError: set Auto.", "Then reset() and settle."]
        assert missing_tokens(units, ("Auto", "reset()")) == ["reset()"]
        assert missing_tokens(["Auto, then reset()."], ("Auto", "reset()")) == []

    def test_a_phrase_need_is_not_met_by_its_scattered_tokens(self) -> None:
        scattered = "After reset() and settle, set Auto."
        assert missing_tokens([scattered], (REMEDY,)) == [REMEDY.pattern]
        for ok in ('set_config_item(..., "Auto"), then reset() and a ~3 s settle',
                   "back to Auto, then reset() and settle, before"):
            assert missing_tokens([ok], (REMEDY,)) == [], ok
        assert missing_tokens(["RR-Net and UCI"], (SHARING,)) == [SHARING.pattern]
        assert missing_tokens(["it rules out a UCI STATE-bit wedge (uci_wedge_probe)"],
                              (WEDGE_LIMIT,)) == [WEDGE_LIMIT.pattern]
        assert missing_tokens(["UCIInterfaceAbsentError(identifier, cartridge_preference=None)"],
                              (ATTRIBUTES,)) == [ATTRIBUTES.pattern]

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
        "It does not rule out a STATE-bit wedge (uci_wedge_probe; reboot() clears it).",
        "A UCI STATE-bit wedge in $DF1C: reset()/reboot() clears it.",
        "Set the preference back to Auto; the reset is required before the first routine.",
        "Putting Cartridge Preference back to Auto needs a reset() to take effect.",
    ])
    def test_an_overclaim_is_flagged(self, text: str) -> None:
        assert overclaims(text), text

    @pytest.mark.parametrize("text", [
        "A $C9 at $DF1D does not rule out a UCI STATE-bit wedge.",
        "$C9 only means the slot is on the bus; run uci_wedge_probe for the STATE bits.",
        "The identifier check costs one bodyless GET.",
        "It does not rule out a STATE-bit wedge (uci_wedge_probe; reset()/reboot() do not clear it).",
        "The running program has wedged the CPU. A soft reset() clears it instantly.",
        "reboot() clears REU/DMA stuck state.",
        "Back to Auto, then reset(); the reset is a precaution, not a known requirement.",
        "enable_uci's reset is required: every routine times out at the sentinel without it.",
    ])
    def test_the_correct_limit_is_not_an_overclaim(self, text: str) -> None:
        assert overclaims(text) == [], text
