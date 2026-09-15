"""REST API client for the Ultimate 64 / Ultimate II+ family.

Targets the device's HTTP v1 API (plain HTTP, no TLS). Zero runtime
dependencies — `urllib.request` only.

Response shape for config queries is always:
    { "<Category Name>": { ...items... }, "errors": [] }

`get_config_category` does NOT auto-unwrap the category key — callers
inspect the raw response, and `errors` is passed through for them to
treat as a soft failure. The single-item accessors do unwrap (issue
#214): `get_config_item` returns the item's own map
(`{"current": ..., "values": [...], "default": ...}`), `get_config_value`
returns its `current`, and both raise `Ultimate64ProtocolError` on a
non-empty `errors` array or a missing item; `get_config_item_raw` is the
untouched envelope.
"""
from __future__ import annotations

from .._address import refuse_bool_address

import http.client
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any

from .u64_capabilities import (
    THRESHOLD_POST_RISKY,
    THRESHOLD_POST_SAFE,
    DeviceCapabilities,
)

if TYPE_CHECKING:
    from .ultimate64_probe import LivenessResult
    from .ultimate64_temp_gc import TempGCResult

try:  # device_lock needs fcntl — absent on Windows, optional everywhere
    from .device_lock import advisory_lock_check as _advisory_lock_check
    from .device_lock import register_release_callback as _register_release_callback
    from .device_lock import warn_unlocked_client as _warn_unlocked_client

    _HAS_DEVICE_LOCK = True
except Exception:  # pragma: no cover - exercised only without fcntl
    _HAS_DEVICE_LOCK = False

__all__ = [
    "Ultimate64Client",
    "Ultimate64Error",
    "Ultimate64AuthError",
    "Ultimate64TimeoutError",
    "Ultimate64ProtocolError",
    "Ultimate64WireFormatError",
    "Ultimate64UnsafeOperationError",
    "Ultimate64TempHygieneError",
    "Ultimate64UnreachableError",
    "Ultimate64RunnerStuckError",
    "U64UnreachableError",
    "U64WritememDegradedError",
]

_log = logging.getLogger(__name__)


class Ultimate64Error(Exception):
    """Base exception for Ultimate64Client failures."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class Ultimate64AuthError(Ultimate64Error):
    """Raised on HTTP 401/403 — bad or missing X-Password."""


class Ultimate64TimeoutError(Ultimate64Error):
    """Raised when the HTTP request times out, the device is unreachable,
    or the connection drops mid-request (reset / broken pipe / truncated
    HTTP response)."""


class Ultimate64ProtocolError(Ultimate64Error):
    """Raised when a response cannot be parsed (invalid JSON) or has an
    unexpected shape — e.g. a ``readmem`` payload whose length differs
    from the requested length."""


class Ultimate64WireFormatError(Ultimate64Error):
    """Raised on HTTP 400 from a route whose hex arguments the firmware parses strictly.

    GideonZ/1541ultimate#884 made ``/v1/machine:readmem``, ``:writemem``
    and ``:debugreg`` reject any argument that is not bare hexadecimal.
    A harness older than issue #272 sends ``address=0xC000`` and gets a
    400 from every memory read and write on such firmware.

    This exists because of what the *undiagnosed* form of that failure
    leads to: a lane that has always been able to read this device sees
    a bare HTTP 400 and concludes the hardware is faulty, then reaches
    for ``reboot()`` and ``recover()``. On the C64 Ultimate nobody is
    physically present to power-cycle if that reasoning goes astray, so
    the 400 has to name the contract change itself.

    A 400 from these routes is not *necessarily* the wire format — an
    out-of-range ``address + length`` is the other candidate the
    firmware rejects the same way — so the message says so and carries
    the device's own body text.

    Also raised by :meth:`Ultimate64Client.assert_healthy` when the
    liveness probe comes back tagged ``wire_format``, in place of
    :class:`U64WritememDegradedError` — the device is not degraded, and
    the exception type should not say it is.  ``result`` carries the
    :class:`~c64_test_harness.backends.ultimate64_probe.LivenessResult`
    in that case, matching ``U64WritememDegradedError``'s shape.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: str | None = None,
        result: object | None = None,
    ) -> None:
        super().__init__(message, status=status, body=body)
        self.result = result


class Ultimate64UnsafeOperationError(Ultimate64Error):
    """Raised when a destructive call needs an explicit caller-confirmation
    kwarg and didn't get one.

    Reserved for operations whose effect cannot be undone via the network
    API -- specifically ``poweroff``, which leaves the device unreachable
    until someone physically power-cycles it.  ``reset`` and ``reboot``
    are also DESTRUCTIVE but recoverable over the wire (~8s for reboot,
    instant for reset), so they don't require this gate.
    """


class Ultimate64UnreachableError(Ultimate64Error):
    """Raised when the device is unreachable after recovery attempts.

    Used by ``ultimate64_helpers.recover()`` when both the soft reset and
    (optionally) the full reboot escalations failed to bring the device
    back to a probe-reachable state. Caller is expected to escalate to a
    human / physical power-cycle -- the network API has no further
    recovery primitive (``poweroff`` is irrecoverable, not a recovery).
    """


class Ultimate64RunnerStuckError(Ultimate64Error):
    """Raised when the firmware's runner subsystem is wedged.

    Signature: ``run_prg`` (or similar runner endpoint) returns the
    "Cannot open file" error from the device even though the device is
    otherwise reachable (HTTP works, ``/v1/version`` responds). The
    runner state machine is stuck and refuses new programs.

    ``recover()`` (which issues a soft reset and optionally a reboot)
    typically clears this. Do NOT call ``poweroff()`` -- that's
    irrecoverable over the network.
    """


class Ultimate64TempHygieneError(Ultimate64Error):
    """Raised when a leak-prone device's ``/Temp`` hygiene pass cannot run.

    Firmware without the upstream ``/Temp`` collector
    (GideonZ/1541ultimate#686 — every ``3.14``/``3.13`` build and the
    whole CBM ``1.x`` line) turns every attachment-creating REST call
    into a permanent file in the device's ``/Temp`` RAM disk. Enough of
    them wedge the REST API and the UCI bridge together, and only a
    physical power-cycle recovers — which on a shared remote device has
    cost weeks of downtime.

    The harness's mitigation is an FTP-based hygiene pass, and it is
    *prevention, not recovery* — as a consequence of the failure mode,
    not as a caution. What wedges is the device firmware itself (the C64
    FPGA keeps running; the firmware stops answering the network and
    stops responding to the physical menu button), and the FTP server is
    part of that firmware. So the pass is unavailable exactly when a
    device is wedged, and a physical power-cycle is the only instrument
    left.

    That is what makes this worth raising rather than warning: when the
    pass cannot run at all (FTP File Service off and not enablable, wrong
    credentials, no route to the FTP port), continuing to upload walks
    the device towards the wedge with no second chance to clean up
    afterwards. Refusing is not a cautious default; it is the only lever
    still attached. So the client refuses further attachment-creating
    calls instead. Bodyless calls —
    ``reset``, ``reboot``, config PUTs, ``readmem`` — are unaffected, so
    a blocked client can still drive recovery.

    Opt out with ``U64_TEMP_GC_REQUIRED=0`` (or ``temp_hygiene=False`` to
    disarm the whole pass) if you know the device's ``/Temp`` is being
    kept clean some other way.
    """


class U64UnreachableError(Ultimate64Error):
    """Raised by :meth:`Ultimate64Client.assert_healthy` when the device
    fails the reachability portion of the writemem-degradation liveness
    probe (no TCP connect / no version GET).

    Distinct from :class:`Ultimate64UnreachableError`, which is raised by
    ``ultimate64_helpers.recover()`` *after* recovery attempts.  This
    error is raised by ``assert_healthy()`` *before* any recovery is
    attempted, so the caller can decide how to escalate (probe again,
    call ``reboot()``, escalate to a human, etc.).  See issue #107.
    """


class U64WritememDegradedError(Ultimate64Error):
    """Raised by :meth:`Ultimate64Client.assert_healthy` when the device
    answers REST GETs but ``POST /v1/machine:writemem`` is in the fw
    3.14d writemem-degraded state (HTTP 404 on the POST path, or the POST
    timed out / wedged the TCP stack).

    Recovery is not available over the network -- physical power-cycle
    is the documented fix.  Do NOT retry the POST with varying payload
    shapes; repeated 404s are the documented TCP-wedge trigger (see
    issue #107).

    The :class:`~c64_test_harness.backends.ultimate64_probe.LivenessResult`
    is attached at ``.result`` for callers that want the structured
    failure tag.

    **Since issue #272 this no longer covers a wire-format 400.** A probe
    that comes back tagged ``wire_format`` raises
    :class:`Ultimate64WireFormatError` instead, because the firmware
    refused a malformed request and the device is not degraded — an
    ``except U64WritememDegradedError:`` that used to fire on it will
    stop firing.  That is deliberate: a handler that escalates to
    ``reboot()`` and ``recover()`` would be running recovery against a
    healthy device.  Catch :class:`Ultimate64Error` to handle both.
    """

    def __init__(self, message: str, result: object | None = None) -> None:
        super().__init__(message)
        self.result = result


def _encode(value: str) -> str:
    """URL-encode a single path segment (including spaces and colons)."""
    return urllib.parse.quote(value, safe="")


#: Shared tail: true of any 400 from these routes, whatever the cause.
_REFUSED_NOT_WEDGED = (
    " Either way it is a request the firmware refused, not a wedged "
    "device: do not reboot() or recover() on the strength of it."
)

#: For routes that carry an ``address`` this harness formats.
_HEX_ARG_HINT = (
    " — this route needs bare hexadecimal arguments (no 0x prefix) on "
    "firmware carrying GideonZ/1541ultimate#884; see issue #272. If this "
    "harness is current, the other candidate is an out-of-range "
    "address/length." + _REFUSED_NOT_WEDGED
)

#: For routes #884 tightened that take no hex argument from this harness.
#: Saying what the cause is *not* is the honest half of the diagnosis:
#: whatever the firmware objected to is in the response body above.
_NO_HEX_ARG_HINT = (
    " — GideonZ/1541ultimate#884 tightened argument parsing on this "
    "route (see issue #272), but this harness sends it no hex argument, "
    "so the 0x prefix is not the cause here; what the firmware objected "
    "to is in its response body above." + _REFUSED_NOT_WEDGED
)

#: REST routes whose hex arguments GideonZ/1541ultimate#884 (merged
#: 2026-09-11, with #888) tightened to *bare* hexadecimal.  Before it the
#: firmware parsed them with ``strtol(..., 16)``, which tolerated a ``0x``
#: prefix; after it a prefixed value is an HTTP 400.  See issue #272.
#: Maps each of those routes to what this harness can honestly say about
#: a 400 from it.  ``readmem``/``writemem`` carry an ``address`` this
#: harness formats, so the prefix is a real candidate there.
#: ``debugreg`` takes no address and no length — ``get_debug_register``
#: sends no arguments at all and ``set_debug_register`` sends an int —
#: so naming the prefix there would assert a cause that cannot apply.
#: The route stays in the table because #884 did tighten it and a caller
#: building the query by hand can still trip it.
_STRICT_HEX_ROUTE_HINTS: dict[str, str] = {
    "machine:readmem": _HEX_ARG_HINT,
    "machine:writemem": _HEX_ARG_HINT,
    "machine:debugreg": _NO_HEX_ARG_HINT,
}


class _ShippedThreshold(int):
    """The untouched value of ``Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD``.

    An ``int`` in every respect callers can observe; the subclass exists
    only so ``__init__`` can tell the shipped value from a poke (#249).
    A caller that re-assigns the same number stores a plain ``int``, which
    is still a poke and is still honoured.
    """

    __slots__ = ()


def _validate_poked_threshold(value: Any) -> int:
    """A poked ``WRITE_MEM_QUERY_THRESHOLD`` must be a non-negative int."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            "WRITE_MEM_QUERY_THRESHOLD must be an int, got "
            f"{type(value).__name__} {value!r}; prefer the "
            "write_mem_query_threshold= constructor kwarg"
        )
    if value < 0:
        raise ValueError(
            f"WRITE_MEM_QUERY_THRESHOLD must be >= 0, got {value}"
        )
    return int(value)


def _wire_hex16(value: int) -> str:
    """Format a 16-bit address for a REST query argument.

    Bare uppercase hex, no ``0x`` prefix, zero-padded to four digits —
    the only form firmware carrying GideonZ/1541ultimate#884 accepts
    (issue #272).  Every ``readmem``/``writemem`` query argument goes
    through here rather than being formatted at the call site: the
    prefix survived as long as it did because three call sites each
    formatted their own.

    :raises ValueError: if *value* does not fit 16 bits.  A five-digit
        address is not a thing the C64 window has, and the function that
        claims to be the single formatting choke point should not emit
        one silently.
    """
    # bool is an int subclass: True would format as "0001", the 6510
    # processor port (#340).  Refused with the bad-address error.
    refuse_bool_address(value)
    if not isinstance(value, int) or value < 0 or value > 0xFFFF:
        raise ValueError(f"address out of range 0..0xFFFF: {value!r}")
    return "%04X" % value


class Ultimate64Client:
    """HTTP REST client for Ultimate 64 / Ultimate II+ devices.

    All methods either return parsed JSON / raw bytes, or raise
    :class:`Ultimate64Error` (or a subclass) on failure.

    The client is stateless between calls — each call opens a fresh
    TCP connection via ``urllib.request.urlopen``.

    **Locking is advisory and this class does not take the lock.**
    Constructing a client is not permission to drive the device: the
    machine-global :class:`~c64_test_harness.backends.device_lock.DeviceLock`
    is what serialises lanes, and a lane that skips it is invisible to
    every lane that took it.  Because a locked neighbour's
    :meth:`run_prg` is a *load-and-run* that replaces whatever program
    you are driving, an unlocked lane is destructive rather than merely
    rude — and it reads, from the other side, as the device
    mysteriously degrading (issue #194).

    So either go through ``create_manager(backend="u64")`` (which locks
    for you) or hold ``DeviceLock(host)`` yourself for the whole run.
    Constructing a client with no lock held emits one WARNING per
    process per host; see
    :func:`~c64_test_harness.backends.device_lock.warn_unlocked_client`
    and ``docs/device_locking.md``.
    """

    def __init__(
        self,
        host: str,
        password: str | None = None,
        port: int = 80,
        timeout: float = 10.0,
        *,
        write_mem_query_threshold: int | None = None,
        warn_unlocked: bool = True,
        temp_hygiene: bool | None = None,
        temp_gc_budget: int | None = None,
    ) -> None:
        """Construct an Ultimate64 REST client.

        :param host: device hostname or IP.
        :param password: ``X-Password`` header value (optional).
        :param port: HTTP port (default 80).
        :param timeout: per-request socket timeout in seconds.
        :param write_mem_query_threshold: payload-size cutoff (in bytes)
            at which :meth:`write_mem` switches from the legacy
            ``PUT ?data=<hex>`` form to the ``POST`` raw-byte form. If
            ``None`` (the default), it is derived from :attr:`capabilities`:

            * firmware **without** the Temp-folder GC fix (upstream #686) —
              every 3.14/3.13 build, and the whole 1.x CBM line — → **128**,
              which pushes the 48..127 band that wedges the runner off the
              POST path and onto the reliable PUT-with-hex path;
            * firmware **with** it (3.15 and later) → **48**.

            Passing a value explicitly pins it and skips the probe entirely,
            so construction issues no HTTP traffic. If the probe fails, the
            capability set resolves conservatively (fix assumed absent, so
            128) — construction never raises on probe failure.
        :param warn_unlocked: emit the once-per-process "this lane holds
            no device lock" WARNING (see the class docstring).  Pass
            ``False`` only where the caller is about to take the lock and
            simply needs the client first — the harness's own locked
            manager is that case.  ``U64_UNLOCKED_CLIENT_WARNING=0``
            silences it globally instead.  Either way nothing about
            locking *behaviour* changes: this suppresses a message, not
            a check.
        :param temp_hygiene: force the ``/Temp`` hygiene pass on
            (``True``) or off (``False``) for this client.  ``None``
            (the default) means *decide from the device*: see
            :attr:`temp_hygiene_armed`.  Beats the ``U64_AUTO_TEMP_GC``
            environment variable either way.
        :param temp_gc_budget: how many attachment-creating requests may
            go out between hygiene passes.  Defaults to
            ``$U64_TEMP_GC_BUDGET`` or
            :data:`~c64_test_harness.backends.ultimate64_temp_gc.DEFAULT_LEAK_BUDGET`.
        """
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if port <= 0 or port > 65535:
            raise ValueError(f"port out of range: {port}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._base = f"http://{host}:{port}" if port != 80 else f"http://{host}"

        # Before any network traffic: one line per process per host if
        # this lane is driving the device without holding its lock.
        if warn_unlocked and _HAS_DEVICE_LOCK:
            _warn_unlocked_client(
                self.host, what="Ultimate64Client", logger=_log
            )

        # ---- /Temp hygiene state (see temp_hygiene_armed) ----
        from .ultimate64_temp_gc import leak_budget as _leak_budget

        if temp_hygiene is not None and not isinstance(temp_hygiene, bool):
            raise TypeError("temp_hygiene must be True, False or None")
        self._temp_hygiene_force: bool | None = temp_hygiene
        if temp_gc_budget is not None:
            if not isinstance(temp_gc_budget, int) or temp_gc_budget <= 0:
                raise ValueError(
                    f"temp_gc_budget must be a positive int, got {temp_gc_budget!r}"
                )
            self._temp_gc_budget = int(temp_gc_budget)
        else:
            self._temp_gc_budget = _leak_budget()
        from .ultimate64_temp_gc import temp_ledger_for as _temp_ledger_for

        #: The device's accounting, shared by every client of this host in
        #: the process (#295): the pending count, the refusal state and the
        #: one FTP-enable attempt. See ``TempLedger``.
        self._temp_ledger = _temp_ledger_for(host)
        #: This client's own share of the device's pending count, valid only
        #: for the ledger generation it was counted in (a successful sweep by
        #: any client collects it).
        self._own_temp_attachments = 0
        self._own_temp_generation = self._temp_ledger.generation
        self._temp_ledger.attach(self)
        #: Re-entrancy guard: the hygiene pass's own REST calls must not
        #: recurse back into the budget check.
        self._in_temp_hygiene = False

        self._capabilities: DeviceCapabilities | None = None
        #: Has a firmware probe ever actually been issued? ``False`` means
        #: nothing was asked and this client is inert by contract (the
        #: caller pinned the threshold); ``True`` with an unreadable grade
        #: means a device may well be there and simply did not answer in
        #: time. Collapsing those two into one "unknown" is what hid the
        #: re-probe hole. Set inside :meth:`_probe_info`, not here: a
        #: client built with an explicit threshold that *later* touches
        #: ``.capabilities`` does probe for real, and setting this only in
        #: ``__init__``'s else-branch would leave the flag saying
        #: otherwise and block a re-probe the evidence justifies.
        self._probe_attempted = False
        #: One post-evidence re-probe per client (see _maybe_reprobe).
        self._reprobed = False
        #: Guard so the probe's own GET cannot recurse into the re-probe.
        self._probing = False
        #: Has any request to this host ever completed? That is the
        #: evidence a timed-out construct-time probe cannot supply.
        self._saw_successful_request = False
        poked = type(self).WRITE_MEM_QUERY_THRESHOLD
        class_poked = not isinstance(poked, _ShippedThreshold)
        if write_mem_query_threshold is not None:
            # An explicit threshold pins the behaviour, so the probe is not
            # needed at construction; ``capabilities`` stays lazy and this
            # path issues no HTTP traffic at all.
            self.write_mem_query_threshold = int(write_mem_query_threshold)
            if class_poked:
                _log.warning(
                    "Ultimate64Client(%s): %s.WRITE_MEM_QUERY_THRESHOLD = %r "
                    "ignored because the write_mem_query_threshold=%d kwarg "
                    "takes precedence.",
                    self.host, type(self).__name__, poked,
                    self.write_mem_query_threshold,
                )
        else:
            self.write_mem_query_threshold = (
                self.capabilities.write_mem_query_threshold
            )
            if class_poked:
                # Applied (#249) after the probe, so a poke never disarms
                # /Temp hygiene the way the kwarg's skipped probe does, and
                # so the grade is known when deciding whether to refuse it.
                self.write_mem_query_threshold = self._effective_poked_threshold(
                    poked, f"{type(self).__name__}."
                )

        self.log_device_grading()

        # Drain /Temp when this device's lock is handed to the next lane.
        # The device's ledger registers, not the client, so one release
        # drains once however many clients this process built (#295).
        # Registration is weak and idempotent; the ledger holds its clients
        # weakly, so a forgotten client is collected normally.
        if _HAS_DEVICE_LOCK:
            _register_release_callback(
                self.host, self._temp_ledger, "drain_on_lock_release"
            )

    def close(self) -> None:
        """Release client resources.

        The REST side is stateless (a fresh connection per call), so the
        only work here is the ``/Temp`` hygiene drain, best-effort, on an
        armed client (a disarmed one does nothing):

        * if this client leaked, its hygiene pass runs as before;
        * if it leaked nothing, the device's ``/Temp`` is still swept for
          attachments an earlier lane left behind (issue #264) -- but only
          when this process holds the device's ``DeviceLock``, since that
          sweep deletes other lanes' files, and a failed one never enables
          FTP File Service and never blocks this client.

        See :attr:`temp_hygiene_armed` and :meth:`_drain_temp_attachments`.
        """
        self._drain_temp_attachments(reason="client close")
        return None

    #: Bounded timeout (seconds) for the construct-time firmware probe
    #: used by :meth:`_probe_info`. Decoupled from
    #: the per-request ``timeout`` so an unreachable host doesn't stall
    #: ``__init__`` for the full default.
    _AUTODETECT_PROBE_TIMEOUT: float = 0.5

    @property
    def capabilities(self) -> DeviceCapabilities:
        """What this device's firmware can do — probed once, then cached.

        A failed probe yields the conservative all-fixes-absent set rather
        than raising; construction never fails on an unreachable device.
        """
        if self._capabilities is None:
            self._capabilities = DeviceCapabilities.from_info(
                self._probe_info()
            )
        return self._capabilities

    @property
    def cached_capabilities(self) -> DeviceCapabilities | None:
        """The capability grade if one is cached, else ``None`` -- never probes.

        :attr:`capabilities` issues ``GET /v1/info`` on a cold cache.  Use
        this instead wherever a read must not generate device traffic or
        change cached state: hygiene arming, the grading log line, a
        transport's chunking decision, an error message.  ``None`` means
        nothing has been probed (for example a client constructed with an
        explicit ``write_mem_query_threshold``); a probe that ran and got no
        answer caches a grade whose ``firmware_version`` is ``None``, which
        is a different fact.  Read-only (issue #291).
        """
        return self._capabilities

    def _probe_info(self, timeout: float | None = None) -> dict | None:
        """``GET /v1/info``; ``None`` on any failure.

        *timeout* defaults to the bounded
        :data:`_AUTODETECT_PROBE_TIMEOUT` so an unreachable host does not
        stall ``__init__``; pass the client's own timeout for a re-probe,
        where the device has already proven it is there and the only
        question is how slow it is.

        A failure here is logged at DEBUG and surfaced in the INFO grading
        line as ``firmware=unknown(probe-failed)`` — distinct from
        ``unknown(not-attempted)``, which is a different fact. The
        WARNING lives in :meth:`_maybe_reprobe_capabilities` instead,
        where a failure has consequences: construction against an
        unreachable host is routine and warning there would be noise (it
        would also break the existing contract, pinned in
        ``test_device_lock_visibility.py``, that construction emits at
        most the unlocked-client notice). A grade still unreadable at the
        moment it is about to disarm hygiene *on a device that has
        answered a request* is the alarming case, and that one warns.
        """
        original = self.timeout
        self.timeout = (
            min(self._AUTODETECT_PROBE_TIMEOUT, original)
            if timeout is None
            else timeout
        )
        # Set here rather than at the call site: the flag means "a probe
        # was actually issued", and every probe goes through this method.
        self._probe_attempted = True
        self._probing = True
        try:
            info = self.get_info()
        except Exception as exc:  # noqa: BLE001 - construction never raises
            _log.debug(
                "Ultimate device %s: firmware probe failed after %.2fs (%s: %s); "
                "capabilities grade as unknown (write threshold %d).",
                self.host, self.timeout, type(exc).__name__, exc,
                THRESHOLD_POST_RISKY,
            )
            return None
        finally:
            self._probing = False
            self.timeout = original
        return info if isinstance(info, dict) else None

    def _maybe_reprobe_capabilities(self) -> None:
        """Re-probe once, on evidence that a device is actually there.

        The hole this closes: ``__init__`` probes under a 0.5 s cap and
        collapses *every* failure into ``None``, which is cached forever,
        and an unreadable grade disarms ``/Temp`` hygiene. So a real C64U
        that answers ``/v1/info`` in 501 ms gets no accounting, no budget,
        no drain and no refusal for the life of that client — with an INFO
        line indistinguishable from a fake host's.

        The correlation runs the wrong way, which is what makes it
        dangerous rather than merely untidy: a device is slow to answer
        when it is loaded or distressed, which is exactly the state of a
        device accumulating uncollected ``/Temp`` attachments. The mechanism would
        disarm hardest precisely when it is most needed.

        A completed request is the evidence that settles it — a host that
        is not there cannot answer one. So once this client has completed
        one, if the cached grade is unreadable, throw it away and probe
        again at the full timeout. Once per client. This keeps the
        off-the-network property intact: a client that never completes a
        request never re-probes.

        The re-probe fires at the two points where the grade decides
        something — before an attachment-creating request, and before a
        drain — rather than from inside ``_request`` itself. Firing it
        there would inject a ``/v1/info`` GET into the middle of every
        caller's request stream, which changes observable wire behaviour
        for every consumer (and broke nine existing tests that assert on
        exactly which requests a call makes). Deferring costs one thing,
        bounded and harmless: on a slow-probed device the *first*
        attachment-creating call is decided on the stale unknown grade.
        It is still counted, and the second call arms — well inside a
        budget of 6.

        ``write_mem_query_threshold`` is deliberately *not* recomputed. It
        was fixed at construction and callers may have reasoned about it;
        the conservative 128 it holds is safe on any firmware, costing
        only extra PUTs on a device that turns out to carry the fix.
        """
        if self._reprobed or self._probing or not self._probe_attempted:
            return
        if not self._saw_successful_request:
            return
        caps = self.cached_capabilities
        if caps is None or caps.firmware_version is not None:
            return
        self._reprobed = True
        info = self._probe_info(timeout=self.timeout)
        if not isinstance(info, dict):
            # This is the alarming case, and the only one worth a WARNING:
            # the device has answered a request, so it is demonstrably
            # there, yet it will not tell us what firmware it runs -- and
            # an unreadable grade leaves /Temp hygiene disarmed while
            # attachments accumulate.
            _log.warning(
                "Ultimate device %s: firmware probe failed again at the full "
                "%.1fs timeout, on a device that has answered a request. "
                "/Temp hygiene stays DISARMED and attachments will accumulate "
                "uncollected. If this is a C64U (or any firmware without "
                "upstream #686), set U64_AUTO_TEMP_GC=1 to force the pass on, "
                "or pass temp_hygiene=True. See docs/u64_recovery.md.",
                self.host, self.timeout,
            )
            return
        self._capabilities = DeviceCapabilities.from_info(info)
        # The grade changed, so the logged grade must too.
        self.log_device_grading()

    # ----------------------------------------------------------------- internal
    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._base + path

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[int, bytes]:
        if method != "GET":
            self._check_device_lock(f"{method} {path}")
        if self._creates_temp_attachment(method, body):
            # Gate and count in one step under the device ledger's lock
            # (#295), before sending. Counted whether or not the call then
            # fails: the firmware writes the attachment as the body streams
            # in, so a request that errors afterwards has still left one.
            self._reserve_temp_attachments(f"{method} {path}")
        status, data = self._request_uncounted(
            method, path, body=body, content_type=content_type, query=query
        )
        # Reaching here means the device answered, which is the evidence a
        # construct-time probe failure could not supply. Only the fact is
        # recorded here, not the re-probe: firing a GET from inside every
        # request would inject it into every caller's request stream. The
        # re-probe happens at the points where the grade actually decides
        # something (see _maybe_reprobe_capabilities).
        self._saw_successful_request = True
        return status, data

    @staticmethod
    def _creates_temp_attachment(method: str, body: bytes | None) -> bool:
        """Whether this request leaves a managed file in the device's ``/Temp``.

        The firmware's route table is the authority, and it is unambiguous:
        a route either binds ``&attachment_writer`` (which streams the
        request body into a managed ``/Temp`` temp file) or binds ``NULL``
        (the body is ditched). Every ``POST`` route in
        ``software/api/route_*.cc`` binds a writer —
        ``configs``, ``drives:mount``, ``drives:load_rom``,
        ``machine:writemem``, ``runners:{run_prg,load_prg,run_crt,sidplay}``
        — and **every** ``PUT`` route binds ``NULL``, which is why
        ``PUT machine:writemem?data=<hex>`` and the config PUTs are free.

        So the rule is: a body plus ``POST``. That covers every current
        caller and anything added later, by construction.

        Two deliberate conservatisms:

        * ``POST runners:modplay`` binds ``&attachment_reu`` (the body
          goes to the REU, not to ``/Temp``) and ``POST machine:input``
          binds ``&input_json_writer``. Both are counted anyway — the
          cost is an occasional extra hygiene pass, and neither handler
          has been read closely enough here to certify it never touches
          ``/Temp``.
        * The route table read is a 3.15-line checkout. The C64U's 1.1.0
          table is not available, so this assumes the verb/handler
          pairing is the same there. Counting POST-with-body only is the
          **permissive** side of that assumption, not the conservative
          one: a PUT that *did* attach on 1.1.0 is never counted, so it
          never advances ``_pending_temp_attachments``, the budget
          comparison in :meth:`_before_temp_attachment` is never reached,
          the hygiene pass never fires
          and ``pending_temp_attachments`` reads zero while the device
          accumulates. That is the gap to close, not the margin to rely
          on, and one live run on a 1.1.0 device closes it. The genuinely
          conservative half is the over-counting in the bullet above.
        """
        return body is not None and method == "POST"

    def _request_uncounted(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[int, bytes]:
        url = self._url(path)
        if query:
            # Preserve caller-formatted values (e.g. "0400") by stringifying as-is
            qs = "&".join(f"{urllib.parse.quote(str(k))}={urllib.parse.quote(str(v))}" for k, v in query.items())
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, data=body, method=method)
        if self.password:
            req.add_header("X-Password", self.password)
        if content_type:
            req.add_header("Content-Type", content_type)
        _log.debug("Ultimate64 %s %s (body=%s bytes)", method, url, len(body) if body else 0)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                data = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                data = e.read() if e.fp else b""
            except Exception:
                data = b""
            _log.debug("Ultimate64 %s %s -> %d (error body=%s bytes)", method, url, status, len(data))
            self._raise_for_status(status, data, method, url)
            return status, data  # unreachable
        except socket.timeout as e:
            raise Ultimate64TimeoutError(f"timeout after {self.timeout}s: {method} {url}") from e
        except (ConnectionResetError, BrokenPipeError, http.client.HTTPException) as e:
            # urllib only wraps the *send* phase in URLError; failures during
            # getresponse()/read() escape raw (fw 3.14d drops connections
            # mid-response under load — see ultimate64_probe.py).  Map them
            # into the client's exception hierarchy so callers can catch
            # Ultimate64Error uniformly.
            raise Ultimate64TimeoutError(
                f"connection dropped: {method} {url}: {type(e).__name__}: {e}"
            ) from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, socket.timeout):
                raise Ultimate64TimeoutError(f"timeout after {self.timeout}s: {method} {url}") from e
            raise Ultimate64TimeoutError(f"connection failed: {method} {url}: {reason}") from e

        _log.debug("Ultimate64 %s %s -> %d (%d bytes)", method, url, status, len(data))
        if status < 200 or status >= 300:
            self._raise_for_status(status, data, method, url)
        return status, data

    # ------------------------------------------------------ /Temp hygiene
    def log_device_grading(self) -> None:
        """Say at INFO which device this is and how it graded.

        Emitted once per client, whether or not anything acts on the
        grade. That "whether or not" is the point. The hazard this whole
        mechanism exists for is structurally invisible from a machine
        carrying the firmware fix: a script issuing hundreds of leaking
        writes runs perfectly on a 3.15 U64E and wedges a C64U, and
        nothing in its output tells the author which one they are on. A
        hygiene pass that silently does the right thing preserves that
        blindness. One line naming the host, the graded firmware, the
        capability and the resulting threshold makes it legible in every
        log, including the logs of runs where nothing went wrong.
        """
        caps = self.cached_capabilities
        if caps is None or caps.firmware_version is None:
            # These two are not the same fact and must not print the same.
            # "not-attempted" is inert by contract (the caller pinned the
            # threshold). "probe-failed" means a device may well be there
            # and simply did not answer in time -- the case that used to
            # disarm hygiene silently.
            why = "not-attempted" if not self._probe_attempted else "probe-failed"
            grade = (
                f"firmware=unknown({why}) "
                f"writemem_post_safe={None if caps is None else caps.writemem_post_safe}"
            )
        else:
            grade = (
                f"firmware={caps.firmware_version} "
                f"generation={caps.generation} "
                f"writemem_post_safe={caps.writemem_post_safe}"
            )
        _log.info(
            "Ultimate device %s: %s write_mem_query_threshold=%d "
            "/Temp hygiene=%s (leak budget %d, keep-count default). "
            "POSTs above the threshold leak a /Temp attachment on firmware "
            "without upstream #686.",
            self.host,
            grade,
            self.write_mem_query_threshold,
            "armed" if self.temp_hygiene_armed else "disarmed",
            self._temp_gc_budget,
        )

    @property
    def temp_hygiene_armed(self) -> bool:
        """Whether this client collects the device's ``/Temp`` attachments.

        Decided in this order:

        1. the ``temp_hygiene=`` constructor argument, if given;
        2. ``$U64_AUTO_TEMP_GC`` — truthy forces the pass on for *any*
           device, falsy forces it off;
        3. otherwise the device's firmware:
           :attr:`~c64_test_harness.backends.u64_capabilities.DeviceCapabilities.runner_wedge_possible`
           ``False`` (the upstream collector is present — U64E 3.15 and
           later) disarms; ``True`` or ``None`` (absent, or the version
           string cannot settle it) arms, because unknown firmware
           resolves conservatively.

        Note what this is *not* keyed on: the device's address. The C64U
        is reached through ``$U64_HOST``/``--host`` like any other device
        and moves between addresses, so an IP allowlist would miss the
        real path. Capabilities come from ``GET /v1/info``, so the
        protection follows the device.

        With one exception, which is what keeps the unit suite off the
        network: if nothing ever answered the capability probe
        (``firmware_version is None``) there is no device on the far end
        and therefore no ``/Temp`` to collect, so the pass stays inert.
        Every fake host in the test suite is in that state, as is a
        client constructed with an explicit ``write_mem_query_threshold``
        (which by contract issues no HTTP at construction and so never
        probes, and therefore stays disarmed for its lifetime —
        ``U64_AUTO_TEMP_GC=1`` is what forces it back on).

        A real device that is merely *slow* — one whose 0.5 s
        construct-time probe timed out — does **not** stay disarmed:
        :meth:`_maybe_reprobe_capabilities` exists to close exactly that
        hole and runs before every attachment-creating request, re-probing
        at the full timeout once one request has completed. The residue is
        narrower and worth stating precisely: the **first**
        attachment-creating call is decided on the stale unknown grade —
        it is still counted — and the client arms from the second. Pinned
        in ``tests/test_ultimate64_temp_hygiene.py`` by
        ``test_a_device_that_answered_late_is_regraded_after_a_successful_request``.
        """
        if self._temp_hygiene_force is not None:
            return self._temp_hygiene_force
        from .ultimate64_temp_gc import auto_gc_override as _auto_gc_override

        override = _auto_gc_override()
        if override is not None:
            return override
        # Deliberately reads the *cached* capabilities rather than the
        # probing property: arming must never issue HTTP of its own, so
        # a client that has never probed (one constructed with an
        # explicit write_mem_query_threshold, which documents that it
        # issues no traffic at all) stays disarmed. Pass
        # temp_hygiene=True, or set U64_AUTO_TEMP_GC=1, to arm one of
        # those against a leak-prone device.
        caps = self.cached_capabilities
        if caps is None or caps.firmware_version is None:
            return False
        # runner_wedge_possible is the inverse of writemem_post_safe and
        # is the honest name at this call site: the question here is not
        # "may I POST small payloads" but "can this device wedge under
        # write load". ``is not False`` keeps the tri-state conservative
        # — True arms, and so does None (a version string that cannot
        # settle the question).
        return caps.runner_wedge_possible is not False

    @property
    def temp_gc_budget(self) -> int:
        """Attachment-creating requests allowed between hygiene passes."""
        return self._temp_gc_budget

    @property
    def pending_temp_attachments(self) -> int:
        """Attachments counted on this client's **device** since the last
        successful sweep, by any client of that host in this process (#295).

        Not this client's own share, and not other processes' uploads.
        """
        return self._temp_ledger.pending

    # The device ledger (#295) backs what used to be instance state; these
    # keep the old attribute names working for callers and tests.
    @property
    def _pending_temp_attachments(self) -> int:
        return self._temp_ledger.pending

    @property
    def _temp_hygiene_blocked(self) -> str | None:
        return self._temp_ledger.blocked

    @_temp_hygiene_blocked.setter
    def _temp_hygiene_blocked(self, value: str | None) -> None:
        self._temp_ledger.blocked = value

    @property
    def _ftp_enable_attempted(self) -> bool:
        return self._temp_ledger.ftp_enable_attempted

    @_ftp_enable_attempted.setter
    def _ftp_enable_attempted(self, value: bool) -> None:
        self._temp_ledger.ftp_enable_attempted = value

    def _own_pending_temp_attachments(self) -> int:
        """This client's attachments not yet collected by any sweep."""
        ledger = self._temp_ledger
        with ledger.lock:
            if self._own_temp_generation != ledger.generation:
                return 0
            return self._own_temp_attachments

    def _count_temp_attachments(self, count: int) -> None:
        ledger = self._temp_ledger
        with ledger.lock:
            if self._own_temp_generation != ledger.generation:
                self._own_temp_generation = ledger.generation
                self._own_temp_attachments = 0
            self._own_temp_attachments += count
            ledger.pending += count
            if self.temp_hygiene_armed:
                # So the ledger can still sweep these if every client that
                # counted them is collected before the lock release.
                ledger.armed_pending = True

    def _uncount_temp_attachments(self, count: int, generation: int) -> None:
        """Refund a reservation that was not sent, unless a sweep since
        *generation* has already zeroed the count."""
        ledger = self._temp_ledger
        with ledger.lock:
            if count <= 0 or ledger.generation != generation:
                return
            ledger.pending = max(0, ledger.pending - count)
            if self._own_temp_generation == generation:
                self._own_temp_attachments = max(
                    0, self._own_temp_attachments - count
                )

    def _reserve_temp_attachments(self, operation: str, count: int = 1) -> int:
        """Gate, then count, *count* attachments as one step on the device.

        Holding the ledger lock across both is what keeps two clients (or two
        threads) from each passing the budget check and then both counting.

        :returns: the ledger generation the reservation was counted in.
        :raises Ultimate64TempHygieneError: see :meth:`_before_temp_attachment`.
        """
        ledger = self._temp_ledger
        with ledger.lock:
            self._before_temp_attachment(operation, count)
            self._count_temp_attachments(count)
            return ledger.generation

    def _before_temp_attachment(self, operation: str, count: int = 1) -> None:
        """Refuse or make room before *count* attachment-creating requests.

        *count* > 1 reserves an operation's whole cost up front, so a
        multi-POST operation is never refused half-way through (see
        :meth:`liveness_probe`, whose second POST restores RAM).

        The pass runs when ``pending > 0 and pending + count > budget``.
        For ``count=1`` that is exactly ``pending >= budget`` -- which
        relies on the budget being at least 1, as ``__init__`` validates
        and :func:`~c64_test_harness.backends.ultimate64_temp_gc.leak_budget`
        enforces. The ``pending > 0`` guard is kept deliberately: with
        nothing pending a pass could collect nothing this client spent, so a
        reservation larger than the whole budget (a probe on a fresh
        ``temp_gc_budget=1`` client) runs one over and the next
        attachment-creating request sweeps.

        :raises Ultimate64TempHygieneError: when hygiene is armed, has
            been proven impossible, and ``U64_TEMP_GC_REQUIRED`` has not
            opted out.
        """
        if self._in_temp_hygiene:
            return
        self._maybe_reprobe_capabilities()
        if not self.temp_hygiene_armed:
            return
        if self._temp_hygiene_blocked is not None:
            self._refuse_or_warn(operation)
            return
        # The device's count, not this client's (#295). Callers hold the
        # ledger lock: go through _reserve_temp_attachments.
        pending = self._temp_ledger.pending
        if pending > 0 and pending + count > self._temp_gc_budget:
            self._run_temp_hygiene(
                f"budget of {self._temp_gc_budget} attachment(s) spent before {operation}"
            )
            if self._temp_hygiene_blocked is not None:
                self._refuse_or_warn(operation)

    def _refuse_or_warn(self, operation: str) -> None:
        from .ultimate64_temp_gc import hygiene_required as _hygiene_required

        # The cached grade, never the probing property: building an error
        # message must not issue HTTP or fill the cache, on a device the
        # harness has just concluded it cannot clean up after (#265).
        caps = self.cached_capabilities
        firmware = (caps.firmware_version if caps is not None else None) or "unknown"
        message = (
            f"refusing {operation} on {self.host}: this firmware "
            f"({firmware}) leaks a /Temp "
            "attachment for every request that carries a body and never collects "
            "them, and the harness's hygiene pass cannot run: "
            f"{self._temp_hygiene_blocked}. Continuing would walk the device "
            "towards the /Temp-accumulation wedge, which only a physical "
            "power-cycle clears. Remedy: enable Network Settings > FTP File "
            "Service on the device (and save it to flash), or power-cycle it to "
            "empty /Temp. To proceed anyway set U64_TEMP_GC_REQUIRED=0, or pass "
            "temp_hygiene=False to disarm the pass entirely. See "
            "docs/u64_recovery.md."
        )
        if _hygiene_required():
            raise Ultimate64TempHygieneError(message)
        _log.warning("U64_TEMP_GC_REQUIRED=0: proceeding anyway. %s", message)

    def _run_temp_hygiene(self, reason: str) -> bool:
        """Run one hygiene pass; ``True`` if ``/Temp`` was collected.

        Never raises. On failure it makes exactly one attempt per device per
        process (the ledger's ``ftp_enable_attempted``, #295) at enabling
        the device's FTP File Service (off by default on C64U
        1.1.0, which is the one device that needs this pass) and retries.
        The enable is a bodyless config PUT — it creates no attachment of
        its own — and is runtime-only: it lives in firmware RAM until
        ``save_config_to_flash``, and the power-on that reverts it also
        empties ``/Temp`` (a RAM disk).

        **Two contracts, not one** (owner decision on #263): that enable is
        the one sanctioned write into a ``BASELINE_NEVER_TOUCH`` store.
        ``BASELINE_NEVER_TOUCH`` is ``apply_factory_baseline``'s contract --
        the entry-baseline reset never resets or asserts those stores -- and
        says nothing about this pass, which may write exactly one item,
        ``Network Settings > FTP File Service``, once per device per process,
        only for a client that has leaked or is about to (the drain requires
        this client's own uncollected attachments; the budget gate runs
        before an attachment-creating request) and only after its sweep
        failed.  A client that leaked nothing never
        reaches it: :meth:`_sweep_inherited_temp` writes no config.
        """
        self._in_temp_hygiene = True
        try:
            _log.debug("U64 /Temp hygiene on %s: %s", self.host, reason)
            result = self.gc_temp_folder()
            if getattr(result, "ok", False):
                # Only a successful sweep zeroes the device's count (#295).
                self._temp_ledger.collected()
                return True

            first_error = getattr(result, "error", None)
            if not self._ftp_enable_attempted:
                self._ftp_enable_attempted = True
                # WARNING, not INFO: this mutates the device's config and
                # the change persists until a firmware power-on (it lives
                # in firmware RAM; machine:reboot does not clear it). A
                # deliberate, persistent change to a shared device is not
                # an INFO-level fact about this run.
                _log.warning(
                    "U64 /Temp hygiene on %s failed (%s); enabling Network "
                    "Settings > FTP File Service and retrying. This config "
                    "write persists until a firmware power-on -- it is not "
                    "restored on drain, and machine:reboot does not clear it.",
                    self.host, first_error,
                )
                try:
                    self.set_config_item(
                        "Network Settings", "FTP File Service", "Enabled"
                    )
                except Exception as exc:  # noqa: BLE001 - hygiene never raises here
                    _log.info(
                        "U64 /Temp hygiene on %s: could not enable FTP File "
                        "Service (%s: %s)",
                        self.host, type(exc).__name__, exc,
                    )
                else:
                    result = self.gc_temp_folder()
                    if getattr(result, "ok", False):
                        self._temp_ledger.collected()
                        return True

            self._temp_hygiene_blocked = str(
                getattr(result, "error", None) or first_error or "unknown FTP failure"
            )
            return False
        finally:
            self._in_temp_hygiene = False

    def _drain_on_lock_release(self, reason: str = "device lock release") -> bool:
        """Lock-release drain for this client; ``True`` if it attempted a sweep.

        Called by the device's ``TempLedger.drain_on_lock_release``, which is
        what is registered with ``device_lock.register_release_callback``
        (one registration per host, #295). ``DeviceLock.release`` fires
        callbacks only on the outermost release and while the flock is still
        held, so this drain runs under the lock by construction.
        """
        return self._drain_temp_attachments(reason=reason, under_lock=True)

    def _holds_device_lock(self) -> bool:
        """Whether this process holds this device's ``DeviceLock`` (no I/O).

        Asks :meth:`DeviceLock.held_by_this_process` about the **default**
        lock directory only. A process that holds the lock under a custom
        ``lock_dir`` therefore reads as not holding it here, so its
        ``close()`` does not sweep inherited ``/Temp``. Its lock-release
        callback still does: :meth:`_drain_on_lock_release` runs under the
        flock by construction and never consults this. Accepted in review
        (PR #297, round 2) as the conservative direction.
        """
        if not _HAS_DEVICE_LOCK:
            return False
        try:
            from .device_lock import DeviceLock

            return bool(DeviceLock.held_by_this_process(self.host))
        except Exception:  # noqa: BLE001 - a lock query must never fail a drain
            return False

    def _drain_temp_attachments(
        self, reason: str = "drain", *, under_lock: bool = False
    ) -> bool:
        """Sweep the device's ``/Temp`` on the way out. Never raises.

        Returns ``True`` if a hygiene pass or inherited sweep was attempted,
        so the device ledger's release drain can stop after one (#295).
        "Leaked" below is this client's own share of the device's count, not
        yet collected by any client's sweep.

        Two cases, and they are deliberately not treated alike:

        * **This client leaked** (``pending_temp_attachments > 0``): the
          ordinary hygiene pass, unchanged -- including its one FTP-enable
          attempt and the block on failure. A lane that leaked may make that
          write (owner decision on #263); :meth:`_run_temp_hygiene` says why
          it is not a ``BASELINE_NEVER_TOUCH`` violation.
        * **This client leaked nothing** (issue #264): the wedge is a
          property of the device and :meth:`gc_temp_folder` sweeps ``/Temp``
          device-wide, so a lane that inherited a crashed neighbour's
          attachments still collects them. But that sweep deletes files
          this client did not create, so it runs only **under the device
          lock** (the lock-release callback, or a ``close()`` while this
          process holds the lock); and a lane that made only bodyless calls
          must not write ``Network Settings > FTP File Service`` (a
          BASELINE_NEVER_TOUCH store that persists until a firmware
          power-on) or be refused for a failure it did not cause. So a
          failed inherited sweep logs a WARNING naming the manual remedy,
          enables nothing and blocks nothing.

        What keeps a fake host off FTP is arming (a never-answered probe
        stays disarmed), not the counter.
        """
        try:
            with self._temp_ledger.lock:
                # This client's own uncollected share: whether it may take
                # the leaking-lane path (the FTP-enable attempt, the block on
                # failure) is still decided by what *it* did (#263, #264).
                leaked = self._own_pending_temp_attachments() > 0
                if leaked:
                    # Something leaked, so a device is demonstrably there:
                    # settle the grade before deciding not to clean up after
                    # it. Deliberately not done on a zero count -- that would
                    # put a /v1/info GET into close() for every client that
                    # ever completed a request, fake hosts included.
                    self._maybe_reprobe_capabilities()
                if not self.temp_hygiene_armed:
                    return False
                if leaked:
                    self._run_temp_hygiene(reason)
                    return True
                if not (under_lock or self._holds_device_lock()):
                    _log.debug(
                        "U64 /Temp drain on %s (%s): this client leaked nothing and "
                        "does not hold the device lock; not sweeping other lanes' "
                        "attachments",
                        self.host, reason,
                    )
                    return False
                self._sweep_inherited_temp(reason)
                return True
        except Exception as exc:  # noqa: BLE001 - a drain must never fail a run
            _log.debug(
                "U64 /Temp drain on %s raised (%s: %s); ignored",
                self.host, type(exc).__name__, exc,
            )
            return False

    def _sweep_inherited_temp(self, reason: str) -> None:
        """Best-effort sweep for attachments this client did not create.

        No FTP-enable attempt and no block on failure -- see
        :meth:`_drain_temp_attachments`.
        """
        self._in_temp_hygiene = True
        try:
            _log.debug("U64 /Temp inherited sweep on %s: %s", self.host, reason)
            result = self.gc_temp_folder()
        finally:
            self._in_temp_hygiene = False
        if getattr(result, "ok", False):
            # The sweep is device-wide, so it collected every client's
            # attachments, and FTP answered: clear the device's count and
            # any block another client's failed pass left (#295).
            self._temp_ledger.collected()
            return
        _log.warning(
            "U64 /Temp inherited sweep on %s failed (%s). This client leaked "
            "nothing, so the harness neither enables Network Settings > FTP "
            "File Service on its behalf (issue #263) nor refuses its requests; "
            "but /Temp may still hold attachments an earlier lane left behind. "
            "Before uploading to this device, enable FTP File Service manually "
            "(it persists until a firmware power-on) or have it power-cycled. "
            "See docs/u64_recovery.md.",
            self.host, getattr(result, "error", None) or "unknown FTP failure",
        )

    def _check_device_lock(self, operation: str) -> None:
        """Advisory device-lock check for a state-changing request.

        Every non-GET request against the device mutates it: the
        ``/v1/machine:*`` family (reset, reboot, pause, poweroff), the
        runners (``run_prg``, ``run_crt``, sidplay), ``writemem``,
        drive mounts, and every config PUT/POST — which is how CPU turbo
        and REU settings are changed.  Enumerating them individually
        would rot; the HTTP method is the invariant.

        See :func:`~c64_test_harness.backends.device_lock.advisory_lock_check`
        for the warn/raise policy.  No-op when ``device_lock`` is
        unavailable.
        """
        if not _HAS_DEVICE_LOCK:
            return
        _advisory_lock_check(self.host, operation, logger=_log)

    @staticmethod
    def _raise_for_status(status: int, data: bytes, method: str, url: str) -> None:
        body_text = data.decode("utf-8", errors="replace") if data else ""
        msg = f"{method} {url} returned HTTP {status}"
        if body_text:
            msg += f": {body_text[:256]}"
        if status in (401, 403):
            raise Ultimate64AuthError(msg, status=status, body=body_text)
        if status == 400:
            for route, hint in _STRICT_HEX_ROUTE_HINTS.items():
                if route in url:
                    raise Ultimate64WireFormatError(
                        msg + hint, status=status, body=body_text
                    )
        raise Ultimate64Error(msg, status=status, body=body_text)

    def _get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        _, data = self._request("GET", path, query=query)
        return self._parse_json(data)

    def _put_no_body(self, path: str, query: dict[str, Any] | None = None) -> None:
        self._request("PUT", path, query=query)

    def _put_json(self, path: str, payload: Any) -> Any:
        body = json.dumps(payload).encode("utf-8")
        status, data = self._request("PUT", path, body=body, content_type="application/json")
        if data:
            try:
                return self._parse_json(data)
            except Ultimate64ProtocolError:
                return None
        return None

    @staticmethod
    def _parse_json(data: bytes) -> Any:
        if not data:
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Ultimate64ProtocolError(f"invalid JSON from device: {e}") from e

    # ----------------------------------------------------------------- identity
    def get_version(self) -> dict:
        """GET /v1/version — REST API version info."""
        return self._get_json("/v1/version")

    def get_info(self) -> dict:
        """GET /v1/info — product, firmware_version, fpga_version, etc."""
        return self._get_json("/v1/info")

    #: Body-carrying POSTs one :meth:`liveness_probe` issues: the probe
    #: write and the restore of the original bytes. Each is a ``/Temp``
    #: attachment on firmware without upstream #686 (measured on the C64U,
    #: fw 1.1.0: ``/Temp`` 0 -> 2 for one call; issue #250).
    LIVENESS_PROBE_TEMP_ATTACHMENTS = 2

    def liveness_probe(self, http_timeout: float = 2.0) -> "LivenessResult":
        """Run the writemem-degradation liveness probe against this device.

        **Not free: on leak-prone firmware one call costs two /Temp
        attachments** (:attr:`LIVENESS_PROBE_TEMP_ATTACHMENTS`). It
        deliberately exercises ``POST /v1/machine:writemem`` -- a probe
        write and a restore -- which is the point of it and also the price.
        Both POSTs count against :attr:`temp_gc_budget` like any other
        attachment-creating request, and the whole cost is reserved before
        the probe sends anything: if the budget cannot hold both, the
        hygiene pass runs first, and if hygiene has been proven impossible
        this raises :class:`Ultimate64TempHygieneError` without touching the
        device (so the restore is never the request that gets refused).
        Diagnose a suspected wedge with bodyless calls first --
        :meth:`get_info`, :meth:`get_version` and :meth:`read_mem` cost
        nothing -- and probe once, deliberately.

        Delegates to
        :func:`c64_test_harness.backends.ultimate64_probe.liveness_probe`,
        passing the client's ``host``, ``port`` and ``password`` and a
        request sender that routes the accounting through this client.
        Unlike :meth:`get_version` / :meth:`get_info`, it detects the fw
        3.14d writemem-degraded transient state described in issue #107.

        :param http_timeout: per-request socket timeout (default 2 s);
            kept short so a wedged TCP stack returns
            ``failure="tcp_stack_wedged"`` quickly.
        :returns: a structured
            :class:`~c64_test_harness.backends.ultimate64_probe.LivenessResult`.
        :raises Ultimate64TempHygieneError: see above.
        """
        from . import ultimate64_probe as _probe

        cost = self.LIVENESS_PROBE_TEMP_ATTACHMENTS
        # The whole cost is gated and counted up front against the device's
        # count (#295); whatever the probe does not send is refunded below.
        generation = self._reserve_temp_attachments(
            f"liveness_probe ({cost} x POST /v1/machine:writemem)", count=cost
        )
        if self.temp_hygiene_armed:
            _log.info(
                "liveness_probe on %s spends %d /Temp attachments on this "
                "firmware (%d of %d already pending)",
                self.host, cost, self._temp_ledger.pending - cost,
                self._temp_gc_budget,
            )
        sent = [0]

        def _accounted(method, host, port, path, password, timeout, **kwargs):
            # The probe keeps its own raw sender and failure classification;
            # this only adds what _request adds: the lock check and the
            # count. The gate already ran above for the whole operation.
            leaks = self._creates_temp_attachment(method, kwargs.get("body"))
            if method != "GET":
                self._check_device_lock(f"{method} {path}")
            try:
                result = _probe._liveness_request(
                    method, host, port, path, password, timeout, **kwargs
                )
            finally:
                if leaks:
                    sent[0] += 1
            self._saw_successful_request = True
            return result

        try:
            return _probe.liveness_probe(
                self.host,
                port=self.port,
                password=self.password,
                http_timeout=http_timeout,
                request=_accounted,
            )
        finally:
            if sent[0] > cost:
                self._count_temp_attachments(sent[0] - cost)
            else:
                self._uncount_temp_attachments(cost - sent[0], generation)

    def assert_healthy(self, http_timeout: float = 2.0) -> "LivenessResult":
        """Run :meth:`liveness_probe` and raise on failure.

        Costs what :meth:`liveness_probe` costs: two /Temp attachments per
        call on leak-prone firmware, and it raises
        :class:`Ultimate64TempHygieneError` before probing when the hygiene
        pass cannot run. It reads like a free precondition check and is not
        one; do not call it in a loop against a C64U.

        :raises U64UnreachableError: if the device fails the reachability
            portion (TCP / version GET).
        :raises U64WritememDegradedError: if the device is reachable but
            the writemem POST round-trip failed (HTTP 404, timeout, or
            wedged TCP stack).
        :raises Ultimate64WireFormatError: if the probe came back tagged
            ``wire_format`` — an HTTP 400 from a route that parses hex
            strictly (issue #272).  The device is healthy; the request
            was not.
        :returns: the :class:`LivenessResult` on success (healthy device).
        """
        result = self.liveness_probe(http_timeout=http_timeout)
        if result.healthy:
            return result
        if result.failure == "wire_format":
            # Not a degraded device: the firmware refused a malformed
            # request (issue #272).  Raising the degraded error here
            # would put the wrong word in front of whoever is deciding
            # whether to escalate to a physical power-cycle.
            raise Ultimate64WireFormatError(
                f"U64 at {self.host}:{self.port} rejected the request "
                f"({result.failure}): {result.recommendation}",
                status=400,
                result=result,
            )
        if result.failure == "unreachable":
            raise U64UnreachableError(
                f"U64 at {self.host}:{self.port} unreachable: "
                f"{result.recommendation}"
            )
        raise U64WritememDegradedError(
            f"U64 at {self.host}:{self.port} writemem-degraded "
            f"({result.failure}): {result.recommendation}",
            result=result,
        )

    def list_configs(self) -> list[str]:
        """GET /v1/configs — returns the list of config category names."""
        payload = self._get_json("/v1/configs")
        if not isinstance(payload, dict):
            raise Ultimate64ProtocolError(f"expected object from /v1/configs, got {type(payload).__name__}")
        cats = payload.get("categories", [])
        if not isinstance(cats, list):
            raise Ultimate64ProtocolError("categories field is not a list")
        return [str(c) for c in cats]

    def get_config_category(self, category: str) -> dict:
        """GET /v1/configs/<category> — all items in a category.

        Returns the raw response, including the ``<Category>`` wrapper key
        and the ``errors`` array.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        return self._get_json(f"/v1/configs/{_encode(category)}")

    def get_config_item_raw(self, category: str, item: str) -> dict:
        """GET /v1/configs/<category>/<item> — the untouched REST envelope.

        Returns exactly what the firmware sent, wrapper key and
        ``errors`` array included::

            {"C64 and Cartridge Settings":
                {"Cartridge Preference":
                    {"current": "External",
                     "values": ["Auto", "Internal", "External", "Manual"],
                     "default": "Auto"}},
             "errors": []}

        Nothing is validated; a non-empty ``errors`` array is passed
        through for the caller to inspect.  Prefer :meth:`get_config_item`
        (the item map) or :meth:`get_config_value` (its ``current``).
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(item, str) or not item:
            raise ValueError("item must be a non-empty string")
        return self._get_json(f"/v1/configs/{_encode(category)}/{_encode(item)}")

    @staticmethod
    def _resolve_config_key(mapping: dict, name: str, kind: str, where: str) -> str:
        """Return the canonical key in *mapping* that *name* addresses.

        Mirrors how the firmware matched the request: ``route_configs.cc``
        ``emit_store`` runs the caller's category and item through
        ``pattern_match`` (``components/pattern.cc``: ``*``/``?`` glob,
        case-insensitive by default) and keys the JSON by the canonical
        ``store_name`` / ``item_text``.  So the key is found exact first,
        then case-insensitively, then as a glob that must match exactly
        one key.

        :raises Ultimate64ProtocolError: when nothing matches (the message
            lists the keys that are present) or a glob matches several
            (one item map cannot carry several; use
            :meth:`get_config_item_raw` for patterns).
        """
        if name in mapping:
            return name
        present = sorted(k for k in mapping if isinstance(k, str))
        folded = [k for k in present if k.lower() == name.lower()]
        if len(folded) == 1:
            return folded[0]
        if "*" in name or "?" in name:
            regex = re.escape(name).replace(r"\*", ".*").replace(r"\?", ".")
            matched = [k for k in present if re.fullmatch(regex, k, re.IGNORECASE)]
            if len(matched) == 1:
                return matched[0]
            if matched:
                raise Ultimate64ProtocolError(
                    f"{kind} pattern {name!r} matches {len(matched)} of {where}: "
                    f"{matched!r} -- one item map cannot carry several; "
                    f"use get_config_item_raw for patterns"
                )
        raise Ultimate64ProtocolError(
            f"{kind} {name!r} absent from {where}; present: {present!r}"
        )

    def get_config_item(self, category: str, item: str) -> dict:
        """GET /v1/configs/<category>/<item> — the item's own map.

        Returns the map the firmware describes the item with, unwrapped
        from the category/item envelope::

            >>> client.get_config_item("C64 and Cartridge Settings",
            ...                        "Cartridge Preference")
            {'current': 'External',
             'values': ['Auto', 'Internal', 'External', 'Manual'],
             'default': 'Auto'}

        **The key set is the item's type** (``route_configs.cc``
        ``emit_store``, identical on 1.1.0, 3.14d and 3.15):

        * ``"values"`` — enum item.  Built from index 0 to ``max``
          inclusive, so it is **never empty**.
        * ``"presets"`` — preset-file item (the ``Cartridge`` .crt chooser).
          **May be empty** (``[""]`` on the U64E, ``[]`` is possible).
        * ``"min"`` / ``"max"`` / ``"format"`` (+ ``"default"``) — integer
          range item; no choice list.
        * only ``"current"`` / ``"default"`` — free string item.

        So an item map *without* ``"values"`` is a non-enum item, never an
        empty enum; :meth:`get_config_choices` encodes that rule.  The live
        value is always ``"current"`` (:meth:`get_config_value`).

        **Name matching** follows the firmware: category and item are
        ``pattern_match`` globs (``*``/``?``, case-insensitive) and the
        response is keyed by the canonical names, so ``("u64 specific
        settings", "cpu speed")`` resolves to ``"U64 Specific Settings"`` /
        ``"CPU Speed"``.  A glob is accepted only when it matches exactly
        one category and one item; several matches raise (one item map
        cannot represent them — use :meth:`get_config_item_raw`), and a
        plain name that matches nothing raises with the keys present
        rather than being mapped onto whatever single item is there.

        **Unknown names**: stock firmware (1.1.0 / 3.14d) answers an unknown
        *category* with HTTP 200 and no category key at all → this raises
        :class:`Ultimate64ProtocolError`; the 3.15 fork answers 404 → a
        plain :class:`Ultimate64Error` with ``status == 404`` from the
        request layer.  An unknown *item* in a known category is HTTP 200
        with an empty category map on every firmware →
        :class:`Ultimate64ProtocolError`.

        :raises Ultimate64ProtocolError: non-empty ``errors`` array, no
            (or several) matching category/item, or the item not being a
            map (issue #214: an accessor named for the item must not hand
            back a value from a failed read).
        :raises Ultimate64Error: HTTP-level failures (404 on the 3.15 fork).
        """
        envelope = self.get_config_item_raw(category, item)
        if not isinstance(envelope, dict):
            raise Ultimate64ProtocolError(
                f"expected object for config item {category!r}/{item!r}, "
                f"got {type(envelope).__name__}"
            )
        errors = envelope.get("errors")
        if errors:
            raise Ultimate64ProtocolError(
                f"config item {category!r}/{item!r}: device reported errors {errors!r}"
            )
        request = f"GET config item {category!r}/{item!r}"
        categories = {k: v for k, v in envelope.items() if k != "errors"}
        cat_key = self._resolve_config_key(
            categories, category, "category", f"the categories in the response to {request}"
        )
        cat_map = categories[cat_key]
        if not isinstance(cat_map, dict):
            raise Ultimate64ProtocolError(
                f"category {cat_key!r} is not a map in response {envelope!r}"
            )
        item_key = self._resolve_config_key(
            cat_map, item, "item", f"the items of category {cat_key!r} ({request})"
        )
        item_map = cat_map[item_key]
        if not isinstance(item_map, dict):
            raise Ultimate64ProtocolError(
                f"config item {cat_key!r}/{item_key!r} is not a map: {item_map!r} "
                f"(single-item GETs return a map; category GETs return bare values)"
            )
        return item_map

    def get_config_value(self, category: str, item: str) -> Any:
        """Return the item's ``current`` value — what snapshot/restore wants.

            >>> prev = client.get_config_value(cat, item)   # 'Auto'
            >>> client.set_config_item(cat, item, "External")
            >>> client.set_config_item(cat, item, prev)      # restores 'Auto'

        Enum values come back as the firmware's strings (``" 1"`` keeps
        its leading space); range items may come back as ints.

        :raises Ultimate64ProtocolError: as :meth:`get_config_item`, and
            also when the item map has no ``"current"`` key.
        """
        item_map = self.get_config_item(category, item)
        if "current" not in item_map:
            raise Ultimate64ProtocolError(
                f"config item {category!r}/{item!r} has no 'current' value: "
                f"{item_map!r}"
            )
        return item_map["current"]

    def get_config_choices(self, category: str, item: str) -> list[str]:
        """Return the settable choices of an enum or preset-file item.

        ``"values"`` for an enum item (never empty — see
        :meth:`get_config_item`), else ``"presets"`` for a preset-file
        item (may legitimately be empty).  A free-string or integer-range
        item has no choice list and **raises** instead of answering ``[]``,
        so a guard like ``if allowed and prev not in allowed`` cannot be
        silently switched off by a non-enum item.

        :raises Ultimate64ProtocolError: as :meth:`get_config_item`, and
            when the item map carries neither ``"values"`` nor
            ``"presets"``, or the one it carries is not a list.
        """
        item_map = self.get_config_item(category, item)
        for key in ("values", "presets"):
            if key in item_map:
                choices = item_map[key]
                if not isinstance(choices, list):
                    raise Ultimate64ProtocolError(
                        f"config item {category!r}/{item!r}: {key!r} is not a list: "
                        f"{choices!r}"
                    )
                return list(choices)
        raise Ultimate64ProtocolError(
            f"config item {category!r}/{item!r} is not an enum or preset-file item "
            f"(no 'values' or 'presets'; keys: {sorted(item_map)!r})"
        )

    def list_drives(self) -> dict:
        """GET /v1/drives — enumerates all drive slots."""
        return self._get_json("/v1/drives")

    # ------------------------------------------------------------ machine ctrl
    def reset(self) -> None:
        """PUT /v1/machine:reset — soft reset the C64 (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:reset")

    def reboot(self) -> None:
        """PUT /v1/machine:reboot — C64-level reset (DESTRUCTIVE).

        **Not a firmware reboot**, despite the endpoint's name. It is
        ``C64::start_cartridge(NULL)``: the C64 side restarts with
        cartridge and REU re-initialised, ~8 s before it is reachable
        again, which is why it clears REU/DMA stuck state. The firmware
        itself keeps running.

        What that means for state you might expect it to clear:

        * **Config in firmware RAM survives.** A runtime-only REST config
          write (e.g. enabling FTP File Service) is still in effect after
          this call; only a firmware power-on reverts it.
        * **``/Temp`` attachments survive.** ``/Temp`` is a firmware RAM
          disk (``software/filesystem/ramdisk.cc``) and only a firmware
          power-on empties it. Measured on the C64U: one POST leaves
          ``temp0008``; ``reboot()`` plus a 6 s settle leaves
          ``temp0008`` still there. **Do not treat this as cross-run
          ``/Temp`` protection** — see :doc:`../docs/u64_recovery`.
        * **lwIP / UCI stack state survives.** No REST endpoint restarts
          the firmware, so a UCI STATE-bit wedge needs a physical
          power-cycle.
        * **The firmware does not clear C64 RAM — but the reset that
          follows does clear low memory.** Two separate facts, and only
          the first is about the firmware. ``clear_ram()`` belongs to
          ``MENU_C64_CLEARMEM``, not to the ``MENU_C64_REBOOT`` this
          route dispatches (``c64_subsys.cc``, tag ``1.1.0``:
          ``MENU_C64_CLEARMEM`` at :200-208 with ``clear_ram()`` at
          :206, ``MENU_C64_REBOOT`` at :231-237). But
          ``start_cartridge`` pokes ``$8005 = 0`` and asserts
          ``C64_MODE_RESET`` (``c64.cc``), so the 6510 runs the KERNAL
          reset sequence: **RAMTAS clears ``$0000-$0101`` and
          ``$0200-$03FF``**, the screen init clears ``$0400-$07E7``, and
          BASIC's cold start writes ``$0800`` and rebuilds its pointers.

          So **every harness scratch span below ``$0400`` is wiped** by a
          ``reboot()`` — ``$0000-$0001``, ``$00C6``, ``$0277-$0280``,
          ``$0314-$0315``, ``$0334-$0341``, ``$0360-$036D``,
          ``$03F0-$03F1`` (see :mod:`c64_test_harness.memory_policy`).
          Scratch at ``$C000`` and above is not touched by any of this.
          Do not treat ``reboot()`` as preserving anything below
          ``$0801``.

        What it does to the REU and the Command Interface slot, read from
        firmware source at tag ``1.1.0`` (the C64U; the same shape at
        ``7f6fcb51``, the U64E's v3.15-85) and **unmeasured** on a device:
        ``start_cartridge`` first zeroes ``C64_CARTRIDGE_TYPE``,
        ``C64_REU_ENABLE``, ``C64_SAMPLER_ENABLE`` and
        ``CMD_IF_SLOT_ENABLE`` (``c64.cc:913``), then, when no external
        cartridge holds the bus, calls ``set_cartridge(NULL)``
        (``c64.cc:923-924``), whose ``set_emulation_flags()``
        (``c64.cc:992``) restores them from config. So after a reboot the
        REU and the Command Interface come back as configured, except in
        two cases that leave the enables at 0:

        * **An external cartridge holds the bus** (Cartridge Preference
          *External*, or *Automatic* with a cart present):
          ``ConfigureU64SystemBus()`` reports it and ``set_cartridge`` is
          skipped.
        * **The configured ``.crt`` prohibits them.** ``set_cartridge(NULL)``
          loads the image named by ``CFG_C64_CART_CRT``, and that
          definition's ``prohibit`` mask zeroes the UCI enable again
          (``c64.cc:1062-1068``) or the REU enable (``c64.cc:1056-1061``).

        Line numbers are for ``1.1.0``; ``docs/uci_networking.md`` carries
        the ``7f6fcb51`` equivalents and the full trace. The recorded
        observation that ``enable_uci`` needs a ``reset()`` and a ~3 s
        settle before routines answer still stands, but this path does not
        explain it and its cause is open. An earlier revision of this
        docstring gave the unconditional version as that cause (issue
        #299).

        This docstring used to read "full reboot of the Ultimate device".
        That wording was load-bearing in the wrong direction: it is the
        line a caller reads before reaching for this method to "clear"
        something, and it licensed exactly the reboot-as-hygiene
        assumption the measurement above refutes.
        """
        self._put_no_body("/v1/machine:reboot")

    def pause(self) -> None:
        """PUT /v1/machine:pause — halt the emulated CPU (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:pause")

    def resume(self) -> None:
        """PUT /v1/machine:resume — resume the emulated CPU."""
        self._put_no_body("/v1/machine:resume")

    def poweroff(self, *, confirm_irrecoverable: bool = False) -> None:
        """PUT /v1/machine:poweroff — power off the C64 side (DESTRUCTIVE).

        UNSAFE WITHOUT PHYSICAL ACCESS.  After this call the device drops
        off the network entirely (no ICMP, no TCP, no HTTP) and only a
        manual power-cycle restores it.  Multiple agents have called this
        thinking it was a benign reset and then mis-diagnosed the
        unreachable state as a "hung device" -- producing wasted
        troubleshooting cycles each time.

        For "the device looks stuck, recover it" scenarios, prefer:
            * ``reset()``   — soft 6510 reset, instant
            * ``reboot()``  — C64-level reset, ~8s, recovers REU/DMA
              state (but does NOT clear firmware RAM: config and
              ``/Temp`` survive it)

        Pass ``confirm_irrecoverable=True`` only if you (a) intend to
        leave the device off and (b) have physical access to power-cycle
        it later.  Without that explicit confirmation, this method
        raises ``Ultimate64UnsafeOperationError`` rather than firing the
        request.
        """
        if not confirm_irrecoverable:
            raise Ultimate64UnsafeOperationError(
                "Ultimate64Client.poweroff() requires "
                "confirm_irrecoverable=True. After poweroff, the device "
                "is unreachable until someone physically power-cycles "
                "it -- use reboot() (~8s) or reset() (instant) for "
                "recovery scenarios."
            )
        self._put_no_body("/v1/machine:poweroff")

    def menu_button(self) -> None:
        """PUT /v1/machine:menu_button — press the Ultimate menu button (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:menu_button")

    # ------------------------------------------------------------ streams
    def stream_audio_start(self, destination: str) -> None:
        """PUT /v1/streams/audio:start — start streaming audio to *destination*.

        *destination* is an IP address or hostname, optionally with ``:port``
        suffix.  The device sends 16-bit stereo PCM at ~48 kHz over UDP.
        Default multicast destination is ``239.0.1.65:11001``.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty string")
        self._put_no_body("/v1/streams/audio:start", query={"ip": destination})

    def stream_audio_stop(self) -> None:
        """PUT /v1/streams/audio:stop — stop the audio stream."""
        self._put_no_body("/v1/streams/audio:stop")

    def stream_video_start(self, destination: str) -> None:
        """PUT /v1/streams/video:start — start streaming video to *destination*.

        *destination* is an IP address or hostname, optionally with ``:port``
        suffix.  The device sends video frames over UDP to the given address.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty string")
        self._put_no_body("/v1/streams/video:start", query={"ip": destination})

    def stream_video_stop(self) -> None:
        """PUT /v1/streams/video:stop — stop the video stream."""
        self._put_no_body("/v1/streams/video:stop")

    def stream_debug_start(self, destination: str) -> None:
        """PUT /v1/streams/debug:start — start streaming debug data to *destination*.

        *destination* is an IP address or hostname, optionally with ``:port``
        suffix.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty string")
        self._put_no_body("/v1/streams/debug:start", query={"ip": destination})

    def stream_debug_stop(self) -> None:
        """PUT /v1/streams/debug:stop — stop the debug stream."""
        self._put_no_body("/v1/streams/debug:stop")

    # ----------------------------------------------------------------- memory
    def read_mem(self, address: int, length: int) -> bytes:
        """GET /v1/machine:readmem — read `length` bytes from C64 memory via DMA.

        Returns the raw byte payload. Address is formatted as bare
        uppercase hex (``NNNN``, no ``0x`` prefix) — the only form
        firmware carrying GideonZ/1541ultimate#884 accepts (issue #272).

        :raises Ultimate64ProtocolError: if the device returns a payload
            whose length differs from the requested `length`.  Without
            this check, downstream chunked readers would silently
            produce short / misaligned results.
        """
        # bool subclasses int; True would address $0001, the 6510
        # processor port (#340).
        refuse_bool_address(address)
        if not isinstance(address, int) or address < 0 or address > 0xFFFF:
            raise ValueError(f"address out of range 0..0xFFFF: {address}")
        if not isinstance(length, int) or length <= 0:
            raise ValueError(f"length must be positive, got {length}")
        query = {"address": _wire_hex16(address), "length": "%d" % length}
        _, data = self._request("GET", "/v1/machine:readmem", query=query)
        if len(data) != length:
            raise Ultimate64ProtocolError(
                f"readmem at 0x{address:04X} returned {len(data)} bytes, "
                f"expected {length}"
            )
        return data

    #: Override for the raw-byte threshold above which :meth:`write_mem`
    #: switches from the ``PUT ?data=<hex>`` form to the ``POST`` form.
    #: Its shipped value, 48, is the post-safe grade's threshold and is
    #: **not** applied as a default: an untouched client takes its
    #: threshold from :attr:`capabilities` (128 on leak-prone or unknown
    #: firmware). Precedence, highest first: the ``write_mem_query_threshold=``
    #: constructor kwarg (the supported path); then a poke of this name on
    #: the class, a subclass, or -- after construction -- an instance; then
    #: the capability grade. A poke is applied with a WARNING naming the
    #: kwarg, and unlike the kwarg it does not skip the capability probe, so
    #: ``/Temp`` hygiene still arms. It is clamped to 128 (the firmware's
    #: ``data=`` cap) and, on a device not graded post-safe, a poke below 128
    #: is refused and 128 kept -- see :meth:`_effective_poked_threshold`.
    #: A class poke is process-global, and re-assigning a plain ``48`` still
    #: counts as a poke. To undo one, restore the saved original object
    #: (``orig = Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD`` before poking,
    #: then assign ``orig`` back), or poke under pytest's ``monkeypatch``,
    #: which does that for you. Before issue #249 this attribute was never
    #: read and every poke was a silent no-op.
    WRITE_MEM_QUERY_THRESHOLD: int = _ShippedThreshold(THRESHOLD_POST_SAFE)

    def __setattr__(self, name: str, value: Any) -> None:
        # An instance poke of the uppercase name is honoured (#249): it
        # moves the live threshold, which is the only thing it could mean.
        if name == "WRITE_MEM_QUERY_THRESHOLD":
            # A subclass that assigns this before ``super().__init__()`` has
            # no grade cached yet, so it gets a "refused, keeping 128"
            # WARNING here -- and ``__init__`` then sets the threshold from
            # the grade anyway (48 on a post-safe device), which leaves the
            # uppercase name's read-back stale at 128.  Safe, merely noisy;
            # poke on the class body or after construction instead.
            threshold = self._effective_poked_threshold(value, "instance ")
            object.__setattr__(self, "write_mem_query_threshold", threshold)
            # Store the effective value, not the request, so the uppercase
            # name never reads back a refused or clamped poke (review
            # round 2).  ``write_mem_query_threshold`` stays authoritative.
            object.__setattr__(self, name, threshold)
            return
        object.__setattr__(self, name, value)

    def _effective_poked_threshold(self, value: Any, source: str) -> int:
        """The threshold a poke of ``WRITE_MEM_QUERY_THRESHOLD`` may set.

        Review round 1 of #249.  A poke is a blunt, process-global control,
        so two directions are not honoured:

        * **Above 128** it is clamped to 128: the firmware refuses a
          ``PUT ?data=`` payload over 128 bytes on every grade (the
          transport's ``_PUT_DATA_CAP`` is the same limit).
        * **Below 128 on a device not graded post-safe** it is refused and
          128 kept: there a lower threshold only moves writes onto the POST
          path, which leaves a ``/Temp`` attachment per request (hardware
          rule 8).  The explicit ``write_mem_query_threshold=`` kwarg is the
          deliberate way to force it.

        Reads the non-probing :attr:`cached_capabilities` (#291), never
        the probing :attr:`capabilities` property, so a poke issues no
        HTTP (#343).  An unprobed client counts as not post-safe.
        """
        requested = _validate_poked_threshold(value)
        host = getattr(self, "host", "?")
        if requested > THRESHOLD_POST_RISKY:
            _log.warning(
                "Ultimate64Client(%s): %sWRITE_MEM_QUERY_THRESHOLD = %d clamped "
                "to %d: the firmware refuses a PUT ?data= payload over %d bytes "
                "on every grade. Prefer the write_mem_query_threshold= "
                "constructor kwarg.",
                host, source, requested, THRESHOLD_POST_RISKY, THRESHOLD_POST_RISKY,
            )
            return THRESHOLD_POST_RISKY
        # A subclass poking before super().__init__() has no cache attribute
        # yet (the accessor would raise AttributeError); that reads as
        # "never probed".
        caps = (
            self.cached_capabilities
            if "_capabilities" in self.__dict__
            else None
        )
        if requested < THRESHOLD_POST_RISKY and getattr(
            caps, "writemem_post_safe", None
        ) is not True:
            firmware = getattr(caps, "firmware_version", None) or "unknown"
            _log.warning(
                "Ultimate64Client(%s): %sWRITE_MEM_QUERY_THRESHOLD = %d refused, "
                "keeping %d: this device is not graded post-safe (firmware %s), "
                "so a lower threshold only moves writes onto the POST path, "
                "which leaves a /Temp attachment per request. Pass the "
                "write_mem_query_threshold= constructor kwarg to force it "
                "deliberately.",
                host, source, requested, THRESHOLD_POST_RISKY, firmware,
            )
            return THRESHOLD_POST_RISKY
        _log.warning(
            "Ultimate64Client(%s): honouring %sWRITE_MEM_QUERY_THRESHOLD = %d. "
            "Prefer the write_mem_query_threshold= constructor kwarg.",
            host, source, requested,
        )
        return requested

    def write_mem(self, address: int, data: bytes) -> None:
        """Write bytes to C64 memory via DMA (DESTRUCTIVE).

        Uses one of two wire forms depending on payload size:

        * **Small payloads**
          (``len(data) <= self.write_mem_query_threshold``; 48 on firmware
          carrying the Temp-folder fix, 128 without it — see
          :attr:`capabilities`) —
          ``PUT /v1/machine:writemem?address=NNNN&data=<hex>``.  Kept
          for backwards compatibility with existing callers/mocks.
        * **Large payloads** — ``POST /v1/machine:writemem?address=NNNN``
          with the raw bytes as the request body
          (``Content-Type: application/octet-stream``). Required for
          anything past the device's 128-hex-char cap on the ``data=``
          query param (firmware 3.14 responds
          ``"Maximum length of 128 bytes exceeded. Consider using POST
          method with attachment."``).

        Both forms are functionally equivalent for supported sizes; the
        POST form has no upper bound verified at 2048 bytes.
        """
        # bool subclasses int; True would address $0001, the 6510
        # processor port (#340).
        refuse_bool_address(address)
        if not isinstance(address, int) or address < 0 or address > 0xFFFF:
            raise ValueError(f"address out of range 0..0xFFFF: {address}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        if not data:
            return
        payload = bytes(data)
        if len(payload) <= self.write_mem_query_threshold:
            query = {
                "address": _wire_hex16(address),
                "data": payload.hex().upper(),
            }
            self._request("PUT", "/v1/machine:writemem", query=query)
        else:
            # POST with raw-byte attachment — no data= query, body carries payload.
            self._request(
                "POST",
                "/v1/machine:writemem",
                body=payload,
                content_type="application/octet-stream",
                query={"address": _wire_hex16(address)},
            )

    # ------------------------------------------------------------ keyboard
    #: KERNAL keyboard buffer base address ($0277).
    KEYBUF_ADDR: int = 0x0277
    #: KERNAL keyboard buffer fill-count byte ($00C6).
    KEYBUF_COUNT_ADDR: int = 0x00C6
    #: C64 keyboard buffer hardware capacity (10 bytes).
    KEYBUF_MAX: int = 10

    def send_text(self, text: str, *, finish_with_return: bool = True) -> None:
        """Inject *text* as a sequence of keystrokes into the C64 keyboard buffer.

        Convenience wrapper that PETSCII-encodes *text* and writes the
        bytes into the KERNAL keyboard buffer at ``$0277`` (with the
        fill-count byte at ``$00C6``).  When *finish_with_return* is
        True (the default), a trailing CR (PETSCII ``0x0D``) is
        appended — the canonical pattern for triggering a BASIC command
        such as ``"SYS 864"`` after :meth:`run_prg` lands at READY.

        Two trigger patterns for U64 PRGs:

        1. **Hijack an existing parking JMP** in the PRG — no
           ``send_text`` needed; ``run_prg`` alone fires the entry.
        2. **Type a SYS** — call ``send_text("SYS <trampoline>")`` after
           ``run_prg`` returns BASIC to READY.  This is the canonical
           shape for library-only PRGs that have no native main loop.

        For longer strings the buffer's 10-byte hardware limit is
        respected: the call polls ``$00C6`` and waits for the KERNAL
        scan loop to drain the buffer **to empty** before pushing the
        next chunk.  Topping up a partially-full buffer is deliberately
        avoided: the read-$C6 / write-chunk / write-$C6 sequence is
        three HTTP round-trips ~100 ms apart, and the KERNAL dequeues
        from the front of the buffer at 50/60 Hz — any consumption
        inside that window would land the chunk at a stale offset and
        overstate the count (garbage keystrokes).  Writing only into an
        empty buffer means chunks always start at ``$0277`` offset 0 and
        the ``$00C6`` count write is the single publication step.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        # Local import keeps the encoding module out of the import-time graph.
        from ..encoding.petscii import char_to_petscii

        codes = [char_to_petscii(ch) for ch in text]
        if finish_with_return:
            codes.append(0x0D)
        if not codes:
            return

        remaining = list(codes)
        max_iters = len(remaining) * 4 + 16
        iters = 0
        while remaining:
            iters += 1
            if iters > max_iters:
                raise Ultimate64Error(
                    "send_text: keyboard buffer never drained "
                    f"(still {len(remaining)} keys pending)"
                )
            count_byte = self.read_mem(self.KEYBUF_COUNT_ADDR, 1)
            current = count_byte[0] if count_byte else 0
            if current != 0:
                # Buffer not yet drained — never top up a partially-full
                # buffer (the KERNAL may dequeue between our read of $C6
                # and the two writes, corrupting offset and count).
                continue
            chunk = remaining[:self.KEYBUF_MAX]
            remaining = remaining[self.KEYBUF_MAX:]
            self.write_mem(self.KEYBUF_ADDR, bytes(chunk))
            self.write_mem(self.KEYBUF_COUNT_ADDR, bytes([len(chunk)]))

    # ------------------------------------------------------------ code runners
    def load_prg(self, data: bytes) -> None:
        """POST /v1/runners:load_prg — load a PRG into memory (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).
        """
        self._post_binary("/v1/runners:load_prg", data)

    def gc_temp_folder(
        self,
        *,
        keep: int | None = None,
        ftp_port: int | None = None,
        ftp_username: str | None = None,
        ftp_password: str | None = None,
        timeout: float | None = None,
    ) -> "TempGCResult":
        """Best-effort GC of the device's leaked ``/Temp`` attachments over FTP.

        Every REST call that carries a body leaks a managed attachment
        (``temp0000``, ``temp0001``, ...) into ``/Temp``; unreleased
        firmware never collects them, and enough accumulation wedges
        the REST API and UCI bridge together (see GitHub issue #153,
        ``docs/u64_recovery.md``). This deletes them oldest-first,
        keeping the youngest *keep*.

        Delegates to
        :func:`~c64_test_harness.backends.ultimate64_temp_gc.gc_temp_folder`
        with this client's ``host``. Never raises -- any FTP/network
        failure is captured in the returned result's ``.error``.

        You rarely need to call this: on a leak-prone device the client
        calls it for you once the leak budget is spent, on
        :meth:`close`, and when the device lock is released (see
        :attr:`temp_hygiene_armed`). Call it directly for a manual pass,
        or with a non-default ``keep``/credentials.

        Caller responsibility: this does not acquire a DeviceLock. Call
        it only while already holding the lock for this device.
        """
        from .ultimate64_temp_gc import DEFAULT_FTP_TIMEOUT, gc_temp_folder as _gc_temp_folder
        return _gc_temp_folder(
            self.host,
            port=ftp_port,
            username=ftp_username,
            password=ftp_password,
            keep=keep,
            timeout=timeout if timeout is not None else DEFAULT_FTP_TIMEOUT,
        )

    def run_prg(self, data: bytes, *, fallback_on_404: bool = True) -> None:
        """POST /v1/runners:run_prg — load and RUN a PRG (DESTRUCTIVE).

        **This replaces the running program.**  It is a load-and-run,
        not a call that interleaves with whatever the machine was
        already doing: the firmware resets the machine, loads *data*,
        and starts it.  Anything another lane had running — its state,
        its open UCI sockets, the program it believes it is still
        talking to — is gone, with no error on either side.

        That is what makes an *unlocked* ``run_prg`` destructive rather
        than merely rude, and why it is so badly misread from the other
        end: the displaced lane sees its own protocol answering
        nonsense (in issue #194, ``OPEN_UDP`` returning ``21,UNKNOWN
        COMMAND`` where the same opcode answered correctly a minute
        later, and three runs aborting at *different* points) and
        diagnoses device degradation.  The variable was the neighbour's
        timing, not its own code.  Hold the device lock for the whole
        run — see ``docs/device_locking.md``.

        Firmware 3.14 requires POST (PUT returns 400).

        Failure modes:

        * **Normal failure** (HTTP 4xx with informative body) — raised
          as ``Ultimate64Error`` with the device's error message in
          ``.body``. Caller can retry, fix the PRG, etc.
        * **Stuck-runner state** — device returns the firmware's
          ``"Cannot open file"`` signature (either as the body of a 4xx
          or in a JSON ``errors`` array). The device is alive (HTTP and
          ``/v1/version`` still work) but its runner state machine is
          wedged and refuses new programs. Detect via
          ``ultimate64_helpers.runner_health_check()``; clear via
          ``ultimate64_helpers.recover()`` (soft reset, escalates to
          ``reboot()`` if needed).
        * **HTTP 404 on fw 3.14d** — after certain non-PRG load
          sequences the runner endpoint starts returning 404 until the
          device is rebooted.  When ``fallback_on_404=True`` (the
          default), the call transparently retries by sideloading the
          PRG body via :meth:`write_mem` (using the load address from
          the PRG's first two header bytes, little-endian) and then
          triggering it, matching the real endpoint's load-and-RUN
          semantics:

          * load address ``$0801`` (a canonical BASIC-stub PRG) —
            triggers with :meth:`send_text` (``"RUN\\r"``).  ``SYS 2049``
            would execute the BASIC line-link bytes as 6502 opcodes and
            corrupt the machine; ``RUN`` interprets the stub the way
            the runner endpoint does.
          * any other load address (pure-ML PRG) — triggers with
            :meth:`send_text` (``"SYS <addr>\\r"``).

          A ``logging.warning`` is emitted when the fallback fires,
          naming which trigger path was taken.  Pass
          ``fallback_on_404=False`` to surface the 404 as a plain
          :class:`Ultimate64Error`.
        * **Device unreachable** — raised as ``Ultimate64TimeoutError``.
          Use ``recover()`` first; if that raises
          ``Ultimate64UnreachableError`` the device needs a physical
          power-cycle.

        Do NOT call ``poweroff()`` to clear a stuck runner -- it leaves
        the device unreachable until someone physically power-cycles it.
        ``reboot()`` (via ``recover()``) is the correct escalation.

        **Temp-folder hygiene** (issue #153): this upload is one
        of the calls that leaks a ``/Temp`` attachment on firmware
        without the upstream collector, and it is accounted for at the
        request layer rather than here — see
        :attr:`temp_hygiene_armed`. On such a device the hygiene pass
        runs automatically once the budget is spent, on
        :meth:`close`, and when the device lock is released; on firmware
        that carries the fix nothing arms and no FTP traffic happens.
        """
        try:
            self._post_binary("/v1/runners:run_prg", data)
        except Ultimate64Error as exc:
            if not (fallback_on_404 and exc.status == 404):
                raise
            if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
                raise
            load_addr = data[0] | (data[1] << 8)
            body = bytes(data[2:])
            trigger = "RUN" if load_addr == 0x0801 else f"SYS {load_addr}"
            _log.warning(
                "run_prg got HTTP 404 from /v1/runners:run_prg; "
                "falling back to writemem sideload at $%04X, triggering "
                "with %r (fw 3.14d wedged-runner workaround)",
                load_addr,
                trigger,
            )
            if body:
                # /Temp accounting: this sideload is a *second* attachment
                # on a leak-prone device. The runner POST above already
                # carried the whole body before answering 404, and this
                # write_mem is unchunked, so it POSTs the body again.
                # Nothing here has to say so — the request choke point
                # counts what was actually issued rather than one per
                # verb — but it is worth knowing that a 404 from
                # runners:run_prg is itself a wedge symptom, so the path
                # that costs double fires exactly when the device is
                # closest to the edge.
                self.write_mem(load_addr, body)
            self.send_text(trigger, finish_with_return=True)

    def run_crt(self, data: bytes) -> None:
        """POST /v1/runners:run_crt — start a cartridge image (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).
        """
        self._post_binary("/v1/runners:run_crt", data)

    def sid_play(self, data: bytes, songnr: int = 0) -> None:
        """POST /v1/runners:sidplay — play a .sid tune (DESTRUCTIVE).

        Firmware 3.14 exposes this as POST to ``sidplay`` (no underscore);
        the PUT/``sid_play`` form returns HTTP 404.
        """
        if not isinstance(songnr, int) or songnr < 0:
            raise ValueError(f"songnr must be >= 0, got {songnr}")
        self._post_binary("/v1/runners:sidplay", data, query={"songnr": "%d" % songnr})

    def mod_play(self, data: bytes) -> None:
        """POST /v1/runners:modplay — play a .mod file (DESTRUCTIVE).

        Firmware 3.14 exposes this as POST to ``modplay`` (no underscore);
        the PUT/``mod_play`` form returns HTTP 404.
        """
        self._post_binary("/v1/runners:modplay", data)

    def _put_binary(
        self,
        path: str,
        data: bytes,
        query: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        self._request(
            "PUT",
            path,
            body=bytes(data),
            content_type="application/octet-stream",
            query=query,
        )

    def _post_binary(
        self,
        path: str,
        data: bytes,
        query: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        self._request(
            "POST",
            path,
            body=bytes(data),
            content_type="application/octet-stream",
            query=query,
        )

    # ----------------------------------------------------------------- drives
    def mount_disk(
        self,
        drive: str,
        image: bytes,
        image_type: str,
        mode: str = "readwrite",
    ) -> None:
        """POST /v1/drives/<drive>:mount — upload and mount an image (DESTRUCTIVE).

        `drive` is the slot id ("a", "b" or "softiec"). `image_type` is
        e.g. "d64", "d71", "d81", "g64", "g71". `mode` is "readwrite",
        "readonly", or "unlinked".

        **POST, not PUT.** The firmware registers two different routes on
        this path: ``PUT`` mounts an image *already on the device* and
        takes an ``image`` query argument with no body handler at all,
        while ``POST`` is the upload-and-mount form that accepts a
        multipart or ``application/octet-stream`` body.  S: 3.15's
        ``software/api/route_drives.cc:109`` (``PUT ... NULL,
        ARRAY({{"image", P_REQUIRED}, ...})``) versus ``:141``
        (``POST ... &attachment_writer``).

        This used to PUT the body, which reaches the route that wants a
        query argument and has nowhere to put the bytes -- so it answered
        400 for every body shape tried, multipart and raw alike.  That
        looked like "this firmware cannot accept an upload"; it was the
        wrong verb.  See :meth:`mount_disk_path` for the PUT form.
        """
        if not isinstance(image, (bytes, bytearray)):
            raise TypeError("image must be bytes")
        if not isinstance(image_type, str) or not image_type:
            raise ValueError("image_type must be a non-empty string")
        if mode not in ("readwrite", "readonly", "unlinked"):
            raise ValueError(f"mode must be readwrite/readonly/unlinked, got {mode!r}")
        path = self._drive_slot_path(drive, "mount")

        boundary = "----U64ClientBoundary" + uuid.uuid4().hex
        body = _build_multipart(
            boundary,
            fields={"mode": mode, "type": image_type},
            file_field="file",
            file_name=f"image.{image_type}",
            file_bytes=bytes(image),
        )
        self._request(
            "POST",
            path,
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def mount_disk_path(
        self,
        drive: str,
        image_path: str,
        image_type: str | None = None,
        mode: str = "readwrite",
    ) -> None:
        """PUT /v1/drives/<drive>:mount — mount an image already on the device.

        The counterpart to :meth:`mount_disk`: no bytes cross the wire,
        the device opens a path of its own.  ``image_path`` is a device
        path such as ``/Usb0/games/disk.d64``.

        ``image_type`` may be omitted, in which case the firmware infers
        it from the file extension (S: ``route_drives.cc:97`` -- "Defaults
        to the file extension").

        This is the form that works when an upload is impractical, and
        the one callers were rediscovering by hand as a workaround for
        :meth:`mount_disk` sending its body by the wrong verb.

        :raises ValueError: on a bad drive, an empty path, or a mode
            outside readwrite/readonly/unlinked.
        """
        if not isinstance(image_path, str) or not image_path:
            raise ValueError("image_path must be a non-empty string")
        if mode not in ("readwrite", "readonly", "unlinked"):
            raise ValueError(f"mode must be readwrite/readonly/unlinked, got {mode!r}")
        path = self._drive_slot_path(drive, "mount")
        query: dict[str, Any] = {"image": image_path, "mode": mode}
        if image_type:
            query["type"] = image_type
        self._put_no_body(path, query=query)

    def unmount_disk(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:remove — unmount a drive (DESTRUCTIVE).

        Alias for :meth:`drive_remove_disk`, kept for callers that reach for
        the "unmount" name.

        This used to target ``:unmount``, an endpoint no firmware has ever
        registered — 3.15's ``software/api/route_drives.cc`` exposes mount /
        reset / remove / on / off / unlink / load_rom / set_mode, and
        ``remove`` is the unmount verb. It also percent-encoded a trailing
        colon into the slot, which draws a 400 ("Invalid Drive 'a:'"). Both
        are fixed here; the slot now takes the plain letter.
        """
        self.drive_remove_disk(drive)

    #: Drive slots the firmware accepts in a ``/v1/drives/{drive}:...``
    #: path.  ``softiec`` is the IEC file system; S: 3.15's
    #: ``software/api/route_drives.cc:95``
    #: ``PATH_PARAM_ENUM("drive", "a,b,softiec")``.
    _DRIVE_SLOTS = ("a", "b", "softiec")

    @staticmethod
    def _drive_slot_path(drive: str, action: str) -> str:
        """Build ``/v1/drives/<slot>:<action>``.

        The single place a drive path is constructed.  Every drive
        method routes through here, because the one that did not --
        ``mount_disk`` -- kept the over-encoding bug through a live
        audit that fixed the identical construction in its sibling
        (issue #167).

        The slot is a plain identifier with no trailing colon and no
        percent-encoding: the colon after it is the firmware's verb
        separator, so a slot containing an encoded one draws
        400 "Invalid Drive 'a:'" (verified live 2026-07-28 on fw 3.14,
        again on 3.15).  Callers were long told the trailing colon is
        added for them, so ``"a:"`` and ``"A"`` are accepted and
        normalised here rather than rejected.
        """
        if not isinstance(drive, str):
            raise ValueError(f"drive must be a string, got {type(drive).__name__}")
        slot = drive.strip().rstrip(":").lower()
        if slot not in Ultimate64Client._DRIVE_SLOTS:
            raise ValueError(
                f"drive must be one of {list(Ultimate64Client._DRIVE_SLOTS)}, "
                f"got {drive!r}"
            )
        return f"/v1/drives/{slot}:{action}"

    def drive_on(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:on — power on a drive slot (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "on"))

    def drive_off(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:off — power off a drive slot (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "off"))

    def drive_reset(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:reset — reset a drive slot (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "reset"))

    def drive_remove_disk(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:remove — remove the disk from a drive (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "remove"))

    def drive_unlink(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:unlink — unlink the mounted image (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "unlink"))

    def drive_set_mode(self, drive: str, mode: str) -> None:
        """PUT /v1/drives/<drive>:set_mode?mode=<mode> — set drive mode (DESTRUCTIVE).

        `mode` is one of "1541", "1571", "1581".
        """
        if mode not in ("1541", "1571", "1581"):
            raise ValueError(f"mode must be '1541', '1571', or '1581', got {mode!r}")
        self._put_no_body(self._drive_slot_path(drive, "set_mode"), query={"mode": mode})

    def drive_load_rom(self, drive: str, rom_path_or_data: bytes | bytearray | str) -> None:
        """Load a custom ROM into a drive slot (DESTRUCTIVE).

        If *rom_path_or_data* is a ``bytes``-like object, the ROM is uploaded
        as a multipart body via PUT /v1/drives/<drive>:load_rom (mirrors the
        ``mount_disk`` shape).  If it is a ``str``, it is treated as a
        filename on the device's filesystem and passed via PUT
        /v1/drives/<drive>:load_rom?file=<path>.
        """
        path = self._drive_slot_path(drive, "load_rom")
        if isinstance(rom_path_or_data, (bytes, bytearray)):
            boundary = "----U64ClientBoundary" + uuid.uuid4().hex
            body = _build_multipart(
                boundary,
                fields={},
                file_field="file",
                file_name="drive.rom",
                file_bytes=bytes(rom_path_or_data),
            )
            self._request(
                "PUT",
                path,
                body=body,
                content_type=f"multipart/form-data; boundary={boundary}",
            )
        elif isinstance(rom_path_or_data, str):
            if not rom_path_or_data:
                raise ValueError("rom_path_or_data must be a non-empty string")
            self._put_no_body(path, query={"file": rom_path_or_data})
        else:
            raise TypeError("rom_path_or_data must be bytes or str")

    # ----------------------------------------------------------------- files
    def file_info(self, path: str) -> dict:
        """GET /v1/files/<path>:info — return size/extension info for a file.

        Path segments are URL-encoded individually so embedded slashes
        survive as path delimiters.
        """
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        encoded = "/".join(_encode(seg) for seg in path.lstrip("/").split("/"))
        return self._get_json(f"/v1/files/{encoded}:info")

    def _files_create_path(self, path: str, action: str) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        encoded = "/".join(_encode(seg) for seg in path.lstrip("/").split("/"))
        return f"/v1/files/{encoded}:{action}"

    def create_d64(self, path: str, tracks: int = 35, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_d64 — create an empty .d64 image (DESTRUCTIVE).

        `tracks` must be 35 or 40.
        """
        if tracks not in (35, 40):
            raise ValueError(f"tracks must be 35 or 40, got {tracks!r}")
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_d64"),
            query={"tracks": tracks, "diskname": diskname},
        )

    def create_d71(self, path: str, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_d71 — create an empty .d71 image (DESTRUCTIVE)."""
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_d71"),
            query={"diskname": diskname},
        )

    def create_d81(self, path: str, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_d81 — create an empty .d81 image (DESTRUCTIVE)."""
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_d81"),
            query={"diskname": diskname},
        )

    def create_dnp(self, path: str, tracks: int = 1, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_dnp — create an empty .dnp image (DESTRUCTIVE).

        `tracks` must be 1..255 inclusive.
        """
        if not isinstance(tracks, int) or tracks < 1 or tracks > 255:
            raise ValueError(f"tracks must be 1..255, got {tracks!r}")
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_dnp"),
            query={"tracks": tracks, "diskname": diskname},
        )

    # ------------------------------------------------------------ debug / measure
    def get_debug_register(self) -> int:
        """GET /v1/machine:debugreg — return the byte at $D7FF."""
        _, data = self._request("GET", "/v1/machine:debugreg")
        text = data.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "value" in payload:
            return int(payload["value"], 0) if isinstance(payload["value"], str) else int(payload["value"])
        if isinstance(payload, int):
            return payload
        if text:
            return int(text, 0)
        raise Ultimate64ProtocolError("empty debugreg response")

    def set_debug_register(self, value: int) -> None:
        """PUT /v1/machine:debugreg?value=<value> — write the byte at $D7FF (DESTRUCTIVE).

        `value` must be 0..255 inclusive.
        """
        if not isinstance(value, int) or value < 0 or value > 255:
            raise ValueError(f"value must be 0..255, got {value!r}")
        self._put_no_body("/v1/machine:debugreg", query={"value": value})

    def measure_bus_timing(self) -> bytes:
        """GET /v1/machine:measure — return raw VCD bytes from a bus-timing capture."""
        _, data = self._request("GET", "/v1/machine:measure")
        return data

    # -------------------------------------------------------------- config write
    def set_config_item(self, category: str, item: str, value: Any) -> None:
        """PUT /v1/configs/<category>/<item>?value=<value> — set a single
        config item (DESTRUCTIVE).

        The device expects the new value as a ``value=`` query
        parameter, not as a JSON body.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(item, str) or not item:
            raise ValueError("item must be a non-empty string")
        path = f"/v1/configs/{_encode(category)}/{_encode(item)}"
        self._put_no_body(path, query={"value": value})

    def set_config_items(self, category: str, updates: dict) -> None:
        """Set multiple config items in *category* (DESTRUCTIVE).

        Issues one PUT per item because the device firmware does not
        accept a JSON-object batch body on ``PUT /v1/configs/<category>``
        (it returns HTTP 400 ``"Function none requires parameter value"``).
        Items are applied in dict insertion order; on failure, earlier
        writes are left in place.

        `updates` is a mapping of item name -> new value.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict")
        for item, value in updates.items():
            self.set_config_item(category, item, value)

    def set_config_items_batch(self, updates: dict[str, dict[str, Any]]) -> None:
        """POST /v1/configs — apply many config items in a single request (DESTRUCTIVE).

        `updates` is a mapping of category -> {item: value, ...}.  Sends a
        single JSON body to the device, so all writes are atomic from the
        caller's perspective.  Use :meth:`set_config_items` for the
        per-item PUT fan-out form.
        """
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict")
        for category, items in updates.items():
            if not isinstance(category, str) or not category:
                raise ValueError("category keys must be non-empty strings")
            if not isinstance(items, dict):
                raise TypeError(f"updates[{category!r}] must be a dict")
        body = json.dumps(updates).encode("utf-8")
        self._request("POST", "/v1/configs", body=body, content_type="application/json")

    def save_config_to_flash(self) -> None:
        """PUT /v1/configs:save_to_flash — persist config to flash (DESTRUCTIVE)."""
        self._put_no_body("/v1/configs:save_to_flash")

    def load_config_from_flash(self, category: str | None = None) -> None:
        """PUT /v1/configs:load_from_flash — reload config from flash (DESTRUCTIVE).

        Discards every unsaved in-memory change: config PUTs are volatile
        until ``save_config_to_flash`` (firmware ``route_configs.cc``
        ``load_from_flash``: ``s->read(false); s->at_close_config()`` per
        store, so the reloaded values are re-effectuated too). With
        *category* given, only that category is reloaded via
        ``PUT /v1/configs/<category>:load_from_flash`` (firmware accepts
        a name or pattern; path depth >1 is HTTP 400).
        """
        if category is None:
            self._put_no_body("/v1/configs:load_from_flash")
            return
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        self._put_no_body(f"/v1/configs/{_encode(category)}:load_from_flash")

    def reset_config_to_default(self) -> None:
        """PUT /v1/configs:reset_to_default — reset all config (DESTRUCTIVE).

        **Every** store, ``Ethernet Settings`` / ``Network Settings`` / the
        WiFi store included (firmware ``route_configs.cc``: the global form
        iterates all stores).  The ``Ethernet Settings`` reset drops the
        DHCP lease mid-request (``effectuate_settings`` -> ``dhcp_stop()``
        zeroes the address the request arrived on) and the WiFi reset
        re-effectuates the link a C64U is reached over.  Not the retracted
        "flips a static-addressed device to DHCP and strands it" reading --
        see :data:`~c64_test_harness.backends.ultimate64_baseline.
        BASELINE_NEVER_TOUCH`, retracted reading (1).
        The harness's entry reset (:func:`~c64_test_harness.backends.
        ultimate64_baseline.apply_factory_baseline`) never uses this
        route; prefer :meth:`reset_config_category_to_default`.
        """
        self._put_no_body("/v1/configs:reset_to_default")

    def reset_config_category_to_default(self, category: str) -> list[str]:
        """PUT /v1/configs/<category>:reset_to_default — reset one store
        (DESTRUCTIVE, memory-only).

        The firmware resets every store whose name ``pattern_match``-es
        *category* (``*``/``?`` globs, case-insensitive; a plain name is
        an exact match) — ``ConfigStore::reset()`` then ``effectuate()``
        per store — and answers ``{"reset": [<store names>], "errors":
        []}``.  Flash is not touched: ``load_config_from_flash`` undoes
        this and ``save_config_to_flash`` makes it permanent.

        :returns: the store names the firmware reports as reset.  Empty
            when nothing matched — an absent category is not an error on
            the wire, so callers that need presence check the list.
        :raises Ultimate64ProtocolError: on a non-empty ``errors`` array
            or an unparseable body.
        :raises Ultimate64Error: HTTP 400 when the path depth exceeds 1.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        _, data = self._request(
            "PUT", f"/v1/configs/{_encode(category)}:reset_to_default"
        )
        payload = self._parse_json(data) if data else {}
        if not isinstance(payload, dict):
            raise Ultimate64ProtocolError(
                f"expected object from reset_to_default of {category!r}, "
                f"got {type(payload).__name__}"
            )
        errors = payload.get("errors")
        if errors:
            raise Ultimate64ProtocolError(
                f"reset_to_default of {category!r}: device reported errors {errors!r}"
            )
        names = payload.get("reset", [])
        if not isinstance(names, list):
            raise Ultimate64ProtocolError(
                f"reset_to_default of {category!r}: 'reset' is not a list: {names!r}"
            )
        return [str(n) for n in names]


def _build_multipart(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
) -> bytes:
    """Build an RFC 2388 multipart/form-data body.

    Order: simple fields first, file last. Line endings are CRLF.
    """
    crlf = b"\r\n"
    out = bytearray()
    b = boundary.encode("ascii")
    for name, value in fields.items():
        out += b"--" + b + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode("utf-8") + crlf
        out += crlf
        out += value.encode("utf-8") + crlf
    out += b"--" + b + crlf
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"'
        .encode("utf-8")
        + crlf
    )
    out += b"Content-Type: application/octet-stream" + crlf
    out += crlf
    out += file_bytes + crlf
    out += b"--" + b + b"--" + crlf
    return bytes(out)
