"""The docs publish ``write_bytes`` throughput, scoped to where it was measured (#267).

Until #267 four sites said the only non-leaking bulk-write path was
"untimed".  It has since been measured on the U64E (fw 3.15, bce4535e,
2026-09-15, wired LAN, write-verified, n=4 per arm at 1/4 KiB and n=2 at
16 KiB): about 1.3 KiB/s at the 48-byte chunk a post-safe device uses and
about 3.2 KiB/s at 128 bytes, i.e. roughly 35-55 ms per PUT round trip.
A single 16 KiB POST took about 0.1 s on that device.

Pinned per site, on the list item that carries the claim (every site is a
bullet or numbered item, so the extractor bounds one item, not a
blank-line paragraph that would swallow the whole list):

* the "untimed" / "no published timing" claims are gone;
* the figures travel with their conditions (device, build, date);
* the C64U is never stated as measured: the 128-byte arm is the chunk it
  uses, not its number, and every site says so.
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
MEASURED = ("U64E", "bce4535e", "2026-09-15", "KiB/s")
NOT_C64U = "not measured on the C64U"

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

    def test_c64u_is_not_claimed_measured(self, label, path, anchor):
        item = _item(path, anchor)
        assert NOT_C64U in item, f"{label}: missing {NOT_C64U!r}"


@pytest.mark.parametrize("label,path,anchor,sibling", [
    ("PATTERNS rule 5", SKILL / "PATTERNS.md", SITES[0][2], "**Where you drive the protocol decides your exposure.**"),
    ("SKILL non-leaking", SKILL / "SKILL.md", SITES[2][2], "**Do not assume an API chunks"),
])
def test_item_extractor_stops_at_the_next_sibling(label, path, anchor, sibling):
    """Vacuity guard: one item, not the list it sits in."""
    item = _item(path, anchor)
    assert anchor.strip("*") in item
    assert sibling not in item, f"{label}: extractor ran into the next item"
