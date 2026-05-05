"""Liveness probe for Ultimate 64 hardware devices.

Quick reachability checks — ICMP ping, TCP connect, REST API version —
useful for pre-flight validation before creating a transport.  All
functions use only the standard library (``subprocess``, ``socket``,
``urllib.request``).

Each check returns a ``(ok, detail)`` tuple so callers can inspect
individual results.  The top-level :func:`probe_u64` runs all checks
in sequence (fail-fast) and returns a :class:`ProbeResult` dataclass.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = [
    "ProbeResult",
    "ping_host",
    "check_port",
    "check_api",
    "probe_u64",
    "is_u64_reachable",
]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    """Aggregated result of a liveness probe against one U64 device."""

    host: str
    port: int
    reachable: bool
    ping_ok: bool | None
    port_ok: bool | None
    api_ok: bool | None
    latency_ms: float | None
    error: str | None

    @property
    def summary(self) -> str:
        """One-line status for logging."""
        if self.reachable:
            lat = f" ({self.latency_ms:.1f}ms)" if self.latency_ms is not None else ""
            return f"U64 at {self.host}:{self.port} reachable{lat}"
        return f"U64 at {self.host}:{self.port} UNREACHABLE: {self.error}"


def ping_host(host: str, timeout: float = 2.0) -> tuple[bool, float | None]:
    """ICMP ping via subprocess.  Returns ``(ok, latency_ms)``.

    Uses ``ping -c 1 -W <timeout>`` on Linux.  Catches all exceptions
    (permission denied, no ping binary, etc).  Returns ``(False, None)``
    on any failure.
    """
    t0 = time.monotonic()
    try:
        # -W accepts integer seconds on most Linux ping implementations.
        wait_sec = max(1, int(timeout))
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(wait_sec), host],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 1.0,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if result.returncode == 0:
            _log.debug("ping %s ok (%.1fms)", host, elapsed_ms)
            return True, elapsed_ms
        _log.debug("ping %s failed (rc=%d)", host, result.returncode)
        return False, None
    except Exception as exc:
        _log.debug("ping %s exception: %s", host, exc)
        return False, None


def check_port(
    host: str, port: int = 80, timeout: float = 2.0
) -> tuple[bool, float | None]:
    """TCP connect to *host*:*port*.  Returns ``(ok, latency_ms)``.

    Uses :func:`socket.create_connection`.  Closes the socket
    immediately after a successful connect.
    """
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        sock.close()
        _log.debug("TCP %s:%d ok (%.1fms)", host, port, elapsed_ms)
        return True, elapsed_ms
    except Exception as exc:
        _log.debug("TCP %s:%d failed: %s", host, port, exc)
        return False, None


def check_api(
    host: str,
    port: int = 80,
    timeout: float = 3.0,
    password: str | None = None,
) -> tuple[bool, dict | None]:
    """GET ``/v1/version``.  Returns ``(ok, version_dict_or_None)``.

    Uses :mod:`urllib.request` with a short timeout.  Adds an
    ``X-Password`` header when *password* is set.
    """
    base = f"http://{host}:{port}" if port != 80 else f"http://{host}"
    url = f"{base}/v1/version"
    req = urllib.request.Request(url, method="GET")
    if password:
        req.add_header("X-Password", password)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            version = json.loads(data.decode("utf-8"))
            _log.debug("API %s ok: %s", url, version)
            return True, version
    except Exception as exc:
        _log.debug("API %s failed: %s", url, exc)
        return False, None


def probe_u64(
    host: str,
    port: int = 80,
    password: str | None = None,
    ping_timeout: float = 2.0,
    tcp_timeout: float = 2.0,
    api_timeout: float = 3.0,
    skip_ping: bool = False,
    skip_api: bool = False,
) -> ProbeResult:
    """Full liveness probe: ping -> TCP connect -> API version check.

    Fail-fast: if ping fails, remaining checks are skipped.  If TCP
    fails, the API check is skipped.
    """
    ping_result: bool | None = None
    port_result: bool | None = None
    api_result: bool | None = None
    best_latency: float | None = None
    error: str | None = None

    # --- ping ---
    if not skip_ping:
        ok, lat = ping_host(host, timeout=ping_timeout)
        ping_result = ok
        if lat is not None and (best_latency is None or lat < best_latency):
            best_latency = lat
        if not ok:
            error = (
                f"U64 at {host} unreachable "
                f"(ping failed, timeout {ping_timeout}s)"
            )
            return ProbeResult(
                host=host,
                port=port,
                reachable=False,
                ping_ok=ping_result,
                port_ok=None,
                api_ok=None,
                latency_ms=best_latency,
                error=error,
            )

    # --- TCP connect ---
    ok, lat = check_port(host, port=port, timeout=tcp_timeout)
    port_result = ok
    if lat is not None and (best_latency is None or lat < best_latency):
        best_latency = lat
    if not ok:
        error = (
            f"U64 at {host} port {port} not responding "
            f"(TCP connect failed, timeout {tcp_timeout}s)"
        )
        return ProbeResult(
            host=host,
            port=port,
            reachable=False,
            ping_ok=ping_result,
            port_ok=port_result,
            api_ok=None,
            latency_ms=best_latency,
            error=error,
        )

    # --- API check ---
    if not skip_api:
        ok, version = check_api(host, port=port, timeout=api_timeout, password=password)
        api_result = ok
        if not ok:
            error = (
                f"U64 at {host} API not responding "
                f"(GET /v1/version failed)"
            )
            return ProbeResult(
                host=host,
                port=port,
                reachable=False,
                ping_ok=ping_result,
                port_ok=port_result,
                api_ok=api_result,
                latency_ms=best_latency,
                error=error,
            )

    return ProbeResult(
        host=host,
        port=port,
        reachable=True,
        ping_ok=ping_result,
        port_ok=port_result,
        api_ok=api_result,
        latency_ms=best_latency,
        error=None,
    )


def is_u64_reachable(
    host: str, port: int = 80, password: str | None = None
) -> bool:
    """Quick boolean reachability check."""
    return probe_u64(host, port=port, password=password).reachable
