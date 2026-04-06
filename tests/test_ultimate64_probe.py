"""Unit tests for ultimate64_probe (no network)."""

from __future__ import annotations

import json
import socket
import subprocess
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.ultimate64_probe import (
    ProbeResult,
    check_api,
    check_port,
    is_u64_reachable,
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
