"""ViceProcess — start/stop/wait for a VICE emulator instance.

Provides ``ViceConfig`` (what to launch) and ``ViceProcess`` (context
manager that handles the lifecycle).
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from c64_test_harness.disk import DiskImage


_IS_MACOS = platform.system() == "Darwin"


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
    minimize: bool = True
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

    # Run VICE as root. Required on macOS whenever the pcap ethernet driver
    # is used: even with /dev/bpf* mode 666, the kernel's per-process BPF
    # device allocation refuses to let a non-root process capture on a
    # feth(4) interface, and VICE's error path leaves rawnet_arch_driver
    # NULL so cs8900_activate segfaults.  The harness wraps the launch
    # with ``sudo -n -E`` in that case; the caller must have a passwordless
    # sudoers entry for x64sc (see docs/development.md -> macOS ->
    # Passwordless sudo).  ``None`` means auto-detect: True on Darwin when
    # ethernet is enabled, False otherwise.  Set explicitly to override.
    run_as_root: bool | None = None


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

    def start(self) -> None:
        """Stop any existing process on this instance, then launch VICE."""
        if self._proc is not None:
            self.stop()

        cfg = self.config

        args = [cfg.executable]
        if cfg.prg_path:
            args += ["-autostart", cfg.prg_path]
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
        if cfg.minimize:
            args.append("-minimized")
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

        # Decide whether to wrap with sudo.  On macOS, VICE's pcap driver
        # needs root to open a BPF device for feth(4) capture; running as
        # the user segfaults inside cs8900_activate (NULL rawnet_arch_driver
        # after pcap init fails).  The wrap happens at exec time so the
        # sudo failure mode is clean: if NOPASSWD isn't configured, Popen
        # starts but exits with a "sudo: password is required" error on
        # stderr and a non-zero status, and the caller sees "VICE exited
        # early" through the normal monitor-connect path.
        run_as_root = cfg.run_as_root
        if run_as_root is None:
            run_as_root = sys.platform == "darwin" and cfg.ethernet
        if run_as_root:
            # -E (preserve env) requires a SETENV tag in sudoers which we
            # deliberately do NOT ask for -- it's a privilege expansion.
            # VICE reads $HOME for its config path, but sudo's default
            # env_keep includes HOME, so plain `sudo -n` works.
            args = ["sudo", "-n"] + args
            # Track that we're running under sudo so stop() can send SIGTERM
            # via sudo as well (direct kill of a root child is refused).
            self._is_sudo_child = True
        else:
            self._is_sudo_child = False

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
                        except Exception:
                            pass
            else:
                self._proc.terminate()
                self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
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
