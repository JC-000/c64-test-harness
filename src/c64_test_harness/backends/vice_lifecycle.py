"""ViceProcess — start/stop/wait for a VICE emulator instance.

Provides ``ViceConfig`` (what to launch) and ``ViceProcess`` (context
manager that handles the lifecycle).
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from c64_test_harness.backends.vice_elevation import (
    ViceElevationRequiredError,
    effective_driver_name,
    vice_features,
    ViceEthernetBinaryError,
    ViceEthernetError,
    ViceLaunchPlan,
    plan_vice_launch,
    rawnet_capability,
    vice_binary_supports_ethernet,
)

if TYPE_CHECKING:
    from c64_test_harness.disk import DiskImage


_IS_MACOS = platform.system() == "Darwin"

#: Environment variable naming an ethernet-capable ``x64sc``.
ETHERNET_VICE_BIN_ENV = "VICE_ETHERNET_BIN"


def ethernet_vice_binary() -> str:
    """Path to an ethernet-capable ``x64sc``, or ``""`` when unconfigured.

    **Override, not a requirement -- normally leave this unset.**  The
    ``PATH`` binary is the intended one: Homebrew's VICE 3.10 bottle
    reports ``HAVE_RAWNET yes`` / ``HAVE_PCAP yes`` to ``-features`` and
    links libpcap, so ethernet work needs no separately-built companion.
    (It was long cited as an ethernet-less build, issue #144; that was a
    misreading of the crash an *unelevated* launch produces -- see
    ``vice_elevation``.)

    Set it only for a bench that must launch a different binary.
    :func:`resolve_vice_executable` probes whichever binary it resolves
    (see :func:`~c64_test_harness.backends.vice_elevation.vice_features`)
    and raises rather than falling back to one that cannot do ethernet,
    which is how a suite ends up asserting against emulated CS8900a
    registers while no host packet moves.

    Set ``VICE_ETHERNET_BIN`` to that path, or configure
    ``HarnessConfig.vice_ethernet_executable`` (TOML
    ``[vice] ethernet_executable``).  It is consulted **only** when
    ``ViceConfig.ethernet`` is true, so non-ethernet runs keep using the
    ``PATH`` binary.
    """
    return os.environ.get(ETHERNET_VICE_BIN_ENV, "").strip()


def resolve_vice_executable(cfg: ViceConfig) -> str:
    """The ``x64sc`` binary *cfg* should actually launch.

    Prefers :attr:`ViceConfig.ethernet_executable` when the ethernet cart
    is in play, so a bench can keep a stock ``x64sc`` on ``PATH`` for
    ordinary tests and an ethernet-enabled build for the bridge suite.

    With ``cfg.ethernet`` set, the chosen binary is *checked* for
    raw-network support and a build without it raises
    :class:`ViceEthernetBinaryError`.  It used to fall back to
    ``cfg.executable`` silently, which is how an ethernet suite ends up
    asserting against emulated CS8900a registers while no host packet
    ever moves (issue #144).
    """
    if not cfg.ethernet:
        return cfg.executable

    candidate = cfg.ethernet_executable or cfg.executable
    source = (
        f"ViceConfig.ethernet_executable / ${ETHERNET_VICE_BIN_ENV}"
        if cfg.ethernet_executable
        else "ViceConfig.executable"
    )
    resolved = shutil.which(candidate)
    if resolved is None and os.path.isfile(candidate):
        resolved = candidate
    hint = (
        f"Point the harness at an ethernet-capable x64sc: set "
        f"${ETHERNET_VICE_BIN_ENV}=/path/to/x64sc, or TOML "
        f"``[vice] ethernet_executable`` "
        f"(HarnessConfig.vice_ethernet_executable)."
    )
    if resolved is None:
        raise ViceEthernetBinaryError(
            f"ethernet=True but no x64sc found at {candidate!r} "
            f"(from {source}). {hint}"
        )
    features = vice_features(resolved)
    if not features.rawnet:
        raise ViceEthernetBinaryError(
            f"ethernet=True but {resolved!r} (from {source}) reports "
            f"HAVE_RAWNET no — it was built without raw-network support, "
            f"so the CS8900a would be pure emulation with no host traffic. "
            f"{hint}"
        )
    # A driver the build lacks is the same NULL-driver SIGSEGV by another
    # route, so refuse it here.  Only when the binary actually told us
    # which drivers it has: the image-scan fallback cannot know.
    driver = effective_driver_name(cfg.ethernet_driver)
    if features.drivers_known and not features.has_driver(driver):
        have = ", ".join(d for d in ("pcap", "tuntap") if features.has_driver(d))
        raise ViceEthernetBinaryError(
            f"ethernet_driver={driver!r} but {resolved!r} was built "
            f"without it (-features says HAVE_{driver.upper()} no; "
            f"this build has: {have or 'no drivers'}). VICE would leave "
            f"rawnet_arch_driver NULL and SIGSEGV on reset."
        )
    # Return the caller's spelling, not the resolved path: sudoers and
    # PATH lookups downstream expect what was configured.
    return candidate


def _find_pid_on_port_linux(port: int) -> int | None:
    """Linux: find the PID listening on *port* via /proc/net/tcp + /proc/*/fd."""
    hex_port = f"{port:04X}"
    try:
        with open("/proc/net/tcp") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                local = parts[1]
                if local.endswith(f":{hex_port}") and parts[3] == "0A":
                    # 0A = LISTEN state
                    inode = parts[9] if len(parts) > 9 else None
                    if inode is None:
                        continue
                    # Find PID via /proc/*/fd
                    for pid_dir in os.listdir("/proc"):
                        if not pid_dir.isdigit():
                            continue
                        fd_dir = f"/proc/{pid_dir}/fd"
                        try:
                            for fd in os.listdir(fd_dir):
                                link = os.readlink(f"{fd_dir}/{fd}")
                                if f"socket:[{inode}]" in link:
                                    return int(pid_dir)
                        except (PermissionError, FileNotFoundError):
                            continue
    except FileNotFoundError:
        pass  # /proc not mounted
    return None


def _find_pid_on_port_macos(port: int) -> int | None:
    """macOS: find the PID listening on *port* via ``lsof``.

    ``lsof -nP -iTCP:<port> -sTCP:LISTEN -t`` prints one PID per line.
    Returns the first, or ``None`` if there is no listener / lsof is
    unavailable / the call fails.
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(line)
        except ValueError:
            return None
    return None


def _find_pid_on_port(port: int) -> int | None:
    """Find the PID of the process listening on *port*.

    Linux: uses ``/proc/net/tcp`` + ``/proc/*/fd``.
    macOS: uses ``lsof -nP -iTCP:<port> -sTCP:LISTEN -t``.
    Returns ``None`` on other platforms or when no listener is found.
    """
    if _IS_MACOS:
        return _find_pid_on_port_macos(port)
    return _find_pid_on_port_linux(port)


@dataclass
class ViceConfig:
    """Configuration for launching a VICE instance."""

    executable: str = "x64sc"
    prg_path: str = ""
    port: int = 6502
    text_monitor_port: int = 0  # 0 = no text monitor; >0 = enable -remotemonitor on this port
    warp: bool = True
    ntsc: bool = True
    sound: bool = False
    monitor: bool = True
    # Headless launch.  ``-console`` runs the full emulation (binary
    # monitor, VIC/SID state, -exitscreenshot) without creating the GTK
    # window, so VICE never activates and steals focus on macOS.  With
    # ``console=False`` the window is created and ``minimize`` applies.
    console: bool = True
    minimize: bool = True  # only meaningful when console=False
    extra_args: list[str] = field(default_factory=list)
    disk_image: DiskImage | None = None
    drive_unit: int = 8

    # Sound recording
    sounddev: str = ""  # e.g. "wav", "pulse"
    soundarg: str = ""  # e.g. WAV output path
    soundrate: int = 44100  # sample rate
    soundoutput: int = 1  # 1=mono, 2=stereo

    # Cycle limiting (batch mode)
    limit_cycles: int = 0  # if >0, VICE exits after this many cycles

    # Process environment (None = inherit parent)
    env: dict[str, str] | None = None

    # Ethernet / RR-Net
    ethernet: bool = False
    ethernet_mode: str = "rrnet"  # "rrnet" or "tfe"
    ethernet_interface: str = ""  # host interface (e.g. "tap-c64")
    ethernet_driver: str = ""  # "tuntap" or "pcap" (empty = VICE default)
    ethernet_base: int = 0xDE00  # I/O base address
    ethernet_mac: bytes = b""  # 6-byte MAC (empty = VICE default)
    #: Ethernet-capable x64sc, used instead of ``executable`` when
    #: ``ethernet`` is true.  Defaults from ``$VICE_ETHERNET_BIN``; see
    #: :func:`ethernet_vice_binary`.  Empty means "just use ``executable``".
    ethernet_executable: str = field(default_factory=ethernet_vice_binary)

    # Event replay / determinism / audio capture.
    #
    # Every field here maps to a flag verified present in VICE 3.10's
    # cmdline tables.  An earlier revision carried two more --
    # ``load_snapshot`` and ``event_recording_start`` -- that mapped to
    # flags VICE has never had: there is no ``-loadsnapshot`` anywhere in
    # the source (load a ``.vsf`` through the monitor's
    # ``undump_snapshot()`` instead), and the entire ``-event*`` table is
    # five options (S ``event.c:1279-1301``) with no way to start a
    # recording -- ``event_record_start()`` (S ``event.c:758``) is
    # reachable only from the UI and the monitor.

    #: ``EventStartMode``: 0 file save, 1 file load, 2 reset, 3 playback
    #: (S ``event.c:1293-1295``).
    event_snapshot_mode: int | None = None
    #: ``EventSnapshotDir`` -- where event recordings are written.  VICE
    #: normalises the value to a trailing separator.
    event_snapshot_dir: str | None = None
    #: ``EventImageInclude`` -- whether disk images are included in an
    #: event recording.  ``None`` leaves VICE's factory default, which is
    #: already **enabled** (S ``event.c:1248``), so only ``False`` changes
    #: anything.
    event_image_include: bool | None = None
    seed: int | None = None
    #: ``SoundRecordDeviceName`` (S ``sound.c:806``), e.g. ``"wav"``.
    sound_record_driver: str | None = None
    #: ``SoundRecordDeviceArg`` (S ``sound.c:809``) -- the recording
    #: driver's parameter, which for ``wav`` is the output path.
    sound_record_file: str | None = None
    exit_screenshot: str | None = None

    # Run VICE as root.  VICE selects a pcap ethernet driver only when
    # ``archdep_rawnet_capability()`` holds -- ``geteuid() == 0``, plus a
    # Linux-only ``CAP_NET_RAW`` branch.  It never inspects ``/dev/bpf*``:
    # a rig that ran ``chmod o+rw /dev/bpf*`` has changed nothing VICE
    # looks at, and an unelevated ethernet launch leaves
    # ``rawnet_arch_driver`` NULL and SIGSEGVs on the first reset with no
    # log output.
    #
    # Earlier revisions of this comment claimed the opposite in both
    # directions -- first that the kernel refuses non-root capture on
    # feth(4) at mode 666, then that open ``/dev/bpf*`` nodes make
    # elevation unnecessary.  Both were readings of the same
    # ``cs8900_activate`` segfault; the cause is the NULL driver, and it
    # is the euid that decides.  Confirmed live: with ``/dev/bpf0`` at
    # ``crw----rw-`` and uid 501, VICE still refuses the pcap driver.
    #
    # ``None`` means auto-detect (ethernet + a root-gated driver + no
    # capability).  Set explicitly to override -- but note that
    # ``run_as_root=False`` on a launch that needs root is refused rather
    # than crashed; see ``vice_elevation.plan_vice_launch``.  A
    # passwordless sudoers entry is only needed when elevation actually
    # fires, and it must name the *exact* x64sc path being launched (see
    # docs/development.md -> macOS -> Passwordless sudo).
    run_as_root: bool | None = None


def _should_run_as_root(cfg: ViceConfig) -> bool:
    """Whether this launch is *intended* to run as root.

    An explicit :attr:`ViceConfig.run_as_root` is honoured verbatim.
    ``None`` auto-detects: elevate exactly when the ethernet cart is in
    play and VICE would otherwise have no raw-network capability for the
    driver in use.

    This reports intent only.  :func:`plan_vice_launch` decides how the
    exec actually happens, and refuses rather than launching a VICE that
    would SIGSEGV.
    """
    if cfg.run_as_root is not None:
        return cfg.run_as_root
    from c64_test_harness.backends.vice_elevation import driver_requires_root

    return (
        cfg.ethernet
        and driver_requires_root(cfg.ethernet_driver)
        and not rawnet_capability(as_root=False)
    )


class ViceProcess:
    """Context manager for a VICE emulator process.

    Usage::

        config = ViceConfig(prg_path="game.prg")
        with ViceProcess(config) as vice:
            transport = BinaryViceTransport(port=config.port)
            ...
    """

    def __init__(self, config: ViceConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        # Temp vicerc used to activate CS8900a ethernet (see start()).
        # Cleaned up in stop().
        self._tmp_vicerc: str | None = None
        # True when the child was launched via ``sudo -n -E`` so it runs as
        # root.  stop() uses this flag to route SIGTERM / SIGKILL through
        # ``sudo -n kill`` instead of Popen.terminate(), which on macOS
        # cannot signal a root-owned child from an unprivileged parent.
        self._is_sudo_child: bool = False

    def __enter__(self) -> ViceProcess:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def is_sudo_child(self) -> bool:
        """True when the launch was wrapped with ``sudo -n`` (macOS
        ethernet path).  In that case :attr:`pid` is the sudo wrapper,
        not x64sc itself — use :meth:`resolve_vice_pid` for the actual
        emulator PID."""
        return self._is_sudo_child

    def resolve_vice_pid(self) -> int | None:
        """PID of the actual x64sc process.

        For a plain launch this is :attr:`pid`.  For a sudo-wrapped
        launch (macOS ethernet), :attr:`pid` is the sudo wrapper, so the
        x64sc child is resolved via the process table (``ps``, matching
        the ``pgrep -P <sudo_pid> x64sc`` pattern).  Returns ``None``
        when the process is not running or the child cannot be found.
        """
        if not self._is_sudo_child:
            return self.pid
        return self._find_x64sc_child_pid()

    def start(self) -> None:
        """Stop any existing process on this instance, then launch VICE."""
        if self._proc is not None:
            self.stop()

        cfg = self.config

        if cfg.event_snapshot_mode is not None and not 0 <= cfg.event_snapshot_mode <= 3:
            raise ValueError(
                "event_snapshot_mode must be 0, 1, 2, or 3 "
                f"(got {cfg.event_snapshot_mode})"
            )

        # VICE scans argv for a handful of flags *before* it initialises the
        # UI or loads the config file (S main.c:267-303), and that scan
        # ``break``s at the first argument it does not recognise.  Anything
        # it handles must therefore appear before every other flag.
        #
        # ``-console`` is the one that bites: ``ui_init_with_args``
        # (S main.c:385) is gated on the ``console_mode`` flag that only
        # this early scan sets.  The late handler registered via
        # initcmdline.c:307 fires at main.c:421, long after the window
        # exists.  Emitted after ``-autostart``/``-warp`` the flag is
        # silently ineffective on macOS: VICE opens the window anyway.
        #
        # ``-seed`` has the identical shape -- ``lib_rand_seed()`` is called
        # from the early scan and nowhere else, so a late ``-seed`` is
        # parsed as a resource option and never seeds the RNG.
        early_args: list[str] = []
        if cfg.console:
            early_args.append("-console")
        if cfg.seed is not None:
            early_args += ["-seed", str(cfg.seed)]

        args = [resolve_vice_executable(cfg)] + early_args
        if cfg.prg_path:
            args += ["-autostart", cfg.prg_path]
            if sys.platform == "darwin" and "-autostartprgmode" not in cfg.extra_args:
                args += ["-autostartprgmode", "1"]
        if cfg.warp:
            args.append("-warp")
        if cfg.ntsc:
            args.append("-ntsc")
        if cfg.monitor:
            args += ["-binarymonitor", "-binarymonitoraddress",
                     f"ip4://127.0.0.1:{cfg.port}"]
        if cfg.text_monitor_port > 0:
            args += ["-remotemonitor", "-remotemonitoraddress",
                     f"ip4://127.0.0.1:{cfg.text_monitor_port}"]
        if cfg.sounddev:
            # Force sound on when a sound device is configured
            args += ["-sounddev", cfg.sounddev]
            if cfg.soundarg:
                args += ["-soundarg", cfg.soundarg]
            args += ["-soundrate", str(cfg.soundrate)]
            args += ["-soundoutput", str(cfg.soundoutput)]
        elif not cfg.sound:
            args.append("+sound")
        if cfg.limit_cycles > 0:
            args += ["-limitcycles", str(cfg.limit_cycles)]
        if not cfg.console and cfg.minimize:
            args.append("-minimized")
        if cfg.event_snapshot_mode is not None:
            args += ["-eventstartmode", str(cfg.event_snapshot_mode)]
        if cfg.event_snapshot_dir is not None:
            args += ["-eventsnapshotdir", cfg.event_snapshot_dir]
        if cfg.event_image_include is not None:
            # A +flag disables in VICE's cmdline convention.
            args.append("-eventimageinc" if cfg.event_image_include else "+eventimageinc")
        if cfg.sound_record_driver is not None:
            args += ["-soundrecdev", cfg.sound_record_driver]
        if cfg.sound_record_file is not None:
            args += ["-soundrecarg", cfg.sound_record_file]
        if cfg.exit_screenshot is not None:
            args += ["-exitscreenshot", cfg.exit_screenshot]
        args += cfg.extra_args

        if cfg.ethernet:
            # VICE 3.10 ethernet activation has TWO quirks that must both
            # be worked around:
            #
            # 1. The ``-ethernetcart`` / ``-tfe`` / ``-rrnet`` CLI flags
            #    appear in ``-help`` but are rejected at parse time
            #    ("Option '-ethernetcart' not valid.").
            #
            # 2. If ``ETHERNETCART_ACTIVE`` is only set via a vicerc file
            #    (``-addconfig`` / ``-config``) WITHOUT also supplying
            #    ``-ethernetioif`` / ``-ethernetiodriver`` on the command
            #    line, VICE sets the resource to 1 and exposes the
            #    CS8900a Product ID to the C64 — BUT never attaches a TAP
            #    file descriptor on the host side, so frames never leave
            #    the emulator (carrier stays 0, tcpdump sees nothing).
            #    Conversely, if you only supply the CLI interface/driver
            #    flags WITHOUT also activating the cart via addconfig,
            #    the TAP gets attached (carrier=1) but the cart stays
            #    disabled.
            #
            # The working combination, verified empirically with
            # ``scripts/verify_vice_ethernet.py``, is:
            #
            #     -addconfig <tmp.rc>      (must come FIRST)
            #     -ethernetioif <iface>
            #     -ethernetiodriver <drv>
            #
            # In this order, VICE both attaches the TAP and activates
            # the cart, and the C64 can TX/RX real frames.  If the
            # ``-addconfig`` comes AFTER the CLI iface flags, the
            # ETHERNETCART_ACTIVE value in the rc file is NOT honoured
            # (reads back as 0).
            mode = 1 if cfg.ethernet_mode == "rrnet" else 0
            rc_lines = [
                "[Version]",
                "ConfigVersion=3.10",
                "",
                "[C64SC]",
                "ETHERNETCART_ACTIVE=1",
                f"EthernetCartMode={mode}",
            ]
            if cfg.ethernet_interface:
                rc_lines.append(f'EthernetIOIF="{cfg.ethernet_interface}"')
            if cfg.ethernet_driver:
                rc_lines.append(f'EthernetIODriver="{cfg.ethernet_driver}"')
            if cfg.ethernet_base != 0xDE00:
                rc_lines.append(f"EthernetCartBase={cfg.ethernet_base}")
            rc_lines.append("SaveResourcesOnExit=0")
            rc_lines.append("")

            fd, path = tempfile.mkstemp(prefix="vice_eth_", suffix=".rc")
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(rc_lines))
            self._tmp_vicerc = path

            # ORDER MATTERS: -addconfig must come BEFORE the interface/
            # driver CLI flags.  See note above.
            args += ["-addconfig", path]
            if cfg.ethernet_interface:
                args += ["-ethernetioif", cfg.ethernet_interface]
            if cfg.ethernet_driver:
                args += ["-ethernetiodriver", cfg.ethernet_driver]

        if cfg.disk_image is not None:
            args += [
                f"-{cfg.drive_unit}", str(cfg.disk_image.path),
                f"-drive{cfg.drive_unit}type", str(cfg.disk_image.drive_type),
            ]

        popen_kwargs: dict[str, object] = dict(
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if cfg.env is not None:
            popen_kwargs["env"] = cfg.env

        # Decide how to exec: plain, or wrapped in ``sudo -n`` when the
        # ethernet cart needs raw-network capability this process lacks.
        # An unelevated ethernet launch does not degrade -- VICE leaves
        # ``rawnet_arch_driver`` NULL and SIGSEGVs on the first reset with
        # no log output -- so plan_vice_launch() refuses it and reports
        # the command to run instead.  See vice_elevation.py.
        try:
            plan = plan_vice_launch(cfg, args)
        except ViceEthernetError:
            self._cleanup_tmp_vicerc()
            raise
        args = plan.argv
        # Track sudo wrapping so stop() routes signals through sudo: an
        # unprivileged parent cannot signal a root-owned child.
        self._is_sudo_child = plan.sudo_wrapped

        self._proc = subprocess.Popen(args, **popen_kwargs)  # type: ignore[arg-type]

    def wait_for_exit(self, timeout: float = 60.0) -> int:
        """Wait for the VICE process to exit on its own.

        Returns the exit code.  Useful with ``-limitcycles`` where VICE
        terminates itself after a fixed number of CPU cycles.

        Raises ``subprocess.TimeoutExpired`` if the process does not exit
        within *timeout* seconds.  On timeout the process is killed and
        the internal handle is cleared.
        """
        if self._proc is None:
            raise RuntimeError("VICE process has not been started")
        try:
            self._proc.wait(timeout=timeout)
            return self._proc.returncode
        except subprocess.TimeoutExpired:
            self.stop()
            raise
        finally:
            # Clear internal handle so stop() becomes a no-op
            self._proc = None

    def stop(self) -> None:
        """Terminate VICE: SIGTERM → wait 5s → SIGKILL fallback.

        When VICE is running as root (macOS ethernet path), signals from
        an unprivileged parent are dropped.  In that case we route the
        terminate / kill via ``sudo -n kill``; if the sudo invocation
        itself is the Popen target, signalling sudo forwards to x64sc
        (sudo's default signal-forwarding behaviour on POSIX), so we try
        that first and only escalate to ``sudo -n kill -9 <x64sc-pid>``
        if sudo itself refuses to exit.
        """
        if self._proc is None:
            self._cleanup_tmp_vicerc()
            return

        try:
            if self._is_sudo_child:
                # sudo forwards SIGTERM to its child when it runs in the
                # foreground.  SIGTERM → sudo → x64sc (as root).
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # sudo / x64sc didn't exit; find the root child and kill
                    # it with sudo, then give Popen a moment to reap.
                    child_pid = self._find_x64sc_child_pid()
                    if child_pid is not None:
                        subprocess.run(
                            ["sudo", "-n", "kill", "-9", str(child_pid)],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # Last resort: kill the sudo wrapper too.  Works
                        # because sudo itself runs as our UID (it elevates
                        # only its exec'd child).
                        try:
                            self._proc.kill()
                            # Reap the wrapper: without wait() the killed
                            # process lingers as a zombie, and BSD ps
                            # keeps the comm name on zombies, confusing
                            # cleanup helpers (macOS trap #3).
                            self._proc.wait(timeout=3)
                        except Exception:
                            pass
            else:
                self._proc.terminate()
                self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
                # Reap after SIGKILL so we don't leave a zombie behind
                # (see comment on the sudo path above).
                self._proc.wait(timeout=3)
            except Exception:
                pass
        self._proc = None
        self._cleanup_tmp_vicerc()

    def _find_x64sc_child_pid(self) -> int | None:
        """Find the x64sc process spawned under our sudo wrapper.

        Only meaningful when ``self._is_sudo_child`` is True.  Returns the
        PID of an x64sc process whose parent is our Popen child (the sudo
        wrapper), or None if no such process is found.  Uses ``ps -axo
        pid,ppid,comm`` which is available on both Linux and macOS.
        """
        if self._proc is None:
            return None
        sudo_pid = self._proc.pid
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,comm="],
                capture_output=True,
                check=False,
                text=True,
            ).stdout
        except OSError:
            return None
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            comm = parts[2]
            # On macOS `comm` may be the full path; match on basename.
            name = os.path.basename(comm)
            if ppid == sudo_pid and name == "x64sc":
                return pid
        return None

    def _cleanup_tmp_vicerc(self) -> None:
        if self._tmp_vicerc is not None:
            try:
                os.unlink(self._tmp_vicerc)
            except OSError:
                pass
            self._tmp_vicerc = None

    @staticmethod
    def get_listener_pid(port: int) -> int | None:
        """Return the PID of the process listening on *port*, or None.

        Cross-platform:
            Linux -- parses ``/proc/net/tcp`` + ``/proc/*/fd``.
            macOS -- shells out to ``lsof -nP -iTCP:<port> -sTCP:LISTEN -t``.
        Returns ``None`` if the port has no listener (or on platforms
        where neither path is available).
        """
        return _find_pid_on_port(port)

    @staticmethod
    def kill_on_port(port: int) -> bool:
        """Kill the process listening on *port*.

        Resolves the listener PID via :meth:`get_listener_pid` (works on
        Linux and macOS) and sends SIGTERM. Returns True if a process
        was found and signalled, False otherwise. This is an opt-in
        replacement for the old ``pkill -f`` approach.
        """
        import os
        import signal

        pid = _find_pid_on_port(port)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                return True
            except OSError:
                pass
        return False
