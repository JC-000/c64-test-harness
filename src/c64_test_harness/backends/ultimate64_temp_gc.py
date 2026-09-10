"""FTP-based garbage collection for the Ultimate's leaked ``/Temp`` attachments.

Every Ultimate REST call that carries a body (``writemem`` POST,
``run_prg``, ``load_prg``, ...) lands as a managed attachment
(``temp0000``, ``temp0001``, ...) in the device's ``/Temp`` folder;
firmware without the #686 cleanup never collects them. Enough of them
wedge the REST API and the C64-facing UCI bridge together by **crashing
the device firmware**: the C64 FPGA keeps running, while the firmware
stops answering the network and stops responding to the physical menu
button on the case. Only a physical power-cycle recovers.

Two things are deliberately not claimed here, because neither is
established. **The trigger threshold**: one U64E on 3.14d wedged at ~15
cycles of a 63 KB PRG (n unrecorded), which is the reproduction that
prompted this module — a datapoint, not a limit, and with no standing for
a C64U on 1.1.0. It is emphatically *not* a capacity figure: ``/Temp`` is
a ~3 MB RAM disk, so that is ~31% of it across 15 directory entries, and
nothing was near exhaustion. ``/Temp`` "filling" does not describe this
wedge and earlier versions of this docstring were wrong to say it did.
**The crash cause**: the pre-fix ``attachment_writer`` created
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
on the bench U64E (v3.15-85) this module finds nothing to delete
(measured 2026-09-02: 0 managed files before and after 15 ``run_prg``
uploads with the GC off). It still matters on the C64 Ultimate, whose
1.1.0 firmware predates the fix; ``u64_capabilities.writemem_post_safe``
is the per-device switch.

:func:`gc_temp_folder` is deliberately *never raising*: every FTP or
network failure is caught and reported via :class:`TempGCResult.error`
so a hygiene pass can never fail a test run. Callers that hold the
device's :class:`~c64_test_harness.backends.device_lock.DeviceLock` (as
:meth:`~c64_test_harness.backends.ultimate64_client.Ultimate64Client.run_prg`
does implicitly via its caller) should call this while still holding
that lock -- this module does not acquire one itself.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from ftplib import FTP, all_errors as _FTP_ALL_ERRORS

_log = logging.getLogger(__name__)

#: Set to any non-empty, non-"0"/"false" value to enable the automatic
#: hygiene pass inside Ultimate64Client.run_prg. Off by default so the
#: unit-test suite (which exercises run_prg against fake hosts) never
#: makes a real network connection -- see AUTO_GC_ENV usage in
#: ultimate64_client.py.
AUTO_GC_ENV = "U64_AUTO_TEMP_GC"

#: Override the default keep-count (see DEFAULT_KEEP).
KEEP_ENV = "U64_TEMP_GC_KEEP"

#: Override the per-client leak budget -- how many attachment-creating
#: requests may go out between hygiene passes (see DEFAULT_LEAK_BUDGET).
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
#: lettered name uncollected). Deliberately narrow -- user files and
#: mounted disk images that also live in /Temp must never match.
_MANAGED_ATTACHMENT_RE = re.compile(r"^temp([0-9a-fA-F]+)$")

DEFAULT_KEEP = 2

#: How many attachment-creating requests one client may issue before the
#: next one triggers a hygiene pass.
#:
#: One measurement and one upstream precedent bound this from above --
#: not two enforced limits, and the difference matters.
#:
#: The precedent is the firm one: the firmware's own post-#686 collector
#: keeps at most **10** managed files
#: (``software/filemanager/filemanager.cc``: ``kManagedTempMaxFiles``),
#: which is upstream's own statement of a safe resident count. The
#: firmware that needs this pass does not enforce it -- that is the whole
#: point -- but it is upstream's judgement about the same folder on the
#: same device family, and it needs no conditions attached.
#:
#: The measurement is much weaker than it is usually quoted as being: one
#: U64E on 3.14d wedged at about **15** uploads of a 63 KB PRG, n
#: unrecorded. It has no standing for a C64U on 1.1.0, and it is not a
#: capacity measurement -- the RAM disk is ~3 MB
#: (``software/filesystem/ramdisk.cc``), so 945 KB is ~31% of it with 15
#: directory entries used. Treat 15 as "a device once wedged here", not
#: as a limit.
#:
#: **What actually fails is the firmware, not the folder.** On a wedged
#: machine the C64 FPGA keeps running while the device firmware is dead:
#: it stops answering the network *and* stops responding to the physical
#: menu button. So this was never about ``/Temp`` running out of room --
#: at 31% full with 15 entries, nothing was near exhausted, which is why
#: no capacity story ever fit. Accumulation crashes the firmware; the
#: **cause is not established** (the pre-fix ``attachment_writer``
#: created ``/Temp/temp%04x`` from a static counter and never deleted
#: them, but ``TempfileWriter``'s destructor does free the ``strdup``'d
#: names and the buffers, so a naive per-request heap-leak story does not
#: hold on its face; heap fragmentation, per-entry allocation in
#: directory traversal and FileManager bookkeeping growth are all
#: candidates, none run down). Do not claim a cause.
#:
#: Since the trigger threshold is not established, the budget is a choice
#: about **which error to make**: 6, with :data:`DEFAULT_KEEP` = 2, holds
#: the steady state at 8 resident attachments -- inside upstream's own
#: notion of safe -- at the cost of an occasional FTP pass nobody needed.
#:
#: Consumer call counts bound it from below, and show a low budget costs
#: normal consumers nothing (recounted across all six consumer lanes,
#: 2026-09-10; reported, not verified here). Ordinary runner-verb
#: consumers issue **1-2** leaking POSTs per run across ~14 call sites and
#: one host-Python UCI driver reaches 4-8, so at 6 the pass never fires
#: for any of them. The case this exists for is a **17**-call
#: ``ALL_SPEEDS`` sweep (``bench_p256_u64.py`` / ``bench_p384_u64.py``,
#: one ``run_prg`` per speed), which is pure runner verbs -- it cannot be
#: moved off POST by chunking or by driving the protocol C64-side, so
#: hygiene is its only available fix, and it would otherwise accumulate
#: 17 attachments in a single invocation. At 6 the pass fires on that
#: run's 7th and 13th calls, holding resident attachments at budget +
#: keep = 8. A budget of 10 or more would let that sweep run to
#: completion with nothing having happened, which is why this is low
#: rather than generous. (Lanes doing hundreds of raw ``write_memory``
#: calls exist, but those are lane bugs to fix by chunking, not counts to
#: size against.)
#:
#: Note the unit: this counts *attachments*, not logical operations, and
#: on a C64U most generated code blobs exceed the 128-byte PUT ceiling
#: (``build_socket_write`` is 170 bytes, payload-independent;
#: ``turbo_safe=True`` roughly triples every builder). So a UCI socket
#: write spends one of the budget for its routine code, plus a second
#: only when the payload itself exceeds the ceiling (the 800/892-byte
#: large-send tests; a small write stays on PUT). ``enable_uci`` /
#: ``disable_uci`` are bodyless config writes and cost nothing. Counting
#: at the request layer gets all of that right for free without anyone
#: maintaining a table; see ``docs/u64_recovery.md``.
#:
#: Do not try to establish the trigger threshold experimentally: the
#: experiment is "upload until the firmware crashes", on hardware nobody
#: can power-cycle remotely. It becomes safely measurable only with
#: someone physically present.
#:
#: Override with :data:`BUDGET_ENV` or the client's ``temp_gc_budget=``.
DEFAULT_LEAK_BUDGET = 6

DEFAULT_FTP_PORT = 21
DEFAULT_FTP_TIMEOUT = 10.0
DEFAULT_FTP_USER = "anonymous"
DEFAULT_FTP_PASSWORD = "anonymous@"

__all__ = [
    "TempGCResult",
    "gc_temp_folder",
    "auto_gc_enabled",
    "auto_gc_override",
    "hygiene_required",
    "leak_budget",
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

    @property
    def ok(self) -> bool:
        """True when the pass ran without an FTP/network failure (skips still count as ok)."""
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
    """The per-client leak budget, honouring :data:`BUDGET_ENV`."""
    value = _int_env(BUDGET_ENV, default)
    return value if value > 0 else default


def gc_temp_folder(
    host: str,
    *,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    keep: int | None = None,
    timeout: float = DEFAULT_FTP_TIMEOUT,
) -> TempGCResult:
    """Best-effort GC of the U64's managed ``/Temp`` attachments over FTP.

    Deletes ``^temp[0-9a-fA-F]+$``-named files in ``/Temp``, oldest-first
    (by the suffix parsed as base-16 -- the firmware's counter is hex, so
    ``temp0009`` is followed by ``temp000A``), keeping the *keep*
    youngest. Nothing else in ``/Temp`` (user files, mounted
    ``.d64``/``.crt`` images) matches the pattern and so is never
    touched.

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
    :returns: A :class:`TempGCResult`. Never raises -- any connect,
        login, or delete failure is captured in ``.error`` and logged at
        INFO/WARNING; the caller's run must never fail on hygiene.
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

            if resolved_keep <= 0:
                to_delete, to_keep = managed_names, []
            elif resolved_keep >= len(managed_names):
                to_delete, to_keep = [], managed_names
            else:
                to_delete = managed_names[:-resolved_keep]
                to_keep = managed_names[-resolved_keep:]

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
            return TempGCResult(host=host, deleted=deleted, kept=to_keep)
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
