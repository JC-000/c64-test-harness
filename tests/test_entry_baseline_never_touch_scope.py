"""``BASELINE_NEVER_TOUCH`` and the ``/Temp`` hygiene write are two contracts (#263).

Owner decision, 2026-09-15: a client that has leaked ``/Temp`` attachments
**may** still enable ``Network Settings > FTP File Service`` during its
hygiene pass (PR #297's behaviour stands), and a client that leaked nothing
never writes config.  So:

* ``BASELINE_NEVER_TOUCH`` is the **entry-baseline reset's** contract:
  :func:`apply_factory_baseline` never resets and never asserts those
  stores, and refuses them as arguments.
* ``Ultimate64Client._run_temp_hygiene`` may write **exactly one item** in
  one of those stores -- ``Network Settings > FTP File Service`` -- once per
  client, only for a client that leaked, only after its sweep failed.

#263 found the two modules each stating its own contract as though it were
the only one ("must never reset, read, PUT or otherwise touch" beside an
automatic PUT into the same store).  The fix is documentary; these pins
keep it that way:

* each module's doc names the other contract and cites it;
* the superseded wording ("otherwise touch", "not decided here", "open in
  issue #263") stays gone;
* no doc anywhere claims the harness never writes ``Network Settings`` (or
  a never-touch store) at all.  That last pin is a heuristic over prose
  with positive controls below; it found no offender on master, so its red
  evidence is the planted cases, not the tree.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from c64_test_harness.backends import ultimate64_baseline, ultimate64_temp_gc
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

_REPO = Path(__file__).resolve().parent.parent


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("``", "").replace("#: ", " "))


def _never_touch_comment() -> str:
    """The ``#:`` block directly above ``BASELINE_NEVER_TOUCH``."""
    src = inspect.getsource(ultimate64_baseline)
    end = src.index("BASELINE_NEVER_TOUCH: dict[str, str] = {")
    start = src.rindex("\n\n", 0, end)
    return src[start:end]


# --------------------------------------------------------------------------- #
# Each side names the other                                                    #
# --------------------------------------------------------------------------- #

class TestEachContractCitesTheOther:
    def test_never_touch_is_scoped_to_the_entry_reset_and_names_the_exception(self) -> None:
        block = _flat(_never_touch_comment())
        # Vacuity guard: the extractor found the comment, not an empty span.
        assert len(block) > 200 and "Pinned literally" in block, block
        for token in ("apply_factory_baseline", "never resets", "never asserts",
                      "Ultimate64Client._run_temp_hygiene", "FTP File Service",
                      "exactly one item", "leaked", "#263"):
            assert token in block, f"BASELINE_NEVER_TOUCH comment lacks {token!r}"
        # The overbroad reading #263 quoted must not come back.
        assert "otherwise touch" not in block

    def test_the_hygiene_pass_names_the_never_touch_contract(self) -> None:
        doc = _flat(inspect.getdoc(Ultimate64Client._run_temp_hygiene) or "")
        for token in ("BASELINE_NEVER_TOUCH", "apply_factory_baseline",
                      "exactly one item", "Network Settings > FTP File Service",
                      "leaked", "#263"):
            assert token in doc, f"_run_temp_hygiene docstring lacks {token!r}"

    def test_the_drain_no_longer_calls_it_undecided(self) -> None:
        doc = _flat(inspect.getdoc(Ultimate64Client._drain_temp_attachments) or "")
        assert "not decided here" not in doc
        assert "#263" in doc and "_run_temp_hygiene" in doc

    def test_the_gc_module_points_at_both(self) -> None:
        doc = _flat(ultimate64_temp_gc.__doc__ or "")
        for token in ("BASELINE_NEVER_TOUCH", "_run_temp_hygiene", "#263"):
            assert token in doc, f"ultimate64_temp_gc docstring lacks {token!r}"

    def test_the_skill_hygiene_rule_states_both_contracts(self) -> None:
        text = (_REPO / ".claude" / "skills" / "c64-test" / "PATTERNS.md").read_text(
            encoding="utf-8")
        items = [line for line in text.splitlines()
                 if line.startswith("3. **A hygiene result with `.error` set")]
        assert len(items) == 1, "PATTERNS.md hygiene rule 3 not found"
        flat = _flat(items[0])
        for token in ("#263", "apply_factory_baseline", "leaked"):
            assert token in flat, f"PATTERNS hygiene rule 3 lacks {token!r}"

    def test_the_recovery_doc_no_longer_calls_it_open(self) -> None:
        text = _flat((_REPO / "docs" / "u64_recovery.md").read_text(encoding="utf-8"))
        assert "open in issue #263" not in text
        assert "#263" in text, "u64_recovery.md no longer cites the decision"


# --------------------------------------------------------------------------- #
# Nothing claims the harness never writes the store at all                     #
# --------------------------------------------------------------------------- #

#: Sentences about the store or the never-touch set.
_SUBJECT = re.compile(r"Network Settings|BASELINE_NEVER_TOUCH|never-touch|FTP File Service",
                      re.IGNORECASE)
#: A claim about the harness as a whole, not about one mechanism.
_WHOLE = re.compile(r"\b(?:harness|package|no code|nothing|no lane|any lane|every lane)\b",
                    re.IGNORECASE)
#: A universal negation of writing.
_NEGATED_WRITE = re.compile(
    r"\b(?:never|nothing|no|must not|may not|cannot|does not|doesn't)\b[^.;]{0,50}?"
    # Not the second half of "never-touch": the red run on master matched
    # "argument and nothing else, so a direct client call bypasses the
    # never-touch list" in the baseline module, which claims nothing of the kind.
    r"(?<!-)\b(?:writ\w*|PUT\w*|touch\w*|modif\w*)",
    re.IGNORECASE,
)
#: Phrases that scope the claim to one mechanism or one kind of lane.
_SCOPED = re.compile(
    r"entry reset|entry-baseline|entry baseline|apply_factory_baseline|"
    r"leaked nothing|leaks nothing|only bodyless|neither suite|except|exception|"
    r"_run_temp_hygiene|hygiene pass may",
    re.IGNORECASE,
)

_SCANNED: tuple[Path, ...] = (
    _REPO / "README.md",
    _REPO / "docs" / "development.md",
    _REPO / "docs" / "u64_recovery.md",
    _REPO / ".claude" / "skills" / "c64-test" / "SKILL.md",
    _REPO / ".claude" / "skills" / "c64-test" / "PATTERNS.md",
    _REPO / ".claude" / "skills" / "c64-test" / "REFERENCE.md",
    _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_baseline.py",
    _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_client.py",
    _REPO / "src" / "c64_test_harness" / "backends" / "ultimate64_temp_gc.py",
)


def _unscoped_absolute_claims(text: str) -> list[str]:
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", _flat(text)):
        if (_SUBJECT.search(sentence) and _WHOLE.search(sentence)
                and _NEGATED_WRITE.search(sentence) and not _SCOPED.search(sentence)):
            out.append(sentence)
    return out


class TestNoDocClaimsTheStoreIsNeverWritten:
    @pytest.mark.parametrize("path", _SCANNED, ids=lambda p: str(p.relative_to(_REPO)))
    def test_no_absolute_claim(self, path: Path) -> None:
        found = _unscoped_absolute_claims(path.read_text(encoding="utf-8"))
        assert not found, (
            f"{path.relative_to(_REPO)} claims the harness never writes a never-touch "
            f"store; scope it to the entry reset or name the hygiene exception "
            f"(#263): {found!r}"
        )

    def test_the_scanned_files_mention_the_subject(self) -> None:
        """Vacuity guard: a scan over files that never name the store passes."""
        for path in _SCANNED:
            assert _SUBJECT.search(path.read_text(encoding="utf-8")), path

    @pytest.mark.parametrize("text", [
        "The harness never writes Network Settings.",
        "Nothing in the harness ever PUTs a BASELINE_NEVER_TOUCH store.",
        "No lane may modify the never-touch stores.",
        "BASELINE_NEVER_TOUCH means the harness does not touch Network Settings at all.",
        "The package must not write FTP File Service on a shared device.",
    ])
    def test_the_scan_flags(self, text: str) -> None:
        assert _unscoped_absolute_claims(text), text

    @pytest.mark.parametrize("text", [
        "The entry reset never resets or asserts Network Settings; the hygiene "
        "pass may write FTP File Service.",
        "A client that leaked nothing never writes config, so the harness does "
        "not enable Network Settings > FTP File Service on its behalf.",
        "It does not touch the addressing, so it is a milder case than Ethernet "
        "Settings; it stays never-touch for the blanked password.",
        "The harness never writes a BASELINE_NEVER_TOUCH store except the one "
        "FTP File Service item the hygiene pass enables.",
        # The master-tree false positive, verbatim in shape.
        "Ultimate64Client.reset_config_category_to_default type-checks its "
        "argument and nothing else, so a direct client call bypasses the "
        "never-touch list entirely.",
    ])
    def test_the_scan_leaves_scoped_statements_alone(self, text: str) -> None:
        assert not _unscoped_absolute_claims(text), text
