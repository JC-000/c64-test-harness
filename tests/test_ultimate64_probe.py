"""Unit tests for ultimate64_probe (no network)."""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.ultimate64_probe import (
    LivenessResult,
    ProbeResult,
    check_api,
    check_port,
    is_u64_reachable,
    liveness_probe,
    ping_host,
    probe_u64,
)


# ---- ping_host ---------------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.subprocess.run")
def test_ping_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    ok, lat = ping_host("10.0.0.1", timeout=2.0)
    assert ok is True
    assert lat is not None and lat >= 0
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "ping"
    assert "-c" in args and "1" in args


@patch("c64_test_harness.backends.ultimate64_probe.subprocess.run")
def test_ping_timeout(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ping", timeout=2.0)
    ok, lat = ping_host("10.0.0.1", timeout=2.0)
    assert ok is False
    assert lat is None


@patch("c64_test_harness.backends.ultimate64_probe.subprocess.run")
def test_ping_no_binary(mock_run):
    mock_run.side_effect = FileNotFoundError("ping not found")
    ok, lat = ping_host("10.0.0.1")
    assert ok is False
    assert lat is None


@patch("c64_test_harness.backends.ultimate64_probe.subprocess.run")
def test_ping_nonzero_exit(mock_run):
    mock_run.return_value = MagicMock(returncode=1)
    ok, lat = ping_host("10.0.0.1")
    assert ok is False
    assert lat is None


# ---- check_port --------------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.socket.create_connection")
def test_check_port_success(mock_conn):
    mock_sock = MagicMock()
    mock_conn.return_value = mock_sock
    ok, lat = check_port("10.0.0.1", port=80)
    assert ok is True
    assert lat is not None and lat >= 0
    mock_sock.close.assert_called_once()


@patch("c64_test_harness.backends.ultimate64_probe.socket.create_connection")
def test_check_port_refused(mock_conn):
    mock_conn.side_effect = ConnectionRefusedError("refused")
    ok, lat = check_port("10.0.0.1", port=80)
    assert ok is False
    assert lat is None


@patch("c64_test_harness.backends.ultimate64_probe.socket.create_connection")
def test_check_port_timeout(mock_conn):
    mock_conn.side_effect = socket.timeout("timed out")
    ok, lat = check_port("10.0.0.1", port=80, timeout=1.0)
    assert ok is False
    assert lat is None


# ---- check_api ---------------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_check_api_success(mock_urlopen):
    version_data = {"version": "1.0", "api": "v1"}
    resp = MagicMock()
    resp.read.return_value = json.dumps(version_data).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = resp

    ok, ver = check_api("10.0.0.1")
    assert ok is True
    assert ver == version_data


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_check_api_error_response(mock_urlopen):
    from urllib.error import HTTPError

    mock_urlopen.side_effect = HTTPError(
        url="http://10.0.0.1/v1/version",
        code=500,
        msg="Internal Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b"error"),
    )
    ok, ver = check_api("10.0.0.1")
    assert ok is False
    assert ver is None


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_check_api_timeout(mock_urlopen):
    mock_urlopen.side_effect = socket.timeout("timed out")
    ok, ver = check_api("10.0.0.1", timeout=1.0)
    assert ok is False
    assert ver is None


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_check_api_with_password(mock_urlopen):
    resp = MagicMock()
    resp.read.return_value = b'{"version": "1.0"}'
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = resp

    ok, _ = check_api("10.0.0.1", password="secret")
    assert ok is True
    # Verify the request had the password header.
    req = mock_urlopen.call_args[0][0]
    assert req.get_header("X-password") == "secret"


# ---- probe_u64 ---------------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.check_api")
@patch("c64_test_harness.backends.ultimate64_probe.check_port")
@patch("c64_test_harness.backends.ultimate64_probe.ping_host")
def test_probe_all_pass(mock_ping, mock_port, mock_api):
    mock_ping.return_value = (True, 1.5)
    mock_port.return_value = (True, 0.8)
    mock_api.return_value = (True, {"version": "1.0"})

    r = probe_u64("10.0.0.1")
    assert r.reachable is True
    assert r.ping_ok is True
    assert r.port_ok is True
    assert r.api_ok is True
    assert r.latency_ms == 0.8  # fastest
    assert r.error is None


@patch("c64_test_harness.backends.ultimate64_probe.check_api")
@patch("c64_test_harness.backends.ultimate64_probe.check_port")
@patch("c64_test_harness.backends.ultimate64_probe.ping_host")
def test_probe_ping_fails_stops_early(mock_ping, mock_port, mock_api):
    mock_ping.return_value = (False, None)

    r = probe_u64("10.0.0.1")
    assert r.reachable is False
    assert r.ping_ok is False
    assert r.port_ok is None
    assert r.api_ok is None
    assert "ping failed" in r.error
    mock_port.assert_not_called()
    mock_api.assert_not_called()


@patch("c64_test_harness.backends.ultimate64_probe.check_api")
@patch("c64_test_harness.backends.ultimate64_probe.check_port")
@patch("c64_test_harness.backends.ultimate64_probe.ping_host")
def test_probe_tcp_fails_stops_early(mock_ping, mock_port, mock_api):
    mock_ping.return_value = (True, 1.0)
    mock_port.return_value = (False, None)

    r = probe_u64("10.0.0.1")
    assert r.reachable is False
    assert r.ping_ok is True
    assert r.port_ok is False
    assert r.api_ok is None
    assert "TCP connect failed" in r.error
    mock_api.assert_not_called()


@patch("c64_test_harness.backends.ultimate64_probe.check_api")
@patch("c64_test_harness.backends.ultimate64_probe.check_port")
@patch("c64_test_harness.backends.ultimate64_probe.ping_host")
def test_probe_api_fails(mock_ping, mock_port, mock_api):
    mock_ping.return_value = (True, 1.0)
    mock_port.return_value = (True, 0.5)
    mock_api.return_value = (False, None)

    r = probe_u64("10.0.0.1")
    assert r.reachable is False
    assert r.ping_ok is True
    assert r.port_ok is True
    assert r.api_ok is False
    assert "API not responding" in r.error


@patch("c64_test_harness.backends.ultimate64_probe.check_api")
@patch("c64_test_harness.backends.ultimate64_probe.check_port")
@patch("c64_test_harness.backends.ultimate64_probe.ping_host")
def test_probe_skip_ping(mock_ping, mock_port, mock_api):
    mock_port.return_value = (True, 0.5)
    mock_api.return_value = (True, {"version": "1.0"})

    r = probe_u64("10.0.0.1", skip_ping=True)
    assert r.reachable is True
    assert r.ping_ok is None
    mock_ping.assert_not_called()


@patch("c64_test_harness.backends.ultimate64_probe.check_api")
@patch("c64_test_harness.backends.ultimate64_probe.check_port")
@patch("c64_test_harness.backends.ultimate64_probe.ping_host")
def test_probe_skip_api(mock_ping, mock_port, mock_api):
    mock_ping.return_value = (True, 1.0)
    mock_port.return_value = (True, 0.5)

    r = probe_u64("10.0.0.1", skip_api=True)
    assert r.reachable is True
    assert r.api_ok is None
    mock_api.assert_not_called()


# ---- is_u64_reachable --------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_is_u64_reachable_true(mock_probe):
    mock_probe.return_value = ProbeResult(
        host="10.0.0.1", port=80, reachable=True,
        ping_ok=True, port_ok=True, api_ok=True,
        latency_ms=1.0, error=None,
    )
    assert is_u64_reachable("10.0.0.1") is True


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_is_u64_reachable_false(mock_probe):
    mock_probe.return_value = ProbeResult(
        host="10.0.0.1", port=80, reachable=False,
        ping_ok=False, port_ok=None, api_ok=None,
        latency_ms=None, error="ping failed",
    )
    assert is_u64_reachable("10.0.0.1") is False


# ---- ProbeResult.summary -----------------------------------------------


def test_summary_reachable():
    r = ProbeResult(
        host="10.0.0.1", port=80, reachable=True,
        ping_ok=True, port_ok=True, api_ok=True,
        latency_ms=1.23, error=None,
    )
    assert "reachable" in r.summary
    assert "1.2ms" in r.summary


def test_summary_reachable_no_latency():
    r = ProbeResult(
        host="10.0.0.1", port=80, reachable=True,
        ping_ok=None, port_ok=True, api_ok=None,
        latency_ms=None, error=None,
    )
    assert "reachable" in r.summary
    assert "ms" not in r.summary


def test_summary_unreachable():
    r = ProbeResult(
        host="10.0.0.1", port=80, reachable=False,
        ping_ok=False, port_ok=None, api_ok=None,
        latency_ms=None, error="ping failed",
    )
    assert "UNREACHABLE" in r.summary
    assert "ping failed" in r.summary


# ---- Manager integration with probe ------------------------------------


@patch("c64_test_harness.backends.ultimate64_manager.probe_u64")
@patch("c64_test_harness.backends.ultimate64_manager.Ultimate64Transport")
def test_manager_acquire_probes_device(mock_transport, mock_probe):
    """Manager probes device before creating transport."""
    from c64_test_harness.backends.ultimate64_manager import (
        Ultimate64Device,
        Ultimate64InstanceManager,
    )

    mock_probe.return_value = ProbeResult(
        host="10.0.0.1", port=80, reachable=True,
        ping_ok=True, port_ok=True, api_ok=None,
        latency_ms=1.0, error=None,
    )
    mock_transport.return_value = MagicMock()

    devices = [Ultimate64Device(host="10.0.0.1")]
    mgr = Ultimate64InstanceManager(devices)
    inst = mgr.acquire()
    assert inst is not None
    mock_probe.assert_called_once()
    # skip_api should be True
    assert mock_probe.call_args[1].get("skip_api") is True
    mgr.release(inst)


@patch("c64_test_harness.backends.ultimate64_manager.probe_u64")
@patch("c64_test_harness.backends.ultimate64_manager.Ultimate64Transport")
def test_manager_acquire_skips_unreachable_device(mock_transport, mock_probe):
    """Manager skips unreachable device, acquires the next one."""
    from c64_test_harness.backends.ultimate64_manager import (
        Ultimate64Device,
        Ultimate64InstanceManager,
    )

    bad = ProbeResult(
        host="10.0.0.1", port=80, reachable=False,
        ping_ok=False, port_ok=None, api_ok=None,
        latency_ms=None, error="ping failed",
    )
    good = ProbeResult(
        host="10.0.0.2", port=80, reachable=True,
        ping_ok=True, port_ok=True, api_ok=None,
        latency_ms=1.0, error=None,
    )
    mock_probe.side_effect = [bad, good]
    mock_transport.return_value = MagicMock()

    devices = [
        Ultimate64Device(host="10.0.0.1"),
        Ultimate64Device(host="10.0.0.2"),
    ]
    mgr = Ultimate64InstanceManager(devices)
    inst = mgr.acquire()
    assert inst.device.host == "10.0.0.2"
    mgr.release(inst)


@patch("c64_test_harness.backends.ultimate64_manager.probe_u64")
def test_manager_acquire_all_fail_probe(mock_probe):
    """Manager raises PoolExhaustedError when all devices fail probe."""
    from c64_test_harness.backends.ultimate64_manager import (
        Ultimate64Device,
        Ultimate64InstanceManager,
        Ultimate64PoolExhaustedError,
    )

    mock_probe.return_value = ProbeResult(
        host="10.0.0.1", port=80, reachable=False,
        ping_ok=False, port_ok=None, api_ok=None,
        latency_ms=None, error="ping failed",
    )

    devices = [Ultimate64Device(host="10.0.0.1")]
    mgr = Ultimate64InstanceManager(devices)
    with pytest.raises(Ultimate64PoolExhaustedError, match="failed liveness probe"):
        mgr.acquire()


@patch("c64_test_harness.backends.ultimate64_manager.probe_u64")
def test_manager_failed_device_goes_to_end(mock_probe):
    """Failed device is pushed to the end of the available list."""
    from c64_test_harness.backends.ultimate64_manager import (
        Ultimate64Device,
        Ultimate64InstanceManager,
        Ultimate64PoolExhaustedError,
    )

    mock_probe.return_value = ProbeResult(
        host="x", port=80, reachable=False,
        ping_ok=False, port_ok=None, api_ok=None,
        latency_ms=None, error="fail",
    )

    devices = [
        Ultimate64Device(host="10.0.0.1"),
        Ultimate64Device(host="10.0.0.2"),
    ]
    mgr = Ultimate64InstanceManager(devices)
    with pytest.raises(Ultimate64PoolExhaustedError):
        mgr.acquire()
    # Both devices should still be in the pool (returned to end).
    assert mgr.available_count == 2


# ============================================================================ #
# liveness_probe (issue #107)                                                  #
# ============================================================================ #
#
# These tests mock the network primitives at two layers:
#
#   * ``probe_u64`` is patched to control the reachability outcome.
#   * ``urllib.request.urlopen`` (within ultimate64_probe) is patched to
#     control GET /v1/info, GET /v1/machine:readmem, and POST
#     /v1/machine:writemem responses.
#
# The expected order of urlopen calls on a healthy device is:
#   1. GET /v1/info          (firmware version)
#   2. GET /v1/machine:readmem (read original 128 bytes)
#   3. POST /v1/machine:writemem (write probe pattern)
#   4. GET /v1/machine:readmem (readback to verify)
#   5. POST /v1/machine:writemem (restore original)


_HEALTHY_PROBE = ProbeResult(
    host="10.0.0.1", port=80, reachable=True,
    ping_ok=None, port_ok=True, api_ok=True,
    latency_ms=1.0, error=None,
)

_UNREACHABLE_PROBE = ProbeResult(
    host="10.0.0.1", port=80, reachable=False,
    ping_ok=None, port_ok=False, api_ok=None,
    latency_ms=None, error="TCP connect failed",
)


def _make_urlopen_response(status: int, body: bytes) -> MagicMock:
    """Build a context-manager-compatible MagicMock for urlopen()."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://10.0.0.1/v1/machine:writemem",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body),
    )


# ---- unreachable -------------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_unreachable_reports_unreachable(mock_probe):
    """When probe_u64 says unreachable, liveness_probe short-circuits."""
    mock_probe.return_value = _UNREACHABLE_PROBE

    r = liveness_probe("10.0.0.1")
    assert isinstance(r, LivenessResult)
    assert r.healthy is False
    assert r.reachable is False
    assert r.writemem_ok is None
    assert r.firmware_version is None
    assert r.failure == "unreachable"
    assert r.recommendation is not None


# ---- healthy fw 3.14d --------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_healthy_fw_314d(mock_probe, mock_urlopen):
    """Healthy fw 3.14d: GET info, readmem, POST writemem, readback,
    restore.  Probe pattern round-trips successfully."""
    mock_probe.return_value = _HEALTHY_PROBE

    # Build the probe pattern the implementation will use (i ^ 0x5A).
    expected_pattern = bytes((i ^ 0x5A) & 0xFF for i in range(128))
    original_bytes = b"\x00" * 128

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, original_bytes),     # readmem
        _make_urlopen_response(200, b""),                 # POST writemem
        _make_urlopen_response(200, expected_pattern),    # readback
        _make_urlopen_response(200, b""),                 # restore POST
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is True
    assert r.reachable is True
    assert r.writemem_ok is True
    assert r.firmware_version == "V3.14d"
    assert r.failure is None
    assert r.recommendation is None
    # All 5 urlopen calls were made (info + readmem + write + readback + restore).
    assert mock_urlopen.call_count == 5


# ---- writemem 404 ------------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_writemem_404_degraded(mock_probe, mock_urlopen):
    """fw 3.14d writemem-degraded: POST returns 404; liveness_probe
    reports failure='writemem_404' and does NOT retry."""
    mock_probe.return_value = _HEALTHY_PROBE

    original_bytes = b"\xFF" * 128

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, original_bytes),
        # POST writemem returns 404.  urllib raises HTTPError for non-2xx.
        _http_error(404, b"Could not read data from attachment"),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.reachable is True
    assert r.writemem_ok is False
    assert r.failure == "writemem_404"
    assert r.firmware_version == "V3.14d"
    assert r.recommendation is not None
    assert "404" in r.recommendation or "writemem-degraded" in r.recommendation
    # Exactly one writemem POST -- no retry.
    # Count POSTs by looking at the calls.
    post_calls = [
        c for c in mock_urlopen.call_args_list
        if c.args and getattr(c.args[0], "get_method", lambda: "")() == "POST"
    ]
    assert len(post_calls) == 1, (
        f"liveness_probe MUST NOT retry POST on 404; got {len(post_calls)} POSTs"
    )


# ---- tcp_stack_wedged --------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_tcp_stack_wedged_on_writemem_timeout(mock_probe, mock_urlopen):
    """POST writemem timing out -> failure='writemem_timeout'."""
    mock_probe.return_value = _HEALTHY_PROBE

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, b"\x00" * 128),
        socket.timeout("write timeout"),  # POST writemem times out
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.reachable is True
    assert r.writemem_ok is False
    assert r.failure == "writemem_timeout"
    assert r.recommendation is not None
    assert "power-cycle" in r.recommendation.lower()


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_tcp_stack_wedged_on_readmem_timeout(mock_probe, mock_urlopen):
    """If initial readmem (to capture original bytes) times out, we tag
    failure='tcp_stack_wedged' — REST answered version but the readmem
    socket gave up, which is the wedged-stack signature."""
    mock_probe.return_value = _HEALTHY_PROBE

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        socket.timeout("readmem timeout"),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.reachable is True
    assert r.writemem_ok is None
    assert r.failure == "tcp_stack_wedged"


# ---- connection_reset (one-shot TCP RST mid-request) ------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_connection_reset_on_readmem(mock_probe, mock_urlopen):
    """A TCP RST on the pre-POST readmem -> failure='connection_reset'."""
    mock_probe.return_value = _HEALTHY_PROBE

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        ConnectionResetError(54, "Connection reset by peer"),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.reachable is True
    assert r.writemem_ok is None
    assert r.failure == "connection_reset"
    assert r.recommendation is not None
    assert "retry" in r.recommendation.lower()


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_connection_reset_on_writemem_post(mock_probe, mock_urlopen):
    """A TCP RST on the POST writemem itself -> failure='connection_reset'.

    Important: there must be exactly one POST attempt; the probe must
    NOT retry internally (per issue #107, repeated POSTs against a
    degraded endpoint are the documented TCP-wedge trigger).
    """
    mock_probe.return_value = _HEALTHY_PROBE

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, b"\x00" * 128),
        ConnectionResetError(54, "Connection reset by peer"),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.reachable is True
    assert r.writemem_ok is False
    assert r.failure == "connection_reset"
    assert r.recommendation is not None
    assert "retry" in r.recommendation.lower()
    # Exactly one POST: 3 urlopen calls total (info, readmem, POST).
    assert mock_urlopen.call_count == 3


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_connection_reset_on_readback(mock_probe, mock_urlopen):
    """A TCP RST on the post-POST readback -> failure='connection_reset'.

    The writemem POST already happened, so round-trip cannot be confirmed;
    the recommendation should suggest a retry, not assume wedged.
    """
    mock_probe.return_value = _HEALTHY_PROBE

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, b"\x00" * 128),
        _make_urlopen_response(200, b""),
        ConnectionResetError(54, "Connection reset by peer"),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.reachable is True
    assert r.writemem_ok is False
    assert r.failure == "connection_reset"


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_broken_pipe_treated_as_connection_reset(mock_probe, mock_urlopen):
    """BrokenPipeError is the symmetric write-side RST — same tag."""
    mock_probe.return_value = _HEALTHY_PROBE

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, b"\x00" * 128),
        BrokenPipeError(32, "Broken pipe"),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.failure == "connection_reset"


# ---- unknown firmware --------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_unknown_firmware_still_probes(mock_probe, mock_urlopen):
    """If GET /v1/info fails / returns no firmware_version, the probe
    still proceeds with the safe 128-byte payload."""
    mock_probe.return_value = _HEALTHY_PROBE

    expected_pattern = bytes((i ^ 0x5A) & 0xFF for i in range(128))
    original_bytes = b"\x42" * 128

    mock_urlopen.side_effect = [
        # /v1/info returns an unexpected shape (no firmware_version key).
        _make_urlopen_response(200, b'{"product": "U64"}'),
        _make_urlopen_response(200, original_bytes),
        _make_urlopen_response(200, b""),
        _make_urlopen_response(200, expected_pattern),
        _make_urlopen_response(200, b""),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is True
    assert r.firmware_version is None
    assert r.writemem_ok is True


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_info_endpoint_404_still_probes(mock_probe, mock_urlopen):
    """Even if /v1/info returns 404 (firmware too old / unknown), the
    probe still attempts the writemem round-trip."""
    mock_probe.return_value = _HEALTHY_PROBE

    expected_pattern = bytes((i ^ 0x5A) & 0xFF for i in range(128))

    mock_urlopen.side_effect = [
        _http_error(404, b"not found"),  # info 404 → firmware unknown
        _make_urlopen_response(200, b"\x00" * 128),
        _make_urlopen_response(200, b""),
        _make_urlopen_response(200, expected_pattern),
        _make_urlopen_response(200, b""),
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is True
    assert r.firmware_version is None


# ---- readback mismatch -------------------------------------------------


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
def test_liveness_readback_mismatch_reports_unknown(mock_probe, mock_urlopen):
    """POST succeeds (200) but readback returns wrong bytes -> 'unknown'."""
    mock_probe.return_value = _HEALTHY_PROBE

    wrong_bytes = b"\x99" * 128  # not the probe pattern

    mock_urlopen.side_effect = [
        _make_urlopen_response(
            200, json.dumps({"firmware_version": "V3.14d"}).encode("utf-8"),
        ),
        _make_urlopen_response(200, b"\x00" * 128),
        _make_urlopen_response(200, b""),
        _make_urlopen_response(200, wrong_bytes),
        _make_urlopen_response(200, b""),  # restore best-effort
    ]

    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.writemem_ok is False
    assert r.failure == "unknown"
    assert r.recommendation is not None
    assert "readback" in r.recommendation.lower()


# ---- LivenessResult.summary --------------------------------------------


def test_liveness_summary_healthy():
    r = LivenessResult(
        host="10.0.0.1", port=80, healthy=True, reachable=True,
        writemem_ok=True, firmware_version="V3.14d",
        failure=None, recommendation=None,
    )
    assert "healthy" in r.summary
    assert "V3.14d" in r.summary


def test_liveness_summary_unhealthy():
    r = LivenessResult(
        host="10.0.0.1", port=80, healthy=False, reachable=True,
        writemem_ok=False, firmware_version=None,
        failure="writemem_404", recommendation="rebooty",
    )
    assert "UNHEALTHY" in r.summary
    assert "writemem_404" in r.summary


# ---- Ultimate64Client.liveness_probe / assert_healthy ------------------


@patch(
    "c64_test_harness.backends.ultimate64_probe.liveness_probe",
)
def test_client_liveness_probe_delegates(mock_lp):
    """Ultimate64Client.liveness_probe() delegates to the free function
    with host/port/password from the client."""
    from c64_test_harness.backends.ultimate64_client import Ultimate64Client

    sentinel = LivenessResult(
        host="10.0.0.1", port=80, healthy=True, reachable=True,
        writemem_ok=True, firmware_version="V3.14d",
        failure=None, recommendation=None,
    )
    mock_lp.return_value = sentinel

    # Construct client without firmware autodetection.
    client = Ultimate64Client(
        "10.0.0.1", password="secret", write_mem_query_threshold=128,
    )
    r = client.liveness_probe()
    assert r is sentinel
    mock_lp.assert_called_once_with(
        "10.0.0.1", port=80, password="secret", http_timeout=2.0,
    )


@patch(
    "c64_test_harness.backends.ultimate64_probe.liveness_probe",
)
def test_client_assert_healthy_passes(mock_lp):
    from c64_test_harness.backends.ultimate64_client import Ultimate64Client

    mock_lp.return_value = LivenessResult(
        host="10.0.0.1", port=80, healthy=True, reachable=True,
        writemem_ok=True, firmware_version="V3.14d",
        failure=None, recommendation=None,
    )
    client = Ultimate64Client("10.0.0.1", write_mem_query_threshold=128)
    r = client.assert_healthy()
    assert r.healthy is True


@patch(
    "c64_test_harness.backends.ultimate64_probe.liveness_probe",
)
def test_client_assert_healthy_raises_unreachable(mock_lp):
    from c64_test_harness.backends.ultimate64_client import (
        U64UnreachableError,
        Ultimate64Client,
    )

    mock_lp.return_value = LivenessResult(
        host="10.0.0.1", port=80, healthy=False, reachable=False,
        writemem_ok=None, firmware_version=None,
        failure="unreachable", recommendation="no TCP",
    )
    client = Ultimate64Client("10.0.0.1", write_mem_query_threshold=128)
    with pytest.raises(U64UnreachableError):
        client.assert_healthy()


@patch(
    "c64_test_harness.backends.ultimate64_probe.liveness_probe",
)
def test_client_assert_healthy_raises_writemem_degraded(mock_lp):
    from c64_test_harness.backends.ultimate64_client import (
        U64WritememDegradedError,
        Ultimate64Client,
    )

    degraded = LivenessResult(
        host="10.0.0.1", port=80, healthy=False, reachable=True,
        writemem_ok=False, firmware_version="V3.14d",
        failure="writemem_404", recommendation="power-cycle",
    )
    mock_lp.return_value = degraded
    client = Ultimate64Client("10.0.0.1", write_mem_query_threshold=128)
    with pytest.raises(U64WritememDegradedError) as excinfo:
        client.assert_healthy()
    assert excinfo.value.result is degraded
