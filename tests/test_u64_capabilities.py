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

    What the coupling guards today (#294/#252): ``memory.write_bytes`` on
    an Ultimate transport chunks at ``transport.rest_put_chunk_size`` (the
    client threshold, capped at the 128-byte PUT limit), so every chunk is a
    PUT on any grade, and no fixed chunk size is involved.  What still keys
    on the grade is ``Ultimate64Transport.write_memory``'s single-request
    path (``_rest_write_is_post_safe``), and that must agree with the
    threshold leg by leg: see
    :func:`test_the_transport_single_request_path_agrees_with_the_threshold`.
    """
    from c64_test_harness.backends.u64_capabilities import (
        THRESHOLD_POST_RISKY,
        THRESHOLD_POST_SAFE,
    )

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

    # The hand-built unknown grade (#247): not produced by from_info, but a
    # legal value, and it lands on the conservative leg.
    unknown = replace_grade(fixed, None)
    assert unknown.write_mem_query_threshold == THRESHOLD_POST_RISKY == 128


def replace_grade(caps, grade):
    from dataclasses import replace

    return replace(caps, writemem_post_safe=grade)


# ------------------------------------------------ #247: the domain, made explicit
#: Every ``info`` shape from_info can meet: probe failed, empty, unparseable,
#: non-string, an unknown major, both lines below and at their fixes, a
#: future major, and a CBM release newer than the last known unfixed one.
_EVERY_INFO_SHAPE = (
    None,
    {},
    {"firmware_version": "not-a-version"},
    {"firmware_version": 315},
    {"firmware_version": "2.5"},
    {"firmware_version": "V3.14d"},
    {"firmware_version": "3.14e"},
    {"firmware_version": "3.15"},
    {"firmware_version": "4.0"},
    {"firmware_version": "1.0.9"},
    {"firmware_version": "1.1.0"},
    {"firmware_version": "1.2.0"},
)


@pytest.mark.parametrize("info", _EVERY_INFO_SHAPE, ids=repr)
def test_from_info_always_grades_writemem_post_safe_as_a_bool(info):
    """``None`` is never produced by probing (#247); only a hand-built value is.

    ``type(...) is bool``, not truthiness: a probe path that returned ``0``
    or ``None`` would pass an ``in (True, False)`` check.
    """
    import warnings

    from c64_test_harness.backends.u64_capabilities import CbmFixConstantStaleWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CbmFixConstantStaleWarning)
        caps = DeviceCapabilities.from_info(info)
    assert type(caps.writemem_post_safe) is bool, (info, caps.writemem_post_safe)
    assert type(caps.runner_wedge_possible) is bool, info


def test_the_info_shapes_reach_both_answers():
    """Vacuity guard for the enumeration above: it is not all one answer."""
    import warnings

    from c64_test_harness.backends.u64_capabilities import CbmFixConstantStaleWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CbmFixConstantStaleWarning)
        grades = {DeviceCapabilities.from_info(i).writemem_post_safe for i in _EVERY_INFO_SHAPE}
        generations = {DeviceCapabilities.from_info(i).generation for i in _EVERY_INFO_SHAPE}
    assert grades == {True, False}
    assert generations == {"ultimate", "cbm", "unknown"}


@pytest.mark.parametrize("grade", [True, False, None])
def test_hand_built_true_false_and_none_are_accepted(grade):
    caps = replace_grade(DeviceCapabilities.from_info({"firmware_version": "3.15"}), grade)
    assert caps.writemem_post_safe is grade
    overridden = DeviceCapabilities.from_info(
        {"firmware_version": "3.15"}, overrides={"writemem_post_safe": grade}
    )
    assert overridden.writemem_post_safe is grade


@pytest.mark.parametrize("bad", [1, 0, "no", "True", 48, 1.0])
def test_a_non_bool_grade_is_rejected(bad):
    """A truthy non-bool used to grade post-safe: ``"no"`` read as 48."""
    fixed = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    with pytest.raises(TypeError, match="writemem_post_safe"):
        replace_grade(fixed, bad)
    with pytest.raises(TypeError, match="writemem_post_safe"):
        DeviceCapabilities.from_info(
            {"firmware_version": "3.15"}, overrides={"writemem_post_safe": bad}
        )


def test_the_threshold_keys_on_is_true_even_if_validation_is_bypassed():
    """Defence in depth: a value forced past ``__post_init__`` (``object.__setattr__``
    on the frozen instance) still only reaches 48 through an explicit ``True``."""
    from c64_test_harness.backends.u64_capabilities import (
        THRESHOLD_POST_RISKY,
        THRESHOLD_POST_SAFE,
    )

    caps = DeviceCapabilities.from_info({"firmware_version": "3.15"})
    assert caps.write_mem_query_threshold == THRESHOLD_POST_SAFE
    for forced in (1, "no", object()):
        object.__setattr__(caps, "writemem_post_safe", forced)
        assert caps.write_mem_query_threshold == THRESHOLD_POST_RISKY, forced


@pytest.mark.parametrize("grade", [True, False, None])
def test_the_transport_single_request_path_agrees_with_the_threshold(grade):
    """The coupling that still guards something (CLAUDE.md 2c after #294/#252).

    ``Ultimate64Transport.write_memory`` sends one request, POST-eligible,
    only when ``_rest_write_is_post_safe()``; otherwise it chunks onto PUT.
    That decision and the capability's threshold must be the same leg for
    every legal grade, or a device could get the 48 threshold on the
    leaking path or the single-request path at 128.
    """
    from types import SimpleNamespace

    from c64_test_harness.backends.u64_capabilities import THRESHOLD_POST_SAFE
    from c64_test_harness.backends.ultimate64 import Ultimate64Transport

    caps = replace_grade(DeviceCapabilities.from_info({"firmware_version": "3.15"}), grade)
    fake = SimpleNamespace(_client=SimpleNamespace(cached_capabilities=caps))
    single_request = Ultimate64Transport._rest_write_is_post_safe(fake)
    assert single_request is (caps.write_mem_query_threshold == THRESHOLD_POST_SAFE), grade
    assert single_request is (grade is True)


@pytest.mark.parametrize("put_size", [48, 128])
def test_write_bytes_chunks_at_the_transports_put_size_not_a_literal(put_size):
    """Restated for #247 as the invariant now is: ``write_bytes`` on a
    transport reporting ``rest_put_chunk_size`` chunks at exactly that size,
    on either grade's threshold; the 84-byte VICE constant is only the
    fallback for a transport that reports none."""
    from types import SimpleNamespace

    from c64_test_harness import memory

    writes: list[tuple[int, bytes]] = []
    transport = SimpleNamespace(
        rest_put_chunk_size=put_size,
        write_memory=lambda addr, data: writes.append((addr, bytes(data))),
    )
    memory.write_bytes(transport, 0x4000, bytes(range(256)) * 2)
    assert {len(d) for _, d in writes[:-1]} == {put_size}
    assert b"".join(d for _, d in writes) == bytes(range(256)) * 2

    vice_like = SimpleNamespace(write_memory=lambda addr, data: None)
    assert memory._write_chunk_size(vice_like) == memory._WRITE_CHUNK_SIZE


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


# --------------------------------------------------------------------------- #
# #248 — a CBM firmware update must not be silently graded leak-prone forever #
# --------------------------------------------------------------------------- #
#
# ``_CBM_WRITEMEM_FIXED_FROM`` is ``None`` until someone establishes which
# ``u64ii`` 1.x release carries GideonZ/1541ultimate#686.  Staying on the
# 128 threshold is safe, so the grade itself must stay ``False``; what must
# not happen is that a C64U reporting a *newer* release than the last one
# known to leak is graded ``False`` in silence.  Every test here either reads
# the module constants live or pins them with ``monkeypatch``, so none of
# them passes whatever the constant is.

import logging

import c64_test_harness.backends.u64_capabilities as caps_mod

_CAPS_LOGGER = "c64_test_harness.backends.u64_capabilities"


@pytest.fixture
def fresh_stale_notice(monkeypatch):
    """The stale-constant notice is once per version per process; reset it."""
    monkeypatch.setattr(caps_mod, "_CBM_STALE_NOTICE_ISSUED", set(), raising=False)


def _stale_records(caplog):
    return [
        r for r in caplog.records
        if r.name == _CAPS_LOGGER
        and r.levelno >= logging.WARNING
        and "_CBM_WRITEMEM_FIXED_FROM" in r.getMessage()
    ]


@pytest.mark.parametrize("raw", ["1.1.1", "1.2.0", "1.2", "1.10.0"])
def test_newer_cbm_release_with_unset_constant_warns_loudly(
    raw, caplog, fresh_stale_notice, monkeypatch
):
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    with pytest.warns(caps_mod.CbmFixConstantStaleWarning):
        caps = DeviceCapabilities.from_info({"firmware_version": raw})
    # still conservative: the grade does not guess
    assert caps.writemem_post_safe is False
    assert caps.write_mem_query_threshold == 128
    records = _stale_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    msg = records[0].getMessage()
    assert raw in msg
    assert "#248" in msg
    assert "1.1.0" in msg  # names the last release known to leak


@pytest.mark.parametrize("raw", ["1.1.0", "1.0.9", "1.1"])
def test_last_known_unfixed_or_older_cbm_release_is_quiet(
    raw, caplog, fresh_stale_notice, monkeypatch
):
    """Guards the opposite mutation: a notice on every C64U is noise nobody reads."""
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    caps = DeviceCapabilities.from_info({"firmware_version": raw})
    assert caps.writemem_post_safe is False
    assert _stale_records(caplog) == []


def test_ultimate_line_never_raises_the_cbm_notice(caplog, fresh_stale_notice):
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    for raw in ("3.15", "V3.14d", "4.0", "2.5"):
        DeviceCapabilities.from_info({"firmware_version": raw})
    assert _stale_records(caplog) == []


def test_stale_notice_is_once_per_version_per_process(
    caplog, fresh_stale_notice, monkeypatch
):
    """Every client probes capabilities; one line per version, not per client."""
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    with pytest.warns(caps_mod.CbmFixConstantStaleWarning):
        for _ in range(3):
            DeviceCapabilities.from_info({"firmware_version": "1.2.0"})
        DeviceCapabilities.from_info({"firmware_version": "1.3.0"})
    assert len(_stale_records(caplog)) == 2


@pytest.mark.parametrize("fixed_from", [(1, 2), (1, 2, 0)])
def test_setting_the_cbm_constant_actually_grades_post_safe(
    fixed_from, caplog, fresh_stale_notice, monkeypatch
):
    """The day the constant is set, it must work in either spelling.

    The C64U reports three-part versions (``1.1.0``), so the natural edit is
    ``(1, 2, 0)``.  A comparison that truncates the device version to two
    parts compares ``(1, 2) >= (1, 2, 0)``, which is ``False``, and the fix
    would stay silently ungraded after someone did exactly what the comment
    asked.
    """
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", fixed_from)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    for raw in ("1.2.0", "1.2", "1.2.1", "1.10.0"):
        caps = DeviceCapabilities.from_info({"firmware_version": raw})
        assert caps.writemem_post_safe is True, (fixed_from, raw)
        assert caps.runner_wedge_possible is False, (fixed_from, raw)
        assert caps.write_mem_query_threshold == 48, (fixed_from, raw)
    for raw in ("1.1.0", "1.1.9", "1.1"):
        caps = DeviceCapabilities.from_info({"firmware_version": raw})
        assert caps.writemem_post_safe is False, (fixed_from, raw)
        assert caps.write_mem_query_threshold == 128, (fixed_from, raw)
    # once the constant is set there is nothing stale to report
    assert _stale_records(caplog) == []


# ---------------------------------------------- #248 review round 1 (PR #290)

import warnings


def _stale_warnings(recorded):
    return [
        w for w in recorded
        if issubclass(w.category, caps_mod.CbmFixConstantStaleWarning)
    ]


def _assert_remedy_text(msg: str, raw: str) -> None:
    """The message must say *what to do*, with each version in its own slot."""
    assert msg.startswith(f"C64U firmware {raw} is newer than 1.1.0,"), msg
    assert f"Establish whether {raw} carries #686 and set that constant" in msg, msg
    assert "backends/u64_capabilities.py" in msg, msg
    assert "_CBM_WRITEMEM_FIXED_FROM" in msg, msg
    assert "#248" in msg, msg


def test_stale_notice_is_a_python_warning_a_green_pytest_run_shows(
    fresh_stale_notice, monkeypatch
):
    """A log line is invisible in a passing pytest run, which is #248's exact
    scenario (a live C64U suite, green, after a firmware bump).  A warning
    lands in pytest's warnings summary and can be escalated by category."""
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    category = caps_mod.CbmFixConstantStaleWarning
    assert issubclass(category, UserWarning)
    with pytest.warns(category) as recorded:
        caps = DeviceCapabilities.from_info({"firmware_version": "1.2.0"})
    assert caps.writemem_post_safe is False
    stale = _stale_warnings(recorded)
    assert len(stale) == 1
    _assert_remedy_text(str(stale[0].message), "1.2.0")
    # stacklevel: the warning names the caller of from_info, not this module
    assert Path(stale[0].filename).resolve() == Path(__file__).resolve(), stale[0].filename


def test_stale_log_line_names_the_remedy_and_versions_in_their_slots(
    caplog, fresh_stale_notice, monkeypatch
):
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    with pytest.warns(caps_mod.CbmFixConstantStaleWarning):
        DeviceCapabilities.from_info({"firmware_version": "1.3.7"})
    records = _stale_records(caplog)
    assert len(records) == 1
    _assert_remedy_text(records[0].getMessage(), "1.3.7")


def test_no_stale_warning_where_the_log_is_quiet(fresh_stale_notice, monkeypatch):
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        for raw in ("1.1.0", "1.0.9", "1.1", "3.15", "V3.14d", "4.0", "2.5"):
            DeviceCapabilities.from_info({"firmware_version": raw})
    assert _stale_warnings(recorded) == []
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", (1, 2, 0))
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        DeviceCapabilities.from_info({"firmware_version": "1.3.0"})
    assert _stale_warnings(recorded) == []


def test_stale_warning_is_once_per_version_per_process(fresh_stale_notice, monkeypatch):
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        for _ in range(3):
            DeviceCapabilities.from_info({"firmware_version": "1.2.0"})
        DeviceCapabilities.from_info({"firmware_version": "1.3.0"})
    assert len(_stale_warnings(recorded)) == 2


@pytest.mark.parametrize("pinned", [True, None])
def test_override_that_lifts_the_grade_suppresses_the_notice(
    pinned, caplog, fresh_stale_notice, monkeypatch
):
    """A probe that pinned ``writemem_post_safe`` has, by definition, checked:
    "graded False without anyone having checked" would be false."""
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        caps = DeviceCapabilities.from_info(
            {"firmware_version": "1.2.0"},
            overrides={"writemem_post_safe": pinned},
        )
    assert caps.writemem_post_safe is pinned
    assert _stale_records(caplog) == []
    assert _stale_warnings(recorded) == []
    # and suppression did not use up the once-per-version notice
    with pytest.warns(caps_mod.CbmFixConstantStaleWarning):
        DeviceCapabilities.from_info({"firmware_version": "1.2.0"})
    assert len(_stale_records(caplog)) == 1


@pytest.mark.parametrize("overrides", [
    {"writemem_post_safe": False},
    {"uci_socket_read_multiblock": True},
])
def test_override_that_leaves_the_grade_false_still_notices(
    overrides, caplog, fresh_stale_notice, monkeypatch
):
    monkeypatch.setattr(caps_mod, "_CBM_WRITEMEM_FIXED_FROM", None)
    caplog.set_level(logging.WARNING, logger=_CAPS_LOGGER)
    with pytest.warns(caps_mod.CbmFixConstantStaleWarning):
        caps = DeviceCapabilities.from_info(
            {"firmware_version": "1.2.0"}, overrides=overrides
        )
    assert caps.writemem_post_safe is False
    assert len(_stale_records(caplog)) == 1


def test_stale_warning_category_is_exported_from_the_package_root():
    """Downstream ``-W error::...`` / ``filterwarnings`` need a stable name."""
    import c64_test_harness

    assert c64_test_harness.CbmFixConstantStaleWarning is caps_mod.CbmFixConstantStaleWarning
    assert "CbmFixConstantStaleWarning" in c64_test_harness.__all__
    assert "CbmFixConstantStaleWarning" in caps_mod.__all__


def test_cbm_constants_are_mutually_consistent():
    """Reads the real constants: the last known leak-prone release must grade
    unfixed, and a fix floor, once set, must lie above it."""
    last = caps_mod._CBM_LAST_KNOWN_UNFIXED
    assert last == (1, 1, 0)
    raw = ".".join(str(p) for p in last)
    assert DeviceCapabilities.from_info({"firmware_version": raw}).writemem_post_safe is False
    fixed = caps_mod._CBM_WRITEMEM_FIXED_FROM
    if fixed is not None:
        def pad(t):
            return tuple(t) + (0,) * (3 - len(t))
        assert pad(fixed) > pad(last), (fixed, last)
