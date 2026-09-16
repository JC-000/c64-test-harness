"""Docs pin: the declarations attached to ``payloads_discarded`` (#443 round 4).

PR #443 round 3 added ``payloads_discarded`` to ``CaptureResult`` and
``DebugCaptureResult``, and attached four declarations to it that the
counter's own behaviour cannot carry:

1. **The #451 boundary** -- the silent-loss set is exactly
   1..``max_held``, *inclusive at the top*.  This is the clause #451 will
   be closed on, and the misreading it exists to prevent ("the first 8
   numbers = 0..7, so 8 is safe") is one word away.
2. **The framing** -- the field is an *upper bound on packets of lost
   time, not lost content*.  Inverting this to "an exact count of lost
   audio content" would make callers discard good captures.
3. **Not a gate** -- a genuine retransmission is a *correct* discard, so a
   non-zero count is not by itself a fault.  Inverting this to "so it is a
   gate" would fail every capture that saw one retransmission.
4. **The #452 reachability note** -- whether a stream start resets the
   FPGA's sequence counter is unestablished, and that question grades the
   whole restart path.

Plus **the invariant** ``packets_received == delivered + payloads_discarded``,
which is what makes the loss checkable at all, and the **field name** in
REFERENCE.md, which is the only thing a skill reader can search for.

reviewer-8 found all seven able to regress silently: deleting or inverting
any of them left the suite green at 122 passed.  Each is now pinned
verbatim (flattened), and the inversions are pinned as *absences*, in the
style of ``tests/test_write_bytes_throughput_docs.py`` (#446 round 2).

The behaviour behind declaration 1 is pinned separately and behaviourally
by ``test_stream_sequence_accounting.py::
test_the_silent_loss_set_is_exactly_1_to_max_held``, so the prose and the
code cannot drift apart: this module only keeps the prose honest.

**What still gets through:** a rewording that keeps every pinned sentence
and adds a contradicting one elsewhere in the same file, and any
declaration restated in a file this module does not read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SEQ = _REPO / "src" / "c64_test_harness" / "backends" / "_stream_seq.py"
_AUDIO = _REPO / "src" / "c64_test_harness" / "backends" / "u64_audio_capture.py"
_DEBUG = _REPO / "src" / "c64_test_harness" / "backends" / "u64_debug_capture.py"
_REFERENCE = _REPO / ".claude" / "skills" / "c64-test" / "REFERENCE.md"


def _flat(path: Path) -> str:
    """Flatten a file, stripping the ``#:`` field-comment markers first.

    The dataclass declarations live in Sphinx ``#:`` comments, so without
    this every pinned sentence would be interrupted by a marker and the
    pins would silently match nothing.
    """
    text = re.sub(r"(?m)^\s*#:\s?", "", path.read_text())
    return " ".join(text.split())


#: (label, file, sentence that must be present verbatim once flattened)
PRESENT = [
    # -- 1. the #451 boundary, in both places it is stated -----------------
    ("#451 boundary (tracker)", _SEQ,
     "The triggering set is **exactly 1..``max_held``, inclusive at the "
     "top** (#451): ``max_held`` itself still triggers it, ``max_held`` + 1 "
     "does not, and 0 never does"),
    ("#451 boundary (tracker) reason", _SEQ,
     "because nothing precedes the first number the tracker observes so 0 "
     "is never a tracked missing number"),
    ("#451 boundary (CaptureResult)", _AUDIO,
     "That middle condition triggers on exactly the numbers **1..8** "
     "(``MAX_HELD_DUPLICATES``), inclusive at the top: 8 still triggers it, "
     "9 does not, and 0 never does (#451)."),
    # -- 2. the framing: time, not content ---------------------------------
    ("framing (CaptureResult)", _AUDIO,
     "**Read it as an upper bound on packets of lost time, not lost "
     "content.**"),
    ("framing (CaptureResult) duration", _AUDIO,
     "so what the capture loses is **duration, not audio**"),
    ("framing (DebugCaptureResult)", _DEBUG,
     "An **upper bound on datagrams of lost time, not lost content**"),
    ("framing (REFERENCE)", _REFERENCE,
     "An upper bound on datagrams of lost time rather than lost content"),
    # -- 3. not a gate ------------------------------------------------------
    ("not a gate (CaptureResult)", _AUDIO,
     "so a non-zero count is not by itself a fault and this is deliberately "
     "not a gate"),
    ("not a gate (DebugCaptureResult)", _DEBUG,
     "and deliberately not a gate: a genuine retransmission's cycles are "
     "already in the trace, so discarding them is correct"),
    ("not a gate (REFERENCE)", _REFERENCE,
     "so non-zero is not by itself a fault"),
    ("not a gate (REFERENCE debug)", _REFERENCE,
     "and not a gate: a genuine retransmission is a correct discard"),
    ("time_base_intact unmoved (tracker)", _SEQ,
     "The flag is deliberately *not* moved: a true duplicate's payload is a "
     "correct discard, so a non-zero count is not by itself a fault."),
    ("time_base_intact unmoved (REFERENCE)", _REFERENCE,
     "Deliberately **not** affected by `payloads_discarded`"),
    # -- 4. the #452 reachability note --------------------------------------
    ("#452 reachability (tracker)", _SEQ,
     "**How reachable any of this is on hardware is unestablished** (#452)"),
    ("#452 reachability (tracker) reason", _SEQ,
     "whether starting a stream resets it, so a mid-capture restart may be "
     "routine or may be unreachable"),
    ("#452 reachability (CaptureResult)", _AUDIO,
     "whether starting a stream resets the FPGA's sequence counter is an "
     "open question (#452)"),
    # -- the invariant ------------------------------------------------------
    ("invariant (tracker)", _SEQ,
     "``packets_received`` == delivered + ``payloads_discarded`` closes the "
     "books"),
    # #410 added gap fill, so the WAV also holds packets that were never
    # received; the invariant carries that term since the #449 merge.
    ("invariant (CaptureResult)", _AUDIO,
     "``packets_received`` equals the packets in the WAV, less any "
     "``packets_filled``, plus this, so the accounting closes"),
    ("invariant (DebugCaptureResult)", _DEBUG,
     "``packets_received`` equals the datagrams in the trace plus this"),
    ("invariant (REFERENCE audio)", _REFERENCE,
     "`packets_received` == packets in the WAV + `payloads_discarded`"),
    ("invariant (REFERENCE debug)", _REFERENCE,
     "`packets_received` == datagrams in the trace + this"),
    # -- the second discard path, which nothing else declares ---------------
    ("stop() path (CaptureResult)", _AUDIO,
     "which needs **no restart and no loss at all**"),
    ("stop() path (DebugCaptureResult)", _DEBUG,
     "including datagrams still held at ``stop()`` -- which needs no "
     "restart and no loss at all"),
]


@pytest.mark.parametrize(
    "label,path,sentence", PRESENT, ids=[p[0] for p in PRESENT],
)
def test_the_declaration_is_stated_verbatim(
    label: str, path: Path, sentence: str,
) -> None:
    assert sentence in _flat(path), f"{label}: missing from {path.name}"


#: (label, file, regex that must NOT match -- an inversion or a deletion)
ABSENT = [
    # The #451 misreading, and its inversion.
    ("#451 inverted (tracker)", _SEQ,
     r"``max_held`` itself does not trigger"),
    ("#451 inverted (CaptureResult)", _AUDIO,
     r"8 does not trigger it|inclusive at the bottom"),
    ("#451 first-8 misreading", _AUDIO, r"its first 8 numbers"),
    # The framing, inverted.
    ("framing inverted", _AUDIO,
     r"exact count of lost (audio )?content|exact count of the audio lost"),
    ("framing inverted (debug)", _DEBUG, r"exact count of lost"),
    ("framing inverted (REFERENCE)", _REFERENCE, r"exact count of lost"),
    # "not a gate", inverted.
    ("gate inverted (CaptureResult)", _AUDIO, r"so it is a gate"),
    ("gate inverted (debug)", _DEBUG, r"so it is a gate"),
    ("gate inverted (REFERENCE)", _REFERENCE, r"so it is a gate"),
    # The invariant, truncated so it no longer counts the discards.
    ("invariant truncated (tracker)", _SEQ,
     r"== delivered(?! \+ ``payloads_discarded``)"),
    ("invariant truncated (REFERENCE)", _REFERENCE,
     r"== packets in the WAV(?! \+ `payloads_discarded`)"),
]


@pytest.mark.parametrize(
    "label,path,pattern", ABSENT, ids=[a[0] for a in ABSENT],
)
def test_the_declaration_is_not_inverted(
    label: str, path: Path, pattern: str,
) -> None:
    hit = re.search(pattern, _flat(path))
    assert hit is None, f"{label}: {path.name} now says {hit.group(0)!r}"


def test_reference_names_the_field_on_both_dataclasses() -> None:
    """The field name is what a skill reader searches for; a rename in
    REFERENCE.md alone would leave them searching for something that does
    not exist (reviewer-8's D7)."""
    text = _flat(_REFERENCE)
    assert text.count("`.payloads_discarded: int`") == 2, (
        "REFERENCE.md must name .payloads_discarded on both CaptureResult "
        "and DebugCaptureResult"
    )


def test_the_prose_and_the_behavioural_pin_agree_on_max_held() -> None:
    """The prose states the boundary as the literal 1..8; the behavioural
    pin derives it from ``MAX_HELD_DUPLICATES``.  If that constant ever
    moves, the literal in ``CaptureResult`` is wrong and this says so."""
    from c64_test_harness.backends import _stream_seq

    assert _stream_seq.MAX_HELD_DUPLICATES == 8, (
        "MAX_HELD_DUPLICATES moved: the literal '1..8' in "
        "CaptureResult.packets_reordered and the '(its first 8 numbers)' "
        "style wording elsewhere must move with it"
    )
