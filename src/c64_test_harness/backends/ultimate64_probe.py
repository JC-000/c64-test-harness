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
    "LivenessResult",
    "ping_host",
    "check_port",
    "check_api",
    "probe_u64",
    "is_u64_reachable",
    "liveness_probe",
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


# --------------------------------------------------------------------------- #
# Writemem-degradation liveness probe (issue #107)                            #
# --------------------------------------------------------------------------- #
#
# ``probe_u64().reachable`` only exercises ICMP + TCP connect + the version
# GET; it does not exercise the ``POST /v1/machine:writemem`` path.  On
# Ultimate 64 firmware 3.14d, the device can slip into a "writemem-degraded"
# transient state in which:
#
#   * ``probe_u64.reachable`` still returns ``True``;
#   * ``POST /v1/machine:writemem`` returns HTTP 404 ("Could not read data
#     from attachment") on any body shape;
#   * ``PUT ?data=<hex>`` (the small-payload form) still works;
#   * only a physical power-cycle clears the state — ``reset()``/``reboot()``
#     over REST may not, and *repeated* malformed POSTs against the degraded
#     endpoint can wedge the TCP stack itself, at which point only a
#     power-cycle helps.
#
# :func:`liveness_probe` mirrors :func:`probe_u64`'s shape but additionally
# exercises the POST writemem path exactly once.  It is firmware-aware: on
# fw 3.14* it uses a >=128-byte payload to stay out of the 48..127 POST
# wedge range covered by issue #84.  It is also bounded by a short socket
# timeout so a wedged TCP stack returns quickly with
# ``failure="tcp_stack_wedged"``.
#
# Scratch byte choice
# -------------------
# The issue suggests $02A9 (KERNAL unused workspace) or $02A0..$02BF, but
# the probe writes >=128 bytes, and 128 bytes starting at $02A9 would
# clobber the KERNAL interrupt vectors at $0314..$0333 and crash the
# device.  Instead, the probe writes into the cassette buffer at $0334,
# which is the harness-owned scratch range $0334-$03FB documented in
# ``docs/memory_safety.md`` (198 bytes available, easily fits 128).  The
# original bytes are read with GET /v1/machine:readmem before the POST
# and restored after — so a healthy device sees no net side effect.  The
# probe is intended for pre-flight checks before any test starts, when no
# trampoline / jsr() is in flight against the same scratch area.

#: Address used by :func:`liveness_probe` for the writemem POST round-trip.
#: Within the harness-owned cassette-buffer scratch range $0334-$03FB
#: (see ``docs/memory_safety.md``).  128 bytes starting here lands at
#: $0334..$03B3, comfortably inside the scratch range.
_LIVENESS_PROBE_ADDR: int = 0x0334

#: Payload size for the writemem POST.  Must be >= 128 on fw 3.14* to
#: stay out of the 48..127 POST wedge range (issue #84).  We use 128
#: unconditionally — it is below the device's 256-byte safety budget
#: and above the wedge range on every known firmware.
_LIVENESS_PROBE_LEN: int = 128

#: Bounded per-request timeout for liveness_probe HTTP calls.  Short
#: enough that a wedged TCP stack returns ``failure="tcp_stack_wedged"``
#: in 1-2s rather than the default 10s ``Ultimate64Client`` timeout.
_LIVENESS_PROBE_HTTP_TIMEOUT: float = 2.0


@dataclass(frozen=True)
class LivenessResult:
    """Aggregated result of a writemem-degradation liveness probe.

    Mirrors :class:`ProbeResult`'s shape with extra fields covering the
    POST-writemem exercise:

    :ivar host: device hostname/IP that was probed.
    :ivar port: HTTP port that was probed.
    :ivar healthy: ``True`` iff reachable AND the writemem POST round-trip
        succeeded.  This is the recommended top-level gate.
    :ivar reachable: ``True`` iff the device's REST API answered the
        version GET (equivalent to ``probe_u64(...).reachable``).
    :ivar writemem_ok: ``True`` if the POST writemem round-trip succeeded;
        ``False`` if it failed; ``None`` if the probe was skipped (e.g.
        because the device was unreachable in the first place).
    :ivar firmware_version: ``firmware_version`` reported by
        ``GET /v1/info``, or ``None`` if it could not be read.
    :ivar failure: short tag identifying the failure mode, one of
        ``"unreachable"``, ``"writemem_404"``, ``"writemem_timeout"``,
        ``"tcp_stack_wedged"``, ``"connection_reset"``, ``"unknown"``,
        or ``None`` when healthy.  ``"connection_reset"`` is a one-shot
        TCP RST mid-request — empirically transient on fw 3.14d; callers
        may retry once before treating it as a wedged stack.
    :ivar recommendation: optional human-readable hint about what to do
        next (e.g. ``"physical power-cycle required"``), or ``None``.
    """

    host: str
    port: int
    healthy: bool
    reachable: bool
    writemem_ok: bool | None
    firmware_version: str | None
    failure: str | None
    recommendation: str | None

    @property
    def summary(self) -> str:
        """One-line status for logging."""
        if self.healthy:
            fw = f" fw {self.firmware_version}" if self.firmware_version else ""
            return f"U64 at {self.host}:{self.port} healthy{fw}"
        tag = self.failure or "unknown"
        return f"U64 at {self.host}:{self.port} UNHEALTHY ({tag})"


def _liveness_request(
    method: str,
    host: str,
    port: int,
    path: str,
    password: str | None,
    timeout: float,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    query: str | None = None,
) -> tuple[int, bytes]:
    """One-shot HTTP request for the liveness probe.

    Returns ``(status_code, body_bytes)`` on success, including non-2xx
    responses (HTTPError is converted, not re-raised).  Raises
    ``socket.timeout``, ``urllib.error.URLError``, ``ConnectionResetError``,
    or ``BrokenPipeError`` for connection-level failures — callers map those
    onto the ``LivenessResult.failure`` tag.
    """
    base = f"http://{host}:{port}" if port != 80 else f"http://{host}"
    url = f"{base}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, data=body, method=method)
    if password:
        req.add_header("X-Password", password)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read() if e.fp else b""
        except Exception:
            data = b""
        return e.code, data


def liveness_probe(
    host: str,
    port: int = 80,
    password: str | None = None,
    *,
    http_timeout: float = _LIVENESS_PROBE_HTTP_TIMEOUT,
    skip_ping: bool = True,
) -> LivenessResult:
    """Full writemem-degradation liveness probe.

    Runs in this order:

    1.  Reachability — TCP connect + GET /v1/version (equivalent to
        ``probe_u64(skip_ping=True)``).  On failure the probe returns
        ``failure="unreachable"`` and skips everything else.
    2.  Firmware-version discovery — GET /v1/info.  Failure here is
        non-fatal; the probe proceeds with ``firmware_version=None``.
    3.  Single POST writemem of 128 bytes at $0334 (harness-owned
        cassette-buffer scratch).  The original bytes are read first and
        restored after, so a healthy device sees no net side effect.
        Round-trip is verified via GET /v1/machine:readmem.

    The probe issues **exactly one** writemem POST.  Retrying with
    varying payload shapes against an already-degraded endpoint is the
    documented TCP-wedge trigger (see issue #107).

    :param host: device hostname or IP.
    :param port: HTTP port (default 80).
    :param password: ``X-Password`` header value (optional).
    :param http_timeout: per-request socket timeout (default 2 s).  Kept
        short so a wedged TCP stack returns
        ``failure="tcp_stack_wedged"`` quickly.
    :param skip_ping: skip the ICMP ping step (default ``True``); the
        TCP connect and version GET are enough to declare reachability,
        and ``ping`` can be unavailable in CI/container environments.
    :returns: :class:`LivenessResult` summarising the probe.
    """
    # ----------------------------------------------------------------- #
    # Step 1: reachability                                              #
    # ----------------------------------------------------------------- #
    base_probe = probe_u64(
        host,
        port=port,
        password=password,
        tcp_timeout=http_timeout,
        api_timeout=http_timeout,
        skip_ping=skip_ping,
    )
    if not base_probe.reachable:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=False,
            writemem_ok=None,
            firmware_version=None,
            failure="unreachable",
            recommendation=base_probe.error,
        )

    # ----------------------------------------------------------------- #
    # Step 2: firmware version discovery                                #
    # ----------------------------------------------------------------- #
    firmware_version: str | None = None
    try:
        status, data = _liveness_request(
            "GET", host, port, "/v1/info", password, http_timeout
        )
        if 200 <= status < 300 and data:
            info = json.loads(data.decode("utf-8"))
            if isinstance(info, dict):
                fw = info.get("firmware_version")
                if isinstance(fw, str):
                    firmware_version = fw
    except (
        socket.timeout,
        urllib.error.URLError,
        ConnectionResetError,
        BrokenPipeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        # Non-fatal: proceed with firmware_version=None.  Probe payload
        # is sized for the worst-known case (fw 3.14*) regardless.
        firmware_version = None

    # ----------------------------------------------------------------- #
    # Step 3: writemem POST round-trip                                  #
    # ----------------------------------------------------------------- #
    probe_addr = _LIVENESS_PROBE_ADDR
    probe_len = _LIVENESS_PROBE_LEN
    addr_query = f"address=0x{probe_addr:04X}&length={probe_len}"

    # Read the original bytes so we can restore them after the POST.
    # Failure here is treated as "unknown" — the device answered version
    # but readmem failed, which is unusual.
    try:
        rd_status, original_bytes = _liveness_request(
            "GET",
            host,
            port,
            "/v1/machine:readmem",
            password,
            http_timeout,
            query=addr_query,
        )
    except (ConnectionResetError, BrokenPipeError) as exc:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=None,
            firmware_version=firmware_version,
            failure="connection_reset",
            recommendation=(
                f"readmem TCP reset mid-request ({exc!s}); empirically "
                "transient on fw 3.14d — retry once; if persistent the "
                "device may need a physical power-cycle"
            ),
        )
    except (socket.timeout, urllib.error.URLError) as exc:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=None,
            firmware_version=firmware_version,
            failure="tcp_stack_wedged",
            recommendation=(
                f"readmem timed out after {http_timeout}s ({exc!s}); "
                "device may need a physical power-cycle"
            ),
        )
    if rd_status != 200 or len(original_bytes) != probe_len:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=None,
            firmware_version=firmware_version,
            failure="unknown",
            recommendation=(
                f"readmem at ${probe_addr:04X} returned status={rd_status}, "
                f"len={len(original_bytes)} (expected 200/{probe_len})"
            ),
        )

    # Build a deterministic probe pattern that is NOT the original bytes
    # (so a stuck-but-not-erroring write would still produce a mismatch).
    probe_pattern = bytes((i ^ 0x5A) & 0xFF for i in range(probe_len))
    post_query = f"address=0x{probe_addr:04X}"

    try:
        post_status, post_body = _liveness_request(
            "POST",
            host,
            port,
            "/v1/machine:writemem",
            password,
            http_timeout,
            body=probe_pattern,
            content_type="application/octet-stream",
            query=post_query,
        )
    except socket.timeout:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="writemem_timeout",
            recommendation=(
                f"POST /v1/machine:writemem timed out after {http_timeout}s; "
                "device may need a physical power-cycle"
            ),
        )
    except (ConnectionResetError, BrokenPipeError) as exc:
        # Per issue #107, do NOT retry the POST internally — repeated
        # POSTs against a degraded endpoint are the documented TCP-wedge
        # trigger.  Surface the RST and let the caller decide.
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="connection_reset",
            recommendation=(
                f"POST /v1/machine:writemem TCP reset mid-request ({exc!s}); "
                "empirically transient on fw 3.14d — caller may retry once; "
                "if persistent the device may need a physical power-cycle"
            ),
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return LivenessResult(
                host=host,
                port=port,
                healthy=False,
                reachable=True,
                writemem_ok=False,
                firmware_version=firmware_version,
                failure="writemem_timeout",
                recommendation=(
                    f"POST /v1/machine:writemem timed out after "
                    f"{http_timeout}s; device may need a physical power-cycle"
                ),
            )
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="tcp_stack_wedged",
            recommendation=(
                f"POST /v1/machine:writemem connection failed ({reason!s}); "
                "device may need a physical power-cycle"
            ),
        )

    if post_status == 404:
        # Writemem-degraded transient state.  Per issue #107, do NOT retry
        # with varying shapes — repeated 404 POSTs are the TCP-wedge trigger.
        # PUT ?data=<hex> may still work, but exposing that fallback here
        # would mask the degradation from the caller; report it instead.
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="writemem_404",
            recommendation=(
                "POST /v1/machine:writemem returned HTTP 404; device is in "
                "writemem-degraded state.  PUT ?data=<hex> small-payload "
                "form may still work, but a physical power-cycle is the "
                "only documented recovery on fw 3.14d."
            ),
        )

    if post_status < 200 or post_status >= 300:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="unknown",
            recommendation=(
                f"POST /v1/machine:writemem returned HTTP {post_status} "
                f"(body={post_body[:128]!r})"
            ),
        )

    # ----------------------------------------------------------------- #
    # Step 4: readback to confirm round-trip                            #
    # ----------------------------------------------------------------- #
    try:
        rb_status, readback = _liveness_request(
            "GET",
            host,
            port,
            "/v1/machine:readmem",
            password,
            http_timeout,
            query=addr_query,
        )
    except (ConnectionResetError, BrokenPipeError) as exc:
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="connection_reset",
            recommendation=(
                f"readback TCP reset mid-request ({exc!s}); writemem POST "
                "appeared to succeed but round-trip cannot be confirmed — "
                "retry the probe before treating as wedged"
            ),
        )
    except (socket.timeout, urllib.error.URLError):
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="tcp_stack_wedged",
            recommendation=(
                "readback after writemem POST failed; device TCP stack "
                "may be wedged"
            ),
        )
    if rb_status != 200 or readback != probe_pattern:
        # Restore best-effort even on mismatch, then report.
        _restore_quiet(
            host, port, password, http_timeout,
            probe_addr, original_bytes,
        )
        return LivenessResult(
            host=host,
            port=port,
            healthy=False,
            reachable=True,
            writemem_ok=False,
            firmware_version=firmware_version,
            failure="unknown",
            recommendation=(
                f"writemem readback mismatch at ${probe_addr:04X}: "
                f"status={rb_status}, got {len(readback)} bytes, "
                "expected probe pattern to round-trip"
            ),
        )

    # ----------------------------------------------------------------- #
    # Step 5: restore original bytes (best-effort)                      #
    # ----------------------------------------------------------------- #
    _restore_quiet(
        host, port, password, http_timeout,
        probe_addr, original_bytes,
    )

    return LivenessResult(
        host=host,
        port=port,
        healthy=True,
        reachable=True,
        writemem_ok=True,
        firmware_version=firmware_version,
        failure=None,
        recommendation=None,
    )


def _restore_quiet(
    host: str,
    port: int,
    password: str | None,
    timeout: float,
    addr: int,
    original: bytes,
) -> None:
    """Restore *original* bytes at *addr* via POST writemem.

    Best-effort: swallows all exceptions.  Used by :func:`liveness_probe`
    to undo its scratch write after the round-trip verification, so the
    probe is side-effect-free on a healthy device.
    """
    try:
        _liveness_request(
            "POST",
            host,
            port,
            "/v1/machine:writemem",
            password,
            timeout,
            body=original,
            content_type="application/octet-stream",
            query=f"address=0x{addr:04X}",
        )
    except Exception as exc:
        _log.debug("liveness_probe restore at $%04X failed: %s", addr, exc)
