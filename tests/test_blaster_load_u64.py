"""Device-free tests of scripts/blaster_load_u64.py: the gates around a volatile U64E reload through a USB-Blaster.

Every external command (ssh, scp) goes to a stand-in runner that answers as the Quartus tools on the far side would;
REST and the locks are stand-ins that record calls. Nothing here talks to a device, a jtagd or a VM. The ids, the
ssh destination and the cable name are fake; the bench's real ones live only in the owner's local bench.toml.
"""
import hashlib
import json
import os
import re
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import jtag_load as jl  # noqa: E402
import blaster_load_u64 as bl  # noqa: E402

UID = "ABC123"
HASH = "abc123def"
OLD = "0ld0ld0ld".replace("l", "1")          # "01d01d01d": another build's hash
WORKDIR = "/tmp/blaster_load.Zz9aB3"
SSH = "blaster-vm"
ADAPTER_LOCK = "jtag-blaster-blaster-vm"

BENCH_TOML = f"""\
[devices.abc123]
jtag_blaster_ssh = "{SSH}"
jtag_blaster_cable = "USB-Blaster"

[devices.def456]
jtag_adapter_location = "9-9.9"
jtag_adapter_baud = 921600
jtag_scan_only = true
jtag_blaster_ssh = "{SSH}"
jtag_blaster_cable = "USB-Blaster"
jtag_blaster_ssh_port = 2022
jtag_blaster_ssh_identity = "/keys/fake_ed25519"
jtag_blaster_env = ". /etc/profile.d/tools.sh"
jtag_blaster_jtagd_start = "start-jtagd --user"

[devices.fed789]
jtag_adapter_location = "9-9.9"
jtag_adapter_baud = 921600
jtag_scan_only = true

[devices.0a0b0c]
jtag_blaster_ssh = "{SSH}"

[devices.0d0e0f]
jtag_blaster_cable = "USB-Blaster"
"""
KEYS = {"u64e": "uid-abc123", "u64e-full": "uid-def456", "scan-only": "uid-fed789", "no-cable": "uid-0a0b0c",
        "no-ssh": "uid-0d0e0f", "untabled": "uid-123456"}

CHAIN = """\
1) USB-Blaster [1-1]
  02B030DD   5CEBA4/5CEFA4
    Node 19104600   Nios II #0
    Node 0C006E00
"""


def elf(hash_=HASH, machine=113, entry=0x120, paddr=0, magic=b"\x7fELF", klass=1, extra=b""):
    """A minimal little-endian ELF32 with one PT_LOAD at 0 that embeds the git hash as text."""
    payload = b"\x13" * 0x200 + hash_.encode() + b"\x00" * 16 + extra
    ident = magic + bytes([klass, 1, 1]) + bytes(9)
    ehdr = struct.pack("<16sHHIIIIIHHHHHH", ident, 2, machine, 1, entry, 52, 0, 0, 52, 32, 1, 0, 0, 0)
    phdr = struct.pack("<8I", 1, 84, 0, paddr, len(payload), len(payload), 5, 4)
    return ehdr + phdr + payload


class Lock:
    def __init__(self, ev, label, ok=True):
        self.ev, self.label, self.ok = ev, label, ok

    def acquire(self, timeout=None):
        self.ev.append(("acquire", self.label))
        return self.ok

    def release(self):
        self.ev.append(("release", self.label))


def locks(ev, dev_ok=True, ad_ok=True, seen=None):
    def factory(name, allow_nested=False):
        if seen is not None:
            seen.append((name, allow_nested))
        device = name in KEYS
        return Lock(ev, "device" if device else "adapter", dev_ok if device else ad_ok)
    return factory


class Client:
    """get_info answers from a script; None is "no answer". The last item repeats."""

    def __init__(self, ev, script):
        self.ev, self.script = ev, list(script)

    def __call__(self, host, timeout=5.0):
        return self

    def get_info(self):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        self.ev.append(("info", None if item is None else item.get("git_commit_hash")))
        if item is None:
            raise ConnectionError
        return item


def info(h=HASH, uid=UID, product="Ultimate 64 Elite"):
    return {"unique_id": uid, "product": product, "git_commit_hash": h}


class Remote:
    """The runner: scp records the copy's hash; ssh commands are answered as the far side's tools would.

    Each reply is (rc, stdout, stderr), or a list of them consumed in order (the last repeats). The two process
    checks, unless a reply is given for them, run the command's own grep pattern over ``procs`` plus the shell that
    carries the command, as `ps | grep -E` would; pkill removes what its pattern matches."""

    def __init__(self, ev, **replies):
        self.ev, self.argv, self.uploaded, self.timeouts = ev, [], None, {}
        self.noise = replies.pop("noise", "")       # text an env script would print before the answer
        self.procs = list(replies.pop("procs", []))  # the far side's process table, as `ps -eo pid=,args=` lines
        self.replies = {"tools": (0, "", ""), "busy": None, "mktemp": (0, WORKDIR + "\n", ""), "sha": None,
                        "srec": (0, "5" * 64 + f"  {WORKDIR}/app.elf.srec\n", ""), "stray": None,
                        "kill": (0, "", ""), "jtagd": (0, "4711\n", ""), "jtagd_start": (0, "", ""),
                        "chain": (0, CHAIN, ""), "download": (0, "Downloaded 1234KB\nVerified OK\nStarting processor\n", ""),
                        "cleanup": (0, "", "")}
        self.replies.update(replies)

    def kind(self, command):
        for needle, kind in (("nios2-download -g", "download"), ("[n]ios2-download", "busy"), ("command -v", "tools"), ("mktemp", "mktemp"),
                             ("objcopy", "srec"), ("sha256sum", "sha"), ("pkill", "kill"), ("ps -eo", "stray"),
                             ("pgrep -x jtagd", "jtagd"), ("start-jtagd", "jtagd_start"), ("jtagconfig", "chain"),
                             ("rm -rf", "cleanup")):
            if needle in command:
                return kind
        raise AssertionError(f"unexpected remote command: {command!r}")

    def __call__(self, argv, timeout):
        self.argv.append(list(argv))
        if argv[0] == "scp":
            self.uploaded = hashlib.sha256(open(argv[-2], "rb").read()).hexdigest()
            self.ev.append(("scp", argv[-1]))
            return 0, "", ""
        assert argv[0] == "ssh", argv
        kind = self.kind(argv[-1])
        self.ev.append((kind,))
        self.timeouts[kind] = timeout
        reply = self.replies[kind]
        if kind in ("busy", "stray", "kill") and (reply is None or kind == "kill"):
            pattern = re.search(r"-[Ef] '([^']+)'", argv[-1]).group(1)
            table = self.procs + [" 1 sh -c " + argv[-1].replace("\n", "; ")]
            hits = [line for line in table if re.search(pattern, line)]
            if kind == "kill":
                self.procs = [line for line in self.procs if line not in hits]
            else:
                reply = (0, "\n".join(hits) + "\n", "") if hits else (1, "", "")
        if kind == "sha" and reply is None:
            reply = (0, f"{self.uploaded}  {WORKDIR}/app.elf\n", "")
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if isinstance(reply, BaseException):
            raise reply
        if kind in ("mktemp", "sha", "srec"):
            reply = (reply[0], self.noise + reply[1], reply[2])
        return reply

    def commands(self, kind):
        return [a[-1] for a in self.argv if a[0] == "ssh" and self.kind(a[-1]) == kind]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    toml = tmp_path / "bench.toml"
    toml.write_text(BENCH_TOML)
    monkeypatch.setenv("C64_BENCH_BINDINGS_FILE", str(toml))
    monkeypatch.delenv("C64U_LOCK_PID", raising=False)
    monkeypatch.delenv("C64U_LOCK_HOST", raising=False)
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: KEYS.get(h, h))
    img = tmp_path / "ultimate.elf"
    img.write_bytes(elf())
    return [], img, hashlib.sha256(img.read_bytes()).hexdigest()


def reload_(env_, client_script, remote=None, host="u64e", **kw):
    ev, img, sha = env_
    remote = remote if remote is not None else Remote(ev)
    args = dict(sha256=sha, expect_git_hash=HASH, net_timeout=20, drop_window=10, runner=remote,
                which=lambda tool: "/usr/bin/" + tool, lock_factory=locks(ev), client_factory=Client(ev, client_script),
                sleep=lambda s: None, clock=Clock(), log=lambda m: None)
    args.update(kw)
    return bl.blaster_load(host, img, **args)


# The bound env text runs first, in the same shell, with its own output discarded.
ENV_PREFIX = "{ . /etc/profile.d/tools.sh\n} >/dev/null\n"
FULL = [info(OLD, uid="DEF456"), info(HASH, uid="DEF456")]      # what the fully bound device ("u64e-full") answers


def kinds(ev):
    return [e[0] for e in ev]


# ---- the load itself ------------------------------------------------------------------------------------------

def test_a_load_runs_the_gates_in_order_and_proves_the_restart_by_the_hash_change(env):
    ev, img, sha = env
    remote = Remote(ev)
    rec = reload_(env, [info(OLD), info(HASH)], remote)
    assert ev[:3] == [("acquire", "device"), ("acquire", "adapter"), ("info", OLD)]
    assert kinds(ev)[3:] == ["tools", "busy", "mktemp", "scp", "sha", "stray", "jtagd", "chain", "download", "info",
                             "cleanup", "release", "release"]
    assert ev[-2:] == [("release", "adapter"), ("release", "device")]
    assert remote.commands("download") == [f"nios2-download -g {WORKDIR}/app.elf"]
    assert rec["result"] == "ok" and rec["restart_proof"] == "hash-change"
    assert rec["pre_info"]["git_commit_hash"] == OLD and rec["info"]["git_commit_hash"] == HASH
    assert rec["cable"] == "USB-Blaster [1-1]" and rec["sha256"] == sha


def test_the_pinned_copy_is_uploaded_not_the_source(env):
    ev, img, sha = env
    remote = Remote(ev)
    reload_(env, [info(OLD), info(HASH)], remote)
    scp = next(a for a in remote.argv if a[0] == "scp")
    assert scp[-2] != str(img) and remote.uploaded == sha
    assert scp[-1] == f"{SSH}:{WORKDIR}/app.elf"


def test_the_bound_ssh_route_reaches_every_command(env):
    ev = env[0]
    remote = Remote(ev)
    reload_(env, FULL, remote, host="u64e-full")
    for argv in remote.argv:
        assert "BatchMode=yes" in argv and "/keys/fake_ed25519" in argv
        port_flag = "-P" if argv[0] == "scp" else "-p"
        assert argv[argv.index(port_flag) + 1] == "2022"
        if argv[0] == "ssh":
            assert argv[-2] == SSH and argv[-1].startswith(ENV_PREFIX)


def test_the_device_lock_is_nestable_and_the_adapter_lock_is_keyed_by_the_route(env):
    ev, seen = env[0], []
    reload_(env, [info(OLD), info(HASH)], lock_factory=locks(ev, seen=seen))
    assert seen == [("u64e", True), (ADAPTER_LOCK, False)]


# ---- pins, refused before any lock or command -----------------------------------------------------------------

def test_a_sha256_mismatch_refuses_before_any_lock_or_command(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], sha256="0" * 64)
    assert e.value.stage == "hash" and ev == []


@pytest.mark.parametrize("pin", ["", None, "abc"])
def test_a_missing_sha256_refuses(env, pin):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], sha256=pin)
    assert e.value.stage == "hash" and ev == []


@pytest.mark.parametrize("h", ["", "abc", "zzzzzzzzz", None])
def test_the_expected_git_hash_is_required_and_hex(env, h):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], expect_git_hash=h)
    assert e.value.stage == "hash" and ev == []


@pytest.mark.parametrize("image", [
    b"not an elf at all" * 10,
    elf(magic=b"\x7fELG"),
    elf(klass=2),                       # ELF64
    elf(machine=243),                   # RISC-V: a C64U-class application
    elf(entry=0x10000),                 # entry outside the loaded segment
    elf(paddr=0x1000000),               # loads above the application's region
    elf()[:300],                        # truncated: the segment runs past the file
    elf()[:60],                         # the program header is cut off
], ids=["text", "magic", "elf64", "riscv", "entry", "region", "truncated", "no-phdr"])
def test_anything_but_a_nios_application_elf_refuses_before_any_lock_or_command(env, image):
    ev, img, _ = env
    img.write_bytes(image)
    with pytest.raises(jl.JtagLoadError) as e:
        reload_((ev, img, hashlib.sha256(image).hexdigest()), [info(OLD)])
    assert e.value.stage == "image" and ev == []


def test_an_elf_of_another_build_than_the_expected_hash_refuses_before_any_lock_or_command(env):
    ev, img, _ = env
    image = elf(hash_="fedcba987")
    img.write_bytes(image)
    with pytest.raises(jl.JtagLoadError) as e:
        reload_((ev, img, hashlib.sha256(image).hexdigest()), [info(OLD)])
    assert e.value.stage == "hash" and "embed" in str(e.value) and ev == []


def test_an_elf_that_embeds_only_six_digits_of_the_hash_is_another_build(env):
    ev, img, _ = env
    image = elf(hash_=HASH[:6] + "000")
    img.write_bytes(image)
    with pytest.raises(jl.JtagLoadError) as e:
        reload_((ev, img, hashlib.sha256(image).hexdigest()), [info(OLD)])
    assert e.value.stage == "hash" and ev == []


PAYLOAD = 0x200 + len(HASH) + 16              # the size of elf()'s one segment


def test_the_region_and_entry_checks_are_exact_at_their_boundaries():
    bl.check_elf(elf(paddr=bl.APP_REGION_END - PAYLOAD), HASH)             # ends exactly at the limit
    bl.check_elf(elf(entry=PAYLOAD - 1), HASH)                             # the segment's last byte
    for image in (elf(paddr=bl.APP_REGION_END - PAYLOAD + 1), elf(entry=PAYLOAD)):
        with pytest.raises(jl.JtagLoadError) as e:
            bl.check_elf(image, HASH)
        assert e.value.stage == "image"


def test_a_malformed_srec_pin_refuses_before_any_lock_or_command(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], srec_sha256="5" * 63)
    assert e.value.stage == "hash" and ev == []


# ---- bindings -------------------------------------------------------------------------------------------------

# "hostabc123" is not in devices.toml; cut like a lock key it would read as the bound table's id.
@pytest.mark.parametrize("host", ["scan-only", "no-cable", "no-ssh", "untabled", "192.0.2.7", "hostabc123"])
def test_a_device_without_the_blaster_binding_is_refused_before_anything(env, host):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], host=host)
    assert e.value.stage == "binding" and ev == []


def test_the_blaster_binding_does_not_lift_scan_only_for_the_esp_adapter(env):
    """One table may carry both: jtag_scan_only still forbids a jtag_load.py load, the Blaster route is its own keys."""
    ev = env[0]
    assert reload_(env, FULL, host="u64e-full")["result"] == "ok"
    with pytest.raises(jl.JtagLoadError) as e:
        jl.adapter_binding("DEF456")
    assert e.value.stage == "binding"
    assert jl.adapter_binding("DEF456", scan=True)["location"] == "9-9.9"


@pytest.mark.parametrize("text", [None, "[devices.abc123]\njtag_blaster_shh = 'x'\n"], ids=["missing", "typo"])
def test_a_missing_or_broken_bench_file_is_a_binding_refusal(env, text, tmp_path):
    ev = env[0]
    toml = tmp_path / "bench.toml"
    toml.unlink() if text is None else toml.write_text(text)
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)])
    assert e.value.stage == "binding" and ev == []


def test_missing_local_tools_refuse_before_any_lock_or_command(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], which=lambda tool: None if tool == "scp" else "/usr/bin/" + tool)
    assert e.value.stage == "setup" and "scp" in str(e.value) and ev == []


# ---- dry run --------------------------------------------------------------------------------------------------

def test_a_dry_run_touches_nothing_and_returns_the_plan(env):
    ev = env[0]
    rec = reload_(env, [info(OLD)], dry_run=True)
    assert ev == [] and rec["result"] == "dry-run ok"
    assert any("nios2-download -g" in step for step in rec["plan"])
    assert any("jtagconfig -n" in step for step in rec["plan"])
    assert not any(str(env[1]) in step for step in rec["plan"])       # the upload is the pinned copy, not the source


def test_a_dry_run_still_refuses_a_wrong_pin(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], dry_run=True, sha256="1" * 64)
    assert e.value.stage == "hash" and ev == []


# ---- locks ----------------------------------------------------------------------------------------------------

def test_a_busy_device_lock_runs_no_command(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], lock_factory=locks(ev, dev_ok=False))
    assert e.value.stage == "lock" and ev == [("acquire", "device")]


def test_a_busy_adapter_releases_the_device_and_runs_no_command(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)], lock_factory=locks(ev, ad_ok=False))
    assert e.value.stage == "lock"
    assert ev == [("acquire", "device"), ("acquire", "adapter"), ("release", "device")]


def test_a_lock_shell_holder_is_honoured_and_a_stale_claim_refused(env, monkeypatch):
    import c64_test_harness.backends.device_lock as dl
    ev = env[0]
    shell = os.getppid()                                # a real ancestor: nothing about the parent walk is faked
    monkeypatch.setenv("C64U_LOCK_PID", str(shell))
    monkeypatch.setenv("C64U_LOCK_HOST", "u64e")
    monkeypatch.setattr(dl, "device_lock_holder", lambda h, lock_dir=None: {"pid": shell})
    assert reload_(env, [info(OLD), info(HASH)])["result"] == "ok"
    assert ("acquire", "device") not in ev and ("acquire", "adapter") in ev and ("download",) in ev
    del ev[:]
    monkeypatch.setattr(dl, "device_lock_holder", lambda h, lock_dir=None: {"pid": 1})
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD), info(HASH)])
    assert e.value.stage == "lock" and ("download",) not in ev and "scp" not in kinds(ev)


# ---- identity before, and the dark path -----------------------------------------------------------------------

def test_a_box_that_does_not_answer_is_refused_unless_the_caller_says_dark(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [None])
    assert e.value.stage == "network" and "--dark" in str(e.value)
    assert kinds(ev) == ["acquire", "acquire", "info", "release", "release"]


def test_a_dark_box_is_loaded_and_identified_only_afterwards(env):
    ev = env[0]
    rec = reload_(env, [None, None, None, None, info(HASH)], dark=True)
    assert rec["pre_info"] is None and rec["dark"] is True
    assert rec["result"] == "ok" and rec["restart_proof"] == "no-answer-before"
    assert rec["info"]["unique_id"] == UID
    assert kinds(ev)[:6] == ["acquire", "acquire", "info", "info", "info", "tools"]      # three misses, then work
    assert kinds(ev).index("download") < len(ev) - 1 - kinds(ev)[::-1].index("info")


@pytest.mark.parametrize("misses", [1, 2])
def test_a_box_that_drops_a_request_or_two_is_not_dark_and_proves_nothing_by_it(env, misses):
    """--dark on a healthy box already on the expected hash: fewer than three misses in a row is not an outage."""
    ev = env[0]
    rec = reload_(env, [None] * misses + [info(HASH)], dark=True)
    assert rec["pre_info"]["git_commit_hash"] == HASH
    assert rec["result"] == "ok-unproven" and rec["restart_proof"] is None


def test_a_box_that_answers_late_under_dark_is_still_identified_before_any_command(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [None, info(OLD, uid="FFFFFF")], dark=True)
    assert e.value.stage == "identity"
    assert kinds(ev) == ["acquire", "acquire", "info", "info", "release", "release"]


@pytest.mark.parametrize("pre", [{"unique_id": UID, "product": "Ultimate 64 Elite"}, info(""), info("unknown")],
                         ids=["no-hash", "empty-hash", "not-a-hash"])
def test_a_device_that_reported_no_hash_before_has_not_changed_its_hash(env, pre):
    ev = env[0]
    rec = reload_(env, [pre, info(HASH)])
    assert rec["result"] == "ok-unproven" and rec["restart_proof"] is None and rec["rest_drop_seen"] is False
    del ev[:]
    rec = reload_(env, [pre, None, None, None, info(HASH)])
    assert rec["result"] == "ok" and rec["restart_proof"] == "rest-drop"


@pytest.mark.parametrize("after", [info(HASH, uid="FFFFFF"), info(HASH, product="C64 Ultimate")],
                         ids=["other-unique-id", "other-product"])
def test_another_device_answering_after_a_dark_load_is_an_identity_failure(env, after):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [None, None, None, after], dark=True)
    assert e.value.stage == "identity" and ("download",) in ev
    assert ev[-2:] == [("release", "adapter"), ("release", "device")]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("pre", [info(OLD, uid="FFFFFF"), info(OLD, product="C64 Ultimate")],
                         ids=["other-unique-id", "other-product"])
def test_another_device_answering_before_the_load_refuses_before_any_command(env, pre, dark):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [pre], dark=dark)
    assert e.value.stage == "identity"
    assert kinds(ev) == ["acquire", "acquire", "info", "release", "release"]


# ---- preflight: everything that can fail fails before the Nios is paused --------------------------------------

def refused_before_download(env, stage, script=None, **kw):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, script or [info(OLD), info(HASH)], **kw)
    assert e.value.stage == stage, e.value
    assert ("download",) not in ev
    assert ev[-2:] == [("release", "adapter"), ("release", "device")]
    return e.value


def test_missing_remote_tools_refuse_before_the_download(env):
    err = refused_before_download(env, "setup", remote=Remote(env[0], tools=(127, "missing: nios2-download\n", "")))
    assert "nios2-download" in str(err)


def test_an_unreachable_remote_refuses_before_the_download(env):
    refused_before_download(env, "remote", remote=Remote(env[0], tools=(255, "", "ssh: connect refused")))
    del env[0][:]
    refused_before_download(env, "remote", remote=Remote(env[0], tools=TimeoutError()))


def test_a_copy_that_arrives_with_another_hash_refuses_before_the_download(env):
    remote = Remote(env[0], sha=(0, "7" * 64 + f"  {WORKDIR}/app.elf\n", ""))
    refused_before_download(env, "hash", remote=remote)
    assert ("cleanup",) in env[0]


@pytest.mark.parametrize("answer", ["/tmp/x; rm -rf ~", "/", "/tmp/..", "/tmp/blaster_load.abc", "/home/blaster_load.Zz9aB3",
                                    "/tmp/blaster_load.Zz9aB3/../.."])
def test_only_a_directory_of_our_own_mktemp_template_is_used_and_later_removed(env, answer):
    refused_before_download(env, "remote", remote=Remote(env[0], mktemp=(0, answer + "\n", "")))
    assert "scp" not in kinds(env[0]) and ("cleanup",) not in env[0]


def test_text_printed_before_an_answer_does_not_become_the_answer(env):
    ev = env[0]
    remote = Remote(ev, noise="Quartus environment loaded\nlicense ok\n")
    rec = reload_(env, [info(OLD), info(HASH)], remote, srec_sha256="5" * 64)
    assert rec["result"] == "ok" and remote.commands("cleanup") == [f"rm -rf {WORKDIR}"]


@pytest.mark.parametrize("running", [" 900 /bin/sh nios2-download -g /tmp/x/app.elf", " 901 jtagconfig -n"],
                         ids=["download", "jtagconfig"])
def test_a_jtag_tool_still_running_on_the_far_side_refuses_and_is_never_killed(env, running):
    """A download whose ssh timed out here may still run there, after both locks were released."""
    ev = env[0]
    remote = Remote(ev, procs=[" 12 sshd: listener", running])
    refused_before_download(env, "remote", remote=remote, kill_stray_gdb=True)
    assert ("kill",) not in ev and "scp" not in kinds(ev) and ("mktemp",) not in ev
    assert remote.procs == [" 12 sshd: listener", running]


@pytest.mark.parametrize("stray", [" 812 nios2-gdb-server --tcpport 2342", " 813 nios2-elf-gdb app.elf"])
def test_the_stray_debugger_check_finds_and_kills_exactly_the_debuggers(env, stray):
    """The patterns are run for real over a process table that includes the shell carrying them."""
    ev = env[0]
    others = [" 12 sshd: listener", " 40 jtagd --user-start"]
    refused_before_download(env, "remote", remote=Remote(ev, procs=others + [stray]))
    del ev[:]
    remote = Remote(ev, procs=others + [stray])
    assert reload_(env, [info(OLD), info(HASH)], remote, kill_stray_gdb=True)["result"] == "ok"
    assert remote.procs == others and ("kill",) in ev


def test_the_download_gets_the_callers_timeout(env):
    remote = Remote(env[0])
    reload_(env, [info(OLD), info(HASH)], remote, download_timeout=777)
    assert remote.timeouts["download"] == 777


@pytest.mark.parametrize("reply", [(1, "", "rm: permission denied"), TimeoutError("slow")], ids=["rc", "timeout"])
def test_a_failed_cleanup_is_recorded_and_does_not_fail_the_load(env, reply):
    rec = reload_(env, [info(OLD), info(HASH)], Remote(env[0], cleanup=reply))
    assert rec["result"] == "ok" and rec["cleanup_error"]


def test_a_clean_run_records_no_cleanup_error(env):
    assert "cleanup_error" not in reload_(env, [info(OLD), info(HASH)])


def test_the_srec_pin_is_checked_on_the_remote_conversion_before_the_download(env):
    ev = env[0]
    refused_before_download(env, "hash", srec_sha256="6" * 64)
    assert ("srec",) in ev
    del ev[:]
    rec = reload_(env, [info(OLD), info(HASH)], srec_sha256="5" * 64)
    assert rec["result"] == "ok" and kinds(ev).index("srec") < kinds(ev).index("download")
    upper = Remote(ev, srec=(0, "ab" * 32 + f"  {WORKDIR}/app.elf.srec\n", ""))
    assert reload_(env, [info(OLD), info(HASH)], upper, srec_sha256="AB" * 32)["result"] == "ok"
    del ev[:]
    reload_(env, [info(OLD), info(HASH)])
    assert ("srec",) not in ev                    # no pin, no conversion


STRAY = (0, " 812 nios2-gdb-server --tcpport 2342\n", "")


def test_a_stray_gdb_server_refuses_unless_the_caller_asks_for_the_kill(env):
    ev = env[0]
    refused_before_download(env, "remote", remote=Remote(ev, stray=STRAY))
    assert ("kill",) not in ev
    del ev[:]
    rec = reload_(env, [info(OLD), info(HASH)], Remote(ev, stray=[STRAY, (1, "", "")]), kill_stray_gdb=True)
    assert rec["result"] == "ok" and kinds(ev).index("kill") < kinds(ev).index("download")
    del ev[:]
    refused_before_download(env, "remote", remote=Remote(ev, stray=STRAY), kill_stray_gdb=True)
    assert ("kill",) in ev


def test_no_kill_is_sent_when_nothing_strays(env):
    ev = env[0]
    reload_(env, [info(OLD), info(HASH)], kill_stray_gdb=True)
    assert ("kill",) not in ev


def test_a_stopped_jtagd_refuses_unless_a_start_command_is_bound(env):
    ev = env[0]
    refused_before_download(env, "remote", remote=Remote(ev, jtagd=(1, "", "")))
    assert ("jtagd_start",) not in ev
    del ev[:]
    remote = Remote(ev, jtagd=[(1, "", ""), (0, "99\n", "")])
    assert reload_(env, FULL, remote, host="u64e-full")["result"] == "ok"
    assert kinds(ev).index("jtagd_start") < kinds(ev).index("chain")
    assert remote.commands("jtagd_start") == [ENV_PREFIX + "start-jtagd --user"]
    del ev[:]
    refused_before_download(env, "remote", remote=Remote(ev, jtagd=(1, "", "")), host="u64e-full", script=FULL)
    assert ("jtagd_start",) in ev


@pytest.mark.parametrize("chain", [
    "No JTAG hardware available\n",
    CHAIN + "2) USB-Blaster [1-2]\n  02B030DD   5CEBA4/5CEFA4\n    Node 19104600   Nios II #0\n",
    CHAIN.replace("USB-Blaster", "Arrow-USB-Other"),
    CHAIN.replace("5CEBA4/5CEFA4", "10M08SA"),
    CHAIN + "  02B030DD   5CEBA4/5CEFA4\n",
    "1) USB-Blaster [1-1]\n  Unable to read device chain - JTAG chain broken\n",
    CHAIN.replace("Nios II #0", ""),
    CHAIN.replace("    Node 19104600", "  a second device in a layout nobody captured\n    Node 19104600"),
    "jtagd restarted\n" + CHAIN,
    "  02B030DD   5CEBA4/5CEFA4\n" + CHAIN,
], ids=["no-cable", "two-cables", "other-cable", "other-fpga", "two-devices", "broken-chain", "no-nios-node",
        "unknown-line-under-the-cable", "unknown-line-before-the-cable", "device-before-any-cable"])
def test_a_chain_that_is_not_the_one_bound_cable_with_a_nios_refuses_before_the_download(env, chain):
    refused_before_download(env, "chain", remote=Remote(env[0], chain=(0, chain, "")))


def test_preflight_only_stops_before_the_download_and_cleans_up(env):
    ev = env[0]
    rec = reload_(env, [info(OLD)], preflight_only=True)
    assert rec["result"] == "preflight ok" and ("download",) not in ev
    assert kinds(ev)[-4:] == ["chain", "cleanup", "release", "release"]


# ---- the download and what comes back -------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [(1, "", "There are no Nios II processors available"), TimeoutError()],
                         ids=["rc", "timeout"])
def test_a_failed_download_is_never_retried_and_still_cleans_up_and_releases(env, reply):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD), info(HASH)], Remote(ev, download=reply))
    assert e.value.stage == "download" and "STOP" in str(e.value)
    assert kinds(ev).count("download") == 1
    assert kinds(ev)[-3:] == ["cleanup", "release", "release"]


def test_the_wrong_hash_after_the_load_is_a_failure(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD)])              # the device keeps reporting the old build
    assert e.value.stage == "network" and ("download",) in ev
    assert ev[-2:] == [("release", "adapter"), ("release", "device")]


def test_a_device_that_never_comes_back_is_a_failure(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(OLD), None])
    assert e.value.stage == "network" and "STOP" in str(e.value)


def test_a_same_hash_load_is_proven_only_by_a_rest_drop(env):
    ev = env[0]
    rec = reload_(env, [info(HASH), None, None, None, info(HASH)])
    assert rec["result"] == "ok" and rec["restart_proof"] == "rest-drop" and rec["rest_drop_seen"] is True


def pauses(ev):
    """An injected sleep that records each pause among the other events."""
    return lambda seconds: ev.append(("sleep", seconds))


def test_the_three_dark_probes_are_a_second_apart(env):
    """Three refused connections in one burst are not an outage: the criterion has a duration."""
    ev = env[0]
    reload_(env, [None, None, None, None, info(HASH)], dark=True, sleep=pauses(ev))
    assert ev[2:8] == [("info", None), ("sleep", 1.0), ("info", None), ("sleep", 1.0), ("info", None), ("tools",)]


def test_the_three_misses_of_a_drop_are_a_second_apart(env):
    ev = env[0]
    rec = reload_(env, [info(HASH), None, None, None, info(HASH)], sleep=pauses(ev))
    after = ev[ev.index(("download",)) + 1:]
    assert after[:7] == [("info", None), ("sleep", 1.0)] * 3 + [("info", HASH)]
    assert rec["restart_proof"] == "rest-drop"


def test_blank_lines_in_the_chain_listing_are_not_unknown_lines(env):
    chain = "\n" + CHAIN.replace("    Node 0C006E00", "\n    Node 0C006E00") + "\n\n"
    rec = reload_(env, [info(OLD), info(HASH)], Remote(env[0], chain=(0, chain, "")))
    assert rec["result"] == "ok" and rec["cable"] == "USB-Blaster [1-1]"


def test_three_misses_with_an_answer_between_them_are_not_a_drop(env):
    """Packet loss is not an outage: the three misses must be consecutive."""
    rec = reload_(env, [info(HASH), None, None, info(HASH), None, info(HASH)])
    assert rec["result"] == "ok-unproven" and rec["restart_proof"] is None and rec["rest_drop_seen"] is False


def test_a_same_hash_load_without_a_rest_drop_is_reported_unproven(env):
    ev = env[0]
    rec = reload_(env, [info(HASH), None, None, info(HASH)])      # two misses are not a drop
    assert rec["result"] == "ok-unproven" and rec["restart_proof"] is None
    assert "#570" in rec["unproven"] and ("download",) in ev


def test_another_device_answering_during_the_same_hash_watch_is_an_identity_failure(env):
    with pytest.raises(jl.JtagLoadError) as e:
        reload_(env, [info(HASH), info(HASH, uid="FFFFFF"), info(HASH)])     # it answers once, mid-watch
    assert e.value.stage == "identity"


def test_a_failure_is_recorded_too(env, tmp_path):
    record = tmp_path / "loads.jsonl"
    with pytest.raises(jl.JtagLoadError):
        reload_(env, [info(OLD)], Remote(env[0], download=(1, "", "boom")), record_path=record)
    reload_(env, [info(OLD), info(HASH)], record_path=record)
    first, second = [json.loads(line) for line in record.read_text().splitlines()]
    assert first["result"] == "error" and "download" in first["error"]
    assert second["result"] == "ok" and second["restart_proof"] == "hash-change"


# ---- command line ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("script, code", [
    ([info(OLD), info(HASH)], 0),
    ([info(HASH), None, None, info(HASH)], 3),          # came back, restart unproven
    ([info(OLD)], 2),
], ids=["proven", "unproven", "failed"])
def test_the_exit_code_separates_proven_unproven_and_failed(env, monkeypatch, capsys, script, code):
    ev, img, sha = env
    real = bl.blaster_load

    def faked(host, elf_path, **kw):
        kw.update(net_timeout=20, drop_window=10, runner=Remote(ev), which=lambda t: "/usr/bin/" + t,
                  lock_factory=locks(ev), client_factory=Client(ev, script), sleep=lambda s: None, clock=Clock(),
                  log=lambda m: None)
        return real(host, elf_path, **kw)
    monkeypatch.setattr(bl, "blaster_load", faked)
    assert bl.main(["load", "u64e", str(img), "--sha256", sha, "--expect-hash", HASH]) == code
    capsys.readouterr()


def test_the_command_line_requires_both_pins(env, capsys):
    ev, img, sha = env
    for argv in (["load", "u64e", str(img), "--sha256", sha], ["load", "u64e", str(img), "--expect-hash", HASH]):
        with pytest.raises(SystemExit) as e:
            bl.main(argv)
        assert e.value.code == 2
    capsys.readouterr()
    assert ev == []


def test_the_default_runner_reports_rc_and_output_and_raises_on_timeout():
    rc, out, err = bl.run_command([sys.executable, "-c", "import sys; print('o'); print('e', file=sys.stderr); "
                                   "sys.exit(7)"], timeout=30)
    assert (rc, out.strip(), err.strip()) == (7, "o", "e")
    with pytest.raises(TimeoutError):
        bl.run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
