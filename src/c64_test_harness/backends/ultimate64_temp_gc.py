"""FTP-based garbage collection for the Ultimate's leaked ``/Temp`` attachments.

Every Ultimate REST call that carries a body (``writemem`` POST,
``run_prg``, ``load_prg``, ...) lands as a managed attachment
(``temp0000``, ``temp0001``, ...) in the device's ``/Temp`` folder, and
no released firmware collects them. Once ``/Temp`` fills (~15 cycles of
a 63 KB PRG in the U64E reproduction that prompted this module), the
REST API and the C64-facing UCI bridge wedge together and only a
physical power-cycle recovers — see ``docs/u64_recovery.md`` for the
wedge-tier writeup and GitHub issue #153 for the FTP-based mitigation
this module implements.

This is shared 1541ultimate firmware behaviour, not U64E-specific: it
affects both device generations (U64E on 3.14d, C64U on 1.1.0). The
module itself is host-generic (no generation branching) and has been
verified live end-to-end (leak via ``run_prg`` + FTP GC trims to the
keep-count) on both: originally on the U64E, and on the C64U at
10.53.21.158 via ``tests/test_temp_gc_live.py`` (2026-08-21) — anonymous
FTP against ``/Temp`` worked with the same defaults as the U64E.

Upstream root cause and fix: GideonZ/1541ultimate#686 (auto-cleanup of
managed ``/Temp`` files, oldest-first, keep youngest 10) — merged but not
in any released firmware as of this writing (U64E on 3.14d, C64U on
1.1.0). Once a firmware release containing it ships, this module becomes
redundant (it will simply find nothing to delete).

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

#: Override FTP credentials. Bench devices run anonymous FTP; a device
#: with credentials configured needs these set.
FTP_USER_ENV = "U64_TEMP_GC_FTP_USER"
FTP_PASSWORD_ENV = "U64_TEMP_GC_FTP_PASSWORD"

#: The firmware's managed-attachment naming (see 1541ultimate#686 and
#: the c64-https tools/uci/_temp_gc.py workaround this supersedes).
#: Deliberately narrow -- user files and mounted disk images that also
#: live in /Temp must never match.
_MANAGED_ATTACHMENT_RE = re.compile(r"^temp(\d+)$")

DEFAULT_KEEP = 2
DEFAULT_FTP_PORT = 21
DEFAULT_FTP_TIMEOUT = 10.0
DEFAULT_FTP_USER = "anonymous"
DEFAULT_FTP_PASSWORD = "anonymous@"

__all__ = [
    "TempGCResult",
    "gc_temp_folder",
    "auto_gc_enabled",
    "AUTO_GC_ENV",
    "KEEP_ENV",
    "FTP_USER_ENV",
    "FTP_PASSWORD_ENV",
    "DEFAULT_KEEP",
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
    """Whether ``U64_AUTO_TEMP_GC`` requests the automatic run_prg hook."""
    return _truthy_env(AUTO_GC_ENV)


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

    Deletes ``^temp\\d+$``-named files in ``/Temp``, oldest-first (by the
    numeric suffix), keeping the *keep* youngest. Nothing else in
    ``/Temp`` (user files, mounted ``.d64``/``.crt`` images) matches the
    pattern and so is never touched.

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
                    managed.append((int(m.group(1)), name))
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
    except Exception as exc:  # noqa: BLE001 - deliberately blanket, see module docstring
        _log.info("gc_temp_folder: /Temp hygiene pass skipped on %s (%s: %s)", host, type(exc).__name__, exc)
        return TempGCResult(host=host, error=f"{type(exc).__name__}: {exc}")
