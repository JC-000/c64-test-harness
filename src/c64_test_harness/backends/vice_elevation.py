"""Raw-network capability for a VICE launch, and the elevation it needs.

VICE gates its ethernet drivers on ``archdep_rawnet_capability()``
(``src/arch/shared/archdep_rawnet_capability.c``), which is::

    geteuid() == 0                      -- every UNIX build
    || cap_check(CAP_NET_RAW)           -- Linux only (HAVE_CAPABILITIES)

That is the *whole* test in stock VICE.  It never looks at
``/dev/bpf*``: a rig that ran ``chmod o+rw /dev/bpf*`` has changed nothing
stock VICE consults.  The result gates driver *selection* in ``rawnetarch.c:set_ethernet_driver()``
and ``rawnet_arch_resources_init()``, both of which admit the pcap driver
only when the capability holds.  Without it ``rawnet_arch_driver`` stays
``NULL`` and the first reset dereferences it in
``rawnet_arch_pre_reset()`` — SIGSEGV, no log line, no diagnostics.

So an unelevated ethernet launch does not degrade, it crashes.  This
module answers the question honestly *before* anything is spawned and, if
the answer is no, hands the operator the exact command to run instead.

The tuntap driver (Linux) is selected without consulting the capability,
so tuntap launches need no elevation at all.

On macOS, ``scripts/build-vice-eth.sh`` builds Homebrew's VICE plus
``vice/rawnet-bpf-permissions.patch``, which adds the check macOS actually
grants capture by -- can this user open a ``/dev/bpf*`` node read-write? --
and reports it in ``-features`` as ``RAWNET_BPF_PERMISSIONS yes``.  For such
a binary :func:`rawnet_capability` mirrors that check, so the launch runs
as the user; for any other binary it still needs root.

Public surface: :func:`plan_vice_launch` (used by ``ViceProcess.start``),
:func:`vice_binary_supports_ethernet`, :func:`rawnet_capability`,
:func:`sudo_can_run`, :class:`ViceLaunchPlan`, and the
:class:`ViceEthernetError` family.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
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

#: The ``-features`` row of a VICE whose rawnet check also accepts a
#: read-write ``/dev/bpf*`` node (``vice/rawnet-bpf-permissions.patch``).
BPF_PERMISSIONS_FEATURE = "RAWNET_BPF_PERMISSIONS"

#: Where ``scripts/build-vice-eth.sh``'s keg-only ``vice-eth`` lands, after
#: ``$HOMEBREW_PREFIX/opt/vice-eth/bin/x64sc`` when that is set.
PATCHED_VICE_CANDIDATES = (
    "/opt/homebrew/opt/vice-eth/bin/x64sc",
    "/usr/local/opt/vice-eth/bin/x64sc",
)

#: Directory holding the ``bpf<N>`` nodes; a module attribute for tests.
_BPF_DEV_DIR = "/dev"

#: ``-features`` rows the harness cares about, mapped to field names.
_FEATURE_ROWS = {
    "HAVE_RAWNET": "rawnet",
    "HAVE_PCAP": "pcap",
    "HAVE_TUNTAP": "tuntap",
    BPF_PERMISSIONS_FEATURE: "bpf_permissions",
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

#: A sudoers Digest_Spec as ``sudo -l`` prints it ahead of the command.
_SUDO_DIGEST = re.compile(r"^sha(?:224|256|384|512):\S+$")

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

    ``bpf_permissions`` is the ``RAWNET_BPF_PERMISSIONS`` row: the binary
    accepts a read-write ``/dev/bpf*`` node in place of root.  The scan
    fallback never sets it, so an unexecutable binary is treated as stock.
    """

    rawnet: bool
    pcap: bool
    tuntap: bool
    source: str
    drivers_known: bool
    bpf_permissions: bool = False

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
            bpf_permissions=values.get("bpf_permissions", False),
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


def bpf_node_openable() -> bool:
    """Whether this user can open some ``/dev/bpf*`` node read-write.

    The same walk the patched ``archdep_rawnet_capability()`` makes:
    ``bpf0`` upwards, a busy or denied node does not end it, a missing one
    does.  Each node opened is closed at once.
    """
    for i in range(256):
        try:
            fd = os.open(os.path.join(_BPF_DEV_DIR, f"bpf{i}"), os.O_RDWR)
        except FileNotFoundError:
            return False
        except OSError:
            continue
        os.close(fd)
        return True
    return False


def patched_vice_binary() -> str | None:
    """The ``vice-eth`` x64sc built by ``scripts/build-vice-eth.sh``, if any.

    macOS only.  Existence only: whether it really is the patched build is
    read from its ``-features`` at launch time (:func:`rawnet_capability`).
    """
    if sys.platform != "darwin":
        return None
    prefix = os.environ.get("HOMEBREW_PREFIX", "").strip()
    candidates = ((os.path.join(prefix, "opt", "vice-eth", "bin", "x64sc"),) if prefix else ())
    for path in candidates + PATCHED_VICE_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def usable_patched_vice_binary() -> str | None:
    """:func:`patched_vice_binary`, but only when it would run ethernet as
    this user now: it reports ``RAWNET_BPF_PERMISSIONS`` and a ``/dev/bpf*``
    node opens read-write.  Otherwise ``None``, so a launch falls back to
    the configured binary (and whatever sudo rule names it) rather than
    being steered onto a build this user cannot use.
    """
    path = patched_vice_binary()
    if path is None or not rawnet_capability(as_root=False, binary=path):
        return None
    return path


def patched_vice_lag(path: str) -> tuple[str, str] | None:
    """``(vice-eth version, vice version)`` when the patched build's keg is
    not the version of Homebrew's ``vice`` beside it, else ``None``.

    Read from the opt symlinks (``opt/<name>`` -> ``Cellar/<name>/<version>``),
    so it costs no subprocess; ``None`` too when either is not such a keg.
    This is the run-time half of ``build-vice-eth.sh --check``: it sees a
    ``brew upgrade vice`` that left the patched build behind, not a formula
    revision or a patch change, which only ``--check`` reads.
    """
    def keg_version(x64sc: str, name: str) -> str | None:
        real = os.path.realpath(x64sc)
        parts = real.split(os.sep)
        if len(parts) >= 5 and parts[-5] == "Cellar" and parts[-4] == name:
            return parts[-3]
        return None

    opt_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    eth = keg_version(path, "vice-eth")
    stock = keg_version(os.path.join(opt_dir, "vice", "bin", "x64sc"), "vice")
    if eth is None or stock is None or eth == stock:
        return None
    return eth, stock


def rawnet_capability(*, as_root: bool | None = None, binary: str | None = None) -> bool:
    """What ``archdep_rawnet_capability()`` would return for a child.

    *as_root* says whether the child will run as root; ``None`` asks
    about this process as it stands.  *binary* is the x64sc to be
    launched: a build reporting ``RAWNET_BPF_PERMISSIONS`` (macOS) also
    passes when this user can open a ``/dev/bpf*`` node read-write.
    Without *binary* the answer is stock VICE's.
    """
    running_as_root = os.geteuid() == 0 if as_root is None else as_root
    if running_as_root:
        return True
    if (
        binary is not None
        and sys.platform == "darwin"
        and vice_features(binary).bpf_permissions
        and bpf_node_openable()
    ):
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
    would not cover the argv we intend to launch.  The one pinned argv
    kept is the empty one (``/path ""``, sudoers(5): no arguments at
    all), in ``no_arg_commands``: it covers a helper run bare, as the
    bridge scripts are, and never an x64sc launch.
    """

    all_commands: bool
    commands: frozenset[str]
    no_arg_commands: frozenset[str] = frozenset()

    def allows(self, binary: str, *, no_args: bool = False) -> bool:
        if self.all_commands or binary in self.commands:
            return True
        return no_args and binary in self.no_arg_commands


def parse_sudo_listing(text: str) -> SudoAuthorisation:
    """Extract the NOPASSWD rules from ``sudo -l`` output.

    Only NOPASSWD matters.  A plain ``(ALL) ALL`` line means the user may
    become root *with a password* — useless to an unattended harness, and
    the reason a per-command ``sudo -n -l -- <cmd>`` probe is worthless:
    it exits 0 for anything such a user may run, ``/bin/ls`` included.
    """
    all_commands = False
    commands: set[str] = set()
    no_arg_commands: set[str] = set()
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
            # A digest pin (``sha256:<hex> /path``) precedes the command.
            if parts and _SUDO_DIGEST.match(parts[0]):
                parts.pop(0)
            if not parts or "PASSWD:" in tags:
                continue
            if parts[0] == "ALL":
                all_commands = True
            elif len(parts) == 1 or parts[1:] == ["*"]:
                # No pinned arguments, or the bare wildcard that admits any.
                commands.add(parts[0])
            elif parts[1:] == ['""']:
                no_arg_commands.add(parts[0])
    return SudoAuthorisation(
        all_commands, frozenset(commands), frozenset(no_arg_commands)
    )


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


def sudo_can_run(binary: str, *, no_args: bool = False) -> bool:
    """Whether *binary* can be run as root **without a password**.

    Asks sudo for its rule list and looks for a NOPASSWD entry naming
    this exact path.  Being *permitted* to run it is not enough: an
    unattended launch that stops at a password prompt is a failed launch.
    ``no_args=True`` asks about a run with no arguments, which a rule
    pinned to ``""`` also covers.

    Caveat: a cached sudo timestamp would let a launch through that this
    reports False for.  Refusing with an actionable message is the safe
    direction, and ``VICE_ETHERNET_ALLOW_UNELEVATED=1`` overrides it.
    """
    return sudo_authorisation().allows(binary, no_args=no_args)


# ------------------------------------------------------------ the plan


def _sudoers_entry(binary: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        user = os.environ.get("USER", "<user>")
    return f"{user} ALL=(root) NOPASSWD: {binary}"


def launch_path(binary: str) -> str:
    """*binary* as an absolute path, resolved the way sudo matches it.

    A bare name must be resolved before it is handed to sudo: sudoers
    matches the absolute command path, so a NOPASSWD rule can only ever
    name one, and Linux sudoers commonly set ``secure_path`` (Ubuntu
    does), under which ``sudo -n x64sc`` cannot find a binary that lives
    outside it.  (Stock macOS sets no ``secure_path`` Default --
    ``sudo -n x64sc -features`` exits 0 there -- so on that platform the
    resolution serves the rule match, not the lookup.)

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


#: Shown in the printed remedy in place of a ``-addconfig`` path -- see
#: ``_display_command()``.
_ADDCONFIG_PLACEHOLDER = "<the rc ViceProcess writes at launch>"


def _display_command(interactive: list[str]) -> str:
    """*interactive* rendered for a human to paste, not to exec.

    ``ViceProcess.start()`` writes the ``-addconfig`` rc to a per-launch
    temp file and ``stop()``'s ``_cleanup_tmp_vicerc()`` deletes it the
    moment this refusal unwinds (``start()`` calls ``stop()`` on any
    failure) -- so the literal path in *interactive* already points at a
    file that is gone by the time an operator reads this message.
    Pasting it verbatim fails immediately with "No such file or
    directory", not with the elevation problem being reported. The path
    argument immediately after ``-addconfig`` is replaced with a
    placeholder; everything else, including the rest of the command,
    stays exact.
    """
    rendered = list(interactive)
    for i, arg in enumerate(rendered):
        if arg == "-addconfig" and i + 1 < len(rendered):
            rendered[i + 1] = _ADDCONFIG_PLACEHOLDER
    return shlex.join(rendered)


def _unprivileged_route(binary: str) -> str:
    """The non-root remedy for *binary* on macOS, or ``""`` elsewhere."""
    if sys.platform != "darwin":
        return ""
    bpf_remedy = (
        "Install Wireshark's ChmodBPF (brew install --cask "
        "wireshark-chmodbpf), make sure `id -Gn` lists access_bpf (log in "
        "again after joining), or sudo chmod o+rw /dev/bpf* until the next "
        "reboot.\n"
    )
    if vice_features(binary).bpf_permissions:
        return (
            f"This x64sc reports {BPF_PERMISSIONS_FEATURE} yes, so it would run "
            f"as you, but no /dev/bpf* node opens read-write for this user. "
            + bpf_remedy
        )
    patched = patched_vice_binary()
    if patched is not None and usable_patched_vice_binary() is not None:
        return (
            f"The patched build at {patched} would run ethernet as you, but "
            f"this launch chose a stock x64sc ($VICE_ETHERNET_BIN, "
            f"ethernet_executable or executable). Unset that to use it.\n"
        )
    if patched is not None:
        return (
            f"The patched build at {patched} is installed, but it cannot run "
            f"ethernet as you (no /dev/bpf* node opens read-write for this "
            f"user), so this stock x64sc was chosen instead. " + bpf_remedy
        )
    return (
        "This x64sc is a stock build, which enables ethernet only for root. "
        "To run it as you instead, build the patched VICE with "
        "scripts/build-vice-eth.sh (Homebrew's vice plus one patch; it checks "
        "/dev/bpf* permissions instead of root, and the harness picks it up "
        "automatically).\n"
    )


def _refuse(argv: list[str], binary: str, reason: str) -> ViceElevationRequiredError:
    # The remedy names the *resolved* binary, like ``binary`` and the
    # sudoers line do: sudoers matches the absolute command path (and
    # Linux sudoers commonly set secure_path), and the three must agree
    # so the pasted command is the one the pasted rule authorises.
    interactive = ["sudo", binary] + argv[1:]
    # .command / .argv (below) stay the exact, real argv -- useful to a
    # caller that wants to re-run this launch *programmatically*, right
    # now, before the temp rc is cleaned up. Only the PRINTED remedy
    # substitutes a placeholder for a path that won't outlive this call.
    command = shlex.join(interactive)
    display_command = _display_command(interactive)
    entry = _sudoers_entry(binary)
    addconfig_note = (
        "    (the -addconfig path above is a per-launch temp file VICE "
        "already deleted; the sudoers rule below is the durable, "
        "always-re-runnable fix)\n"
        if display_command != command
        else ""
    )
    message = (
        f"{reason}\n"
        f"VICE selects an ethernet driver only when "
        f"archdep_rawnet_capability() holds (euid 0, or CAP_NET_RAW on "
        f"Linux); without it rawnet_arch_driver stays NULL and x64sc "
        f"SIGSEGVs on the first reset with no log output.\n"
        f"{_unprivileged_route(binary)}"
        f"Run this launch elevated:\n"
        f"    {display_command}\n"
        f"{addconfig_note}"
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
        and not rawnet_capability(binary=binary)
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
