"""ViceProcess — start/stop/wait for a VICE emulator instance.

Provides ``ViceConfig`` (what to launch) and ``ViceProcess`` (context
manager that handles the lifecycle).
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from c64_test_harness.backends.vice_elevation import (
    ViceElevationRequiredError,
    effective_driver_name,
    vice_features,
    ViceEthernetBinaryError,
    ViceEthernetError,
    ViceLaunchPlan,
    plan_vice_launch,
    vice_binary_supports_ethernet,
)

if TYPE_CHECKING:
    from c64_test_harness.disk import DiskImage


_log = logging.getLogger(__name__)

_IS_MACOS = platform.system() == "Darwin"

#: Environment variable naming an ethernet-capable ``x64sc``.
ETHERNET_VICE_BIN_ENV = "VICE_ETHERNET_BIN"

#: First and last address of the primary SID's register window.  A read
#: anywhere in here is only meaningful when VICE's sound core is running
#: -- see :func:`sid_emulation_enabled`.
SID_REGISTER_FIRST = 0xD400
SID_REGISTER_LAST = 0xD41F

#: The two SID registers whose *fallback* value is not a constant:
#: ``$D41B`` (OSC3) and ``$D41C`` (ENV3).  With the sound core off VICE
#: answers both from ``maincpu_clk % 256`` (S ``sid.c:137`` in
#: ``sid_read_off``, and again at ``sid.c:279`` for the ``val < 0``
#: fallback), so a sampling loop reads a clean ramp at its own stride.
#: ``$D419``/``$D41A`` (the paddle ports) answer ``0xff`` and every other
#: register answers ``0``.
SID_CLOCK_LEAK_REGISTERS = (0xD41B, 0xD41C)

# One warning per process is enough: whether the sound core runs is a
# static property of the launch configuration, not of the run.
_warned_sid_unemulated = False


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

    Set ``VICE_ETHERNET_BIN`` to that path, or pass
    ``ViceConfig(ethernet_executable=...)`` directly.  (There is no
    ``HarnessConfig`` / TOML knob for this: nothing maps
    ``HarnessConfig.vice_*`` fields into :class:`ViceConfig`.)  It is
    consulted **only** when ``ViceConfig.ethernet`` is true, so
    non-ethernet runs keep using the ``PATH`` binary.
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
        f"{ETHERNET_VICE_BIN_ENV}=/path/to/x64sc in the environment, or "
        f"pass ViceConfig(ethernet_executable=/path/to/x64sc)."
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


def build_ethernet_rc(cfg: ViceConfig) -> str:
    """The vicerc body that activates the CS8900a for *cfg*.

    Extracted from :meth:`ViceProcess.start` so the resource names can be
    checked against a running VICE rather than against the generated
    text.  Asserting the text is what let ``EthernetIOIF`` /
    ``EthernetIODriver`` survive: neither is a VICE resource in any
    casing, so VICE logged ``Unknown resource`` and ignored them, and
    the settings only ever arrived through the ``-ethernetioif`` /
    ``-ethernetiodriver`` CLI flags that are passed alongside.

    The real names are ``ETHERNET_INTERFACE`` (S ``cs8900io.c:309``) and
    ``ETHERNET_DRIVER`` (S ``rawnetarch.c:146``).  Verified elevated with
    no ethernet CLI flags at all: this rc alone brings up
    ``ETHERNET_DRIVER='pcap'``, ``ETHERNET_INTERFACE='feth0'`` and
    ``ETHERNETCART_ACTIVE=1``, with two BPF peers attached.  The rc is
    therefore sufficient by itself and the CLI flags are redundant, not
    load-bearing -- which is why the old misspellings mattered: any path
    relying on the rc alone was silently unconfigured.

    ``EthernetCartMode`` / ``EthernetCartBase`` are correct as they
    stand: the *resource* table lookup is case-insensitive
    (``util_strcasecmp`` at S ``resources.c:243``, and
    ``resources_calc_hash_key`` lowercases every character).  That is a
    different lookup from the *option* table in ``cmdline.c``, which is
    both case-sensitive and prefix-matching.
    """
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
        rc_lines.append(f'ETHERNET_INTERFACE="{cfg.ethernet_interface}"')
    if cfg.ethernet_driver:
        rc_lines.append(f'ETHERNET_DRIVER="{cfg.ethernet_driver}"')
    if cfg.ethernet_base != 0xDE00:
        rc_lines.append(f"EthernetCartBase={cfg.ethernet_base}")
    rc_lines.append("SaveResourcesOnExit=0")
    rc_lines.append("")
    return "\n".join(rc_lines)


@dataclass
class ViceConfig:
    """Configuration for launching a VICE instance."""

    executable: str = "x64sc"
    prg_path: str = ""
    port: int = 6502
    text_monitor_port: int = 0  # 0 = no text monitor; >0 = enable -remotemonitor on this port
    warp: bool = True
    ntsc: bool = True
    #: Run VICE's sound core.
    #:
    #: **This is not an output switch -- it is the SID emulation switch.**
    #: ``False`` emits ``+sound``, which stops the sound core being
    #: clocked at all, and reSID with it.  Reads of ``$D400-$D41F`` then
    #: come from VICE's sound-off fallback (S ``sid.c:130-142``,
    #: ``sid.c:276-284``):
    #:
    #: - ``$D41B`` / ``$D41C`` (OSC3 / ENV3) return ``maincpu_clk % 256``
    #: - ``$D419`` / ``$D41A`` (paddles) return ``0xff``
    #: - everything else returns ``0``
    #:
    #: Nothing raises, and a sampling loop reading OSC3 gets a clean ramp
    #: at its own stride -- which looks like a working oscillator, not
    #: like a broken one.  Every SID measurement taken against a
    #: ``sound=False`` instance is a measurement of the CPU clock.
    #:
    #: ``-soundwarpmode 1`` (which this module always passes) does not
    #: help: it only decides whether an *enabled* sound core keeps running
    #: under warp.
    #:
    #: The default stays ``False`` because turning it on opens a host
    #: audio device, and the harness is expected to run headless.  The
    #: healthy headless configuration is ``sound=True`` with
    #: ``sounddev="wav"`` writing to a scratch file -- a file writer
    #: always drains the buffer, so it needs no audio hardware.  See
    #: :func:`headless_sid_config`.
    #:
    #: ``sounddev="dummy"`` is **not** a substitute.  Sound is enabled but
    #: nothing drains the buffer, so the SID stops advancing and reads
    #: return real-but-arbitrarily-stale register state.  It fails
    #: *silently and differently*: there is no telltale ramp, and a
    #: freeze-and-read health check cannot separate it from a healthy SID
    #: holding ``TEST``.  Detecting it needs a released oscillator and an
    #: assertion that the value *moves*.
    sound: bool = False
    monitor: bool = True
    # Headless launch.  ``-console`` runs the full emulation (binary
    # monitor, VIC/SID state, -exitscreenshot) without creating the GTK
    # window, so VICE never activates and steals focus on macOS.  With
    # ``console=False`` the window is created and ``minimize`` applies.
    console: bool = True
    minimize: bool = True  # only meaningful when console=False
    #: Load the operator's ``~/.config/vice/vicerc``.
    #:
    #: Default **False**, which emits ``-default``.  A launch that reads
    #: an ambient vicerc is not reproducible -- consumers of this harness
    #: assert on machine state (turbo, REU, video standard, drive type),
    #: and any of it can be overridden by whatever the operator last
    #: clicked in VICE's settings dialog.
    #:
    #: It is also a crash, for a specific class of vicerc: when the file's
    #: ``ConfigVersion`` is absent, empty, or does not match the running
    #: VICE, ``check_resource_file_version`` calls ``ui_error()``
    #: (S ``resources.c:1281,1291``), and in console mode that reaches GTK
    #: state which was never initialised -- SIGSEGV on both 3.10 builds
    #: measured here.  A vicerc written by the same VICE version is fine;
    #: one left by an older VICE, or hand-written, is not.  Windowed
    #: launches survive either way, which is why it went unnoticed while
    #: ``-console`` was positionally broken and every launch was really
    #: windowed.
    #:
    #: Set True only to deliberately test config inheritance.
    load_user_config: bool = False
    extra_args: list[str] = field(default_factory=list)
    disk_image: DiskImage | None = None
    drive_unit: int = 8

    # Sound recording
    sounddev: str = ""  # e.g. "wav", "pulse"
    soundarg: str = ""  # e.g. WAV output path
    soundrate: int = 44100  # sample rate
    soundoutput: int = 1  # 1=mono, 2=stereo
    #: ``SoundVolume`` (``-soundvolume``, 0..100; S ``sound.c:729,789``).
    #: ``None`` leaves VICE's factory volume.
    #:
    #: ``0`` stops a test run playing warp-speed SID noise and is safe for
    #: *register-domain* measurement: the volume is applied after
    #: ``sound_machine_calculate_samples()`` has already clocked reSID
    #: (S ``sound.c:1432`` then ``sound.c:1441-1449``), so the chip state
    #: an ``$D41B`` read observes is unaffected.
    #:
    #: It is **destructive for audio-domain measurement**: at volume 0
    #: ``amp`` is 0 and VICE ``memset``s the sample buffer to zero
    #: (S ``sound.c:1446``) before it reaches the play *or* record device.
    #: A ``sounddev="wav"`` capture taken at volume 0 is a well-formed
    #: file full of silence.  :func:`render_wav` rejects it for that
    #: reason.
    soundvolume: int | None = None

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


# --------------------------------------------------------------------------- #
# SID emulation / sound configuration guards (issue #193)                     #
# --------------------------------------------------------------------------- #

def sid_emulation_enabled(config: ViceConfig) -> bool:
    """True when this launch will actually clock reSID.

    VICE's sound core is what advances the SID.  ``sound=False`` emits
    ``+sound`` and the core never runs, so ``$D400-$D41F`` answers from
    the sound-off fallback in ``sid.c`` rather than from a chip.  A
    configured ``sounddev`` forces ``-sound`` on regardless of the
    ``sound`` flag (see ``ViceProcess.start``), so either one is enough.

    This is a *configuration* predicate: it says the sound core will be
    enabled, not that it is healthy.  ``sounddev="dummy"`` enables the
    core and still yields stale registers because nothing drains the
    buffer -- see :func:`sid_sound_device_drains`.

    :param config: The launch configuration.
    :returns: True when reSID will be clocked.
    """
    return bool(config.sound or config.sounddev)


#: Sound devices that never drain the sample buffer.  With one of these
#: the sound core is enabled but stalls, and SID reads return real state
#: frozen at an arbitrary past moment -- a failure with no ramp to notice
#: it by.  ``"dummy"`` is VICE's null *playback* device.
NON_DRAINING_SOUND_DEVICES = ("dummy",)


def sid_sound_device_drains(config: ViceConfig) -> bool:
    """True when the configured sound device will drain the buffer.

    A file writer (``"wav"``, ``"iff"``, ``"aiff"``, ``"voc"``) always
    drains and needs no audio hardware, which is what makes it the
    headless-safe choice.  ``"dummy"`` does not drain.  An empty
    ``sounddev`` means VICE picks its platform default, which drains when
    a working audio device exists -- unknowable from here, so it counts
    as draining.

    :param config: The launch configuration.
    :returns: False only for a device known not to drain.
    """
    return config.sounddev not in NON_DRAINING_SOUND_DEVICES


def warn_if_sid_reads_unemulated(
    config: ViceConfig, addr: int, length: int = 1
) -> bool:
    """Warn when a read of *addr* would hit VICE's sound-off fallback.

    Call this from a read path that is about to fetch SID registers from
    a VICE instance whose configuration is known.  It logs at WARNING and
    never raises: a read of ``$D41B`` on a ``sound=False`` instance
    returns ``maincpu_clk % 256``, which is data, just not SID data.

    :param config: The launch configuration of the instance being read.
    :param addr: First address of the read.
    :param length: Number of bytes read.
    :returns: True when a warning was emitted.
    """
    if length < 1:
        return False
    if sid_emulation_enabled(config):
        return False
    last = addr + length - 1
    if last < SID_REGISTER_FIRST or addr > SID_REGISTER_LAST:
        return False
    leaks = [
        f"${reg:04X}"
        for reg in SID_CLOCK_LEAK_REGISTERS
        if addr <= reg <= last
    ]
    _log.warning(
        "Read of $%04X..$%04X on a VICE instance launched with sound "
        "disabled: reSID is not clocked, so these are sound-off fallback "
        "values from sid.c, not SID state%s. Launch with sound=True and "
        "sounddev='wav' (see headless_sid_config).",
        addr,
        last,
        f" -- {', '.join(leaks)} will read back maincpu_clk %% 256" if leaks else "",
    )
    return True


def _warn_about_sound_configuration(config: ViceConfig) -> None:
    """Log the sound-configuration traps that fail silently.

    Called once per :meth:`ViceProcess.start`.  Three distinct failures,
    none of which raises anything on its own:

    - sound disabled: reSID never clocked (issue #193).  Warned once per
      process, since it is the harness default and a per-launch line
      would drown a parallel run.
    - a non-draining sound device: the core runs but stalls.
    - warp with a sound device configured: VICE discards every sample
      (issue #196).
    """
    global _warned_sid_unemulated

    if not sid_emulation_enabled(config):
        if not _warned_sid_unemulated:
            _warned_sid_unemulated = True
            _log.warning(
                "VICE launched with sound disabled (+sound): reSID is not "
                "clocked, so $D400-$D41F reads are sid.c fallbacks -- "
                "$D41B/$D41C return maincpu_clk %% 256, which looks like a "
                "working oscillator. Use headless_sid_config() for SID "
                "measurement. (Logged once per process.)"
            )
        return

    if not sid_sound_device_drains(config):
        _log.warning(
            "VICE launched with sounddev=%r, which never drains the sample "
            "buffer: the SID stops advancing and register reads return real "
            "but arbitrarily stale state. Use sounddev='wav' instead.",
            config.sounddev,
        )

    # ``soundvolume == 0`` says the caller only wants the device for its
    # draining side effect (see :func:`headless_sid_config`) -- the audio
    # is already deliberately silence, so warp discarding it is not news.
    if config.warp and config.sounddev and config.soundvolume != 0:
        _log.warning(
            "VICE launched with warp on and sounddev=%r: sound_flush() "
            "discards the sample buffer under warp (S sound.c:1528, and the "
            "device-write loop at sound.c:1573 is skipped outright), so the "
            "capture will be well-formed and empty. Set warp=False for "
            "audio capture.",
            config.sounddev,
        )


def headless_sid_config(
    wav_path: str | Path | None = None,
    *,
    base: ViceConfig | None = None,
    silent: bool = True,
    soundrate: int = 44100,
    soundoutput: int = 1,
) -> ViceConfig:
    """A :class:`ViceConfig` whose SID is actually emulated, headlessly.

    The only configuration that clocks reSID without depending on the
    host having a working audio device: sound on, with the ``wav`` sound
    device draining the buffer into a file.

    Use this for *register-domain* work -- reading ``$D41B``/``$D41C``,
    oscillator and envelope trajectories.  The WAV it writes is a
    byproduct and, with the default ``silent=True``, is silence (see
    :attr:`ViceConfig.soundvolume`).  For *audio-domain* work use
    :func:`render_wav`, which keeps the volume alone and forces warp off.

    :param wav_path: Where the drained audio goes.  ``None`` allocates a
        file in the system temp directory; the caller owns it.
    :param base: Optional config to inherit ``executable``, ``prg_path``,
        ``port``, ``ntsc``, ``monitor``, ``console`` and ``extra_args``
        from.
    :param silent: Pass ``-soundvolume 0``.  Safe for register reads,
        and it stops warped runs screeching.  Set False when the WAV
        contents matter.
    :param soundrate: ``-soundrate``.
    :param soundoutput: 1 mono, 2 stereo.
    :returns: A new :class:`ViceConfig`.
    """
    src = base or ViceConfig()
    if wav_path is None:
        fd, wav_path = tempfile.mkstemp(prefix="c64-sid-", suffix=".wav")
        os.close(fd)
    return ViceConfig(
        executable=src.executable,
        prg_path=src.prg_path,
        port=src.port,
        text_monitor_port=src.text_monitor_port,
        warp=src.warp,
        ntsc=src.ntsc,
        sound=True,
        monitor=src.monitor,
        console=src.console,
        minimize=src.minimize,
        load_user_config=src.load_user_config,
        extra_args=list(src.extra_args),
        disk_image=src.disk_image,
        drive_unit=src.drive_unit,
        sounddev="wav",
        soundarg=str(wav_path),
        soundrate=soundrate,
        soundoutput=soundoutput,
        soundvolume=0 if silent else None,
        env=src.env,
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
        # True when the child was launched via plain ``sudo -n`` (no -E:
        # that would need a SETENV tag in sudoers; see plan_vice_launch)
        # so it runs as root.  stop() uses this flag to route SIGTERM /
        # SIGKILL through ``sudo -n kill`` instead of Popen.terminate(),
        # which on macOS cannot signal a root-owned child from an
        # unprivileged parent.
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

        _warn_about_sound_configuration(cfg)

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
        if not cfg.load_user_config:
            # Also an early-scan flag: it sets ``loadconfig = false``
            # (S main.c:285-291) before resources_load() runs at main.c:390.
            early_args.append("-default")
        if cfg.console:
            early_args.append("-console")
        if cfg.seed is not None:
            early_args += ["-seed", str(cfg.seed)]

        args = [resolve_vice_executable(cfg)] + early_args
        if cfg.prg_path:
            args += ["-autostart", cfg.prg_path]

        # ------------------------------------------------------------------
        # Resources pinned explicitly rather than inherited.
        #
        # -default (above) stops the vicerc being read, which leaves VICE's
        # factory defaults -- and several of those are not what the harness
        # wants.  Pin them here so a run means the same thing on every
        # machine.  cfg.extra_args is appended after this block, and VICE
        # takes the last occurrence of a resource flag, so a caller can
        # still override any of them.
        # ------------------------------------------------------------------

        # Factory is 2/Disk (S autostart-prg.h:45).  1/Inject is what the
        # harness has always wanted, but it was set only on macOS, so Linux
        # silently took the disk path.
        if "-autostartprgmode" not in cfg.extra_args:
            args += ["-autostartprgmode", "1"]

        # Factory is 1 (S autostart.c:413): autostart ran warped even when
        # the caller explicitly asked for warp=False.
        args.append("-autostart-warp" if cfg.warp else "+autostart-warp")

        # Factory is 0, but an operator's vicerc may set it -- in which case
        # our runs would rewrite their settings file on exit.
        args.append("+saveres")

        # Determinism knobs, named explicitly so they stay put if a future
        # VICE changes its factory values.
        #
        # -jamaction 0 is DIALOG, deliberately *not* the factory 1
        # (CONTINUE).  VICE emits the 0x61 JAM event only from
        # monitor_binary_ui_jam_dialog, which machine_jam reaches only
        # when jam_action == 0 (S machine.c:131-139); with the binary
        # monitor connected the "dialog" is routed to the monitor and
        # the machine stops, so wait_for_stopped can report the jam.
        # Under CONTINUE machine_jam returns JAM_NONE (S machine.c:145-150,
        # actions[0] == -1, falling through to :162) and the core's JAM()
        # default branch is a bare CLK++ with no PC advance
        # (S maincpu.c:607-628; opcode $02 reaches it via JAM_02(),
        # 6510core.c:1242-1249): the 6510 halts in place, silently, and
        # that report is unreachable.
        #
        # DIALOG is safe only while a monitor client can take the dialog:
        # monitor_is_binary() is connected_socket != NULL
        # (S monitor_binary.c:2110-2113), and with nothing connected
        # machine.c:140's `else if (!console_mode)` opens the GTK jam
        # dialog and the emulator blocks on it.  A launch with no binary
        # monitor therefore keeps the factory CONTINUE.
        args += ["-jamaction", "0" if cfg.monitor else "1"]
        args += ["-speed", "100"]        # no ambient speed limit
        # Keep an *enabled* sound core clocked under warp, so SID register
        # reads stay meaningful in a warped run (S sound.c:1390: the early
        # return that would stop generating samples is taken only when
        # SoundSpeedAdjustment-on-warp is 0).
        #
        # It does NOT make audio capture work under warp, and an earlier
        # revision of this comment claiming "or render_wav() records
        # silence" had the mechanism wrong.  sound_flush() discards the
        # buffer outright when warp is on and no record device is
        # configured (S sound.c:1528), and even with -soundrecdev set the
        # device-write loop is `while (!warp_mode_enabled)` (S
        # sound.c:1573-1613) so it never executes -- the samples are
        # dropped by `snddata.bufptr -= nr` either way.  Warp must be OFF
        # for any VICE-side audio capture; render_wav() forces warp=False.
        args += ["-soundwarpmode", "1"]
        if cfg.disk_image is None or cfg.drive_unit != 8:
            args += ["-drive8type", "1542"]
        if cfg.warp:
            args.append("-warp")
        # ntsc=False used to emit nothing at all and inherit
        # MachineVideoStandard.  PAL/NTSC changes cycle counts and TOD
        # rates, which tod_timer.py calibrates against.
        args.append("-ntsc" if cfg.ntsc else "-pal")
        if cfg.monitor:
            args += ["-binarymonitor", "-binarymonitoraddress",
                     f"ip4://127.0.0.1:{cfg.port}"]
        if cfg.text_monitor_port > 0:
            args += ["-remotemonitor", "-remotemonitoraddress",
                     f"ip4://127.0.0.1:{cfg.text_monitor_port}"]
        if cfg.sounddev:
            # Force sound on when a sound device is configured.  The
            # comment here has always said so; the flag was never emitted.
            args.append("-sound")
            args += ["-sounddev", cfg.sounddev]
            if cfg.soundarg:
                args += ["-soundarg", cfg.soundarg]
            args += ["-soundrate", str(cfg.soundrate)]
            args += ["-soundoutput", str(cfg.soundoutput)]
        else:
            # Factory Sound is 1 (S sound.c:721), so sound=True emitting
            # nothing was an inherit, not a default.
            args.append("-sound" if cfg.sound else "+sound")
        if cfg.soundvolume is not None:
            if not isinstance(cfg.soundvolume, int) or isinstance(
                cfg.soundvolume, bool
            ):
                raise ValueError("soundvolume must be an int in 0..100 or None")
            if not 0 <= cfg.soundvolume <= 100:
                raise ValueError(
                    f"soundvolume must be in 0..100 (got {cfg.soundvolume})"
                )
            args += ["-soundvolume", str(cfg.soundvolume)]
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
            # The CS8900a is activated through a temporary vicerc passed
            # with ``-addconfig``.  That rc is sufficient on its own: it
            # names the real resources (``ETHERNETCART_ACTIVE``,
            # ``ETHERNET_INTERFACE``, ``ETHERNET_DRIVER``) and, launched
            # elevated with no ethernet CLI flags at all, brings the cart
            # up with two BPF peers attached -- see build_ethernet_rc().
            #
            # Two earlier accounts in this comment were wrong and are
            # recorded here so they are not re-derived:
            #
            # * "``-ethernetcart`` / ``-tfe`` / ``-rrnet`` are rejected at
            #   parse time."  False: all three are registered
            #   (S ``ethernetcart.c:434-451``).  Passed *unelevated* they
            #   SIGSEGV instead -- the cart activates with
            #   ``rawnet_arch_driver`` NULL and ``rawnet_arch_pre_reset``
            #   dereferences it (S ``rawnetarch.c:251``; rc=139 on 6 of 6
            #   flag x build combinations).  docs/vice_upstream_bugs.md § 2.
            #
            # * "The rc alone activates the cart but never attaches a TAP,
            #   so ``-ethernetioif`` / ``-ethernetiodriver`` must follow it,
            #   and must follow it in that order."  That was an artefact
            #   of the rc misspelling the interface and driver resources
            #   (``EthernetIOIF`` / ``EthernetIODriver`` are not VICE
            #   resources in any casing; VICE logged ``Unknown resource``
            #   and ignored them), so the interface and driver only ever
            #   arrived via the CLI flags.  There is no VICE ordering
            #   quirk to work around.
            #
            # The CLI flags are still emitted, as belt-and-braces, and
            # ``-addconfig`` is kept ahead of them so a run means the same
            # thing it always has; neither is load-bearing.
            fd, path = tempfile.mkstemp(prefix="vice_eth_", suffix=".rc")
            with os.fdopen(fd, "w") as f:
                f.write(build_ethernet_rc(cfg))
            self._tmp_vicerc = path

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
