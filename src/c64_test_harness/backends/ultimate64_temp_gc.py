"""FTP-based garbage collection for the Ultimate's leaked ``/Temp`` attachments.

Every Ultimate REST call that carries a body (``writemem`` POST,
``run_prg``, ``load_prg``, ...) lands as a managed attachment
(``temp0000``, ``temp0001``, ...) in the device's ``/Temp`` folder;
firmware without the #686 cleanup never collects them. Enough of them
wedge the REST API and the C64-facing UCI bridge together by **crashing
the device firmware**: the C64 FPGA keeps running, while the firmware
stops answering the network and stops responding to the physical menu
button on the case. Only a physical power-cycle recovers.

The owner's account (2026-09-15) is that the attachments fill ``/Temp``
and a full ``/Temp`` crashes unpatched firmware. **No count is claimed**:
nobody knows how many uploads an unpatched device survives, and the earlier
guessed wedge count is retired. The RAM disk's size is known but is not a
count of uploads either: ``/Temp`` is a 16 MiB RAM disk -- ``ramdisk.cc``
sizes it as ``__ram_disk_limit - __ram_disk_start``, and the
"3 * 1024 * 1024" comment beside that computation is stale (issue #261).
For the U64E's 3.14d firmware that is
``0x2000000``-``0x3000000`` in ``software/nios_appl_bsp/linker.x`` at
``v3.14d`` (:389-390): the shipped U64E image is the nios2 build, and
``target/u64/nios2/ultimate/Makefile`` sets ``BSP = $(PATH_SW)/nios_appl_bsp``
(:11), links with ``LINK = $(BSP)/linker.x`` (:236) and builds
``ramdisk.cc`` (:168) -- issue #316. For the C64U it is
``0x02000000``-``0x03000000`` in ``target/u64ii/riscv/ultimate/linker.x``
at ``1.1.0``.
**Why a full /Temp crashes the firmware** is not claimed either: the pre-fix ``attachment_writer`` created
``/Temp/temp%04x`` from a static counter and never deleted the files, but
``TempfileWriter``'s destructor freed both the ``strdup``'d filenames and
the buffers *before* the fix too (``4c40e9db^:software/api/attachment_writer.h``
:53-60), so a per-request heap-accumulation story is refuted at source
level. Heap fragmentation, per-entry allocation in directory traversal
and FileManager bookkeeping growth are all live candidates and none has
been run down. Name no cause.

See ``docs/u64_recovery.md`` for the wedge-tier writeup and GitHub issue
#153 for the FTP-based mitigation this module implements.

This is shared 1541ultimate firmware behaviour, not U64E-specific: it
affects any generation whose firmware predates #686 (it was verified on
the U64E while it ran 3.14d; the C64U on 1.1.0 still qualifies). The
module itself is host-generic (no generation branching) and has been
verified live end-to-end (leak via ``run_prg`` + FTP GC trims to the
keep-count) on both: originally on the U64E, and on the C64U at
10.53.21.158 via ``tests/test_temp_gc_live.py`` (2026-08-21) — anonymous
FTP against ``/Temp`` worked with the same defaults as the U64E.

Upstream root cause and fix: GideonZ/1541ultimate#686 (auto-cleanup of
managed ``/Temp`` files, oldest-first, keep youngest 10). The merge is an
ancestor of the ``v3.15`` tag, so every Ultimate-line 3.15 build has it;
on the bench U64E (fork build 7f6fcb51) this module finds nothing to delete
(measured 2026-09-02: 0 managed files before and after 15 ``run_prg``
uploads with the GC off). It still matters on the C64 Ultimate, whose
1.1.0 firmware predates the fix; ``u64_capabilities.writemem_post_safe``
is the per-device switch.

This module only reports that FTP may need enabling; it writes no config.
The harness's one automatic enable of ``Network Settings > FTP File
Service`` lives in ``Ultimate64Client._run_temp_hygiene``, for a client
that leaked, and is the sanctioned exception to
``ultimate64_baseline.BASELINE_NEVER_TOUCH``, whose contract covers the
entry-baseline reset only (owner decision on #263).

:func:`gc_temp_folder` is deliberately *never raising*: every FTP or
network failure is caught and reported via :class:`TempGCResult.error`
so a hygiene pass can never fail a test run. Callers that hold the
device's :class:`~c64_test_harness.backends.device_lock.DeviceLock` (as
:meth:`~c64_test_harness.backends.ultimate64_client.Ultimate64Client.run_prg`
does implicitly via its caller) should call this while still holding
that lock -- this module does not acquire one itself.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import weakref
from dataclasses import dataclass, field
from ftplib import FTP, all_errors as _FTP_ALL_ERRORS
from typing import Any, Callable, NamedTuple

from .device_lock import DEFAULT_DEVICE_PORT, normalize_device_host

_log = logging.getLogger(__name__)

#: Set to any non-empty, non-"0"/"false" value to enable the automatic
#: hygiene pass inside Ultimate64Client.run_prg. Off by default so the
#: unit-test suite (which exercises run_prg against fake hosts) never
#: makes a real network connection -- see AUTO_GC_ENV usage in
#: ultimate64_client.py.
AUTO_GC_ENV = "U64_AUTO_TEMP_GC"

#: Override the default keep-count (see DEFAULT_KEEP).
KEEP_ENV = "U64_TEMP_GC_KEEP"

#: Override the leak budget -- how many attachment-creating requests may go
#: out to one device between hygiene passes, counted across every client of
#: that host in the process (see DEFAULT_LEAK_BUDGET and TempLedger, #295).
BUDGET_ENV = "U64_TEMP_GC_BUDGET"

#: Set to a falsy value to opt out of the *refusal*: by default, once a
#: leak-prone device's hygiene pass cannot run at all, the client stops
#: issuing further attachment-creating requests rather than walking the
#: device towards the wedge. Setting this to "0"/"false"/"no" downgrades
#: that to a warning.
REQUIRED_ENV = "U64_TEMP_GC_REQUIRED"

#: Override FTP credentials. Bench devices run anonymous FTP; a device
#: with credentials configured needs these set.
FTP_USER_ENV = "U64_TEMP_GC_FTP_USER"
FTP_PASSWORD_ENV = "U64_TEMP_GC_FTP_PASSWORD"

#: The firmware's managed-attachment naming (see 1541ultimate#686 and
#: the c64-https tools/uci/_temp_gc.py workaround this supersedes). The
#: counter is HEX, not decimal -- ``temp0009`` is followed by
#: ``temp000A`` -- so the suffix must accept a-f/A-F, not just digits
#: (issue #153 correction; a decimal-only pattern silently leaves every
#: lettered name uncollected). Deliberately narrow -- user files and named
#: uploads that also live in /Temp must never match. Images uploaded
#: unnamed (harness ``mount_disk`` since #427) do match; a mounted one is
#: protected by the ``GET /v1/drives`` exclusion, not by this pattern.
_MANAGED_ATTACHMENT_RE = re.compile(r"^temp([0-9a-fA-F]+)$")

#: How many of the youngest managed attachments a sweep leaves in place.
#:
#: **1**, from the firmware source (issue #511; 1541ultimate tag ``1.1.0``,
#: the C64U's firmware). No uploaded file is held open once its request has
#: returned. ``TempfileWriter::collect`` closes the file on ``eDataEnd`` /
#: ``eTerminate`` *before* it calls the route handler
#: (``software/api/attachment_writer.h``). Every handler then reads the file
#: synchronously and closes it before it answers:
#:
#: * the runners: ``route_runners.cc`` -> ``FileTypePRG::start_prg``
#:   (``filetype_prg.cc:198-202``) -> ``C64_DMA_LOAD``, which does
#:   ``fopen`` / ``dma_load`` / ``fclose`` (``c64_subsys.cc:367-375``);
#:   ``load_crt`` and ``FileTypeSID::play_file`` also load and return;
#: * ``machine:writemem`` (``route_machine.cc:125-160``, ``load_file``);
#: * ``configs`` (``route_configs.cc``, ``buffer_file``);
#: * ``drives:load_rom``: ``C1541::load_dos_from_file`` (``load_file``).
#:
#: The youngest file is in use only **while its own request is still
#: streaming**: ``collect`` creates it with ``FA_CREATE_ALWAYS`` on
#: ``eDataStart``, from a counter that only goes up, so an in-flight upload
#: is always the youngest managed name. Keeping one covers a single
#: concurrent upload the ``DeviceLock`` cannot see (another thread of this
#: process, or an unlocked neighbour). The one file held open *after* its
#: request is a **mounted disk image**. ``C1541::executeCommand`` opens it
#: ``FA_READ | FA_WRITE`` and keeps it as ``mount_file`` until the next
#: mount, ``remove`` or ``unlink`` on that drive (``c1541.cc`` around
#: 1020-1030 and ``remove_disk`` around 420-430). That is at most one per
#: drive, and it need not be the youngest. The ``GET /v1/drives`` exclusion
#: in :func:`gc_temp_folder` (#418) covers it, not this count. So the
#: owner's "only the most recent file gets a lock" (2026-09-28) holds for an
#: in-flight upload. It does not hold for a mounted image.
DEFAULT_KEEP = 1

#: How many attachment-creating requests may go out to one device before
#: the next one triggers a hygiene pass: **1** (issue #511). Once one
#: attachment is pending, every further attachment-creating request sweeps
#: first. With :data:`DEFAULT_KEEP` = 1 that holds a device's resident
#: managed files at **2**: the kept youngest plus the one just sent, not
#: counting mounted images.
#:
#: The source (see :data:`DEFAULT_KEEP`) gives no reason to let more pile
#: up. A file nobody holds is garbage the moment its request returns. And
#: nobody knows how many a device survives. The owner's account
#: (2026-09-15) is that attachments fill ``/Temp`` and a full ``/Temp``
#: crashes the firmware. The earlier guessed wedge count is retired and none
#: is kept here. The RAM disk is 16 MiB: ``ramdisk.cc`` computes
#: ``__ram_disk_limit - __ram_disk_start``, and the "3 * 1024 * 1024"
#: comment beside it is stale (issue #261). But that size is not a count of
#: uploads either. Do not try to establish one experimentally: the
#: experiment is "upload until the firmware crashes", on hardware nobody can
#: power-cycle remotely.
#:
#: **Cost:** each upload after the first in a hold is preceded by one FTP
#: session (connect, login, ``CWD /Temp``, ``NLST``, ``QUIT``). From the
#: third upload on, that session also sends one ``DELE`` and one bodyless
#: ``GET /v1/drives`` (the mounted-image probe, sent only when something
#: would be deleted). None of it costs a ``/Temp`` attachment. The first
#: upload after the process takes the device is preceded by the handover
#: sweep instead (:meth:`TempLedger.handover_sweep_due`), so every upload
#: to a leak-prone device follows a sweep.
#:
#: **A reservation larger than the budget still goes through whole** when
#: nothing is pending (``Ultimate64Client._before_temp_attachment``), so
#: ``liveness_probe``'s two POSTs (rule 3a) are not made impossible. They
#: follow a sweep, and the next attachment-creating request sweeps them.
#: Resident managed files peak at 3 during a probe.
#:
#: The unit is *attachments*, counted at the request layer: a body-carrying
#: ``POST``. ``transport.write_memory`` splits large writes into PUTs on any
#: grade that is not post-safe (#294), so UCI routines and payloads cost
#: nothing on the C64U. A direct ``client.write_mem`` above the threshold,
#: the runner verbs and the multipart mount/ROM uploads still POST; see
#: ``docs/u64_recovery.md``.
#:
#: Override with :data:`BUDGET_ENV` or the client's ``temp_gc_budget=``.
DEFAULT_LEAK_BUDGET = 1

#: The device's REST port (``Ultimate64Client``'s ``port`` default). A
#: ``host:80`` spelling names the same device as a bare ``host``, so
#: :func:`temp_ledger_key` folds that one port and keeps every other.
#: ``DeviceLock`` keys ``gw:8080`` and ``gw:8081`` apart, so a ledger that
#: folded all ports would let a failed pass against one device refuse
#: requests to a different one behind the same name (#434). Since #434 both
#: sides read this one constant, through the one shared normaliser, so the
#: two cannot drift apart.
DEFAULT_REST_PORT = DEFAULT_DEVICE_PORT

DEFAULT_FTP_PORT = 21
DEFAULT_FTP_TIMEOUT = 10.0
DEFAULT_FTP_USER = "anonymous"
DEFAULT_FTP_PASSWORD = "anonymous@"

#: Socket timeout for :func:`_default_mounted_probe`'s bodyless
#: ``GET /v1/drives`` (#418).
#:
#: **This governs the built-in probe only.** The production path is
#: :meth:`~c64_test_harness.backends.ultimate64_client.Ultimate64Client.gc_temp_folder`,
#: which passes its own ``list_drives`` and so uses the *client's* timeout
#: (10 s by default). An earlier revision of this comment claimed the
#: constant kept a slow device from delaying the hygiene pass; it does not
#: hold on that path, and the probe is issued inside the open FTP session
#: between ``nlst()`` and the first ``delete()``, so the control connection
#: idles for whichever timeout is in force (#418 review, finding 3).
DEFAULT_DRIVES_PROBE_TIMEOUT = 5.0

__all__ = [
    "TempGCResult",
    "gc_temp_folder",
    "DEFAULT_DRIVES_PROBE_TIMEOUT",
    "auto_gc_enabled",
    "auto_gc_override",
    "hygiene_required",
    "leak_budget",
    "TempLedger",
    "TempReservation",
    "temp_ledger_for",
    "temp_ledger_key",
    "DEFAULT_REST_PORT",
    "AUTO_GC_ENV",
    "KEEP_ENV",
    "BUDGET_ENV",
    "REQUIRED_ENV",
    "FTP_USER_ENV",
    "FTP_PASSWORD_ENV",
    "DEFAULT_KEEP",
    "DEFAULT_LEAK_BUDGET",
]


@dataclass
class TempGCResult:
    """Outcome of one :func:`gc_temp_folder` call.

    Always returned, never raised -- see the module docstring.
    """

    host: str
    skipped: bool = False
    deleted: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    error: str | None = None
    #: Managed names left alone because a drive has them mounted (#418).
    #: Disjoint from ``kept``, which is the keep-count's own survivors.
    mounted_excluded: list[str] = field(default_factory=list)
    #: Why the mounted-image listing could not be read, if it could not.
    #: When it is set, the sweep deleted nothing and :attr:`error` is set
    #: too, so the pass has failed (#513 review, finding 4).
    mounted_probe_error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the pass ran without a failure (skips still count as ok).

        An unreadable drives listing is a failure: the sweep then deletes
        nothing and sets :attr:`error` (#513 review, finding 4).
        """
        return self.error is None


def _truthy_env(name: str) -> bool:
    val = os.environ.get(name)
    return bool(val) and val.strip().lower() not in ("0", "false", "no")


def _int_env(name: str, default: int) -> int:
    val = os.environ.get(name)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        _log.warning("%s=%r is not an int; using default %d", name, val, default)
        return default


def auto_gc_enabled() -> bool:
    """Whether ``U64_AUTO_TEMP_GC`` requests the hygiene pass.

    Kept for backwards compatibility; :func:`auto_gc_override` is the
    tri-state form the client uses (it has to tell "unset" from
    "explicitly off").
    """
    return _truthy_env(AUTO_GC_ENV)


def auto_gc_override() -> bool | None:
    """``U64_AUTO_TEMP_GC`` as a tri-state.

    ``True`` force the hygiene pass on for any device, ``False`` force it
    off, ``None`` when the variable is unset -- in which case the client
    decides from the device's firmware capabilities.
    """
    if AUTO_GC_ENV not in os.environ:
        return None
    return _truthy_env(AUTO_GC_ENV)


def hygiene_required() -> bool:
    """Whether an unrunnable hygiene pass should block further uploads.

    Default ``True``; :data:`REQUIRED_ENV` set to a falsy value opts out.
    """
    if REQUIRED_ENV not in os.environ:
        return True
    return _truthy_env(REQUIRED_ENV)


def leak_budget(default: int = DEFAULT_LEAK_BUDGET) -> int:
    """The leak budget a client checks the device's count against,
    honouring :data:`BUDGET_ENV`.

    The *count* is per device (:class:`TempLedger`, issue #295); each client
    compares it with its own budget, so clients built with different budgets
    against one device each apply their own threshold to the shared count.
    """
    value = _int_env(BUDGET_ENV, default)
    return value if value > 0 else default


# --------------------------------------------------------------------------- #
# Per-device accounting (#295)                                                #
# --------------------------------------------------------------------------- #

def temp_ledger_key(host: str) -> str:
    """Normalise a client's host string to the key its device's ledger uses.

    **This delegates to**
    :func:`~c64_test_harness.backends.device_lock.normalize_device_host`
    and adds nothing -- the ledger and the ``DeviceLock`` key their state
    through one normaliser, deliberately
    (#434). They key per device for the same reason, so a spelling that
    reaches one lockfile must reach one ledger: if they disagreed, a lane
    could hold the lock under one spelling while another spelling of the
    same device spent its own ``/Temp`` budget on the same hardware.

    Folds surrounding whitespace, case, an ``http://``/``https://`` scheme,
    a trailing path, IPv6 brackets, a trailing dot, and the textual forms of
    one IP address. Keeps a non-default port -- ``gw:8080`` and ``gw:8081``
    are two devices, in the lock and in the ledger alike. Does **not** fold
    a name with the address it resolves to (no DNS in the keying path)
    unless both are listed for one device in the offline alias map, which
    folds every listed spelling, ethernet and WiFi addresses included, into
    ``uid-<unique_id>`` (#519); see that function for the rules and why.
    """
    return normalize_device_host(host)


class TempLedger:
    """``/Temp`` accounting for one device, shared by every client in the process.

    The wedge accumulates per device, so the count, the refusal state and the
    one FTP-enable attempt live here rather than on ``Ultimate64Client``
    (issue #295). Before this, a fresh client per upload started a fresh
    budget and two clients against one device spent a budget each.

    Everything is guarded by :attr:`lock`, an ``RLock``: a client holds it
    across a budget check and the count that follows, and across a hygiene
    pass, so concurrent clients of one device take turns rather than both
    sweeping, and the count never rises above the budget between them.

    **Across processes: a sweep at every handover, failing closed** (#511,
    replacing the accept-and-document outcome of #433). Each process keeps
    its own ledger, so the refusal a failed pass sets (:attr:`blocked`)
    lives in process memory. Before #511 a new process against a device
    whose FTP sweep was failing therefore spent a fresh budget before it
    found that out. Now a ledger records the ``DeviceLock`` acquire epoch of
    its last successful sweep (:attr:`swept_epoch`). A ledger that has never
    swept, or whose process has taken the device's lock since, is
    :meth:`handover_sweep_due`. An armed client then sweeps before its first
    attachment-creating request. If FTP refuses, it makes this ledger's one
    FTP-enable attempt (:attr:`ftp_enable_attempted`; owner decision
    2026-09-28) and retries. If the sweep still fails, the request is
    refused before anything is sent. So no process spends anything on a device it
    could not clean first. That includes each test of a
    ``run_u64_parallel_locked.py`` run, which takes and releases the lock
    per test. The lock-release drain
    (:meth:`drain_on_lock_release`) still hands the device on clean. The
    residual is two processes uploading concurrently without the lock (it is
    advisory; ``docs/device_locking.md``). There the bound is
    :data:`DEFAULT_LEAK_BUDGET` per process after each one's own sweep.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.lock = threading.RLock()
        #: Attachments counted on this device since the last successful sweep.
        self.pending = 0
        #: Bumped by every successful sweep; a client's own share is valid
        #: only for the generation it was counted in.
        self.generation = 0
        #: The reason string once a leaking client's hygiene pass has failed;
        #: ``None`` again after any successful sweep.
        self.blocked: str | None = None
        #: One attempt per device per process at enabling FTP File Service.
        self.ftp_enable_attempted = False
        #: Whether an **armed** client counted any of :attr:`pending`. Lets a
        #: lock release sweep the device after every client that leaked has
        #: been garbage-collected, without sweeping for a disarmed or
        #: post-safe client's attachments.
        self.armed_pending = False
        #: The host string of the most recently attached client, for that
        #: orphaned sweep.
        self.host: str | None = None
        #: REST port and ``X-Password`` of the most recently attached
        #: client, so the orphaned sweep can still read the drives listing
        #: when no client object survives to do it (#418 review, finding 2).
        self.probe_port: int | None = None
        self.probe_password: str | None = None
        #: Attachments reserved (counted) whose request has not returned yet.
        #: A sweep cannot collect what is still being sent, so :meth:`collected`
        #: carries these into the new generation instead of zeroing them.
        self.in_flight = 0
        #: How many of :attr:`in_flight` an **armed** client reserved, so the
        #: carry-over can restore :attr:`armed_pending` truthfully.
        self.in_flight_armed = 0
        #: The ``DeviceLock`` acquire epoch at the last successful sweep
        #: (:func:`~c64_test_harness.backends.device_lock.acquire_epoch`),
        #: or ``None`` if this process has never swept the device. See
        #: :meth:`handover_sweep_due` (#511).
        self.swept_epoch: int | None = None
        self._clients: weakref.WeakSet = weakref.WeakSet()

    def attach(self, client: object) -> None:
        """Remember *client* weakly, so a lock release can pick a drainer."""
        with self.lock:
            self._clients.add(client)
            self.host = getattr(client, "host", None) or self.host
            self.probe_port = getattr(client, "port", None) or self.probe_port
            self.probe_password = getattr(client, "password", None) or self.probe_password

    def clients(self) -> list:
        with self.lock:
            return list(self._clients)

    def mounted_probe(self) -> "Callable[[], Any] | None":
        """A drives-listing probe for the clientless sweep (#418, finding 2).

        Prefers a live client's ``list_drives``, which carries that
        client's port, password and timeout. Falls back to
        :func:`_default_mounted_probe` rebuilt from the port and password
        the last attached client registered -- the orphaned-sweep case,
        where every client has been garbage-collected. ``None`` when no
        host is known, which leaves :func:`gc_temp_folder` on its own
        default.
        """
        with self.lock:
            for client in self.clients():
                probe = getattr(client, "list_drives", None)
                if callable(probe):
                    return probe
            host, port, password = self.host, self.probe_port, self.probe_password
        if not host:
            return None
        return lambda: _default_mounted_probe(host, port=port, password=password)

    def current_epoch(self) -> int:
        """This process's acquire epoch for the device (``0`` without a lock
        module, or before the first acquire)."""
        try:
            from .device_lock import acquire_epoch
        except ImportError:  # pragma: no cover - only without fcntl
            return 0
        return acquire_epoch(self.key)

    def handover_sweep_due(self) -> bool:
        """Whether ``/Temp`` must be swept before the next attachment (#511).

        True for a ledger that has never swept the device (a new process,
        with or without the lock) and after every outermost ``DeviceLock``
        acquire since the last successful sweep: the device queue has
        advanced, so what other lanes left in ``/Temp`` is unknown. A
        successful sweep of any kind (handover, budget, drain) clears it
        for the current epoch.
        """
        with self.lock:
            return self.swept_epoch != self.current_epoch()

    def collected(self) -> None:
        """A sweep succeeded: only still-in-flight reservations stay pending.

        **Not a reset to zero.** A sweep that lands between another client's
        reservation and its send cannot have collected that attachment --
        the request has not finished sending -- so zeroing here would leave
        the count reading 0 while the device holds one. In-flight
        reservations are therefore carried into the new generation, and
        :meth:`end_reservation` refunds whatever of them was never sent.
        """
        with self.lock:
            self.pending = self.in_flight
            self.blocked = None
            self.swept_epoch = self.current_epoch()
            self.armed_pending = self.in_flight_armed > 0
            self.generation += 1

    def begin_reservation(self, count: int, *, armed: bool) -> None:
        """Note *count* counted attachments whose request has not returned."""
        with self.lock:
            self.in_flight += count
            if armed:
                self.in_flight_armed += count

    def end_reservation(self, reservation: "TempReservation", *, sent: int) -> int:
        """End *reservation*: it is no longer in flight; refund what was not sent.

        Safe across a sweep: :meth:`collected` carried the whole reservation
        into the new generation, so the unsent part is still counted there
        and is still owed back.

        :returns: the refunded (unsent) count, for the caller to take off its
            own share too.
        """
        with self.lock:
            count = reservation.count
            unsent = max(0, min(count, count - sent))
            self.in_flight = max(0, self.in_flight - count)
            if reservation.armed:
                self.in_flight_armed = max(0, self.in_flight_armed - count)
            if unsent:
                self.pending = max(0, self.pending - unsent)
            return unsent

    def drain_on_lock_release(self, reason: str = "device lock release") -> bool:
        """Release callback: **one** drain for this device, however many clients.

        Registered once per host with
        :func:`~c64_test_harness.backends.device_lock.register_release_callback`
        in place of one registration per client, which made N clients open N
        FTP sessions on one release. Clients are tried in order of how much
        they have to say about the device: armed with a leak of their own,
        armed, then leaked but not yet armed (whose drain re-probes first),
        then the rest. The first client whose drain attempts a pass or sweep
        ends the loop. Never raises.

        **When no live client attempts one** but armed clients counted
        attachments that are still pending (they were garbage-collected
        before the release), the ledger sweeps the device itself with the
        default FTP settings. A failure writes no config: no client is left
        to write it for. It logs a WARNING and blocks later
        attachment-creating requests to the device until a sweep succeeds,
        as a leaking client's failed pass would.
        """
        with self.lock:
            candidates = []
            for client in self.clients():
                try:
                    armed = bool(client.temp_hygiene_armed)
                    own = client._own_pending_temp_attachments() > 0
                except Exception:  # noqa: BLE001 - a release must never fail
                    continue
                rank = 0 if armed and own else 1 if armed else 2 if own else 3
                candidates.append((rank, len(candidates), client))
            for _, _, client in sorted(candidates, key=lambda item: item[:2]):
                try:
                    if client._drain_on_lock_release(reason=reason):
                        return True
                except Exception as exc:  # noqa: BLE001 - a release must never fail
                    _log.debug(
                        "U64 /Temp release drain on %s raised (%s: %s); ignored",
                        self.key, type(exc).__name__, exc,
                    )
            if not (self.pending > 0 and self.armed_pending and self.host):
                return False
            try:
                result = gc_temp_folder(
                    self.host,
                    keep=sweep_keep(self),
                    mounted_probe=self.mounted_probe(),
                )
            except Exception as exc:  # noqa: BLE001 - a release must never fail
                result = TempGCResult(host=self.host, error=f"{type(exc).__name__}: {exc}")
            if result.ok:
                self.collected()
                return True
            self.blocked = str(result.error or "unknown FTP failure")
            _log.warning(
                "U64 /Temp release drain on %s: no live client was left to drain, "
                "and sweeping the %d uncollected attachment(s) it counted failed "
                "(%s). No config was written. Later attachment-creating requests "
                "to this device are refused until a sweep succeeds; enable FTP "
                "File Service on the device or power-cycle it. See "
                "docs/u64_recovery.md.",
                self.host, self.pending, self.blocked,
            )
            return True


class TempReservation(NamedTuple):
    """One client's counted-but-not-yet-sent attachment reservation (#295).

    Returned by ``Ultimate64Client._reserve_temp_attachments`` and handed
    back to :meth:`TempLedger.end_reservation` when the request returns.
    Carrying ``armed`` and ``count`` on the token (rather than re-reading
    them at the end) keeps the in-flight accounting symmetric even if the
    client's grade is re-probed mid-request.

    ``pending_before`` is the device's count as it stood **inside the
    ledger lock** at reservation time -- the only non-racy value to log.
    """

    generation: int
    pending_before: int
    armed: bool
    count: int


_TEMP_LEDGERS: dict[str, TempLedger] = {}
_TEMP_LEDGERS_GUARD = threading.Lock()


def temp_ledger_for(host: str) -> TempLedger:
    """The process-wide ledger for *host*'s device (created on first use)."""
    key = temp_ledger_key(host)
    with _TEMP_LEDGERS_GUARD:
        ledger = _TEMP_LEDGERS.get(key)
        if ledger is None:
            ledger = _TEMP_LEDGERS[key] = TempLedger(key)
        return ledger


def _reset_temp_ledgers() -> None:
    """Forget every device's accounting (tests only).

    Clients that already hold a ledger keep it; clients built afterwards
    start from a fresh one.
    """
    with _TEMP_LEDGERS_GUARD:
        _TEMP_LEDGERS.clear()


def lock_held_for(host: str) -> bool:
    """Whether this process holds *host*'s ``DeviceLock`` (any lock dir).

    The ``/Temp`` handover gate (#511) sweeps, and may write config, only
    for a process that is in the device's queue. That means one holding the
    lock: supervisor ruling on the #513 review, following the owner's "when
    the device queue advances to the next user" and CLAUDE.md rule 4.
    ``False`` without ``device_lock`` (no fcntl), so the gate refuses.
    """
    try:
        from .device_lock import held_by_this_process_in_any_dir
    except ImportError:  # pragma: no cover - only without fcntl
        return False
    return held_by_this_process_in_any_dir(host)


def sweep_keep(ledger: "TempLedger") -> int:
    """The keep-count for a sweep of *ledger*'s device (#513 review, finding 1).

    At least ``DEFAULT_KEEP`` (or ``$U64_TEMP_GC_KEEP``) plus one per
    attachment this process still has **in flight**. Those are the youngest
    names on the device (the firmware's counter only goes up), and one still
    streaming is an open file. At 1.1.0, FatFS is built with
    ``FF_FS_LOCK 0`` (``software/chan_fat/full/ffconf.h:265``), and
    ``FileManager::delete_file_impl`` (``filemanager.cc:508``) does not check
    for open files, so an FTP ``DELE`` of an open file is not refused (read
    from source). Call with ``ledger.lock`` held so the in-flight count cannot
    move under the sweep.
    """
    base = _int_env(KEEP_ENV, DEFAULT_KEEP)
    return max(base, DEFAULT_KEEP + max(0, ledger.in_flight))


def _split_by_keep(names: list[str], keep: int) -> tuple[list[str], list[str]]:
    """Split *names* (oldest-first) into (to_delete, to_keep) for *keep*."""
    if keep <= 0:
        return list(names), []
    if keep >= len(names):
        return [], list(names)
    return names[:-keep], names[-keep:]


def _managed_names_in(payload: Any) -> set[str]:
    """Every managed ``temp%04x`` **basename** mentioned anywhere in *payload*.

    *payload* is a parsed ``GET /v1/drives`` document. This walks the
    whole structure rather than reading the two keys the firmware happens
    to use today (``image_file``/``image_path`` in ``route_drives.cc``'s
    ``drive_info``), and compares basenames rather than full paths, for
    one reason: the failure modes are not symmetric. Over-matching costs
    a deferred deletion -- the file is swept on the next pass once it is
    unmounted. Under-matching deletes the backing file of a mounted image
    (#418). A mounted name also appears at different depths across
    generations: measured on the U64E (fw 3.15 bce4535e, 2026-09-15,
    n=1) as ``/Temp/cache/upload/temp0082``, while the C64U's 1.1.0
    reports it at ``/Temp/temp%04x`` (measured 2026-10-07, n=1, #427:
    ``/Temp/temp0003``), which is the one the top-level sweep actually
    lists.
    """
    found: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            basename = current.replace("\\", "/").rsplit("/", 1)[-1]
            if _MANAGED_ATTACHMENT_RE.match(basename):
                found.add(basename)
    return found


def _default_mounted_probe(
    host: str,
    timeout: float = DEFAULT_DRIVES_PROBE_TIMEOUT,
    port: int | None = None,
    password: str | None = None,
) -> Any:
    """Read ``GET /v1/drives`` without a client. Bodyless: no ``/Temp`` cost.

    :func:`gc_temp_folder` takes a host, not a client, so it needs a way
    to ask what is mounted on its own. Callers that *have* a client
    should pass its
    :meth:`~c64_test_harness.backends.ultimate64_client.Ultimate64Client.list_drives`
    as ``mounted_probe`` instead.

    *port* and *password* exist because the one caller that cannot supply
    a client -- :meth:`TempLedger.drain_on_lock_release`'s orphaned sweep
    -- is also the one that deletes files this process did not create, so
    it is exactly where the exclusion matters most (#418 review, finding
    2). Firmware 1.1.0 answers an unauthenticated call with
    ``HTTP_FORBIDDEN`` when a Network Password is set (``routes.h``
    collects ``X-Password``; ``routes.cc`` maps the mismatch), and a
    device on a non-default REST port is not at ``http://host/`` at all --
    either way the probe would raise and the sweep would proceed
    unprotected.
    """
    base = f"http://{host}:{port}" if port and port != 80 else f"http://{host}"
    req = urllib.request.Request(f"{base}/v1/drives", method="GET")
    if password:
        req.add_header("X-Password", password)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def gc_temp_folder(
    host: str,
    *,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    keep: int | None = None,
    timeout: float = DEFAULT_FTP_TIMEOUT,
    mounted_probe: Callable[[], Any] | None = None,
) -> TempGCResult:
    """Best-effort GC of the U64's managed ``/Temp`` attachments over FTP.

    Deletes ``^temp[0-9a-fA-F]+$``-named files in ``/Temp``, oldest-first
    (by the suffix parsed as base-16 -- the firmware's counter is hex, so
    ``temp0009`` is followed by ``temp000A``), keeping the *keep*
    youngest, and **skipping any whose name a drive currently has
    mounted**.

    That last exclusion is #418. The pattern was described here as
    matching nothing but leaked attachments; that was wrong for one
    shape. An image uploaded as a **raw** body, or as a multipart part
    with no ``filename=``, keeps the firmware's managed ``temp%04x``
    name and is then mounted *from that file*, so a sweep -- including
    one a later lane runs under the device lock -- could delete a
    mounted image's backing store. Harness uploads have that shape too:
    :meth:`~c64_test_harness.backends.ultimate64_client.Ultimate64Client.mount_disk`
    sends an unnamed part since #427, so that each upload gets its own
    file instead of a shared ``image.<type>``. What the
    1541 emulation does when a mounted read-write image's backing file
    disappears is not established, which is the reason not to find out.

    The exclusion can only ever **shrink** the delete set: mounted names
    are dropped before the keep-count is applied, so the youngest
    survivors are re-chosen from what is left.

    :param host: Device hostname/IP (the REST host -- FTP is a separate
        port on the same device).
    :param port: FTP control port. Defaults to 21.
    :param username: FTP username. Defaults to ``$U64_TEMP_GC_FTP_USER``
        or ``"anonymous"``.
    :param password: FTP password. Defaults to
        ``$U64_TEMP_GC_FTP_PASSWORD`` or ``"anonymous@"``.
    :param keep: Number of youngest managed attachments to retain.
        Defaults to ``$U64_TEMP_GC_KEEP`` or :data:`DEFAULT_KEEP`. A
        value <= 0 deletes everything managed.
    :param timeout: Socket timeout in seconds for the whole FTP session.
    :param mounted_probe: Zero-argument callable returning a parsed
        ``GET /v1/drives`` document. Defaults to
        :func:`_default_mounted_probe`. It is called **only when the
        sweep would otherwise delete something**, so a device with
        nothing to collect gets no extra request at all.
    :returns: A :class:`TempGCResult`. Never raises -- any connect,
        login, or delete failure is captured in ``.error`` and logged at
        INFO/WARNING; the caller's run must never fail on hygiene.

    **A probe failure is a failed pass, and deletes nothing** (#513 review,
    finding 4). Without the listing, the keep-count alone could delete a
    mounted image that is not the youngest. Its drive holds it open, and at
    1.1.0 FatFS does not refuse deleting an open file (``FF_FS_LOCK 0``).
    So the result carries ``.mounted_probe_error`` and ``.error``, and the
    caller's refusal applies. The pass was previously treated as ok; that
    traded a delete of an open file for the chance to keep sweeping, and
    the harness now prefers to stop uploading instead.
    """
    resolved_port = port if port is not None else DEFAULT_FTP_PORT
    resolved_user = username if username is not None else os.environ.get(FTP_USER_ENV, DEFAULT_FTP_USER)
    resolved_password = (
        password if password is not None else os.environ.get(FTP_PASSWORD_ENV, DEFAULT_FTP_PASSWORD)
    )
    resolved_keep = keep if keep is not None else _int_env(KEEP_ENV, DEFAULT_KEEP)

    try:
        with FTP() as ftp:
            ftp.connect(host, resolved_port, timeout=timeout)
            ftp.login(resolved_user, resolved_password)
            ftp.cwd("/Temp")
            names = ftp.nlst()

            managed = []
            for name in names:
                basename = name.rsplit("/", 1)[-1]
                m = _MANAGED_ATTACHMENT_RE.match(basename)
                if m:
                    managed.append((int(m.group(1), 16), name))
            managed.sort(key=lambda pair: pair[0])
            managed_names = [name for _, name in managed]

            to_delete, to_keep = _split_by_keep(managed_names, resolved_keep)

            mounted_excluded: list[str] = []
            probe_error: str | None = None
            if to_delete:
                # Only asked when something would actually be deleted, so a
                # device with nothing to collect costs no extra request.
                probe = mounted_probe
                if probe is None:
                    def probe() -> Any:  # noqa: E306 - local default
                        return _default_mounted_probe(host)
                try:
                    mounted = _managed_names_in(probe())
                except Exception as exc:  # noqa: BLE001 - hygiene must not fail
                    probe_error = f"{type(exc).__name__}: {exc}"
                    _log.warning(
                        "gc_temp_folder: could not read the mounted-image listing on %s "
                        "(%s); deleting nothing. A mounted image that is not the "
                        "youngest is held open by its drive and could otherwise be "
                        "deleted (#418, #513 review).",
                        host, probe_error,
                    )
                    return TempGCResult(
                        host=host,
                        kept=list(managed_names),
                        mounted_probe_error=probe_error,
                        error=(
                            f"mounted-image listing unreadable ({probe_error}); "
                            "deleted nothing"
                        ),
                    )
                else:
                    survivors = []
                    for name in managed_names:
                        if name.rsplit("/", 1)[-1] in mounted:
                            mounted_excluded.append(name)
                        else:
                            survivors.append(name)
                    if mounted_excluded:
                        to_delete, to_keep = _split_by_keep(survivors, resolved_keep)
                        _log.info(
                            "gc_temp_folder: %d managed /Temp name(s) on %s are mounted "
                            "on a drive and were left alone: %s",
                            len(mounted_excluded), host, ", ".join(mounted_excluded),
                        )

            deleted: list[str] = []
            for name in to_delete:
                try:
                    ftp.delete(name)
                    deleted.append(name)
                except _FTP_ALL_ERRORS as exc:
                    _log.warning("gc_temp_folder: failed to delete %s on %s: %s", name, host, exc)

            if deleted:
                _log.info(
                    "gc_temp_folder: removed %d stale /Temp attachment(s) on %s (kept %d)",
                    len(deleted), host, len(to_keep),
                )
            return TempGCResult(
                host=host,
                deleted=deleted,
                kept=to_keep,
                mounted_excluded=mounted_excluded,
                mounted_probe_error=probe_error,
            )
    except ConnectionRefusedError as exc:
        error = (
            f"ConnectionRefusedError: {exc} -- FTP File Service may be disabled on this "
            "device (seen by default on C64U fw 1.1.0; U64E ships it enabled). Enable it "
            "via Network Settings > FTP File Service in the device's REST config; this is "
            "a runtime-only setting -- it lives in firmware RAM until save_config_to_flash, "
            "so a firmware power-on reverts it. That is benign: /Temp is a RAM disk "
            "(S: 1541ultimate software/filesystem/ramdisk.cc), so the same power-on empties "
            "it. Note machine:reboot is a C64-level reset and does neither."
        )
        _log.info("gc_temp_folder: /Temp hygiene pass skipped on %s (%s)", host, error)
        return TempGCResult(host=host, error=error)
    except Exception as exc:  # noqa: BLE001 - deliberately blanket, see module docstring
        _log.info("gc_temp_folder: /Temp hygiene pass skipped on %s (%s: %s)", host, type(exc).__name__, exc)
        return TempGCResult(host=host, error=f"{type(exc).__name__}: {exc}")
