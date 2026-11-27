#!/usr/bin/env python3
"""Volatile reload of an Ultimate 64 (Elite) application over JTAG, through a USB-Blaster served by ``jtagd``.

Nothing here writes flash. ``nios2-download -g`` pauses the Nios II, writes the application ELF into RAM and starts
it; the flashed application comes back with a power cycle. This is the way back for a U64E whose application hangs
(REST dark, FPGA still configured), and a way to run a development build without flashing it (#583).

The Quartus tools (``jtagd``, ``jtagconfig``, ``nios2-download``) run where the Blaster is attached, which is reached
over ssh: on this kind of bench an x86 Linux VM that owns the adapter (USB id 09fb:6001). Everything about that
route is local configuration, in the device's table of bench.toml (_bench_bindings.py), never in this public repo::

    [devices.<unique_id>]
    jtag_blaster_ssh = "<ssh destination>"        # required: where jtagd runs (an ssh_config Host alias is best)
    jtag_blaster_cable = "USB-Blaster"            # required: text the one cable's name in `jtagconfig` must contain
    jtag_blaster_ssh_port = 2200                  # optional
    jtag_blaster_ssh_identity = "<key file>"      # optional
    jtag_blaster_env = ". <quartus env script>"   # optional: shell text run before every remote command (PATH)
    jtag_blaster_jtagd_start = "<command>"        # optional: starts jtagd when it is not running

These keys are a capability of their own. ``jtag_scan_only = true`` and the ``jtag_adapter_*`` keys describe the
esp-jtag adapter that jtag_load.py drives, and are neither read nor changed here: a device may be scan-only for that
adapter and bound for a Blaster reload in the same table, and jtag_load.py still refuses to load it.

Gates, in order. Before any lock or command: the binding; ``ssh`` and ``scp`` present; the ELF copied and the COPY's
SHA-256 equal to the caller's; the copy is a little-endian Nios II ELF32 whose segments sit in the application's
region, with the entry inside one, and which embeds the expected git hash (its first 7 digits). Then, under the device's DeviceLock
(``allow_nested``; a verified lock shell's hold is accepted) and the route's own lock: /v1/info must answer as the
bound unique_id and a U64 product, or, with ``dark=True``, may go unanswered three times in a row. Preflight on the
far side, where everything that can fail must fail before the Nios is paused: the tools exist; no ``nios2-download``
or ``jtagconfig`` is still running there (never killed); the upload's SHA-256 there; the SREC
conversion's SHA-256 when one is pinned; no stray ``nios2-gdb-server``/``nios2-elf-gdb`` (killed only on request);
``jtagd`` running; ``jtagconfig -n`` shows exactly one cable, the bound one, with exactly one 5CE (Cyclone V E)
device and a Nios II node. Then one ``nios2-download -g``, never retried. Afterwards /v1/info must answer with the
bound unique_id, a U64 product and the expected git hash; any other device answering is an identity failure.

What the result proves (#570): a hash that changed to the expected one proves the new image started
(``restart_proof: hash-change``); a device that reported no hash before has not changed it. A box that left three
requests in a row unanswered before and answers afterwards as the bound device with the expected hash was restarted
(``no-answer-before``). Otherwise (the hash already the expected one, or none reported) only three consecutive
unanswered requests after the download are accepted (``rest-drop``: an outage of roughly 3 to 18 s, which a network
fault of that length would also produce); without them the result is ``ok-unproven``, exit 3: the tool returned 0
and the device answers, which it also did before.

    blaster_load_u64.py load HOST ELF --sha256 H --expect-hash G [--srec-sha256 S] [--dark] [--kill-stray-gdb]
                             [--preflight-only] [--dry-run] [--record FILE]

Exit 0: loaded and the restart proven. 3: loaded, came back, restart unproven. 2: refused or failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import jtag_load as jl  # noqa: E402

PRODUCTS = ("Ultimate 64 Elite", "Ultimate 64")
FPGA_PART = "5CE"               # Cyclone V E, as jtagconfig names the U64's FPGA
NIOS_NODE = "Nios II"
EM_ALTERA_NIOS2 = 113
# The application is linked at 0 and must sit below this; taken from the lanes' loader (issue #583), not measured.
APP_REGION_END = 1 << 24
# The bracket keeps the pattern from matching the shell that carries it.
STRAY_GDB = "[n]ios2-(gdb-server|elf-gdb)"
# The JTAG tools themselves: a download whose ssh timed out here can still run there, after the locks were released.
BUSY_JTAG = "[n]ios2-download|[j]tagconfig"
WORKDIR_RE = r"/tmp/blaster_load\.[A-Za-z0-9]{6}"      # what `mktemp -d /tmp/blaster_load.XXXXXX` answers, nothing else
MISSES_FOR_OUTAGE = 3                                  # consecutive unanswered /v1/info requests that count as "down"
REQUIRED_KEYS = ("jtag_blaster_ssh", "jtag_blaster_cable")
STOP = ("STOP: the Nios may be paused or the application not started, and after a timeout nios2-download may still "
        "be running on the far side (the next run refuses while it is). Nothing is retried; a power cycle brings "
        "the flashed application back")


def run_command(argv, timeout):
    """Run one external command; (returncode, stdout, stderr). TimeoutError (an OSError, like a missing program)
    when it outlives *timeout* seconds."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"{argv[0]} did not return within {timeout:.0f} s") from None
    return p.returncode, p.stdout, p.stderr


def blaster_binding(uid: str) -> dict:
    """The Blaster route bound to *uid* in bench.toml; refuse (JtagLoadError "binding") without both required keys.

    jtag_scan_only and the jtag_adapter_* keys are not consulted: they belong to jtag_load.py's esp-jtag adapter."""
    t = jl.device_table(uid)
    missing = [k for k in REQUIRED_KEYS if k not in t]
    if missing:
        raise jl.JtagLoadError("binding", f"no USB-Blaster route is bound to device {uid} (bench.toml needs "
                               f"{' and '.join(REQUIRED_KEYS)} in its table; missing {missing})")
    ssh, port = t["jtag_blaster_ssh"], t.get("jtag_blaster_ssh_port")
    key = re.sub(r"[^a-z0-9.]+", "-", (ssh if port is None else f"{ssh}-{port}").lower()).strip("-")
    return {"ssh": ssh, "port": port, "identity": t.get("jtag_blaster_ssh_identity"),
            "cable": t["jtag_blaster_cable"], "env": t.get("jtag_blaster_env"),
            "jtagd_start": t.get("jtag_blaster_jtagd_start"), "lock": "jtag-blaster-" + key}


def bound_u64(host: str) -> tuple[str, dict]:
    """(unique_id, Blaster binding) of *host*; refuse a host outside devices.toml or without the binding."""
    key = jl.normalize_device_host(host)
    if not key.startswith("uid-"):
        raise jl.JtagLoadError("binding", f"{host} is not in devices.toml, so it has no unique_id to bind a "
                               "USB-Blaster route to")
    uid = key[4:].upper()
    return uid, blaster_binding(uid)


def check_elf(data: bytes, expect_git_hash: str) -> None:
    """Refuse anything but a Nios II application ELF for the expected build, before anything is touched."""
    def bad(why: str):
        raise jl.JtagLoadError("image", f"not a U64 Nios II application ELF: {why}")
    if len(data) < 52 or data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        bad("not a little-endian ELF32")
    machine = struct.unpack_from("<H", data, 18)[0]
    if machine != EM_ALTERA_NIOS2:
        bad(f"e_machine is {machine}, not Nios II ({EM_ALTERA_NIOS2})")
    entry, phoff = struct.unpack_from("<II", data, 24)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    if phentsize < 32 or phoff + phnum * phentsize > len(data):
        bad("its program headers run past the end of the file")
    loads = []
    for i in range(phnum):
        p_type, offset, vaddr, paddr, filesz, memsz = struct.unpack_from("<6I", data, phoff + i * phentsize)
        if p_type == 1 and filesz:
            if offset + filesz > len(data):
                bad("a loadable segment runs past the end of the file (truncated?)")
            loads.append((vaddr, paddr, memsz))
    if not loads:
        bad("no loadable segment")
    if any(paddr + memsz > APP_REGION_END for _vaddr, paddr, memsz in loads):
        bad(f"a segment loads above {APP_REGION_END:#x}")
    if not any(vaddr <= entry < vaddr + memsz for vaddr, _paddr, memsz in loads):
        bad(f"entry {entry:#x} is outside the loaded segments")
    if expect_git_hash[:7].encode() not in data:
        raise jl.JtagLoadError("hash", f"the ELF does not embed git hash {expect_git_hash[:7]}: it is another build "
                               "than --expect-hash names, so the check after the load could never pass")


def parse_jtagconfig(text: str) -> list[dict]:
    """`jtagconfig -n` output as [{"name", "devices": [(idcode, part)], "nodes": [text]}], one per cable.

    Strict: the layout here was written from the tool's documented output, not captured on a bench, so any non-blank
    line that is not a cable, a device or a node is a refusal (JtagLoadError "chain") rather than skipped. A line
    this does not know could be a second device."""
    cables: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(\d+)\)\s+(\S.*?)\s*$", line)
        if m:
            cables.append({"name": m.group(2), "devices": [], "nodes": []})
            continue
        node = re.match(r"^\s+Node\s+[0-9A-Fa-f]{8}\s*(.*?)\s*$", line)
        device = re.match(r"^\s+([0-9A-Fa-f]{8})\s+(\S.*?)\s*$", line)
        if not cables or not (node or device):
            raise jl.JtagLoadError("chain", f"jtagconfig printed a line this script does not know ({line.strip()!r}); "
                                   "board NOT touched")
        if node:
            cables[-1]["nodes"].append(node.group(1))
        else:
            cables[-1]["devices"].append((device.group(1).lower(), device.group(2)))
    return cables


def check_chain(text: str, cable: str) -> dict:
    """The one cable `jtagconfig -n` shows, if it is the bound one with one U64 FPGA and a Nios II node; else refuse.

    Exactly one cable, because the download names none: nios2-download then takes the only one there is."""
    cables = parse_jtagconfig(text)
    shown = text.strip()[-400:]
    if len(cables) != 1:
        raise jl.JtagLoadError("chain", f"jtagconfig shows {len(cables)} cables, not exactly one; board NOT touched: "
                               f"{shown!r}")
    c = cables[0]
    if cable not in c["name"]:
        raise jl.JtagLoadError("chain", f"the cable is {c['name']!r}, not the bound {cable!r}; board NOT touched")
    if len(c["devices"]) != 1 or FPGA_PART not in c["devices"][0][1]:
        raise jl.JtagLoadError("chain", f"cable {c['name']!r} does not show exactly one {FPGA_PART} device (is the "
                               f"U64 on and the cable seated?); board NOT touched: {shown!r}")
    if not any(NIOS_NODE in n for n in c["nodes"]):
        raise jl.JtagLoadError("chain", f"cable {c['name']!r} shows no {NIOS_NODE} node; board NOT touched: {shown!r}")
    return c


def remote_commands(workdir: str) -> dict:
    """The far-side shell commands, by step. One table, so the dry run prints what a load would send."""
    elf = f"{workdir}/app.elf"
    return {
        "tools": ("for t in nios2-download jtagconfig sha256sum pgrep; do command -v $t >/dev/null || "
                  "{ echo missing: $t; exit 127; }; done"),
        "busy": f"ps -eo pid=,args= | grep -E '{BUSY_JTAG}'",
        "mktemp": "mktemp -d /tmp/blaster_load.XXXXXX",
        "sha": f"sha256sum {elf}",
        "srec": f"nios2-elf-objcopy {elf} -O srec {elf}.srec && sha256sum {elf}.srec",
        "stray": f"ps -eo pid=,args= | grep -E '{STRAY_GDB}'",
        "kill": f"pkill -f '{STRAY_GDB}'; sleep 1",
        "jtagd": "pgrep -x jtagd",
        "chain": "jtagconfig -n",
        "download": f"nios2-download -g {elf}",
        "cleanup": f"rm -rf {workdir}",
    }


class _Remote:
    """ssh and scp to the bound destination, through the injected runner."""

    def __init__(self, binding: dict, runner):
        self.b, self.runner = binding, runner

    def _options(self, port_flag: str) -> list:
        opts = ["-o", "BatchMode=yes"]
        if self.b["port"] is not None:
            opts += [port_flag, str(self.b["port"])]
        if self.b["identity"]:
            opts += ["-i", str(Path(self.b["identity"]).expanduser())]
        return opts

    def ssh_argv(self, command: str) -> list:
        env = self.b["env"]             # in the same shell, its own output discarded: answers are parsed
        return ["ssh", *self._options("-p"), self.b["ssh"],
                f"{{ {env}\n}} >/dev/null\n{command}" if env else command]

    def scp_argv(self, src, dst: str) -> list:
        return ["scp", *self._options("-P"), str(src), f"{self.b['ssh']}:{dst}"]

    def run(self, command: str, timeout: float = 60):
        return self.runner(self.ssh_argv(command), timeout)

    def step(self, what: str, command: str, timeout: float = 60, ok=(0,)):
        """A preflight step: (rc, stdout). A timeout or an unexpected rc (ssh itself fails with 255) is a refusal."""
        try:
            rc, out, err = self.run(command, timeout)
        except OSError as e:                # a timeout too: TimeoutError is an OSError
            raise jl.JtagLoadError("remote", f"{what}: {e}; board NOT touched") from None
        if rc not in ok:
            raise jl.JtagLoadError("remote", f"{what}: exit {rc} from {self.b['ssh']}: "
                                   f"{(err or out).strip()[-300:]!r}; board NOT touched")
        return rc, out

    def put(self, src, dst: str, timeout: float = 300) -> None:
        try:
            rc, out, err = self.runner(self.scp_argv(src, dst), timeout)
        except OSError as e:
            raise jl.JtagLoadError("remote", f"upload: {e}; board NOT touched") from None
        if rc != 0:
            raise jl.JtagLoadError("remote", f"upload: scp exit {rc}: {(err or out).strip()[-300:]!r}; board NOT "
                                   "touched")


def _last_line(out: str) -> str:
    """The answer is the last line a command printed; anything before it is somebody's banner."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _hash_answer(out: str) -> str:
    """The hash on a `sha256sum` answer line."""
    words = _last_line(out).split()
    return words[0].lower() if words else ""


def preflight(remote: _Remote, binding: dict, local_elf: Path, sha256: str, srec_sha256, kill_stray_gdb: bool,
              state: dict, sleep, log) -> dict:
    """Everything on the far side that can fail, before the Nios is paused. Returns the cable from jtagconfig."""
    rc, out = remote.step("tools", remote_commands("")["tools"], ok=(0, 127))
    if rc != 0:
        raise jl.JtagLoadError("setup", f"{binding['ssh']} lacks a tool this load needs ({out.strip()!r}); bind "
                               "jtag_blaster_env if they are only missing from PATH; board NOT touched")
    rc, out = remote.step("running JTAG tools", remote_commands("")["busy"], ok=(0, 1))
    if rc == 0:
        raise jl.JtagLoadError("remote", f"a JTAG tool is already running on {binding['ssh']} "
                               f"({out.strip()[-200:]!r}): an earlier download may not have ended. It is not "
                               "killed here; board NOT touched")
    _rc, out = remote.step("workdir", remote_commands("")["mktemp"])
    workdir = _last_line(out)
    if not re.fullmatch(WORKDIR_RE, workdir):       # it is later the argument of rm -rf
        raise jl.JtagLoadError("remote", f"mktemp answered {workdir!r}, not a directory of this script's template; "
                               "board NOT touched")
    state["workdir"] = workdir
    cmd = remote_commands(workdir)
    remote.put(local_elf, f"{workdir}/app.elf")
    got = _hash_answer(remote.step("hash of the upload", cmd["sha"])[1])
    if got != sha256:
        raise jl.JtagLoadError("hash", f"the upload's SHA-256 on {binding['ssh']} is {got!r}, not the pinned "
                               f"{sha256}; board NOT touched")
    if srec_sha256:
        got = _hash_answer(remote.step("SREC conversion", cmd["srec"], timeout=120)[1])
        if got != srec_sha256.lower():
            raise jl.JtagLoadError("hash", f"the SREC conversion on {binding['ssh']} hashes to {got!r}, not the "
                                   f"pinned {srec_sha256.lower()}; board NOT touched")
    rc, out = remote.step("stray debuggers", cmd["stray"], ok=(0, 1))
    if rc == 0 and kill_stray_gdb:
        log(f"killing stray Nios debuggers on {binding['ssh']}: {out.strip()!r}")
        remote.step("kill stray debuggers", cmd["kill"], ok=(0, 1))
        rc, out = remote.step("stray debuggers", cmd["stray"], ok=(0, 1))
    if rc == 0:
        raise jl.JtagLoadError("remote", f"a Nios debugger holds the JTAG node on {binding['ssh']} "
                               f"({out.strip()[-200:]!r}); pass --kill-stray-gdb if it is yours to kill; board NOT "
                               "touched")
    rc, _out = remote.step("jtagd", cmd["jtagd"], ok=(0, 1))
    if rc != 0 and binding["jtagd_start"]:
        log(f"starting jtagd on {binding['ssh']}")
        remote.step("jtagd start", binding["jtagd_start"])
        sleep(2.0)
        rc, _out = remote.step("jtagd", cmd["jtagd"], ok=(0, 1))
    if rc != 0:
        raise jl.JtagLoadError("remote", f"jtagd is not running on {binding['ssh']}"
                               + ("" if binding["jtagd_start"] else " (start it, or bind jtag_blaster_jtagd_start)")
                               + "; board NOT touched")
    _rc, out = remote.step("jtagconfig", cmd["chain"], timeout=120)
    return check_chain(out, binding["cable"])


def watch_for_drop(host: str, uid: str, window: float, client_factory, sleep, clock) -> bool:
    """True once /v1/info went unanswered MISSES_FOR_OUTAGE times in a row (then, if time allows, answered again)
    within *window* seconds. An answer in between starts the count again: scattered misses are packet loss.

    Every answer is identity-checked. A request waits up to the client's timeout (5 s) and they are 1 s apart, so
    the outage this sees lasted roughly 3 to 18 s. It is evidence of a restart, the only one a load has when the
    hash cannot change (#570); a network outage of that length looks the same."""
    end, misses, dropped = clock() + window, 0, False
    while clock() < end:
        info = jl.try_info(host, client_factory)
        if info is None:
            misses += 1
            dropped = dropped or misses >= MISSES_FOR_OUTAGE
        else:
            jl.check_identity(info, uid, PRODUCTS)
            misses = 0
            if dropped:
                break
        sleep(1.0)
    return dropped


def info_before(host: str, uid: str, dark: bool, client_factory, sleep) -> dict | None:
    """The device's /v1/info before the load, identity-checked; None only for a box established as dark.

    Without *dark* one unanswered request is a refusal. With it, the box counts as dark only after
    MISSES_FOR_OUTAGE unanswered requests in a row, the criterion of watch_for_drop: a healthy box that dropped one
    request must not come out of this as "did not answer before, answers now, so it restarted". Any answer on the
    way is checked like every other."""
    for attempt in range(MISSES_FOR_OUTAGE):
        pre = jl.try_info(host, client_factory)
        if pre is not None:
            jl.check_identity(pre, uid, PRODUCTS)
            return pre
        if not dark:
            raise jl.JtagLoadError("network", f"{host} does not answer /v1/info, so it cannot be identified before "
                                   "the load; if the box is dark and this is its recovery, pass --dark")
        if attempt + 1 < MISSES_FOR_OUTAGE:
            sleep(1.0)
    return None


def hash_changes(pre: dict | None, expect: str) -> bool:
    """True when *pre* reports a git hash and it is another one than *expect*: only then can the hash after the load
    prove a restart. A device that reported none (or something that is no hash) has not changed its hash."""
    reported = str((pre or {}).get("git_commit_hash") or "").lower()
    return re.fullmatch(r"[0-9a-f]{7,40}", reported) is not None and not jl.hash_matches(reported, expect)


def blaster_load(host: str, elf, *, sha256: str, expect_git_hash: str, srec_sha256: str | None = None,
                 dark: bool = False, kill_stray_gdb: bool = False, preflight_only: bool = False,
                 dry_run: bool = False, lock_timeout: float = 600, download_timeout: float = 300,
                 net_timeout: float = 120, drop_window: float = 90, record_path=None, runner=run_command,
                 which=shutil.which, lock_factory=jl.DeviceLock, client_factory=jl._client, sleep=None, clock=None,
                 log=jl._log) -> dict:
    """Reload the bound U64's application from *elf* over the bound USB-Blaster route; return a record.

    dark=True: the box need not answer /v1/info before the load. It counts as dark after three unanswered requests
    in a row; if it answers, its identity is checked and the load goes on as for any answering box. The identity of
    a dark box is confirmed only by what answers afterwards. See the module docstring for the gates and for what
    ``restart_proof`` means."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    uid, binding = bound_u64(host)
    expect = jl.check_git_hash(expect_git_hash)
    want = str(sha256 or "").lower()          # an absent or short pin can equal no file's hash: refused below
    if srec_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(srec_sha256)):
        raise jl.JtagLoadError("hash", f"--srec-sha256 {srec_sha256!r} is not a SHA-256")
    rec = {"t0": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": host, "uid": uid, "action": "blaster_load",
           "elf": str(elf), "sha256": want, "expect_git_hash": expect, "dark": dark, "dry_run": dry_run}
    remote = _Remote(binding, runner)
    state: dict = {}
    try:
        with tempfile.TemporaryDirectory(prefix="blaster_load-") as td:
            copy = Path(td) / "app.elf"
            shutil.copyfile(elf, copy)
            data = copy.read_bytes()
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                raise jl.JtagLoadError("hash", f"{elf}: SHA-256 {got} is not the pinned {want}")
            check_elf(data, expect)
            missing = [tool for tool in ("ssh", "scp") if not which(tool)]
            if missing:
                raise jl.JtagLoadError("setup", f"{' and '.join(missing)} not found on PATH: the USB-Blaster route "
                                       "is reached over ssh")
            if dry_run:
                cmd = remote_commands("<workdir>")
                steps = ["tools", "busy", "mktemp", "sha"] + (["srec"] if srec_sha256 else []) + ["stray", "jtagd", "chain"]
                steps += [] if preflight_only else ["download"]
                rec["plan"] = ([f"DeviceLock({host}, allow_nested) then DeviceLock({binding['lock']})",
                                f"GET /v1/info: unique_id {uid}, product in {list(PRODUCTS)}"
                                + (" (may not answer: dark)" if dark else "")]
                               + [" ".join(remote.ssh_argv(cmd[s])) if s != "sha" else
                                  " ".join(remote.scp_argv("<pinned copy of the ELF>", "<workdir>/app.elf")) + " ; "
                                  + " ".join(remote.ssh_argv(cmd[s])) for s in steps]
                               + ([] if preflight_only else
                                  [f"GET /v1/info until unique_id {uid} reports git hash {expect}"])
                               + [" ".join(remote.ssh_argv(cmd["cleanup"]))])
                rec["result"] = "dry-run ok"
                return rec
            with jl._Locked(host, {"lock": binding["lock"]}, lock_timeout, lock_factory):
                pre = info_before(host, uid, dark, client_factory, sleep)
                rec["pre_info"] = pre
                try:
                    cable = preflight(remote, binding, copy, want, srec_sha256, kill_stray_gdb, state, sleep, log)
                    rec["cable"], rec["chain"] = cable["name"], cable["devices"]
                    if preflight_only:
                        rec["result"] = "preflight ok"
                        return rec
                    log(f"nios2-download {Path(elf).name} into {uid} ({host}) through {cable['name']}")
                    t0 = clock()
                    try:
                        rc, out, err = remote.run(remote_commands(state["workdir"])["download"], download_timeout)
                    except OSError as e:            # the runner's timeout included
                        raise jl.JtagLoadError("download", f"{e}. {STOP}") from None
                    rec["download_rc"], rec["download_output"] = rc, (out + err)[-2000:]
                    if rc != 0:
                        raise jl.JtagLoadError("download", f"nios2-download exit {rc}: {(err or out).strip()[-300:]!r}. "
                                               f"{STOP}")
                    changes = hash_changes(pre, expect)
                    # An answering box whose hash cannot be seen to change needs the outage as its evidence.
                    watch = pre is not None and not changes
                    dropped = watch_for_drop(host, uid, drop_window, client_factory, sleep, clock) if watch else None
                    rec["rest_drop_seen"] = dropped
                    try:
                        rec["info"] = jl.wait_info(host, uid, expect, net_timeout, client_factory, sleep, clock,
                                                   PRODUCTS)
                    except jl.JtagLoadError as e:
                        if e.stage != "network":
                            raise
                        raise jl.JtagLoadError("network", f"{str(e).split('] ', 1)[-1]}. {STOP}") from None
                    rec["up_s"] = round(clock() - t0, 1)
                    rec["restart_proof"] = ("no-answer-before" if pre is None else "hash-change" if changes
                                            else "rest-drop" if dropped else None)
                    rec["result"] = "ok"
                    if rec["restart_proof"] is None:
                        rec["result"] = "ok-unproven"
                        rec["unproven"] = (f"the device reported {pre.get('git_commit_hash')!r} before the load, "
                                           f"which cannot be seen to change to {expect}, and /v1/info never went "
                                           f"unanswered {MISSES_FOR_OUTAGE} times in a row: nios2-download returned "
                                           "0, but a restart is not proven (#570)")
                    log(f"up: {rec['info'].get('git_commit_hash')} {rec['info'].get('product')}, restart proof: "
                        f"{rec['restart_proof']}")
                    return rec
                finally:
                    if state.get("workdir"):
                        try:
                            rc, out, err = remote.run(remote_commands(state["workdir"])["cleanup"], 60)
                            if rc != 0:
                                rec["cleanup_error"] = f"exit {rc}: {(err or out).strip()[-200:]}"
                        except Exception as e:          # the load's own outcome is what matters; say so and go on
                            rec["cleanup_error"] = repr(e)
    except Exception as e:
        rec["result"] = "error"
        rec["error"] = str(e)
        raise
    finally:
        if record_path:
            with open(record_path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("host")
    p.add_argument("elf")
    p.add_argument("--sha256", required=True, help="SHA-256 of the ELF; refused before anything on a mismatch")
    p.add_argument("--expect-hash", required=True,
                   help="git_commit_hash /v1/info must report afterwards (7+ hex digits); the ELF must embed it")
    p.add_argument("--srec-sha256", default=None,
                   help="SHA-256 the far side's nios2-elf-objcopy SREC conversion of the ELF must have")
    p.add_argument("--dark", action="store_true",
                   help="the box may not answer /v1/info before the load; identity is confirmed afterwards")
    p.add_argument("--kill-stray-gdb", action="store_true",
                   help="kill nios2-gdb-server/nios2-elf-gdb on the far side instead of refusing")
    p.add_argument("--preflight-only", action="store_true", help="every check up to, not including, the download")
    p.add_argument("--dry-run", action="store_true", help="local checks only; prints the plan, sends nothing")
    p.add_argument("--lock-timeout", type=float, default=600)
    p.add_argument("--download-timeout", type=float, default=300)
    p.add_argument("--net-timeout", type=float, default=120)
    p.add_argument("--drop-window", type=float, default=90,
                   help="same-hash load: seconds to watch for the REST outage that proves a restart")
    p.add_argument("--record", default=None, help="append a JSON line per load here")
    a = ap.parse_args(argv)
    try:
        out = blaster_load(a.host, a.elf, sha256=a.sha256, expect_git_hash=a.expect_hash, srec_sha256=a.srec_sha256,
                           dark=a.dark, kill_stray_gdb=a.kill_stray_gdb, preflight_only=a.preflight_only,
                           dry_run=a.dry_run, lock_timeout=a.lock_timeout, download_timeout=a.download_timeout,
                           net_timeout=a.net_timeout, drop_window=a.drop_window, record_path=a.record)
    except Exception as e:          # JtagLoadError, bench-file and socket errors
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 3 if out.get("result") == "ok-unproven" else 0


if __name__ == "__main__":
    sys.exit(main())
