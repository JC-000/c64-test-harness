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
import shutil
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

#: Seconds to allow ``x64sc -features``.  It prints a table and exits;
#: no emulator starts.
_FEATURES_TIMEOUT = 10.0

#: ``-features`` rows the harness cares about, mapped to field names.
_FEATURE_ROWS = {
    "HAVE_RAWNET": "rawnet",
    "HAVE_PCAP": "pcap",
    "HAVE_TUNTAP": "tuntap",
}

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

#: sudoers tag specifiers as ``sudo -l`` prints them (sudoers(5) "Tag_Spec").
#: Any of these may precede the command in a NOPASSWD entry.
_SUDO_TAGS = frozenset({
    "PASSWD:", "NOPASSWD:", "SETENV:", "NOSETENV:", "EXEC:", "NOEXEC:",
    "LOG_INPUT:", "NOLOG_INPUT:", "LOG_OUTPUT:", "NOLOG_OUTPUT:",
    "MAIL:", "NOMAIL:", "FOLLOW:", "NOFOLLOW:", "INTERCEPT:", "NOINTERCEPT:",
})


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


@dataclass(frozen=True)
class ViceFeatures:
    """What an ``x64sc`` build can do, as the binary itself reports it.

    ``source`` is ``"-features"`` when the binary answered, or ``"scan"``
    when it could not be executed and we fell back to reading its image.
    The fallback can see *whether* raw-network support is compiled in but
    not *which* drivers, so ``drivers_known`` is False there and callers
    must not refuse a driver on its say-so.
    """

    rawnet: bool
    pcap: bool
    tuntap: bool
    source: str
    drivers_known: bool

    def has_driver(self, name: str) -> bool:
        return {"pcap": self.pcap, "tuntap": self.tuntap}.get(name.lower(), False)


def vice_features(path: str) -> ViceFeatures:
    """Probe the build capabilities of the ``x64sc`` at *path*.

    ``x64sc -features`` prints its compile-time feature table and exits
    (no emulator, no window, no monitor), so this is the authoritative
    and cheap answer to "was this built with ethernet?":

        HAVE_RAWNET               yes  Enable raw ethernet emulation.
        HAVE_PCAP                 yes  Use the PCAP library.
        HAVE_TUNTAP               no   Support for TUN/TAP virtual ...

    Cached per binary (path + mtime + size), so a rebuild is re-probed
    but a hot loop pays once.  If the binary cannot be executed or says
    nothing useful, falls back to scanning its image for the rawnet
    resource names.
    """
    try:
        st = os.stat(path)
    except OSError:
        return ViceFeatures(False, False, False, "unreadable", False)
    return _probe_features(os.path.realpath(path), st.st_mtime_ns, st.st_size, path)


@lru_cache(maxsize=32)
def _probe_features(
    real_path: str, mtime_ns: int, size: int, launch_path: str
) -> ViceFeatures:
    values: dict[str, bool] = {}
    try:
        # ``-default`` matters here for the same reason it matters on a
        # real launch: without it VICE reads the ambient vicerc, and a
        # vicerc carrying ``LogToStdout=0`` (or a log-file setting) sends
        # this output somewhere we are not reading.  ``values`` then comes
        # back empty and the probe degrades silently to the image scan,
        # which reports ``drivers_known=False`` and so refuses drivers the
        # binary actually has.
        #
        # Measured on this bench: with such a vicerc on ``$HOME``,
        # ``x64sc -features`` prints 0 feature rows and
        # ``x64sc -default -features`` prints 36.
        proc = subprocess.run(
            [launch_path, "-default", "-features"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_FEATURES_TIMEOUT,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in _FEATURE_ROWS:
                values[_FEATURE_ROWS[parts[0]]] = parts[1].lower() == "yes"
    except (OSError, subprocess.SubprocessError):
        values = {}

    if "rawnet" in values:
        return ViceFeatures(
            rawnet=values.get("rawnet", False),
            pcap=values.get("pcap", False),
            tuntap=values.get("tuntap", False),
            source="-features",
            drivers_known=True,
        )

    # Could not ask the binary; read it instead.
    rawnet = _scan_for_markers(real_path, mtime_ns, size)
    _log.debug("%s did not answer -features; fell back to image scan", launch_path)
    return ViceFeatures(
        rawnet=rawnet, pcap=False, tuntap=False, source="scan", drivers_known=False
    )


@lru_cache(maxsize=32)
def _scan_for_markers(real_path: str, mtime_ns: int, size: int) -> bool:
    """Fallback for a binary we cannot execute: does its image carry the
    rawnet resource names?  Cached on the file's identity, so a rebuilt
    binary at the same path is rescanned rather than remembered."""
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


def vice_binary_supports_ethernet(path: str) -> bool:
    """Whether the x64sc at *path* was built with raw-network support.

    Thin wrapper over :func:`vice_features`.  Returns ``False`` for a
    path that does not exist.
    """
    return vice_features(path).rawnet


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


def effective_driver_name(driver: str) -> str:
    """*driver*, or VICE's default for this platform when it is empty.

    macOS builds ship pcap only (``HAVE_TUNTAP no``, confirmed by
    ``-features`` on the Homebrew 3.10 bottle); Linux defaults to tuntap
    where it is compiled in.
    """
    name = (driver or "").strip().lower()
    if name:
        return name
    return "pcap" if sys.platform == "darwin" else "tuntap"


def driver_requires_root(driver: str) -> bool:
    """Whether *driver* is one VICE gates behind rawnet capability.

    An unrecognised name is treated as ungated — better to let VICE
    reject an unknown driver than to demand root on a guess.
    """
    name = effective_driver_name(driver)
    if name in _UNGATED_DRIVERS:
        return False
    return name in _ROOT_GATED_DRIVERS


# ------------------------------------------------------------ sudo probe


@dataclass(frozen=True)
class SudoAuthorisation:
    """What ``sudo`` will let this user do *without a password*.

    ``all_commands`` is a blanket ``NOPASSWD: ALL``; ``commands`` are the
    individually authorised binaries.  Rules that pin arguments (e.g.
    ``NOPASSWD: /opt/homebrew/bin/brew reinstall --HEAD vice``) authorise
    that one command line and are deliberately *not* counted here: they
    would not cover the argv we intend to launch.
    """

    all_commands: bool
    commands: frozenset[str]

    def allows(self, binary: str) -> bool:
        return self.all_commands or binary in self.commands


def parse_sudo_listing(text: str) -> SudoAuthorisation:
    """Extract the NOPASSWD rules from ``sudo -l`` output.

    Only NOPASSWD matters.  A plain ``(ALL) ALL`` line means the user may
    become root *with a password* — useless to an unattended harness, and
    the reason a per-command ``sudo -n -l -- <cmd>`` probe is worthless:
    it exits 0 for anything such a user may run, ``/bin/ls`` included.
    """
    all_commands = False
    commands: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        marker = "NOPASSWD:"
        if marker not in line:
            continue
        for entry in line.split(marker, 1)[1].split(","):
            parts = entry.split()
            # Further tags may follow NOPASSWD: (``NOPASSWD: SETENV: /x``);
            # they are not part of the command.  A later PASSWD: tag
            # reinstates the prompt, so such an entry is useless to us.
            tags: list[str] = []
            while parts and parts[0] in _SUDO_TAGS:
                tags.append(parts.pop(0))
            if not parts or "PASSWD:" in tags:
                continue
            if parts[0] == "ALL":
                all_commands = True
            elif len(parts) == 1 or parts[1:] == ["*"]:
                # No pinned arguments, or the bare wildcard that admits any.
                commands.add(parts[0])
    return SudoAuthorisation(all_commands, frozenset(commands))


@lru_cache(maxsize=1)
def sudo_authorisation() -> SudoAuthorisation:
    """This user's passwordless sudo rules, read once per process.

    ``sudo -n -l`` lists them without running anything and, with ``-n``,
    without ever prompting.
    """
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-l"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_SUDO_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return SudoAuthorisation(False, frozenset())
    if proc.returncode != 0:
        return SudoAuthorisation(False, frozenset())
    return parse_sudo_listing(proc.stdout or "")


def sudo_can_run(binary: str) -> bool:
    """Whether *binary* can be run as root **without a password**.

    Asks sudo for its rule list and looks for a NOPASSWD entry naming
    this exact path.  Being *permitted* to run it is not enough: an
    unattended launch that stops at a password prompt is a failed launch.

    Caveat: a cached sudo timestamp would let a launch through that this
    reports False for.  Refusing with an actionable message is the safe
    direction, and ``VICE_ETHERNET_ALLOW_UNELEVATED=1`` overrides it.
    """
    return sudo_authorisation().allows(binary)


# ------------------------------------------------------------ the plan


def _sudoers_entry(binary: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        user = os.environ.get("USER", "<user>")
    return f"{user} ALL=(root) NOPASSWD: {binary}"


def launch_path(binary: str) -> str:
    """*binary* as an absolute path, resolved the way sudo matches it.

    A bare name must be resolved before it is handed to sudo: sudo looks
    the command up in ``secure_path``, which on macOS does not include
    ``/opt/homebrew/bin``, so ``sudo -n x64sc`` fails with "command not
    found" even where ``x64sc`` runs fine unelevated.

    A relative path (``./x64sc``) is made absolute against the current
    directory: sudo matches on the absolute command path, and visudo
    rejects ``NOPASSWD: ./x64sc`` outright, so the sudoers line built
    from it would have been unusable.

    PATH lookup and absolutising only -- symlinks are deliberately *not*
    resolved.  sudoers matches the literal command path, so
    ``/opt/homebrew/bin/x64sc`` is what a NOPASSWD rule must name, not
    the ``/opt/homebrew/Cellar/vice/3.10/bin/x64sc`` it points at.
    """
    found = shutil.which(binary) or binary
    if os.sep in found:  # a path, not a bare name that PATH could not place
        return os.path.abspath(found)
    return found


def _unelevated_allowed() -> bool:
    return os.environ.get(ALLOW_UNELEVATED_ENV, "").strip() == "1"


def _refuse(argv: list[str], binary: str, reason: str) -> ViceElevationRequiredError:
    # The remedy names the *resolved* binary, like ``binary`` and the
    # sudoers line do: ``sudo x64sc`` fails on macOS because sudo's
    # secure_path lacks /opt/homebrew/bin, and the three must agree so
    # the pasted command is the one the pasted rule authorises.
    interactive = ["sudo", binary] + argv[1:]
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
    binary = launch_path(args[0])
    already_root = os.geteuid() == 0

    # "Needs root" means "needs *more* than this process has": a process
    # already running as root (or holding CAP_NET_RAW) needs nothing, and
    # run_as_root=False then just means "do not sudo", which is right.
    needs_root = (
        bool(cfg.ethernet)
        and driver_requires_root(cfg.ethernet_driver)
        and not rawnet_capability()
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
    # argv[0] becomes the resolved absolute path: see launch_path().
    return ViceLaunchPlan(
        argv=["sudo", "-n", binary] + args[1:], elevated=True, sudo_wrapped=True
    )
