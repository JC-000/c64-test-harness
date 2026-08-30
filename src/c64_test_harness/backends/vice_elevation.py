"""Raw-network capability for a VICE launch, and the elevation it needs.

VICE gates its ethernet drivers on ``archdep_rawnet_capability()``
(``src/arch/shared/archdep_rawnet_capability.c``), which is::

    geteuid() == 0                      -- every UNIX build
    || cap_check(CAP_NET_RAW)           -- Linux only (HAVE_CAPABILITIES)

That is the *whole* test.  It never looks at ``/dev/bpf*``: a rig that
ran ``chmod o+rw /dev/bpf*`` has changed nothing VICE consults.  The
result gates driver *selection* in ``rawnetarch.c:set_ethernet_driver()``
and ``rawnet_arch_resources_init()``, both of which admit the pcap driver
only when the capability holds.  Without it ``rawnet_arch_driver`` stays
``NULL`` and the first reset dereferences it in
``rawnet_arch_pre_reset()`` — SIGSEGV, no log line, no diagnostics.

So an unelevated ethernet launch does not degrade, it crashes.  This
module answers the question honestly *before* anything is spawned and, if
the answer is no, hands the operator the exact command to run instead.

The tuntap driver (Linux) is selected without consulting the capability,
so tuntap launches need no elevation at all.

Public surface: :func:`plan_vice_launch` (used by ``ViceProcess.start``),
:func:`vice_binary_supports_ethernet`, :func:`rawnet_capability`,
:func:`sudo_can_run`, :class:`ViceLaunchPlan`, and the
:class:`ViceEthernetError` family.
"""

from __future__ import annotations

import getpass
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from c64_test_harness.backends.vice_lifecycle import ViceConfig

_log = logging.getLogger(__name__)

#: Opt out of the pre-launch refusal.  Set to ``1`` when this host grants
#: rawnet capability by a route we cannot observe (Linux file
#: capabilities on the x64sc binary, for example).  The launch proceeds
#: unelevated and a WARNING says so; if the capability really is missing,
#: VICE will SIGSEGV on the first reset.
ALLOW_UNELEVATED_ENV = "VICE_ETHERNET_ALLOW_UNELEVATED"

#: Resource-name strings present in every x64sc built with raw-network
#: support and absent when ethernet is compiled out.  Both live in the
#: rawnet/ethernet-cart translation units, so their presence is a
#: property of the build rather than of any runtime state.
ETHERNET_BUILD_MARKERS = (b"ETHERNET_DRIVER", b"ETHERNETCART_ACTIVE")

#: ``CAP_NET_RAW`` bit position in ``/proc/self/status``'s ``CapEff``.
_CAP_NET_RAW_BIT = 13

#: Drivers VICE selects only when ``archdep_rawnet_capability()`` holds.
_ROOT_GATED_DRIVERS = frozenset({"pcap"})

#: Drivers selected without consulting the capability.
_UNGATED_DRIVERS = frozenset({"tuntap"})

#: Seconds to allow the ``sudo -l`` authorisation probe.
_SUDO_PROBE_TIMEOUT = 5.0


class ViceEthernetError(RuntimeError):
    """Base for ethernet-launch problems detected before spawning VICE."""


class ViceEthernetBinaryError(ViceEthernetError):
    """No ethernet-capable ``x64sc`` could be resolved for this launch."""


class ViceElevationRequiredError(ViceEthernetError):
    """The launch needs root for rawnet and root is not obtainable.

    Carries the exact remedy rather than a diagnosis:

    ``argv``
        The refused launch, elevated — ``["sudo", <binary>, ...]``.  No
        ``-n``, so an operator running it interactively can be prompted
        for a password.
    ``command``
        :attr:`argv` as a shell-ready one-liner.
    ``binary``
        The path elevation must be authorised *for*.  sudoers matches on
        sudo's first non-flag argument, so this exact path is what a
        NOPASSWD rule has to name, and it must never be wrapped in
        ``bash`` (the wrapper would become the matched program).
    ``sudoers_entry``
        A NOPASSWD line that would authorise it.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str],
        binary: str,
        sudoers_entry: str,
    ) -> None:
        super().__init__(message)
        self.argv = list(argv)
        self.command = shlex.join(self.argv)
        self.binary = binary
        self.sudoers_entry = sudoers_entry


@dataclass(frozen=True)
class ViceLaunchPlan:
    """How ``ViceProcess`` should actually exec a configured launch.

    ``argv``
        The final argv, sudo-wrapped when elevation was needed and
        obtainable.
    ``elevated``
        Whether the child will run as root (already root, or wrapped).
    ``sudo_wrapped``
        Whether ``argv`` starts with ``sudo -n`` — the caller needs this
        to signal the child correctly, since an unprivileged parent
        cannot signal a root child directly.
    """

    argv: list[str]
    elevated: bool
    sudo_wrapped: bool


# ------------------------------------------------------- build capability


def vice_binary_supports_ethernet(path: str) -> bool:
    """Whether the x64sc at *path* was built with raw-network support.

    Scans the binary image for the rawnet resource names
    (:data:`ETHERNET_BUILD_MARKERS`).  Cheap, offline, and — unlike
    "is ``$VICE_ETHERNET_BIN`` set?" — an actual property of the build.

    It cannot tell pcap from tuntap: both drivers share those resource
    names.  Use :func:`driver_requires_root` for the driver question.

    Returns ``False`` for a path that does not exist or cannot be read.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return _scan_for_markers(os.path.realpath(path), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=32)
def _scan_for_markers(real_path: str, mtime_ns: int, size: int) -> bool:
    """Marker scan, cached on the file's identity (so a rebuilt binary at
    the same path is rescanned rather than remembered)."""
    remaining = set(ETHERNET_BUILD_MARKERS)
    overlap = max(len(m) for m in ETHERNET_BUILD_MARKERS) - 1
    tail = b""
    try:
        with open(real_path, "rb") as f:
            while remaining:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                window = tail + chunk
                remaining = {m for m in remaining if m not in window}
                tail = window[-overlap:]
    except OSError:
        return False
    return not remaining


# --------------------------------------------- archdep_rawnet_capability


def _has_cap_net_raw() -> bool:
    """Linux: does this process hold an effective ``CAP_NET_RAW``?

    Mirrors VICE's ``cap_check(CAP_NET_RAW)`` branch, which does not
    exist on macOS.  Reads ``CapEff`` from ``/proc/self/status`` rather
    than binding libcap.
    """
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << _CAP_NET_RAW_BIT))
    except (OSError, ValueError, IndexError):
        return False
    return False


def rawnet_capability(*, as_root: bool | None = None) -> bool:
    """What ``archdep_rawnet_capability()`` would return for a child.

    *as_root* says whether the child will run as root; ``None`` asks
    about this process as it stands.
    """
    running_as_root = os.geteuid() == 0 if as_root is None else as_root
    if running_as_root:
        return True
    return _has_cap_net_raw()


def driver_requires_root(driver: str) -> bool:
    """Whether *driver* is one VICE gates behind rawnet capability.

    An empty string means "VICE's default for this platform": pcap on
    macOS (the only driver a Darwin build has), tuntap elsewhere.  An
    unrecognised name is treated as ungated — better to let VICE reject
    an unknown driver than to demand root on a guess.
    """
    name = (driver or "").strip().lower()
    if not name:
        name = "pcap" if sys.platform == "darwin" else "tuntap"
    if name in _UNGATED_DRIVERS:
        return False
    return name in _ROOT_GATED_DRIVERS


# ------------------------------------------------------------ sudo probe


def sudo_can_run(binary: str) -> bool:
    """Whether this user may run *binary* as root without a password.

    ``sudo -n -l -- <binary>`` asks sudo itself, non-interactively and
    without running anything: exit 0 means the rule exists (or a
    credential is cached), non-zero means it does not.  Never prompts —
    stdin is closed and ``-n`` makes sudo fail rather than ask.
    """
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-l", "--", binary],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_SUDO_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# ------------------------------------------------------------ the plan


def _sudoers_entry(binary: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        user = os.environ.get("USER", "<user>")
    return f"{user} ALL=(root) NOPASSWD: {binary}"


def _unelevated_allowed() -> bool:
    return os.environ.get(ALLOW_UNELEVATED_ENV, "").strip() == "1"


def _refuse(argv: list[str], binary: str, reason: str) -> ViceElevationRequiredError:
    interactive = ["sudo"] + argv
    command = shlex.join(interactive)
    entry = _sudoers_entry(binary)
    message = (
        f"{reason}\n"
        f"VICE selects an ethernet driver only when "
        f"archdep_rawnet_capability() holds (euid 0, or CAP_NET_RAW on "
        f"Linux); without it rawnet_arch_driver stays NULL and x64sc "
        f"SIGSEGVs on the first reset with no log output. "
        f"/dev/bpf* permissions are irrelevant — VICE never reads them.\n"
        f"Run this launch elevated:\n"
        f"    {command}\n"
        f"Or authorise it for unattended runs by adding to sudoers "
        f"(visudo; the rule must name this exact path and must not be "
        f"bash-wrapped, because sudo matches its first non-flag argument):\n"
        f"    {entry}\n"
        f"To launch unelevated anyway (expect a SIGSEGV unless this host "
        f"grants the capability another way), set {ALLOW_UNELEVATED_ENV}=1."
    )
    return ViceElevationRequiredError(
        message, argv=interactive, binary=binary, sudoers_entry=entry
    )


def plan_vice_launch(cfg: "ViceConfig", argv: Sequence[str]) -> ViceLaunchPlan:
    """Decide how to exec *argv*, or refuse.

    Raises :class:`ViceElevationRequiredError` when the launch needs root
    for rawnet and root is not obtainable non-interactively, unless
    :data:`ALLOW_UNELEVATED_ENV` opts out.
    """
    args = list(argv)
    binary = args[0]
    already_root = os.geteuid() == 0

    needs_root = (
        bool(cfg.ethernet)
        and driver_requires_root(cfg.ethernet_driver)
        and not rawnet_capability(as_root=False)
    )
    want_root = needs_root if cfg.run_as_root is None else bool(cfg.run_as_root)

    if not want_root:
        if needs_root:
            err = _refuse(
                args,
                binary,
                "Refusing to launch the ethernet cart unelevated: "
                "run_as_root=False was set explicitly, but this launch "
                "needs root to obtain raw-network capability.",
            )
            if not _unelevated_allowed():
                raise err
            _log.warning(
                "%s=1: launching the ethernet cart without rawnet "
                "capability; x64sc may SIGSEGV on reset",
                ALLOW_UNELEVATED_ENV,
            )
        return ViceLaunchPlan(argv=args, elevated=already_root, sudo_wrapped=False)

    if already_root:
        return ViceLaunchPlan(argv=args, elevated=True, sudo_wrapped=False)

    if not sudo_can_run(binary):
        err = _refuse(
            args,
            binary,
            "Cannot elevate this VICE launch: "
            f"'sudo -n' is not authorised for {binary}.",
        )
        if not _unelevated_allowed():
            raise err
        _log.warning(
            "%s=1: no sudo authorisation for %s, launching unelevated; "
            "x64sc may SIGSEGV on reset",
            ALLOW_UNELEVATED_ENV,
            binary,
        )
        return ViceLaunchPlan(argv=args, elevated=False, sudo_wrapped=False)

    # -E (preserve env) would need a SETENV tag in sudoers, which is a
    # privilege expansion we deliberately do not ask for.  VICE reads
    # $HOME for its config path and sudo's default env_keep includes
    # HOME, so plain `sudo -n` suffices.
    return ViceLaunchPlan(argv=["sudo", "-n"] + args, elevated=True, sudo_wrapped=True)
