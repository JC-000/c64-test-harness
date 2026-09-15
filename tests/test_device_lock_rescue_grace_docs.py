"""The cross-thread rescue is documented as supported *within the grace* (#277).

``DeviceLock.acquire`` caps a wait on a lock its own thread holds at
``_SELF_HELD_WAIT_GRACE`` (issue #273).  Another thread may still release
the holder mid-wait -- a supported pattern -- but only inside that grace:
past it the acquire logs a WARNING naming the cap and gives up.  The owner
decided (2026-09-14, #277 option 1) to keep the constant and state the
budget wherever the pattern is called supported, rather than add an
opt-out.  A supported pattern with an undocumented time limit is the
shape that surprises somebody at 3 a.m.

What this pins, per paragraph that calls the rescue supported:

* the grace **value**, derived from the constant (so changing the
  constant without the prose fails here, and the bare name
  ``_SELF_HELD_WAIT_GRACE`` does not count -- a name is not a budget);
* the **WARNING** emitted past it; and
* the **cap** on the caller's timeout.

A presence-only pin would pass against a file that says "supported" in
one paragraph and "within 2.0 s" in another, so the check is per
paragraph.  The vacuity guard is :class:`TestTheClaimSitesExist`: the
acquire docstring and ``docs/device_locking.md`` must each describe the
pattern at least once, or every per-paragraph check passes on nothing.
:class:`TestThePinCanFail` is the positive control -- the checker must
flag each stripped-down variant of a qualified paragraph.

Known residue, stated rather than hidden: a paraphrase that avoids the
word "supported" (e.g. "a helper thread may rescue it") is not a claim
site for this pin.  The pin is about the word the owner's decision names.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from c64_test_harness.backends import device_lock as dl
from c64_test_harness.backends.device_lock import DeviceLock

_REPO = Path(__file__).resolve().parent.parent

#: Files that describe, or could come to describe, the rescue pattern.
#: The first two must describe it (see TestTheClaimSitesExist); the rest
#: are scanned so a future unqualified claim there fails too.
SCANNED: dict[str, Path] = {
    "device_lock.py": _REPO / "src" / "c64_test_harness" / "backends" / "device_lock.py",
    "docs/device_locking.md": _REPO / "docs" / "device_locking.md",
    "README.md": _REPO / "README.md",
    "skill/SKILL.md": _REPO / ".claude" / "skills" / "c64-test" / "SKILL.md",
    "skill/PATTERNS.md": _REPO / ".claude" / "skills" / "c64-test" / "PATTERNS.md",
    "skill/REFERENCE.md": _REPO / ".claude" / "skills" / "c64-test" / "REFERENCE.md",
}

#: The subject: "supported" in a paragraph that is about a thread
#: releasing/rescuing a self-held wait.
_SUPPORTED = re.compile(r"\bsupported\b", re.IGNORECASE)
_RESCUE_SUBJECT = re.compile(
    r"rescue|another thread|second thread|other thread|cross-thread|"
    r"helper thread|releases? it",
    re.IGNORECASE,
)


def _grace_value_re(grace: float) -> re.Pattern[str]:
    """Match the grace value as prose writes it: ``2.0 s``, ``2 s``, ``2.0s``."""
    whole = f"{grace:g}"
    if float(whole).is_integer() and "." not in whole:
        number = re.escape(whole) + r"(?:\.0+)?"
    else:
        number = re.escape(whole) + r"0*"
    return re.compile(rf"(?<![\d.]){number}\s?(?:s\b|seconds?\b)")


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs, with ``#``/``#:`` comment markers stripped."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^#:?\s?", "", line)
        lines.append(line)
    paras, cur = [], []
    for line in lines:
        if line:
            cur.append(line)
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def claim_paragraphs(text: str) -> list[str]:
    return [
        p for p in _paragraphs(text)
        if _SUPPORTED.search(p) and _RESCUE_SUBJECT.search(p)
    ]


def missing_qualifiers(paragraph: str, grace: float) -> list[str]:
    """What a supported-rescue paragraph fails to say about the grace."""
    missing = []
    if not _grace_value_re(grace).search(paragraph):
        missing.append(f"the grace value ({grace:g} s)")
    if "WARNING" not in paragraph:
        missing.append("the WARNING past the grace")
    if not re.search(r"\bcap(?:s|ped|ping)?\b", paragraph, re.IGNORECASE):
        missing.append("that the timeout is capped")
    return missing


GRACE = dl._SELF_HELD_WAIT_GRACE


class TestEverySupportClaimStatesTheGrace:
    @pytest.mark.parametrize("label", list(SCANNED))
    def test_no_unqualified_support_claim(self, label: str) -> None:
        path = SCANNED[label]
        if not path.exists():
            pytest.skip(f"{label} absent from this checkout")
        bad = []
        for para in claim_paragraphs(path.read_text(encoding="utf-8")):
            missing = missing_qualifiers(para, GRACE)
            if missing:
                bad.append(f"- missing {', '.join(missing)}:\n    {para[:300]}")
        assert not bad, (
            f"{label} calls the cross-thread rescue supported without the "
            f"grace nearby (#277):\n" + "\n".join(bad)
        )


class TestTheClaimSitesExist:
    """Vacuity guard: the per-paragraph pin must have something to check."""

    def test_acquire_docstring_describes_the_rescue_within_the_grace(self) -> None:
        doc = inspect.getdoc(DeviceLock.acquire) or ""
        claims = claim_paragraphs(doc)
        assert claims, "DeviceLock.acquire's docstring no longer describes the rescue"
        assert all(not missing_qualifiers(p, GRACE) for p in claims)

    def test_device_locking_doc_describes_the_rescue_within_the_grace(self) -> None:
        text = SCANNED["docs/device_locking.md"].read_text(encoding="utf-8")
        claims = claim_paragraphs(text)
        assert claims, (
            "docs/device_locking.md does not describe the cross-thread rescue "
            "at all -- #277 requires it there, with its grace"
        )


class TestThePinCanFail:
    """Positive control: stripped variants of a qualified paragraph are flagged."""

    QUALIFIED = (
        "Another thread may release the holder mid-wait; that rescue is "
        f"supported within the grace of {GRACE:.1f} s. Past it the acquire logs "
        "a WARNING and the caller's timeout is capped, so it returns False."
    )

    def test_the_qualified_paragraph_passes(self) -> None:
        assert claim_paragraphs(self.QUALIFIED) == [self.QUALIFIED]
        assert missing_qualifiers(self.QUALIFIED, GRACE) == []

    @pytest.mark.parametrize(
        "strip, expect",
        [
            (f"of {GRACE:.1f} s", "the grace value"),
            ("a WARNING and ", "the WARNING"),
            ("is capped", "capped"),
        ],
    )
    def test_each_stripped_qualifier_is_flagged(self, strip: str, expect: str) -> None:
        mangled = self.QUALIFIED.replace(strip, "")
        assert mangled != self.QUALIFIED, f"the mangle {strip!r} changed nothing"
        assert claim_paragraphs(mangled), "the mangle removed the claim itself"
        assert any(expect in m for m in missing_qualifiers(mangled, GRACE))

    def test_a_different_value_does_not_count(self) -> None:
        wrong = self.QUALIFIED.replace(f"{GRACE:.1f} s", f"{GRACE * 10:.1f} s")
        assert wrong != self.QUALIFIED
        assert any("grace value" in m for m in missing_qualifiers(wrong, GRACE))

    def test_the_bare_constant_name_is_not_a_value(self) -> None:
        named = self.QUALIFIED.replace(f"of {GRACE:.1f} s", "of _SELF_HELD_WAIT_GRACE")
        assert named != self.QUALIFIED
        assert any("grace value" in m for m in missing_qualifiers(named, GRACE))

    def test_python_comment_blocks_are_split_into_paragraphs(self) -> None:
        src = (
            "#: the rescue by another thread is supported.\n"
            "#:\n"
            f"#: WARNING, capped, {GRACE:.1f} s.\n"
        )
        claims = claim_paragraphs(src)
        assert len(claims) == 1 and missing_qualifiers(claims[0], GRACE), (
            "a qualifier in a separate comment paragraph must not satisfy the claim"
        )
