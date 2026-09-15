"""Unit tests for the integral /Temp hygiene pass on Ultimate64Client.

No network: ``urllib.request.urlopen`` is mocked and the FTP GC is
patched out.  The one behaviour these tests pin hardest is that a client
whose capability probe never got an answer (every fake host in the unit
suite) does no FTP at all -- see ``test_no_hygiene_when_device_never_answered``.
"""
from __future__ import annotations

import io
import re
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64TempHygieneError,
)
from c64_test_harness.backends.ultimate64_temp_gc import TempGCResult


class _FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _urlopen_mock():
    captured: list[object] = []

    def _fake(req, timeout=None):
        captured.append(req)
        return _FakeResponse()

    return MagicMock(side_effect=_fake), captured


#: A device that answered the probe and reports firmware without the
#: upstream #686 Temp-folder fix (the C64U's 1.1.0).
LEAKY = DeviceCapabilities.from_info({"firmware_version": "1.1.0", "product": "C64 Ultimate"})
#: A device that answered and carries the fix (the bench U64E).
FIXED = DeviceCapabilities.from_info({"firmware_version": "3.15", "product": "Ultimate 64"})
#: No answer at all -- an unreachable host, or every fake host in this suite.
NO_ANSWER = DeviceCapabilities.from_info(None)


def _client(caps: DeviceCapabilities = LEAKY, **kwargs) -> Ultimate64Client:
    """Client with a pinned threshold (so __init__ issues no HTTP) and caps."""
    kwargs.setdefault("write_mem_query_threshold", 128)
    c = Ultimate64Client("fake-host", **kwargs)
    c._capabilities = caps
    return c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        gc_mod.AUTO_GC_ENV,
        gc_mod.BUDGET_ENV,
        gc_mod.REQUIRED_ENV,
        gc_mod.KEEP_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# --------------------------------------------------------------- arming rule
def test_armed_on_leak_prone_firmware():
    assert _client(LEAKY).temp_hygiene_armed is True


def test_disarmed_on_fixed_firmware():
    assert _client(FIXED).temp_hygiene_armed is False


def test_no_hygiene_when_device_never_answered():
    """A host that never answered /v1/info has no /Temp we could clean.

    This is what keeps the unit suite off the network: every fake host
    here fails the capability probe, so hygiene stays inert without any
    per-test opt-out.
    """
    assert _client(NO_ANSWER).temp_hygiene_armed is False


def test_indeterminate_capability_arms_conservatively():
    """A device that answered but whose wedge-proneness is unknown arms.

    ``runner_wedge_possible=None`` is "the version string cannot settle
    it", which must resolve to protected, not to unprotected.
    """
    from dataclasses import replace

    caps = replace(LEAKY, runner_wedge_possible=None, writemem_post_safe=None)
    assert _client(caps).temp_hygiene_armed is True


def test_arming_is_not_keyed_on_the_device_address():
    """Same firmware, different hosts -> same decision.

    Consumer lanes reach the C64U through $U64_HOST/--host, so an
    address-keyed guard would miss it entirely.
    """
    for host in ("10.53.21.158", "10.43.23.81", "u64.lan"):
        c = Ultimate64Client(host, write_mem_query_threshold=128)
        c._capabilities = LEAKY
        assert c.temp_hygiene_armed is True, host
        c._capabilities = FIXED
        assert c.temp_hygiene_armed is False, host


def test_transport_write_memory_above_threshold_is_accounted():
    """The raw transport large-write path leaks and must be counted.

    ``Ultimate64Transport.write_memory`` does not chunk: above the
    threshold it hands the whole payload to ``client.write_mem``, which
    takes the body-POST path. A consumer doing hundreds of such writes
    in one run is a wedge with no ``run_prg`` anywhere in it.
    """
    from c64_test_harness.backends.ultimate64 import Ultimate64Transport

    c = _client(LEAKY)
    t = Ultimate64Transport(host="fake-host", client=c)
    mock, _ = _urlopen_mock()
    with patch("urllib.request.urlopen", mock):
        t.write_memory(0xC000, b"x" * 256)
    assert c.pending_temp_attachments == 1


def test_uci_socket_write_shape_is_accounted_per_attachment():
    """A UCI socket write costs one attachment, or two for a large payload.

    ``uci_socket_write`` issues ``socket_id`` (1 byte), the payload
    (unchunked, conditional), ``data_len`` (2 bytes), then the 170-byte
    routine code. Only the routine (always) and the payload (only when it
    exceeds the ceiling) reach POST; the two small writes are PUTs. The
    budget counts attachments, so it gets that distinction without anyone
    maintaining a per-operation table.
    """
    c = _client(LEAKY)
    mock, captured = _urlopen_mock()
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0xC400, bytes([1]))       # socket_id  -> PUT
        c.write_mem(0xC800, b"\x01" * 8)      # small payload -> PUT
        c.write_mem(0xC402, bytes([8, 0]))    # data_len   -> PUT
        c.write_mem(0xC000, b"\xEA" * 170)    # routine    -> POST
    assert [r.get_method() for r in captured] == ["PUT", "PUT", "PUT", "POST"]
    assert c.pending_temp_attachments == 1

    # A large send adds the payload as a second attachment.
    c2 = _client(LEAKY)
    mock2, captured2 = _urlopen_mock()
    with patch("urllib.request.urlopen", mock2):
        c2.write_mem(0xC800, b"\x01" * 892)   # large payload -> POST
        c2.write_mem(0xC000, b"\xEA" * 170)   # routine       -> POST
    assert [r.get_method() for r in captured2] == ["POST", "POST"]
    assert c2.pending_temp_attachments == 2


def test_arming_never_probes(monkeypatch: pytest.MonkeyPatch):
    """Deciding whether to arm must issue no HTTP of its own.

    A client built with an explicit ``write_mem_query_threshold`` has by
    contract never probed; reading ``temp_hygiene_armed`` must not go
    and probe now, or the first upload on such a client would emit a
    surprise GET ahead of it.
    """
    c = Ultimate64Client("fake-host", write_mem_query_threshold=128)
    assert c._capabilities is None
    with patch("urllib.request.urlopen", side_effect=AssertionError("probed!")):
        assert c.temp_hygiene_armed is False
    assert c._capabilities is None


def test_unprobed_client_can_still_be_armed_explicitly():
    c = Ultimate64Client("fake-host", write_mem_query_threshold=128, temp_hygiene=True)
    assert c.temp_hygiene_armed is True


def test_env_force_on_arms_even_on_fixed_firmware(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, "1")
    assert _client(FIXED).temp_hygiene_armed is True


def test_env_force_on_arms_even_without_an_answer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, "1")
    assert _client(NO_ANSWER).temp_hygiene_armed is True


def test_env_force_off_disarms_leak_prone_device(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, "0")
    assert _client(LEAKY).temp_hygiene_armed is False


def test_kwarg_force_off_beats_env_force_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, "1")
    assert _client(LEAKY, temp_hygiene=False).temp_hygiene_armed is False


def test_kwarg_force_on_beats_env_force_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, "0")
    assert _client(FIXED, temp_hygiene=True).temp_hygiene_armed is True


# ------------------------------------------- the slow-device re-probe hole
def _info_response(version: str = "1.1.0"):
    import json

    payload = json.dumps({"firmware_version": version, "product": "C64 Ultimate"})
    return payload.encode()


def test_a_device_that_answered_late_is_regraded_after_a_successful_request():
    """A present-but-slow device must not stay disarmed for the client's life.

    ``__init__`` probes under a 0.5 s cap and collapses every failure to
    "unknown", which is cached forever and disarms hygiene. So a real
    C64U answering ``/v1/info`` in 501 ms would get no accounting, no
    budget, no drain and no refusal -- and the correlation runs the wrong
    way: a device is slow when it is loaded or distressed, which is the
    state of a device approaching /Temp exhaustion.

    A completed request is the evidence a timed-out probe could not
    supply. The re-probe fires at the decision points -- before an
    attachment-creating call and before a drain -- rather than from
    inside every request, so the first upload is decided on the stale
    grade (and still counted) and the second arms.
    """
    slow = {"n": 0}

    def urlopen(req, timeout=None):
        if "/v1/info" in req.get_full_url():
            slow["n"] += 1
            if slow["n"] == 1:          # the construct-time probe times out
                raise urllib.error.URLError("timed out")
            return _FakeResponse(_info_response())
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=urlopen):
        c = Ultimate64Client("fake-host")
        assert c.temp_hygiene_armed is False, "unknown grade at construction"
        c.run_prg(b"\x01\x08x")
        assert c.pending_temp_attachments == 1, "counted even while unarmed"
        c.run_prg(b"\x01\x08x")
        assert c.temp_hygiene_armed is True
        assert c.pending_temp_attachments == 2
    assert c.capabilities.firmware_version == "1.1.0"


def test_a_drain_regrades_before_deciding_not_to_clean_up():
    """One leaked attachment then close(): the drain must settle the grade."""
    slow = {"n": 0}

    def urlopen(req, timeout=None):
        if "/v1/info" in req.get_full_url():
            slow["n"] += 1
            if slow["n"] == 1:
                raise urllib.error.URLError("timed out")
            return _FakeResponse(_info_response())
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=urlopen):
        c = Ultimate64Client("fake-host")
        with patch.object(
            c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
        ) as gc:
            c.run_prg(b"\x01\x08x")
            c.close()
    gc.assert_called_once()


def test_the_reprobe_happens_at_most_once():
    calls = {"info": 0}

    def urlopen(req, timeout=None):
        if "/v1/info" in req.get_full_url():
            calls["info"] += 1
            raise urllib.error.URLError("timed out")
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=urlopen):
        c = Ultimate64Client("fake-host")
        for _ in range(5):
            c.run_prg(b"\x01\x08x")
    assert calls["info"] == 2, "one construct probe + one re-probe, no more"


def test_the_reprobe_uses_the_full_timeout_not_the_construct_time_cap():
    """The longer timeout *is* the fix, so it has to be asserted.

    ``_probe_info``'s default is the 0.5 s construct-time cap. Re-probing
    under that same cap, on a device that just missed it, buys nothing --
    it would silently restore the hole with a green suite. So pin the
    timeout the re-probe actually passes.
    """
    seen: list[float | None] = []

    def urlopen(req, timeout=None):
        if "/v1/info" in req.get_full_url():
            seen.append(timeout)
            raise urllib.error.URLError("timed out")
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=urlopen):
        c = Ultimate64Client("fake-host", timeout=9.0, warn_unlocked=False)
        c.run_prg(b"\x01\x08x")
        c.run_prg(b"\x01\x08x")

    assert len(seen) == 2, "one construct probe, one re-probe"
    construct_timeout, reprobe_timeout = seen
    assert construct_timeout == Ultimate64Client._AUTODETECT_PROBE_TIMEOUT
    assert reprobe_timeout == c.timeout == 9.0
    assert reprobe_timeout > Ultimate64Client._AUTODETECT_PROBE_TIMEOUT


def test_no_reprobe_when_the_probe_was_never_attempted():
    """A pinned threshold is inert by contract and must stay traffic-free.

    The cached grade here is a real unknown-firmware grade rather than
    ``None``, so the guard is doing the work and is not masked by the
    later ``caps is None`` check.
    """
    c = _client(NO_ANSWER)
    assert c._probe_attempted is False
    mock, captured = _urlopen_mock()
    with patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.run_prg(b"\x01\x08x")
    assert [r.get_full_url() for r in captured] == [
        "http://fake-host/v1/runners:run_prg",
        "http://fake-host/v1/runners:run_prg",
    ]


def test_a_lazily_probed_client_can_still_reprobe():
    """``_probe_attempted`` means "a probe was issued", not "probed in __init__".

    A client built with an explicit threshold that later touches
    ``.capabilities`` does probe for real. If that probe fails, the
    failed grade is cached -- and the flag must say a probe happened, or
    it blocks a re-probe the evidence would justify.
    """
    calls = {"info": 0}

    def urlopen(req, timeout=None):
        if "/v1/info" in req.get_full_url():
            calls["info"] += 1
            if calls["info"] == 1:
                raise urllib.error.URLError("timed out")
            return _FakeResponse(_info_response())
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=urlopen):
        c = Ultimate64Client("fake-host", write_mem_query_threshold=128)
        assert c._probe_attempted is False, "nothing asked yet"
        assert c.capabilities.firmware_version is None, "lazy probe, and it failed"
        assert c._probe_attempted is True, "but it was genuinely attempted"
        c.run_prg(b"\x01\x08x")
        c.run_prg(b"\x01\x08x")
        assert c.temp_hygiene_armed is True
    assert calls["info"] == 2


def test_construction_against_an_unreachable_host_does_not_warn(
    caplog: pytest.LogCaptureFixture,
):
    """Construction emits at most the unlocked-client notice.

    Warning here would be noise -- constructing against an unreachable
    host is routine -- and would break the contract pinned in
    ``test_device_lock_visibility.py``. The probe failure is a DEBUG line
    plus ``unknown(probe-failed)`` in the INFO grade.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="c64_test_harness.backends.ultimate64_client"):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("nope")):
            Ultimate64Client("fake-host", warn_unlocked=False)
    assert [r.getMessage() for r in caplog.records] == []


def test_a_grade_still_unreadable_on_a_live_device_warns(
    caplog: pytest.LogCaptureFixture,
):
    """The alarming case: it answers requests but will not say what it runs.

    Hygiene stays disarmed while attachments accumulate, so this one is a
    WARNING naming the override.
    """
    import logging

    def urlopen(req, timeout=None):
        if "/v1/info" in req.get_full_url():
            raise urllib.error.URLError("timed out")
        return _FakeResponse()

    with caplog.at_level(logging.WARNING, logger="c64_test_harness.backends.ultimate64_client"):
        with patch("urllib.request.urlopen", side_effect=urlopen):
            c = Ultimate64Client("fake-host", warn_unlocked=False)
            c.run_prg(b"\x01\x08x")
            c.run_prg(b"\x01\x08x")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stays DISARMED" in text
    assert "U64_AUTO_TEMP_GC=1" in text
    assert c.pending_temp_attachments == 2


def test_grading_distinguishes_not_attempted_from_probe_failed(
    caplog: pytest.LogCaptureFixture,
):
    """Collapsing these two into one 'unknown' is what hid the hole."""
    import logging

    with caplog.at_level(logging.INFO, logger="c64_test_harness.backends.ultimate64_client"):
        Ultimate64Client("fake-host", write_mem_query_threshold=128)
    assert "unknown(not-attempted)" in "\n".join(r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="c64_test_harness.backends.ultimate64_client"):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("nope")):
            Ultimate64Client("fake-host")
    assert "unknown(probe-failed)" in "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------- visible grading
def test_grading_is_logged_on_every_client(caplog: pytest.LogCaptureFixture):
    """The device's grade must be visible in the log even when nothing acts.

    A hygiene pass that silently does the right thing reproduces exactly
    the blindness that let a 512-leaking-write script through review: it
    was developed against a device carrying the fix, so the hazard was
    structurally invisible from the machine it was written on. One INFO
    line per client naming the device, its graded firmware, the
    capability and the resulting threshold is what makes the difference
    visible to the next author.
    """
    import logging

    with caplog.at_level(logging.INFO, logger="c64_test_harness.backends.ultimate64_client"):
        c = Ultimate64Client("fake-host", write_mem_query_threshold=128)
        c._capabilities = LEAKY
        c.log_device_grading()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "fake-host" in text
    assert "1.1.0" in text
    assert "writemem_post_safe=False" in text
    assert "128" in text


def test_grading_log_names_the_fixed_case_too(caplog: pytest.LogCaptureFixture):
    import logging

    with caplog.at_level(logging.INFO, logger="c64_test_harness.backends.ultimate64_client"):
        c = Ultimate64Client("fake-host", write_mem_query_threshold=48)
        c._capabilities = FIXED
        c.log_device_grading()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "3.15" in text
    assert "writemem_post_safe=True" in text
    assert "hygiene=disarmed" in text


def test_grading_is_logged_once_at_construction(caplog: pytest.LogCaptureFixture):
    """A default-constructed client grades itself without being asked."""
    import logging

    with caplog.at_level(logging.INFO, logger="c64_test_harness.backends.ultimate64_client"):
        with patch("urllib.request.urlopen", side_effect=OSError("no device")):
            Ultimate64Client("fake-host")
    grading = [
        r.getMessage() for r in caplog.records if "writemem_post_safe=" in r.getMessage()
    ]
    assert len(grading) == 1, grading


# ------------------------------------------------------- which calls leak
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.run_prg(b"\x01\x08rest"), id="run_prg"),
        pytest.param(lambda c: c.load_prg(b"\x01\x08rest"), id="load_prg"),
        pytest.param(lambda c: c.run_crt(b"CRT"), id="run_crt"),
        pytest.param(lambda c: c.sid_play(b"PSID"), id="sid_play"),
        pytest.param(lambda c: c.mod_play(b"MOD"), id="mod_play"),
        pytest.param(lambda c: c.mount_disk("a", b"D64", "d64"), id="mount_disk"),
        pytest.param(
            lambda c: c.set_config_items_batch({"Cat": {"Item": "v"}}),
            id="set_config_items_batch",
        ),
        pytest.param(lambda c: c.write_mem(0x0400, b"x" * 200), id="write_mem_post"),
    ],
)
def test_body_carrying_post_counts_as_a_leak(call):
    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch("urllib.request.urlopen", mock):
        call(c)
    assert c.pending_temp_attachments == 1


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.write_mem(0x0400, b"x" * 8), id="write_mem_put_query"),
        pytest.param(lambda c: c.reset(), id="reset"),
        pytest.param(
            lambda c: c.set_config_item("Network Settings", "FTP File Service", "Enabled"),
            id="set_config_item",
        ),
        pytest.param(
            lambda c: c.mount_disk_path("a", "/Usb0/x.d64"), id="mount_disk_path"
        ),
        pytest.param(
            lambda c: c.drive_load_rom("a", "/Usb0/x.rom"), id="drive_load_rom_path"
        ),
        # The one call in this client that PUTs an actual body. The
        # firmware's PUT drives:load_rom route binds NULL and takes a
        # ``file`` query, so the body is ditched and no attachment is
        # created -- a PUT with a body must not be counted just because
        # it has one. (That the firmware's upload form for this is POST,
        # so this call shape looks wrong on its own terms, is a separate
        # bug; if it is fixed to POST the choke point starts counting it
        # with no change here.)
        pytest.param(
            lambda c: c.drive_load_rom("a", b"ROMBYTES"), id="drive_load_rom_bytes_put"
        ),
    ],
)
def test_bodyless_requests_do_not_count_as_leaks(call):
    """Only POST routes attach; a PUT never does, body or not.

    S: 1541ultimate ``software/api/route_*.cc`` -- ``attachment_writer``
    appears only on POST entries, and every PUT entry binds ``NULL``.
    Live corroboration on the C64U (fw 1.1.0, 2026-09-10): ``write_mem``
    at exactly 128 B stays on the PUT path and creates zero managed
    attachments, while 129 B takes POST and creates exactly one.
    """
    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch("urllib.request.urlopen", mock):
        call(c)
    assert c.pending_temp_attachments == 0


def test_run_prg_404_fallback_costs_two_attachments():
    """A run_prg that takes the 404 sideload spends two, not one.

    The runner POST carried the whole body before answering 404, and the
    fallback then writes that body again with an unchunked ``write_mem``
    -- far above either threshold, so a second POST. Two on the
    conservative reading that the firmware attaches before deciding to
    404; unmeasured, and one if it rejects first. Either way this is the
    accounting argument for counting at the request choke point: the cost
    is whatever was actually issued, not one-per-verb.

    Worth knowing operationally: a 404 from ``runners:run_prg`` is itself
    a wedge symptom, so the path that costs double fires exactly when the
    device is closest to the edge.
    """
    c = _client(LEAKY)
    prg = bytes([0x60, 0x03]) + b"\xAA" * 400

    def urlopen(req, timeout=None):
        if req.get_method() == "POST" and "runners:run_prg" in req.get_full_url():
            raise urllib.error.HTTPError(
                req.get_full_url(), 404, "Not Found", {}, io.BytesIO(b"")
            )
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=urlopen), \
            patch.object(c, "send_text"):
        c.run_prg(prg)
    assert c.pending_temp_attachments == 2


def test_a_bodyless_post_is_not_an_attachment():
    """Both halves of the rule are *body* and *POST*, not POST alone.

    Every POST the client issues today carries a body, so dropping the
    body half is an equivalent mutant at this commit -- but the rule is
    documented as covering "anything added later, by construction", and a
    bodyless POST added tomorrow attaches nothing (the firmware's writer
    streams a body; there is none). Pinning it directly is what makes
    that promise true rather than accidental.
    """
    from c64_test_harness.backends.ultimate64_client import Ultimate64Client as _C

    assert _C._creates_temp_attachment("POST", b"x") is True
    assert _C._creates_temp_attachment("POST", None) is False
    assert _C._creates_temp_attachment("PUT", b"x") is False
    assert _C._creates_temp_attachment("GET", None) is False


# --------------------------------------------------------------- the budget
def test_gc_runs_when_the_budget_is_crossed():
    c = _client(LEAKY, temp_gc_budget=3)
    mock, _ = _urlopen_mock()
    with patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc, patch("urllib.request.urlopen", mock):
        for _ in range(3):
            c.run_prg(b"\x01\x08x")
        assert gc.call_count == 0, "must not GC before the budget is spent"
        c.run_prg(b"\x01\x08x")
        assert gc.call_count == 1


def test_successful_gc_resets_the_counter():
    c = _client(LEAKY, temp_gc_budget=2)
    mock, _ = _urlopen_mock()
    with patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ), patch("urllib.request.urlopen", mock):
        for _ in range(3):
            c.run_prg(b"\x01\x08x")
    # 2 spent, GC on the 3rd reset to 0, then the 3rd upload counted 1.
    assert c.pending_temp_attachments == 1


def test_budget_override_via_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.BUDGET_ENV, "1")
    c = _client(LEAKY)
    assert c.temp_gc_budget == 1


def test_default_budget_is_below_the_firmware_keep_limit():
    # The firmware's own post-#686 limit is 10 managed files and the
    # measured wedge was ~15 uploads; the default must sit under both.
    assert 0 < gc_mod.DEFAULT_LEAK_BUDGET <= 8
    assert _client(LEAKY).temp_gc_budget == gc_mod.DEFAULT_LEAK_BUDGET


def test_fixed_firmware_never_runs_the_gc():
    c = _client(FIXED, temp_gc_budget=1)
    mock, _ = _urlopen_mock()
    with patch.object(c, "gc_temp_folder") as gc, patch("urllib.request.urlopen", mock):
        for _ in range(5):
            c.run_prg(b"\x01\x08x")
        c.close()
    gc.assert_not_called()


# ---------------------------------------------------------------- draining
def test_close_drains_pending_attachments():
    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc, patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.close()
    gc.assert_called_once()
    assert c.pending_temp_attachments == 0


def _lock_held(value: bool):
    return patch(
        "c64_test_harness.backends.device_lock.DeviceLock.held_by_this_process",
        return_value=value,
    )


def test_close_sweeps_inherited_temp_when_this_client_leaked_nothing():
    """#264: this used to pin the early return as correct. The wedge is a
    property of the device, not of this object: ``gc_temp_folder`` sweeps
    ``/Temp`` device-wide, so declining it because *this* client's counter
    is zero leaves a crashed neighbour's attachments in place for the next
    lane. Arming, not the counter, is what keeps fake hosts off FTP.

    Round 1: an inherited-only sweep deletes other lanes' attachments, so
    on the ``close()`` path it runs only under this process's DeviceLock.
    """
    c = _client(LEAKY)
    assert c.pending_temp_attachments == 0
    with _lock_held(True), patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc:
        c.close()
    gc.assert_called_once()


def test_close_does_not_sweep_inherited_temp_without_the_device_lock():
    """An unlocked read-only client must not delete another lane's /Temp
    attachments (review round 1, E1b). Nothing on close() -> drain ->
    gc_temp_folder takes the lock, so the drain has to ask."""
    c = _client(LEAKY)
    with _lock_held(False), patch.object(c, "gc_temp_folder") as gc:
        c.close()
    gc.assert_not_called()


def test_close_still_drains_what_this_client_leaked_without_the_lock():
    """The pre-#264 behaviour for a client that DID leak is unchanged:
    its own attachments are collected on close whether or not it holds
    the lock (restricting that is not this change's call)."""
    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with _lock_held(False), patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc, patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.close()
    gc.assert_called_once()


def test_device_lock_release_drains_the_client(tmp_path):
    from c64_test_harness.backends.device_lock import DeviceLock

    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc, patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        lock = DeviceLock("fake-host", lock_dir=tmp_path)
        assert lock.acquire(timeout=5.0)
        lock.release()
    gc.assert_called_once()
    assert c.pending_temp_attachments == 0


def test_nested_lock_release_does_not_fire_callbacks(tmp_path):
    """Only the outermost release fires callbacks -- a documented contract.

    ``DeviceLock.release`` promises this in as many words, and it has
    teeth: firing on the inner release would run an FTP sweep against a
    device an outer holder is still mid-run on, once per inner scope
    exit.
    """
    from c64_test_harness.backends.device_lock import DeviceLock

    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc, patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        outer = DeviceLock("fake-host", lock_dir=tmp_path)
        assert outer.acquire(timeout=5.0)
        inner = DeviceLock("fake-host", lock_dir=tmp_path, allow_nested=True)
        assert inner.acquire(timeout=5.0)

        inner.release()
        gc.assert_not_called()   # still held by the outer holder

        outer.release()
        gc.assert_called_once()


def test_device_lock_release_for_another_host_does_not_drain(tmp_path):
    from c64_test_harness.backends.device_lock import DeviceLock

    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch.object(c, "gc_temp_folder") as gc, patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        lock = DeviceLock("some-other-host", lock_dir=tmp_path)
        assert lock.acquire(timeout=5.0)
        lock.release()
    gc.assert_not_called()


# ------------------------------------------------- FTP unavailable handling
def _refused(host: str = "fake-host") -> TempGCResult:
    return TempGCResult(host=host, error="ConnectionRefusedError: [Errno 61] refused")


def test_ftp_refused_enables_the_file_service_and_retries():
    c = _client(LEAKY, temp_gc_budget=1)
    mock, _ = _urlopen_mock()
    results = [_refused(), TempGCResult(host="fake-host")]
    with patch.object(c, "gc_temp_folder", side_effect=results) as gc, \
            patch.object(c, "set_config_item") as set_item, \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.run_prg(b"\x01\x08x")
    assert gc.call_count == 2
    set_item.assert_called_once_with(
        "Network Settings", "FTP File Service", "Enabled"
    )
    assert c.pending_temp_attachments == 1


def test_hygiene_that_cannot_run_blocks_further_uploads():
    c = _client(LEAKY, temp_gc_budget=1)
    mock, _ = _urlopen_mock()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item"), \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        with pytest.raises(Ultimate64TempHygieneError) as excinfo:
            c.run_prg(b"\x01\x08x")
    msg = str(excinfo.value)
    assert "FTP File Service" in msg
    assert "U64_TEMP_GC_REQUIRED=0" in msg


def test_blocked_hygiene_still_allows_bodyless_recovery_calls():
    """A blocked client must still be able to reset/reboot the device."""
    c = _client(LEAKY, temp_gc_budget=1)
    mock, _ = _urlopen_mock()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item"), \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        with pytest.raises(Ultimate64TempHygieneError):
            c.run_prg(b"\x01\x08x")
        c.reset()
        c.reboot()


def test_required_opt_out_lets_uploads_proceed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.REQUIRED_ENV, "0")
    c = _client(LEAKY, temp_gc_budget=1)
    mock, captured = _urlopen_mock()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item"), \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.run_prg(b"\x01\x08x")
    assert len(captured) == 2


def test_fixed_firmware_never_mutates_config_and_never_blocks():
    c = _client(FIXED, temp_gc_budget=1)
    mock, _ = _urlopen_mock()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()) as gc, \
            patch.object(c, "set_config_item") as set_item, \
            patch("urllib.request.urlopen", mock):
        for _ in range(4):
            c.run_prg(b"\x01\x08x")
    gc.assert_not_called()
    set_item.assert_not_called()


def test_reboot_does_not_reset_the_pending_count():
    """``machine:reboot`` is a C64-level reset, not a firmware boot.

    It leaves the firmware's RAM state -- including /Temp -- alone (see
    CLAUDE.md on ``reboot()``), so the pending count must survive it.
    """
    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.reboot()
    assert c.pending_temp_attachments == 1


# --------------------------------------------------------------------------- #
# The docstrings that describe the two mechanisms above (#262, #283)          #
# --------------------------------------------------------------------------- #

def _flat(text: str) -> str:
    """Docstring text with RST markup and wrapping removed, nothing else."""
    return re.sub(r"\s+", " ", (text or "").replace("`", "").replace("*", ""))


def test_the_counter_docstring_names_the_permissive_direction():
    """#283: the residual was labelled "the safe side" and is the opposite.

    An uncounted attachment never advances ``_pending_temp_attachments``,
    so the budget is never reached, the hygiene pass never fires, and the
    counter reads zero while the device accumulates. That is the reading
    that gets someone to keep uploading, so the label is load-bearing and
    the word "safe" may not stand next to it.
    """
    flat = _flat(Ultimate64Client._creates_temp_attachment.__doc__)
    assert "Counting POST-with-body only is the permissive side" in flat
    assert "not the conservative one" in flat
    assert "the gap to close, not the margin to rely on" in flat
    assert "is the safe side of that assumption" not in flat


def test_the_arming_docstring_does_not_resurrect_the_closed_hole():
    """#262: it claimed a slow-probed device loses hygiene for the client's
    lifetime, which ``_maybe_reprobe_capabilities`` — in the same file —
    exists to prevent. The claim propagated into the skill docs once
    already, so it is pinned rather than merely corrected.

    The explicit-threshold half of the paragraph is genuinely true and
    must survive; asserting only the absence would let it be deleted.
    """
    flat = _flat(Ultimate64Client.temp_hygiene_armed.__doc__)
    assert "loses hygiene for the client's lifetime" not in flat
    assert "the one part of this that a live run should confirm" not in flat
    assert "does not stay disarmed" in flat
    assert "arms from the second" in flat
    # the still-true half
    assert "never probes" in flat


# --------------------------------------------------------------------------- #
# #250: the liveness probe is accounted and gated like every other POST       #
# --------------------------------------------------------------------------- #

def _device_urlopen():
    """A fake device: POST writemem stores, GET readmem returns what is there.

    Returns ``(mock, wire, ram)``. ``wire`` records ``(method, url)`` per
    request so a test counts the body-carrying POSTs on the wire rather than
    trusting the counter alone.
    """
    import json as _json
    import urllib.parse as _up

    ram = bytearray(0x10000)
    wire: list[tuple[str, str]] = []

    def _fake(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        wire.append((method, url))
        parsed = _up.urlparse(url)
        q = dict(_up.parse_qsl(parsed.query))
        if parsed.path == "/v1/info":
            return _FakeResponse(_json.dumps({"firmware_version": "1.1.0"}).encode())
        if parsed.path == "/v1/machine:readmem":
            addr, length = int(q["address"], 16), int(q["length"])
            return _FakeResponse(bytes(ram[addr:addr + length]))
        if parsed.path == "/v1/machine:writemem" and method == "POST":
            addr = int(q["address"], 16)
            ram[addr:addr + len(req.data)] = req.data
        return _FakeResponse()

    return MagicMock(side_effect=_fake), wire, ram


def _reachable():
    return patch(
        "c64_test_harness.backends.ultimate64_probe.probe_u64",
        return_value=MagicMock(reachable=True, error=None),
    )


def _writemem_posts(wire):
    return [u for m, u in wire if m == "POST" and "machine:writemem" in u]


def test_liveness_probe_counts_both_of_its_posts():
    """Measured on the C64U: one probe, two attachments (#250).

    The probe used to build its own ``urllib`` requests and never reach the
    client's choke point, so the counter read zero while ``/Temp`` gained two.
    """
    c = _client(LEAKY)
    mock, wire, _ = _device_urlopen()
    with _reachable(), patch.object(c, "gc_temp_folder") as gc, \
            patch("urllib.request.urlopen", mock):
        result = c.liveness_probe()
    assert result.healthy, result
    assert len(_writemem_posts(wire)) == 2, "sanity: probe write + restore"
    assert c.pending_temp_attachments == 2
    gc.assert_not_called()


def test_liveness_probe_is_refused_once_hygiene_is_known_impossible():
    c = _client(LEAKY, temp_gc_budget=1)
    mock, wire, _ = _device_urlopen()
    with _reachable(), \
            patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item"), \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        with pytest.raises(Ultimate64TempHygieneError):
            c.run_prg(b"\x01\x08x")          # hygiene now proven impossible
        wire.clear()
        with pytest.raises(Ultimate64TempHygieneError):
            c.liveness_probe()
        with pytest.raises(Ultimate64TempHygieneError):
            c.assert_healthy()
    assert _writemem_posts(wire) == []


def test_liveness_probe_makes_room_for_both_attachments_before_it_starts():
    """The pass runs *before* the probe when its two POSTs would overrun.

    Budget 3 with two spent: 2 + 2 > 3, so the pass runs first. A
    single-attachment check (2 >= 3 is false) would let the probe take the
    device to 4 uncollected.
    """
    c = _client(LEAKY, temp_gc_budget=3)
    mock, wire, _ = _device_urlopen()
    order: list[str] = []

    def _gc(**kw):
        order.append(f"gc@{len(_writemem_posts(wire))}")
        return TempGCResult(host="fake-host")

    with _reachable(), patch.object(c, "gc_temp_folder", side_effect=_gc) as gc, \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.run_prg(b"\x01\x08x")
        assert c.pending_temp_attachments == 2
        c.liveness_probe()
    assert gc.call_count == 1
    assert order == ["gc@0"], "the pass must precede the probe's first POST"
    assert c.pending_temp_attachments == 2


def test_liveness_probe_restore_is_never_the_request_that_gets_refused():
    """A refusal between the probe write and its restore would leave the
    probe pattern in RAM at $0334 (``_restore_quiet`` swallows every
    exception). So the whole two-attachment cost is reserved up front: when
    hygiene fails, the probe is refused before it writes anything.
    """
    c = _client(LEAKY, temp_gc_budget=2)
    mock, wire, _ = _device_urlopen()
    with _reachable(), \
            patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item"), \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")               # 1 spent; 1 + 2 > 2
        wire.clear()
        with pytest.raises(Ultimate64TempHygieneError):
            c.liveness_probe()
    assert _writemem_posts(wire) == []


def test_liveness_probe_price_is_stated_where_callers_read_it():
    for fn in (Ultimate64Client.liveness_probe, Ultimate64Client.assert_healthy):
        flat = _flat(fn.__doc__)
        assert "two /Temp attachments" in flat, fn.__name__
    assert Ultimate64Client.LIVENESS_PROBE_TEMP_ATTACHMENTS == 2


def test_free_liveness_probe_docstring_counts_the_restore():
    from c64_test_harness.backends.ultimate64_probe import liveness_probe

    flat = _flat(liveness_probe.__doc__)
    assert "exactly one writemem POST" not in flat
    assert "two body-carrying POSTs" in flat


# --------------------------------------------------------------------------- #
# #264: the drain sweeps the device, not just this client's own leaks         #
# --------------------------------------------------------------------------- #

def test_lock_release_sweeps_inherited_temp_when_this_client_leaked_nothing(tmp_path):
    """A lane that inherits a dirty /Temp from a crashed neighbour must sweep
    it on the way out; the sweep is device-wide and idempotent."""
    from c64_test_harness.backends.device_lock import DeviceLock

    c = _client(LEAKY)
    assert c.pending_temp_attachments == 0
    with patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc:
        lock = DeviceLock("fake-host", lock_dir=tmp_path)
        assert lock.acquire(timeout=5.0)
        lock.release()
    gc.assert_called_once()


def test_disarmed_clients_with_nothing_leaked_do_not_sweep():
    """The off-the-network property rides on arming, not on the counter."""
    for caps in (FIXED, NO_ANSWER):
        c = _client(caps)
        with patch.object(c, "gc_temp_folder") as gc:
            c.close()
        gc.assert_not_called()


def test_a_failed_inherited_sweep_neither_enables_ftp_nor_blocks(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    """Review round 1, E1: a lane that only made bodyless calls must not
    write ``Network Settings > FTP File Service`` -- a BASELINE_NEVER_TOUCH
    store, persisting until a firmware power-on, and an anonymous file
    service on a device whose Network Password defaults empty (#263 is the
    owner's call). So an inherited-only sweep that fails logs the manual
    remedy and leaves this client unblocked: it has spent nothing."""
    from c64_test_harness.backends.device_lock import DeviceLock

    c = _client(LEAKY)
    mock, captured = _urlopen_mock()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()) as gc, \
            patch.object(c, "set_config_item") as set_item, \
            patch("urllib.request.urlopen", mock), \
            caplog.at_level("WARNING"):
        lock = DeviceLock("fake-host", lock_dir=tmp_path)
        assert lock.acquire(timeout=5.0)
        lock.release()
        assert gc.call_count == 1
        set_item.assert_not_called()
        assert c._temp_hygiene_blocked is None
        c.run_prg(b"\x01\x08x")            # not refused
    assert len(captured) == 1
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    inherited = [m for m in warned if "inherited sweep" in m]
    assert len(inherited) == 1, warned
    # The remedy is the point of the WARNING (mutation R11 survived a
    # check that matched only words the message repeats elsewhere).
    assert "enable FTP File Service manually" in inherited[0]
    assert "power-cycled" in inherited[0]
    assert "issue #263" in inherited[0]


def test_a_failed_sweep_by_a_lane_that_leaked_keeps_the_existing_behaviour():
    """#263 is the owner's: a client that DID leak still gets the one
    FTP-enable attempt and then blocks. Pinned so the round-1 carve-out
    for inherited-only sweeps cannot widen into it unnoticed."""
    c = _client(LEAKY)
    mock, _ = _urlopen_mock()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item") as set_item, \
            patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08x")
        c.close()
        set_item.assert_called_once_with(
            "Network Settings", "FTP File Service", "Enabled"
        )
        with pytest.raises(Ultimate64TempHygieneError):
            c.run_prg(b"\x01\x08x")


# --------------------------------------------------------------------------- #
# Review round 1, finding 2: correct behaviours that mutations showed unpinned #
# --------------------------------------------------------------------------- #

def test_a_post_that_raises_mid_request_is_still_counted():
    """The firmware writes the attachment as the body streams in, so a
    request that fails afterwards has still left one behind."""
    from c64_test_harness.backends.ultimate64_client import Ultimate64Error

    c = _client(LEAKY)
    with patch("urllib.request.urlopen", side_effect=ConnectionResetError("rst")):
        with pytest.raises(Ultimate64Error):
            c.run_prg(b"\x01\x08x")
    assert c.pending_temp_attachments == 1


def test_a_fresh_budget_one_client_probes_without_sweeping_first():
    """Kept behaviour, stated in ``_before_temp_attachment``: with nothing
    pending a pass could collect nothing this client spent, so the
    two-attachment reservation does not sweep; the probe runs one over a
    budget of 1 and the next attachment-creating request sweeps."""
    c = _client(LEAKY, temp_gc_budget=1)
    mock, wire, _ = _device_urlopen()
    with _reachable(), patch.object(
        c, "gc_temp_folder", return_value=TempGCResult(host="fake-host")
    ) as gc, patch("urllib.request.urlopen", mock):
        c.liveness_probe()
        gc.assert_not_called()
        assert c.pending_temp_attachments == 2
        c.run_prg(b"\x01\x08x")
        gc.assert_called_once()


def test_a_probe_post_that_raises_mid_request_is_still_counted():
    """Review round 2: the probe's own sender counts in ``finally`` too.

    ``test_a_post_that_raises_mid_request_is_still_counted`` drives
    ``_request``; this drives ``liveness_probe``'s sender. A writemem POST
    that dies with a TCP reset has still streamed its body into ``/Temp``,
    and the probe swallows the reset into a ``connection_reset`` result,
    so nothing else would ever notice the attachment.
    """
    c = _client(LEAKY)
    mock, wire, _ = _device_urlopen()

    def _reset_on_post(req, timeout=None):
        if req.get_method() == "POST":
            wire.append((req.get_method(), req.full_url))
            raise ConnectionResetError("reset mid-body")
        return mock(req, timeout=timeout)

    with _reachable(), patch.object(c, "gc_temp_folder") as gc, \
            patch("urllib.request.urlopen", side_effect=_reset_on_post):
        result = c.liveness_probe()
    assert result.failure == "connection_reset", result
    assert len(_writemem_posts(wire)) == 1, "sanity: the probe write only, no restore"
    assert c.pending_temp_attachments == 1
    gc.assert_not_called()


def test_the_probe_sender_runs_the_advisory_lock_check_per_post():
    c = _client(LEAKY)
    mock, _, _ = _device_urlopen()
    with _reachable(), patch.object(c, "gc_temp_folder"), \
            patch.object(c, "_check_device_lock") as check, \
            patch("urllib.request.urlopen", mock):
        c.liveness_probe()
    assert [a.args[0] for a in check.call_args_list] == [
        "POST /v1/machine:writemem", "POST /v1/machine:writemem",
    ]


# --------------------------------------------------------------------------- #
# #242: a SocketDMA write that falls back to REST is an attachment            #
# --------------------------------------------------------------------------- #

class _FakeDMA:
    """Minimal SocketDMAClient stand-in; never touches a socket."""

    def __init__(self, *, connect_error=False, identify_error=False):
        self.connect_error = connect_error
        self.identify_error = identify_error
        self.enter_count = 0
        self.dma_calls = 0

    def __enter__(self):
        from c64_test_harness.backends.ultimate64_client import Ultimate64Error

        self.enter_count += 1
        if self.connect_error:
            raise Ultimate64Error("fake connect refused")
        return self

    def __exit__(self, *exc):
        return None

    def close(self):
        return None

    def dma_write(self, address, data):
        self.dma_calls += 1

    def identify(self):
        from c64_test_harness.backends.ultimate64_client import Ultimate64Error

        if self.identify_error:
            raise Ultimate64Error("fake barrier: closed by peer")
        return {"title": "FAKE"}


def _dma_transport(monkeypatch, client, fake):
    from c64_test_harness.backends.ultimate64 import Ultimate64Transport

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64.SocketDMAClient",
        lambda **kw: fake,
    )
    return Ultimate64Transport(host="fake-host", client=client, socket_dma=True)


def _bulk(n=8192):
    return bytes(i % 251 for i in range(n))


def test_socket_dma_fast_path_costs_no_attachment(monkeypatch):
    """Positive control for the fallback tests below: same transport, same
    payload, the fast path succeeds -- nothing POSTed, nothing counted."""
    c = _client(LEAKY)
    fake = _FakeDMA()
    t = _dma_transport(monkeypatch, c, fake)
    data = _bulk()
    mock, wire, ram = _device_urlopen()
    ram[0x4000:0x4000 + len(data)] = data   # as the DMA would have left it
    with patch("urllib.request.urlopen", mock):
        t.write_memory(0x4000, data)
    assert fake.dma_calls > 0
    assert wire, "sanity: the tail read-back went over REST"
    assert _writemem_posts(wire) == []
    assert c.pending_temp_attachments == 0


#: The REST fallback's wire shape depends on whether the transport chunks
#: for this grade (the chunking lane, #252, moves leak-prone writes onto
#: PUT). ``FIXED`` with hygiene forced on is the grade on which the fallback
#: is a whole-payload POST on every branch, so exact counts and the refusal
#: are pinned there; the leak-prone grade is held to the invariant that
#: matters on any branch -- every body-carrying POST is counted.


def _armed_fixed_client(**kwargs) -> Ultimate64Client:
    return _client(FIXED, temp_hygiene=True, **kwargs)


def test_socket_dma_connect_fallback_is_accounted_and_the_latch_keeps_leaking(monkeypatch):
    """A refused connect latches the fast path off for the transport's
    lifetime (``_socket_dma_unusable`` is never cleared), so every later
    bulk write takes REST -- each POST counted."""
    c = _armed_fixed_client()
    fake = _FakeDMA(connect_error=True)
    t = _dma_transport(monkeypatch, c, fake)
    mock, wire, _ = _device_urlopen()
    with patch("urllib.request.urlopen", mock):
        t.write_memory(0x4000, _bulk())
        t.write_memory(0x6000, _bulk())
    assert fake.enter_count == 1, "sanity: latched after the first failure"
    assert fake.dma_calls == 0
    assert len(_writemem_posts(wire)) == 2
    assert c.pending_temp_attachments == 2


def test_socket_dma_barrier_fallback_is_accounted(monkeypatch):
    c = _armed_fixed_client()
    fake = _FakeDMA(identify_error=True)
    t = _dma_transport(monkeypatch, c, fake)
    mock, wire, _ = _device_urlopen()
    with patch("urllib.request.urlopen", mock):
        t.write_memory(0x4000, _bulk())
    assert fake.enter_count == 2, "sanity: one retry on a fresh connection"
    assert len(_writemem_posts(wire)) == 1
    assert c.pending_temp_attachments == 1


@pytest.mark.parametrize("fake_kwargs", [
    {"connect_error": True}, {"identify_error": True},
], ids=["connect", "barrier"])
def test_socket_dma_fallback_on_leak_prone_grade_counts_every_post(monkeypatch, fake_kwargs):
    c = _client(LEAKY)
    fake = _FakeDMA(**fake_kwargs)
    t = _dma_transport(monkeypatch, c, fake)
    mock, wire, _ = _device_urlopen()
    with patch("urllib.request.urlopen", mock):
        t.write_memory(0x4000, _bulk())
    assert any("machine:writemem" in u for _, u in wire), "sanity: REST fallback ran"
    assert c.pending_temp_attachments == len(_writemem_posts(wire))


def test_socket_dma_fallback_is_refused_once_hygiene_is_known_impossible(monkeypatch):
    c = _armed_fixed_client(temp_gc_budget=1)
    fake = _FakeDMA(connect_error=True)
    t = _dma_transport(monkeypatch, c, fake)
    mock, wire, _ = _device_urlopen()
    with patch.object(c, "gc_temp_folder", side_effect=lambda **kw: _refused()), \
            patch.object(c, "set_config_item"), \
            patch("urllib.request.urlopen", mock):
        t.write_memory(0x4000, _bulk())
        with pytest.raises(Ultimate64TempHygieneError):
            t.write_memory(0x6000, _bulk())
    assert len(_writemem_posts(wire)) == 1
