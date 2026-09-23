"""``liveness_probe`` reports whether it put ``$0334-$03B3`` back (#274).

The probe writes a ``0x5A`` pattern into a span ``memory_policy`` declares
*transient* -- prior bytes written back afterwards -- and restores it at the
end.  Before #274 the restore's status was discarded: ``_liveness_request``
returns a 400/404 as an ordinary ``(code, body)``, so a rejected restore
looked like success, and a raised one was logged at DEBUG.  The pattern stayed
in memory and nothing said so.

Now every result carries ``scratch_restored``:

* ``True``  -- the probe wrote and the restore POST answered 2xx;
* ``False`` -- the probe sent its write and the original bytes were not
  confirmed written back (the restore was refused or raised, or the probe
  stopped after the write without restoring, deliberately, per #107);
* ``None``  -- the probe never sent its write, so the span is untouched.

``False`` is also logged at WARNING naming the span.  No request is added:
the probe still sends exactly the requests it sent before.

No device: the probe's ``request`` sender is a scripted fake.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
from unittest.mock import patch

import pytest

from c64_test_harness.backends import ultimate64_probe as probe_mod
from c64_test_harness.backends.ultimate64_probe import (
    LivenessResult,
    ProbeResult,
    liveness_probe,
)

_LOGGER = "c64_test_harness.backends.ultimate64_probe"
_PATTERN = bytes((i ^ 0x5A) & 0xFF for i in range(128))
_ORIGINAL = bytes(range(128))

_REACHABLE = ProbeResult(
    host="10.0.0.1", port=80, reachable=True, ping_ok=None, port_ok=True,
    api_ok=True, latency_ms=1.0, error=None,
)
_UNREACHABLE = ProbeResult(
    host="10.0.0.1", port=80, reachable=False, ping_ok=None, port_ok=False,
    api_ok=None, latency_ms=None, error="TCP connect failed",
)


class _Script:
    """A ``request`` sender answering each step from a table.

    Keys: ``info``, ``readmem``, ``write``, ``readback``, ``restore``.  A
    value is ``(status, body)`` or an exception instance to raise.
    """

    def __init__(self, **steps):
        self.steps = {
            "info": (200, json.dumps({"firmware_version": "1.1.0"}).encode()),
            "readmem": (200, _ORIGINAL),
            "write": (200, b""),
            "readback": (200, _PATTERN),
            "restore": (200, b""),
        }
        self.steps.update(steps)
        self.calls: list[tuple[str, str]] = []
        self._gets = 0
        self._posts = 0

    def __call__(self, method, host, port, path, password, timeout, **kw):
        if path == "/v1/info":
            step = "info"
        elif method == "GET":
            step = "readmem" if self._gets == 0 else "readback"
            self._gets += 1
        else:
            step = "write" if self._posts == 0 else "restore"
            self._posts += 1
        self.calls.append((method, step))
        answer = self.steps[step]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _run(script: _Script, probe=_REACHABLE) -> LivenessResult:
    with patch.object(probe_mod, "probe_u64", return_value=probe):
        return liveness_probe("10.0.0.1", request=script)


#: The probe's own logger also carries the unlocked-lane notice (#194/#460):
#: ``liveness_probe`` says once per process and host when nothing here holds
#: the device's ``DeviceLock``.  It is a true statement about a different
#: subject, and which test it lands in is decided by which one happens to
#: probe that host first -- so it is dropped here rather than allowed to
#: decide whether a restore was reported.  Everything else still counts, so
#: the ``== []`` assertions keep their full strength.
_UNLOCKED_NOTICE = "without holding this device's lock"


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == _LOGGER and r.levelno >= logging.WARNING
            and _UNLOCKED_NOTICE not in r.getMessage()]


# --------------------------------------------------------------------------- #
# The field exists and defaults compatibly                                    #
# --------------------------------------------------------------------------- #

def test_existing_constructions_still_work():
    """Downstream code builds LivenessResult positionally/by keyword with the
    original eight fields; the new one must not break that."""
    r = LivenessResult(
        host="h", port=80, healthy=False, reachable=False, writemem_ok=None,
        firmware_version=None, failure="unreachable", recommendation=None,
    )
    assert r.scratch_restored is None


# --------------------------------------------------------------------------- #
# Healthy restore -- positive control                                         #
# --------------------------------------------------------------------------- #

def test_healthy_probe_reports_the_span_restored(caplog):
    s = _Script()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s)
    assert r.healthy is True
    assert r.scratch_restored is True
    assert _warnings(caplog) == []
    assert [c for c in s.calls if c[0] == "POST"] == [("POST", "write"), ("POST", "restore")]


# --------------------------------------------------------------------------- #
# The two silent paths from the issue                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", [400, 404, 500])
def test_restore_rejected_with_an_http_status_is_reported(caplog, status):
    """Cause 1: the returned status was ignored entirely."""
    s = _Script(restore=(status, b"nope"))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s)
    assert r.scratch_restored is False
    msgs = _warnings(caplog)
    assert any("$0334" in m and str(status) in m for m in msgs), msgs


@pytest.mark.parametrize("exc", [
    socket.timeout("timed out"),
    ConnectionResetError("reset"),
    urllib.error.URLError("refused"),
])
def test_restore_that_raises_is_reported_at_warning(caplog, exc):
    """Cause 2: the exception path logged at DEBUG only."""
    s = _Script(restore=exc)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s)
    assert r.scratch_restored is False
    assert any("$0334" in m for m in _warnings(caplog)), _warnings(caplog)


def test_restore_rejected_on_the_mismatch_branch_is_reported(caplog):
    s = _Script(readback=(200, bytes(128)), restore=(404, b""))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s)
    assert r.failure == "unknown"
    assert r.scratch_restored is False
    assert any("$0334" in m for m in _warnings(caplog))


def test_restore_succeeding_on_the_mismatch_branch_is_reported_true():
    s = _Script(readback=(200, bytes(128)))
    r = _run(s)
    assert r.failure == "unknown"
    assert r.scratch_restored is True


# --------------------------------------------------------------------------- #
# Branches that stop after the write without restoring (deliberately)         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("steps, failure", [
    ({"write": socket.timeout("t")}, "writemem_timeout"),
    ({"write": ConnectionResetError("r")}, "connection_reset"),
    ({"write": (404, b"")}, "writemem_404"),
    ({"write": (400, b"")}, "wire_format"),
    ({"write": (500, b"")}, "unknown"),
    ({"readback": ConnectionResetError("r")}, "connection_reset"),
    ({"readback": socket.timeout("t")}, "tcp_stack_wedged"),
], ids=["write-timeout", "write-reset", "write-404", "write-400", "write-500",
        "readback-reset", "readback-timeout"])
def test_a_probe_that_sent_its_write_and_did_not_restore_says_so(caplog, steps, failure):
    s = _Script(**steps)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s)
    assert r.failure == failure
    assert r.scratch_restored is False
    assert any("$0334" in m for m in _warnings(caplog)), _warnings(caplog)
    # No restore is attempted on these branches (#107: no further POSTs
    # against an endpoint that just failed) -- reporting adds no request.
    assert [c for c in s.calls if c == ("POST", "restore")] == []


# --------------------------------------------------------------------------- #
# Branches that never wrote                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("steps, probe, failure", [
    ({}, _UNREACHABLE, "unreachable"),
    ({"readmem": (404, b"")}, _REACHABLE, "unknown"),
    ({"readmem": (400, b"")}, _REACHABLE, "wire_format"),
    ({"readmem": socket.timeout("t")}, _REACHABLE, "tcp_stack_wedged"),
    ({"readmem": ConnectionResetError("r")}, _REACHABLE, "connection_reset"),
], ids=["unreachable", "readmem-404", "readmem-400", "readmem-timeout", "readmem-reset"])
def test_a_probe_that_never_wrote_reports_none_and_does_not_warn(caplog, steps, probe, failure):
    s = _Script(**steps)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s, probe=probe)
    assert r.failure == failure
    assert r.scratch_restored is None
    assert not any("$0334" in m for m in _warnings(caplog))
    assert [c for c in s.calls if c[0] == "POST"] == []


# --------------------------------------------------------------------------- #
# The client path carries the field through                                   #
# --------------------------------------------------------------------------- #

def test_client_liveness_probe_carries_the_restore_status(monkeypatch):
    from c64_test_harness.backends.ultimate64_client import Ultimate64Client

    for var in ("U64_AUTO_TEMP_GC", "U64_TEMP_GC_REQUIRED"):
        monkeypatch.delenv(var, raising=False)
    c = Ultimate64Client("10.0.0.1", write_mem_query_threshold=128,
                         warn_unlocked=False, temp_hygiene=False)
    s = _Script(restore=(404, b""))

    def _sender(method, host, port, path, password, timeout, **kw):
        return s(method, host, port, path, password, timeout, **kw)

    with patch.object(probe_mod, "probe_u64", return_value=_REACHABLE), \
            patch.object(probe_mod, "_liveness_request", _sender):
        r = c.liveness_probe()
    assert r.scratch_restored is False


# --------------------------------------------------------------------------- #
# #328: a refused restore keeps healthy=True                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("restore", [
    (404, b""), (400, b""), (500, b""),
    socket.timeout("t"), ConnectionResetError("r"),
], ids=["404", "400", "500", "timeout", "reset"])
def test_a_refused_restore_keeps_the_probe_healthy(caplog, restore):
    """Owner decision on #328: ``healthy`` is the probe write's round trip.

    The restore outcome is carried by ``scratch_restored`` and a WARNING,
    never by demoting ``healthy`` and never by a retry POST (#107).
    """
    s = _Script(restore=restore)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = _run(s)
    assert (r.healthy, r.failure, r.writemem_ok, r.scratch_restored) == (
        True, None, True, False
    )
    assert any("$0334" in m for m in _warnings(caplog)), _warnings(caplog)
    assert [c for c in s.calls if c[0] == "POST"] == [("POST", "write"), ("POST", "restore")]


def test_assert_healthy_passes_on_a_refused_restore(monkeypatch, caplog):
    """The client gate follows ``healthy``: no raise, the flag on the result."""
    from c64_test_harness.backends.ultimate64_client import Ultimate64Client

    for var in ("U64_AUTO_TEMP_GC", "U64_TEMP_GC_REQUIRED"):
        monkeypatch.delenv(var, raising=False)
    c = Ultimate64Client("10.0.0.1", write_mem_query_threshold=128,
                         warn_unlocked=False, temp_hygiene=False)
    s = _Script(restore=(404, b""))

    def _sender(method, host, port, path, password, timeout, **kw):
        return s(method, host, port, path, password, timeout, **kw)

    with patch.object(probe_mod, "probe_u64", return_value=_REACHABLE), \
            patch.object(probe_mod, "_liveness_request", _sender), \
            caplog.at_level(logging.DEBUG, logger=_LOGGER):
        r = c.assert_healthy()
    assert r.healthy is True
    assert r.scratch_restored is False
    assert any("$0334" in m for m in _warnings(caplog)), _warnings(caplog)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_328_contract_is_written_where_callers_read_it():
    """Docs pin: the ``healthy`` field, ``assert_healthy`` and the recovery
    doc all say a refused restore does not demote ``healthy``."""
    from pathlib import Path

    from c64_test_harness.backends.ultimate64_client import Ultimate64Client

    field = _flat(LivenessResult.__doc__ or "")
    assert "does **not** demote it" in field and "#328" in field, field
    gate = _flat(Ultimate64Client.assert_healthy.__doc__ or "")
    assert "Success includes a refused restore" in gate and "#328" in gate
    recovery = _flat((Path(__file__).resolve().parent.parent
                      / "docs" / "u64_recovery.md").read_text())
    assert "A refused restore does not make the probe unhealthy" in recovery
    assert "healthy=True, failure=None, scratch_restored=False" in recovery


def test_summary_mentions_a_dirty_span():
    r = LivenessResult(
        host="h", port=80, healthy=True, reachable=True, writemem_ok=True,
        firmware_version="1.1.0", failure=None, recommendation=None,
        scratch_restored=False,
    )
    assert "$0334" in r.summary
    clean = LivenessResult(
        host="h", port=80, healthy=True, reachable=True, writemem_ok=True,
        firmware_version="1.1.0", failure=None, recommendation=None,
        scratch_restored=True,
    )
    assert "$0334" not in clean.summary
