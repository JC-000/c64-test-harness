"""Unit tests for the Ultimate firmware capability probe.

No device is touched — every case is derived from a ``get_info()`` payload.

Background: the harness used to branch on ``firmware_version.startswith("3.14")``
to decide the ``write_mem`` POST threshold. That string match has two holes:

* the C64 Ultimate reports ``1.1.0``, which is not ``3.14*`` and so silently
  got the permissive threshold — even though 1.1.0 predates the Temp-folder
  fix (GideonZ/1541ultimate#686) that makes the POST path safe;
* when the U64E was flashed to 3.15 the branch stopped matching and the
  threshold flipped underneath the rig, with nothing asserting it.

:class:`DeviceCapabilities` replaces the string match with named capabilities.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from c64_test_harness.backends.u64_capabilities import DeviceCapabilities


# ------------------------------------------------------------- version parsing
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3.15", (3, 15)),
        ("V3.15", (3, 15)),
        ("v3.15", (3, 15)),
        ("3.14d", (3, 14)),
        ("V3.14d", (3, 14)),
        ("1.1.0", (1, 1, 0)),
        ("3.14", (3, 14)),
    ],
)
def test_version_tuple_parsing(raw, expected):
    caps = DeviceCapabilities.from_info({"firmware_version": raw})
    assert caps.version_tuple == expected


@pytest.mark.parametrize("raw", ["", "not-a-version", "V", None])
def test_unparseable_version_yields_none(raw):
    caps = DeviceCapabilities.from_info({"firmware_version": raw})
    assert caps.version_tuple is None


def test_missing_info_is_unknown():
    caps = DeviceCapabilities.from_info(None)
    assert caps.firmware_version is None
    assert caps.generation == "unknown"


# ----------------------------------------------------------------- generation
def test_three_dot_x_is_the_ultimate_line():
    caps = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    assert caps.generation == "ultimate"


def test_one_dot_x_is_the_cbm_line():
    caps = DeviceCapabilities.from_info({"firmware_version": "1.1.0"})
    assert caps.generation == "cbm"


# ------------------------------------------------------ writemem_post_safe (#686)
def test_u64e_3_15_has_the_writemem_fix():
    """3.15 contains GideonZ/1541ultimate#686 (Temp-folder GC)."""
    caps = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    assert caps.writemem_post_safe is True
    assert caps.write_mem_query_threshold == 48


def test_u64e_3_14d_lacks_the_writemem_fix():
    caps = DeviceCapabilities.from_info({"firmware_version": "V3.14d"})
    assert caps.writemem_post_safe is False
    assert caps.write_mem_query_threshold == 128


def test_u64e_3_14e_lacks_the_writemem_fix():
    """3.14e branched before the fix; only the 3.15 line carries it."""
    caps = DeviceCapabilities.from_info({"firmware_version": "3.14e"})
    assert caps.writemem_post_safe is False
    assert caps.write_mem_query_threshold == 128


def test_c64u_1_1_0_lacks_the_writemem_fix():
    """The regression the string match hid: 1.1.0 predates #686.

    Tag 1.1.0 is not a descendant of the #686 merge, so the C64U needs the
    same protective threshold the 3.14 U64E gets.
    """
    caps = DeviceCapabilities.from_info({"firmware_version": "1.1.0"})
    assert caps.writemem_post_safe is False
    assert caps.write_mem_query_threshold == 128


def test_unknown_firmware_is_conservative():
    """An unreadable version must assume the fix is absent, not present."""
    caps = DeviceCapabilities.from_info({})
    assert caps.writemem_post_safe is False
    assert caps.write_mem_query_threshold == 128


def test_future_ultimate_release_keeps_the_fix():
    caps = DeviceCapabilities.from_info({"firmware_version": "4.0"})
    assert caps.writemem_post_safe is True


# ------------------------------------------- capabilities the version cannot settle
#
# #802/#806 (multi-block socket reads) and #808 (sockets close on C64 reset)
# landed *after* the "Bump to 3.15" commit, so every build on that line reports
# the same "3.15" string whether or not it carries them. Version alone must
# report "unknown" rather than guess — these need a behavioural probe.
@pytest.mark.parametrize("attr", [
    "uci_socket_read_multiblock",
    "uci_sockets_close_on_reset",
    "readmem_rejects_zero_length",
])
def test_post_tag_capabilities_unknown_from_version_alone(attr):
    caps = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    assert getattr(caps, attr) is None


@pytest.mark.parametrize("attr", [
    "uci_socket_read_multiblock",
    "uci_sockets_close_on_reset",
    "readmem_rejects_zero_length",
])
def test_post_tag_capabilities_absent_on_older_lines(attr):
    """On 3.14 and 1.1.0 the answer *is* knowable: the work is not there."""
    for version in ("V3.14d", "1.1.0"):
        caps = DeviceCapabilities.from_info({"firmware_version": version})
        assert getattr(caps, attr) is False, version


def test_runner_wedge_possible_is_the_inverse_of_the_writemem_fix():
    assert DeviceCapabilities.from_info(
        {"firmware_version": "V3.14d"}).runner_wedge_possible is True
    assert DeviceCapabilities.from_info(
        {"firmware_version": "1.1.0"}).runner_wedge_possible is True
    assert DeviceCapabilities.from_info(
        {"firmware_version": "3.15"}).runner_wedge_possible is False


# ------------------------------------------------------------------ overrides
def test_explicit_capability_override_wins():
    """A probe result can pin a capability the version could not settle."""
    caps = DeviceCapabilities.from_info(
        {"firmware_version": "3.15"},
        overrides={"uci_socket_read_multiblock": True},
    )
    assert caps.uci_socket_read_multiblock is True


def test_override_of_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        DeviceCapabilities.from_info(
            {"firmware_version": "3.15"}, overrides={"no_such_capability": True})


def test_capabilities_are_frozen():
    caps = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    with pytest.raises(Exception):
        caps.writemem_post_safe = False  # type: ignore[misc]


# ------------------------------------------ threshold/fix coupling invariant
def test_post_threshold_is_a_pure_function_of_writemem_post_safe():
    """Threshold-48 and carries-#686 are one value read twice, not two that agree.

    ``write_mem_query_threshold`` is derived from ``writemem_post_safe``
    alone, in all three legs:

    * fix present -> 48
    * fix absent -> 128
    * firmware version unreadable -> 128

    The third leg matters most and is the easiest to leave unpinned:
    ``_writemem_post_safe`` deliberately answers ``False`` for an
    unreadable version, because guessing "present" would put small writes
    back on the leaking POST path while guessing "absent" only costs a
    higher PUT threshold. A new device generation hits that path first,
    before anyone has written its version rule.

    Why the coupling is load-bearing: ``memory.write_bytes`` chunks at a
    fixed 84 bytes -- under 128 but *over* 48 -- so on fixed firmware
    every chunk takes the POST path. That is safe only because the 48
    threshold is *selected by* ``writemem_post_safe`` being true, i.e.
    those POSTs land exactly on the firmware that collects them. Sever
    the coupling and ``write_bytes`` becomes a leak generator at 84 bytes
    a chunk.
    """
    from c64_test_harness.backends.u64_capabilities import (
        THRESHOLD_POST_RISKY,
        THRESHOLD_POST_SAFE,
    )
    from c64_test_harness.memory import _WRITE_CHUNK_SIZE

    fixed = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    assert fixed.writemem_post_safe is True
    assert fixed.write_mem_query_threshold == THRESHOLD_POST_SAFE == 48

    leaky = DeviceCapabilities.from_info({"firmware_version": "1.1.0"})
    assert leaky.writemem_post_safe is False
    assert leaky.write_mem_query_threshold == THRESHOLD_POST_RISKY == 128

    for unreadable in (None, {}, {"firmware_version": "not-a-version"}):
        caps = DeviceCapabilities.from_info(unreadable)
        assert caps.writemem_post_safe is False, unreadable
        assert caps.write_mem_query_threshold == THRESHOLD_POST_RISKY, unreadable

    # The chunk size that makes the coupling matter at all.
    assert THRESHOLD_POST_SAFE < _WRITE_CHUNK_SIZE < THRESHOLD_POST_RISKY


# --------------------------------------------------------------------------- #
# #258 — both dispositions argued, and the two "unknown"s told apart          #
# --------------------------------------------------------------------------- #

def _flat(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", (text or "").replace("`", "").replace("*", ""))


def test_the_future_major_disposition_is_argued_where_the_constant_lives():
    """The module grades an unreadable version closed and a future major
    open.  Both are defensible; what was wrong was that only one carried a
    reason, so the next reader could not tell the asymmetry was deliberate.

    Read from the source rather than from ``__doc__`` because the argument
    lives in a ``#:`` comment on the constant — and asserted against the
    lines **immediately above the assignment**, not against the file, so
    that it actually checks what it claims to: that somebody about to
    change ``>=`` to ``==`` reads the reason without going looking for it.
    Flattening the whole file passed with the block cut out and appended
    to the bottom, which is the shape this test exists to prevent.
    """
    import re as _re

    import c64_test_harness.backends.u64_capabilities as mod

    lines = Path(mod.__file__).read_text().splitlines()
    assign = [
        i for i, ln in enumerate(lines)
        if ln.startswith("_ULTIMATE_WRITEMEM_FIXED_FROM = ")
    ]
    assert len(assign) == 1, f"expected one assignment, found {len(assign)}"
    # The comment block that documents the constant, and nothing else.
    window = lines[max(0, assign[0] - 25):assign[0]]
    # Strip the ``#:`` markers before flattening: without that, a claim
    # wrapping across two comment lines comes back with a "#:" welded into
    # the middle and no phrase spanning the break can ever match — a pin
    # that is green because it can never fire.
    src = _flat(_re.sub(r"(?m)^\s*#:?", " ", "\n".join(window)))
    assert "a future major grades as fixed" in src
    assert "That is deliberate" in src
    assert "opposite disposition to the unreadable-version branch" in src
    # and the behaviour the sentence describes
    assert DeviceCapabilities.from_info({"firmware_version": "4.0"}).writemem_post_safe


def test_the_two_unknown_generations_are_distinguishable():
    """A ``2.x`` string parses cleanly and still grades ``unknown``.

    Both unknowns are graded off, which is right; only one is transient.
    A consumer that re-reads ``/v1/info`` on "generation is unknown" burns
    its reads on the parsed case, then refuses with "no usable version"
    two lines below a line printing the version.  ``firmware_version`` is
    the discriminator, so it has to survive the unrecognised-major path.
    """
    parsed = DeviceCapabilities.from_info({"firmware_version": "2.5"})
    assert parsed.generation == "unknown"
    assert parsed.firmware_version == "2.5", "re-reading this cannot help"

    nothing = DeviceCapabilities.from_info({})
    assert nothing.generation == "unknown"
    assert nothing.firmware_version is None, "this one is the transient"


def test_the_generation_docstring_says_which_unknown_is_transient():
    flat = _flat(DeviceCapabilities._generation_for.__doc__)
    assert "only one of them is transient" in flat
    assert "Re-reading cannot help" in flat
