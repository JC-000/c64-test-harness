"""The docs publish ``write_bytes`` throughput, scoped to where it was measured (#267).

Until #267 four sites said the only non-leaking bulk-write path was
"untimed".  It has since been measured on the U64E (fw 3.15, bce4535e,
2026-09-15, host on Wi-Fi (en0), link not instrumented, write-verified,
n=2-4 per arm, interleaved): about 1.3 KiB/s at 48-byte chunks and about
3.2 KiB/s at 128-byte chunks, median ~36-52 ms per PUT, observed 34-79 ms.
The per-PUT time barely moves with chunk size, so it is dominated by the
host link and per-request overhead, not the payload.  A single 16 KiB POST
took ~0.1 s on that device.

Pinned per site, on the list item that carries the claim (every site is a
bullet or numbered item, so the extractor bounds one item, not a
blank-line paragraph that would swallow the whole list):

* the "untimed" / "no published timing" claims are gone -- and, file-wide,
  no other skill file, README.md or docs/ page reintroduces them;
* the load-bearing sentences are present verbatim (flattened), with the
  numbers bound to their chunk sizes, so swapped arms or scaled numbers
  fail -- not just the presence of tokens;
* the conditions (device, build, date, link, sample size) travel with them;
* the C64U is never stated as measured: the 128-byte arm is the chunk it
  uses, not its number, every site says so, and no site inverts that.

The inversion check is a word list (INVERTERS) and cannot know every
phrasing that reverses the disclaimer, so each site's own disclaimer
sentence is also pinned verbatim in SITE_SENTENCES; a rewording of it
fails there even when no listed word appears.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
SKILL = _REPO / ".claude" / "skills" / "c64-test"

#: (label, file, a phrase unique to the item that carries the claim)
SITES = [
    ("PATTERNS rule 5", SKILL / "PATTERNS.md", "**Prefer the routes that do not leak**"),
    ("PATTERNS cliff", SKILL / "PATTERNS.md", "**REST `POST writemem` cliff (C64U)**"),
    ("SKILL non-leaking", SKILL / "SKILL.md", "**Prefer the non-leaking paths.**"),
    # REFERENCE.md lists write_memory three times; only the protocol-method
    # entry carries the SocketDMA / bulk-write note, so anchor on that.
    ("REFERENCE write_memory", SKILL / "REFERENCE.md",
     "`write_memory(addr, data, *, override=None) -> None` -- **Do not enable SocketDMA writes"),
]

RETIRED = ("untimed", "no published timing")
MEASURED = ("U64E", "bce4535e", "2026-09-15")
NOT_C64U = "not measured on the C64U"

#: Load-bearing sentences, as regexes over the flattened item.  A figure is
#: anchored so it cannot sit inside a larger number (1.3 vs 11.3 / 13).
_NUM = r"(?<![\d.])"
LOAD_BEARING = {
    "48-byte rate": _NUM + r"1\.3 KiB/s at 48-byte chunks",
    "128-byte rate": _NUM + r"3\.2 KiB/s at 128-byte chunks",
    "POST figure": r"a single 16 KiB POST took ~0\.1 s",
    "per-PUT latency": _NUM + r"median ~36-52 ms per PUT, observed 34-79 ms",
    "link": r"host on Wi-Fi \(en0\), link not instrumented",
    "sample size": r"n=2-4 per arm, interleaved",
    "link dominates": r"(?<!not )dominated by the host link",
}

#: Per-site sentences: the device bound to its build, and the site's own
#: C64U disclaimer verbatim (anchored to what follows it, or to the item's
#: end), so an inversion the INVERTERS list does not know still fails.
SITE_SENTENCES = {
    "PATTERNS rule 5": {
        "device attribution": r"Measured on the U64E \(fw 3\.15, bce4535e, 2026-09-15",
        "disclaimer": (r"The 128-byte arm is the chunk a C64U uses, but its per-request "
                       r"latency over its own link is not measured on the C64U, so do not "
                       r"read 3\.2 KiB/s as that device's rate\.\s*$"),
    },
    "PATTERNS cliff": {
        "device attribution": (r"the only timing is from the U64E\. Measured there "
                               r"\(fw 3\.15, bce4535e, 2026-09-15"),
        "disclaimer": (r"It is not measured on the C64U: the 128-byte chunk is the same arm, "
                       r"but the latency over that device's link is unknown, so budget for "
                       r"it being slow and do not assume it beats the ~6 s cliff\.\s*$"),
    },
    "SKILL non-leaking": {
        "device attribution": r"Measured on the U64E \(fw 3\.15, bce4535e, 2026-09-15",
        "disclaimer": (r"\. The rate is not measured on the C64U, whose link latency is "
                       r"unknown, so budget for it being slow\. REST POST is the leaking path"),
    },
    "REFERENCE write_memory": {
        "device attribution": r"Measured on the U64E \(fw 3\.15, bce4535e, 2026-09-15",
        "single request": r"on a post-safe U64E `write_memory` itself is a single request\.",
        "disclaimer": r"single request\. The rate is not measured on the C64U\. What the disabled path does",
    },
}

#: Item-wide claims that would put the U64E figure on the C64U, or the link
#: back on a wire nobody measured.
REFUSED_IN_ITEM = (
    "c64u's rate",
    "c64u's throughput",
    "applies to the c64u",
    "wired",
)

#: Words that invert the disclaimer when they sit near it.
INVERTERS = ("no longer", "retired", "outdated", "superseded", "was wrong", "not true")
_NEAR = 160

_ITEM_START = re.compile(r"^(\s*)(?:[-*] |\d+\. )")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _item(path: Path, anchor: str) -> str:
    """The list item (bullet or numbered) whose text contains *anchor*.

    From the item's own start line to the line before the next item at the
    same or a shallower indent, a heading, or a blank line.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    hit = next(i for i, ln in enumerate(lines) if anchor in ln)
    start = hit
    while start > 0 and not _ITEM_START.match(lines[start]):
        start -= 1
    m = _ITEM_START.match(lines[start])
    assert m, f"{anchor!r} is not inside a list item in {path.name}"
    indent = len(m.group(1))
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if not ln.strip() or ln.lstrip().startswith("#"):
            break
        nxt = _ITEM_START.match(ln)
        if nxt and len(nxt.group(1)) <= indent:
            break
        end += 1
    return _flat("\n".join(lines[start:end]))


def _inverted_disclaimers(text: str) -> list[str]:
    """Inverting words within ``_NEAR`` chars of any C64U disclaimer."""
    low = text.lower()
    hits = []
    for m in re.finditer(re.escape(NOT_C64U.lower()), low):
        window = low[max(0, m.start() - _NEAR): m.end() + _NEAR]
        hits += [w for w in INVERTERS if w in window]
    return hits


@pytest.mark.parametrize("label,path,anchor", SITES, ids=[s[0] for s in SITES])
class TestEachSite:
    def test_retired_claims_are_gone(self, label, path, anchor):
        item = _item(path, anchor).lower()
        for phrase in RETIRED:
            assert phrase not in item, f"{label}: still says {phrase!r}"

    def test_figures_carry_their_conditions(self, label, path, anchor):
        item = _item(path, anchor)
        for token in MEASURED:
            assert token in item, f"{label}: missing {token!r}"

    @pytest.mark.parametrize("name", sorted(LOAD_BEARING))
    def test_load_bearing_sentence(self, label, path, anchor, name):
        item = _item(path, anchor)
        assert re.search(LOAD_BEARING[name], item), (
            f"{label}: {name} sentence missing or altered "
            f"(want /{LOAD_BEARING[name]}/)")

    def test_site_sentences(self, label, path, anchor):
        item = _item(path, anchor)
        for name, pattern in SITE_SENTENCES[label].items():
            assert re.search(pattern, item), (
                f"{label}: {name} sentence missing or altered (want /{pattern}/)")

    def test_c64u_is_not_claimed_measured(self, label, path, anchor):
        item = _item(path, anchor)
        assert NOT_C64U in item, f"{label}: missing {NOT_C64U!r}"
        low = item.lower()
        for phrase in REFUSED_IN_ITEM:
            assert phrase not in low, f"{label}: says {phrase!r}"
        assert not _inverted_disclaimers(item), (
            f"{label}: C64U disclaimer inverted by {_inverted_disclaimers(item)}")


def _scan_files() -> list[Path]:
    files = sorted(SKILL.glob("*.md")) + [_REPO / "README.md"]
    files += sorted((_REPO / "docs").rglob("*.md"))
    return files


def _retired_hits(text: str) -> list[str]:
    """'untimed' within ``_NEAR`` chars of write_bytes, or 'no published timing'."""
    flat = _flat(text).lower()
    hits = []
    if "no published timing" in flat:
        hits.append("no published timing")
    for m in re.finditer("untimed", flat):
        if "write_bytes" in flat[max(0, m.start() - _NEAR): m.end() + _NEAR]:
            hits.append(flat[max(0, m.start() - 60): m.end() + 60])
    return hits


def test_scan_covers_the_skill_files_readme_and_docs():
    """Vacuity guard: the file-wide scan actually reads the doc set."""
    names = {p.name for p in _scan_files()}
    assert {"SKILL.md", "PATTERNS.md", "REFERENCE.md", "README.md"} <= names
    assert any(p.parent.name == "docs" for p in _scan_files())


def test_retired_scanner_detects_both_shapes():
    """Vacuity guard: the scanner fires on the retired wording."""
    assert _retired_hits("the `write_bytes` route is\n  untimed (issue #267)")
    assert _retired_hits("there is no published\ntiming anywhere")
    assert not _retired_hits("the stream is untimed " + "x" * 400 + " write_bytes")


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: str(p.relative_to(_REPO)))
def test_no_doc_reintroduces_untimed_write_bytes(path):
    hits = _retired_hits(path.read_text(encoding="utf-8"))
    assert not hits, f"{path.relative_to(_REPO)}: retired timing claim {hits}"


def test_inversion_detector_fires():
    """Vacuity guard: an inverted disclaimer is caught."""
    assert _inverted_disclaimers(
        "the rate is not measured on the C64U -- that caveat is no longer true")
    assert not _inverted_disclaimers("the rate is not measured on the C64U.")


@pytest.mark.parametrize("label,path,anchor,sibling", [
    ("PATTERNS rule 5", SKILL / "PATTERNS.md", SITES[0][2], "**Where you drive the protocol decides your exposure.**"),
    ("SKILL non-leaking", SKILL / "SKILL.md", SITES[2][2], "**Do not assume an API chunks"),
])
def test_item_extractor_stops_at_the_next_sibling(label, path, anchor, sibling):
    """Vacuity guard: one item, not the list it sits in."""
    item = _item(path, anchor)
    assert anchor.strip("*") in item
    assert sibling not in item, f"{label}: extractor ran into the next item"
