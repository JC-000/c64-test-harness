"""README and the c64-test skill state the acquire budget and the grace (#302).

#233 made ``U64_DEVICE_LOCK_TIMEOUT`` the acquire budget and added the
periodic progress line and ``on_wait``; #273/#277 capped a self-held wait at
``_SELF_HELD_WAIT_GRACE``.  ``docs/device_locking.md`` carries all of it,
but README and the skill -- what a person or agent reads before writing a
test -- said nothing, and still presented widening ``lock_timeout`` in code
as the only knob.

What this pins, per file and **per paragraph** (a word in one paragraph
and its qualifier in another does not count):

* **budget** -- a paragraph naming ``U64_DEVICE_LOCK_TIMEOUT`` says it is a
  budget and not a gate, and cites ``docs/device_locking.md``.  In README
  and SKILL.md the same paragraph states both defaults, derived from
  ``DEFAULT_ACQUIRE_TIMEOUT`` and ``unified_manager.DEFAULT_LOCK_TIMEOUT``,
  so changing a constant without the prose fails here.
* **progress** -- a paragraph naming ``on_wait`` also mentions the
  progress line.
* **grace** -- a paragraph about another thread rescuing a self-held wait
  states the grace *value* (from the constant; the bare name is not a
  value) and cites ``docs/device_locking.md``.

* **no invented ceiling** -- no paragraph calls 120 s a ceiling.  Nothing
  in ``device_lock.py`` or ``unified_manager.py`` defines one, and it
  contradicted the budget text (review round 1).

In README and SKILL.md each default is **bound to its call**: the figure
must be the first number after ``acquire(...)`` / ``create_manager(...)``,
within 60 non-digit characters, so swapping the two figures fails.

**Scope, stated rather than implied.**  This pins the presence and binding
of the claims above and nothing else.  Any *other* claim added to these
paragraphs (a wrong NaN rule, a wrong progress interval) passes here;
``docs/device_locking.md`` is the source of truth and
``test_device_lock_timeout_budget.py`` pins that document.

Vacuity guard: every scanned file exists and is non-empty.  Positive
control: :class:`TestThePinCanFail` strips each qualifier from a
passing paragraph and requires the checker to flag it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from c64_test_harness.backends import device_lock as dl
from c64_test_harness.backends import unified_manager as um

_REPO = Path(__file__).resolve().parent.parent
_SKILL = _REPO / ".claude" / "skills" / "c64-test"

ENV = "U64_DEVICE_LOCK_TIMEOUT"
CITE = "docs/device_locking.md"
GRACE = dl._SELF_HELD_WAIT_GRACE

#: label -> (path, whether the budget paragraph must state both defaults)
SCANNED: dict[str, tuple[Path, bool]] = {
    "README.md": (_REPO / "README.md", True),
    "skill/SKILL.md": (_SKILL / "SKILL.md", True),
    "skill/PATTERNS.md": (_SKILL / "PATTERNS.md", False),
}

_RESCUE = re.compile(r"another thread|other thread|cross-thread|rescue", re.IGNORECASE)


def _paragraphs(text: str) -> list[str]:
    paras, cur = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            cur.append(line)
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def _seconds(value: float) -> re.Pattern[str]:
    """``2.0 s`` / ``2 s`` / ``2.0s`` for 2.0; ``30 s`` / ``30.0 s`` for 30."""
    whole = f"{value:g}"
    return re.compile(rf"(?<![\d.]){re.escape(whole)}(?:\.0+)?\s?(?:s\b|seconds?\b)")


def _bound(call: str, value: float) -> re.Pattern[str]:
    """*value* in seconds as the first figure after ``call(...)``.

    At most 60 non-digit characters between the call and the figure: no other number may sit
    in between, so ``acquire() waits 60 s ... create_manager() 30 s`` does
    not bind 30 s to ``acquire``.
    """
    whole = f"{value:g}"
    return re.compile(
        rf"\b{re.escape(call)}\([^)]*\)[^\d]{{0,60}}"
        rf"(?<![\d.]){re.escape(whole)}(?:\.0+)?\s?(?:s\b|seconds?\b)"
    )


_INVENTED_CEILING = re.compile(
    r"(?<![\d.])120(?:\.0+)?\s?s\b[^.]{0,40}\bceiling|\bceiling[^.]{0,40}(?<![\d.])120(?:\.0+)?\s?s\b",
    re.IGNORECASE,
)


def doc_problems(text: str, *, defaults: bool) -> list[str]:
    paras = _paragraphs(text)
    problems = []

    budget = [p for p in paras if ENV in p]
    if not budget:
        problems.append(f"never names {ENV}")
    else:
        def ok(p: str) -> bool:
            if not ("budget" in p.lower() and "not a gate" in p.lower() and CITE in p):
                return False
            if defaults:
                return bool(
                    _bound("acquire", dl.DEFAULT_ACQUIRE_TIMEOUT).search(p)
                    and _bound("create_manager", um.DEFAULT_LOCK_TIMEOUT).search(p)
                )
            return True

        if not any(ok(p) for p in budget):
            want = "budget, not a gate, cites " + CITE
            if defaults:
                want += (
                    f", {dl.DEFAULT_ACQUIRE_TIMEOUT:g} s and "
                    f"{um.DEFAULT_LOCK_TIMEOUT:g} s defaults"
                )
            problems.append(f"no {ENV} paragraph says: {want}")

    if not any("on_wait" in p and "progress" in p.lower() for p in paras):
        problems.append("no paragraph names on_wait together with the progress line")

    if not any(
        _RESCUE.search(p) and _seconds(GRACE).search(p) and CITE in p for p in paras
    ):
        problems.append(
            f"no cross-thread rescue paragraph states the {GRACE:g} s grace and cites {CITE}"
        )
    for p in paras:
        if _INVENTED_CEILING.search(p):
            problems.append(f"claims a 120 s ceiling nothing defines: {p[:160]}")
    return problems


class TestReadmeAndSkillStateTheBudgetAndGrace:
    @pytest.mark.parametrize("label", list(SCANNED))
    def test_doc_states_budget_progress_and_grace(self, label: str) -> None:
        path, defaults = SCANNED[label]
        problems = doc_problems(path.read_text(encoding="utf-8"), defaults=defaults)
        assert not problems, f"{label} (#302):\n  " + "\n  ".join(problems)

    @pytest.mark.parametrize("label", list(SCANNED))
    def test_vacuity_guard_scanned_file_exists(self, label: str) -> None:
        path, _ = SCANNED[label]
        assert path.is_file() and path.stat().st_size > 1000, f"{label} missing or empty"

    def test_the_scanned_set_is_pinned(self) -> None:
        """Dropping an entry from SCANNED shrinks the parametrize set silently.

        Found by mutation (M6 survived round 1): with PATTERNS.md removed
        from the dict, every remaining test still passed.
        """
        assert {label: defaults for label, (_, defaults) in SCANNED.items()} == {
            "README.md": True,
            "skill/SKILL.md": True,
            "skill/PATTERNS.md": False,
        }


class TestThePinCanFail:
    """Positive control: stripping any one qualifier is flagged."""

    PASSING = "\n\n".join(
        [
            f"The acquire budget comes from {ENV}: a budget, not a gate. "
            f"Unset, acquire() waits {dl.DEFAULT_ACQUIRE_TIMEOUT:g} s and "
            f"create_manager() {um.DEFAULT_LOCK_TIMEOUT:g} s. See {CITE}.",
            "A blocked acquire logs a periodic progress line; pass on_wait=cb.",
            f"Another thread may rescue a self-held wait within {GRACE:.1f} s "
            f"only; see {CITE}.",
        ]
    )

    def test_the_passing_text_passes(self) -> None:
        assert doc_problems(self.PASSING, defaults=True) == []

    @pytest.mark.parametrize(
        "old, expect",
        [
            (ENV, "never names"),
            ("a budget, not a gate", "budget, not a gate"),
            (f"waits {dl.DEFAULT_ACQUIRE_TIMEOUT:g} s", "defaults"),
            (f"create_manager() {um.DEFAULT_LOCK_TIMEOUT:g} s", "defaults"),
            (f"See {CITE}.", "budget, not a gate, cites"),
            ("on_wait=cb", "on_wait"),
            ("progress line", "on_wait"),
            (f"{GRACE:.1f} s", "grace"),
            (f"only; see {CITE}", "grace"),
            ("Another thread may rescue", "grace"),
        ],
    )
    def test_each_stripped_qualifier_is_flagged(self, old: str, expect: str) -> None:
        mangled = self.PASSING.replace(old, "XXXX")
        assert mangled != self.PASSING, f"the mangle {old!r} changed nothing"
        problems = doc_problems(mangled, defaults=True)
        assert any(expect in p for p in problems), problems

    def test_a_drifted_default_is_flagged(self) -> None:
        drifted = self.PASSING.replace(
            f"waits {dl.DEFAULT_ACQUIRE_TIMEOUT:g} s",
            f"waits {dl.DEFAULT_ACQUIRE_TIMEOUT * 2:g} s",
        )
        assert drifted != self.PASSING
        assert any("defaults" in p for p in doc_problems(drifted, defaults=True))

    def test_swapped_defaults_are_flagged(self) -> None:
        """Review round 1: numbers present but bound to the wrong calls."""
        a, m = dl.DEFAULT_ACQUIRE_TIMEOUT, um.DEFAULT_LOCK_TIMEOUT
        assert a != m, "the swap control needs two different defaults"
        swapped = self.PASSING.replace(
            f"acquire() waits {a:g} s and create_manager() {m:g} s",
            f"acquire() waits {m:g} s and create_manager() {a:g} s",
        )
        assert swapped != self.PASSING, "the swap changed nothing"
        assert any("defaults" in p for p in doc_problems(swapped, defaults=True))

    @pytest.mark.parametrize("call", ["acquire()", "create_manager()"])
    def test_a_wrong_first_figure_is_flagged_per_call(self, call: str) -> None:
        """The figure must be the *first* one after the call.

        Isolates each half of the binding: the swap control above is killed
        by either half alone, so a binding that let ``acquire()`` reach past
        a wrong figure to the right one survived it (mutation M11).
        """
        value = {"acquire()": dl.DEFAULT_ACQUIRE_TIMEOUT,
                 "create_manager()": um.DEFAULT_LOCK_TIMEOUT}[call]
        right = f"{call} waits {value:g} s" if call == "acquire()" else f"{call} {value:g} s"
        wrong = right.replace(f"{value:g} s", f"{value * 3:g} s (formerly {value:g} s)")
        mangled = self.PASSING.replace(right, wrong)
        assert mangled != self.PASSING, f"the mangle for {call} changed nothing"
        assert any("defaults" in p for p in doc_problems(mangled, defaults=True))

    @pytest.mark.parametrize(
        "claim",
        [
            "default 60 s and 120 s ceiling for ad-hoc work.",
            "120 s is a reasonable ceiling for ad-hoc work.",
            "a ceiling of 120.0 s applies.",
        ],
    )
    def test_an_invented_ceiling_is_flagged(self, claim: str) -> None:
        planted = self.PASSING + "\n\n" + claim
        assert any("ceiling" in p for p in doc_problems(planted, defaults=True))
        # ...while a plain 120 s timeout in an example is not a claim.
        assert doc_problems(
            self.PASSING + "\n\nlock.acquire_or_raise(timeout=120.0)", defaults=True
        ) == []

    def test_the_bare_constant_name_is_not_the_grace(self) -> None:
        named = self.PASSING.replace(f"{GRACE:.1f} s", "_SELF_HELD_WAIT_GRACE")
        assert named != self.PASSING
        assert any("grace" in p for p in doc_problems(named, defaults=True))

    def test_qualifiers_in_separate_paragraphs_do_not_count(self) -> None:
        split = self.PASSING.replace(": a budget, not a gate.", ".\n\nA budget, not a gate.")
        assert split != self.PASSING
        assert any("budget, not a gate" in p for p in doc_problems(split, defaults=True))

    def test_defaults_not_required_where_not_asked(self) -> None:
        no_numbers = self.PASSING.replace(
            f"Unset, acquire() waits {dl.DEFAULT_ACQUIRE_TIMEOUT:g} s and "
            f"create_manager() {um.DEFAULT_LOCK_TIMEOUT:g} s. ",
            "",
        )
        assert no_numbers != self.PASSING
        assert doc_problems(no_numbers, defaults=False) == []
        assert any("defaults" in p for p in doc_problems(no_numbers, defaults=True))
