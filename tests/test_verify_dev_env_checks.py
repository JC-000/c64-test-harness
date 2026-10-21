"""Functional tests for the dev-env scripts' host checks (#500, #502).

The scripts are exercised by extracting their own sections and running them
under ``bash -c`` with the world faked around them: a fake ``/dev`` listing
(``C64H_DEV_DIR``), a fake ``/etc/os-release`` (``C64H_OS_RELEASE``), and
shell-function stubs for ``uname``, ``sudo`` and the result recorder.  No
emulator is started, ``sudo`` is never reached, and the host's network state
is not read or touched.

``scripts/vice_smoke.py`` (``verify-dev-env.sh --smoke``) is driven with a
fake launcher, so no VICE runs here either.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "scripts" / "verify-dev-env.sh"
SETUP = REPO / "scripts" / "setup-dev-env.sh"
SMOKE = REPO / "scripts" / "vice_smoke.py"


def _section(path: Path, begin: str, end: str) -> str:
    text = path.read_text()
    start = text.index(begin)
    return text[start : text.index(end, start)]


# Stubs shared by every verify-dev-env.sh program: rows and hints go to
# stdout, and nothing can reach sudo or a real package manager.
_VERIFY_STUBS = r"""
set -u
record() { printf 'ROW|%s|%s|%s|%s\n' "$2" "$3" "$4" "$5"; }
add_hint() { printf 'HINT|%s\n' "$1"; }
sudo() { return 1; }
nopasswd_for() { return 1; }
have_cmd() { return 1; }
"""


def _run(program: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=full_env,
        timeout=20,
    )


def _check_system(
    uname: str, env: dict[str, str], stubs: str = ""
) -> subprocess.CompletedProcess:
    # Never this host's build-vice-eth.sh, x64sc or bridge helpers unless a
    # test supplies them.
    env = {"C64H_BUILD_VICE_ETH": "/nonexistent/build-vice-eth.sh",
           "C64H_X64SC_PATH": "/nonexistent/x64sc",
           "C64H_BRIDGE_LIBEXEC": "/nonexistent/libexec",
           "C64H_REPO_ROOT": "/nonexistent/repo", **env}
    program = (
        _VERIFY_STUBS
        + stubs
        + f"uname() {{ printf '%s\\n' {shlex.quote(uname)}; }}\n"
        + _section(VERIFY, "# ---------- Section 3: System tools", "# ---------- Section 4")
        + '\ncheck_system "System tools"\n'
    )
    proc = _run(program, env)
    assert proc.returncode == 0, proc.stderr
    return proc


def _rows(out: str) -> list[list[str]]:
    return [line.split("|")[1:] for line in out.splitlines() if line.startswith("ROW|")]


def _hints(out: str) -> list[str]:
    return [line[len("HINT|"):] for line in out.splitlines() if line.startswith("HINT|")]


# ---------------------------------------------------------------------------
# #502: every /dev/bpf* node, not /dev/bpf0 alone
# ---------------------------------------------------------------------------


def _fake_dev(tmp_path: Path, modes: dict[str, int]) -> Path:
    dev = tmp_path / "dev"
    dev.mkdir()
    for name, mode in modes.items():
        node = dev / name
        node.touch()
        node.chmod(mode)
    # A non-bpf node that must not be counted.
    (dev / "null").touch()
    return dev


def _bpf_row(proc: subprocess.CompletedProcess) -> list[str]:
    rows = [r for r in _rows(proc.stdout) if "bpf" in r[0]]
    assert len(rows) == 1, proc.stdout
    return rows[0]


def test_bpf_root_only_higher_node_is_named(tmp_path):
    """bpf0 other-rw but bpf4 root-only: the bench state that #502 records."""
    dev = _fake_dev(
        tmp_path,
        {"bpf0": 0o606, "bpf1": 0o606, "bpf2": 0o606, "bpf3": 0o606, "bpf4": 0o600},
    )
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    label, status, detail, _crit = _bpf_row(proc)
    assert status == "warn", detail
    assert "no rw access: bpf4 " in detail, detail
    for good in ("bpf0", "bpf1", "bpf2", "bpf3"):
        assert good not in detail.split("(")[0], detail
    assert any("bpf4+" in h for h in _hints(proc.stdout)), proc.stdout


def test_bpf_every_failing_node_is_named(tmp_path):
    dev = _fake_dev(tmp_path, {"bpf0": 0o600, "bpf1": 0o606, "bpf10": 0o604, "bpf2": 0o602})
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "warn"
    failing = detail.split("no rw access: ", 1)[1].split(" (", 1)[0].split()
    assert sorted(failing) == ["bpf0", "bpf10", "bpf2"], detail


def test_bpf_all_nodes_other_rw_is_ok(tmp_path):
    dev = _fake_dev(tmp_path, {"bpf0": 0o606, "bpf1": 0o666, "bpf5": 0o606})
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "ok", detail
    assert "all 3 nodes" in detail, detail
    assert not any("bpf" in h for h in _hints(proc.stdout))


def _node_group(dev: Path) -> str:
    import grp

    return grp.getgrgid((dev / "bpf0").stat().st_gid).gr_name


def test_bpf_group_rw_for_a_member_is_ok(tmp_path):
    """Wireshark's ChmodBPF leaves the nodes root:access_bpf 0660: usable by a member."""
    dev = _fake_dev(tmp_path, {"bpf0": 0o660, "bpf1": 0o660})
    groups = f"everyone {_node_group(dev)}"
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev), "C64H_USER_GROUPS": groups})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "ok", detail
    assert not any("bpf" in h for h in _hints(proc.stdout))


def test_bpf_group_rw_found_through_id_for_a_supplementary_group(tmp_path):
    """No override: membership comes from ``id -Gn``, supplementary groups included.

    ChmodBPF's access_bpf is never the primary group (``staff`` on macOS),
    so a lookup of the primary group alone warns about every node again.
    """
    import grp

    primary = os.getgid()
    named = []
    for gid in os.getgroups():
        if gid == primary:
            continue
        try:
            named.append((gid, grp.getgrgid(gid).gr_name))
        except KeyError:
            continue
    if not named:
        pytest.skip("this user has no named supplementary group to chgrp a node to")
    gid, _name = named[0]
    dev = _fake_dev(tmp_path, {"bpf0": 0o660})
    os.chown(dev / "bpf0", -1, gid)
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "ok", detail


@pytest.mark.parametrize(
    ("mode", "groups"),
    [
        (0o660, "everyone"),
        (0o640, None),  # group may read but not write
        (0o660, "x{g}"),  # a group whose name merely contains the node's
        (0o660, "{g}x"),
    ],
)
def test_bpf_group_rw_for_a_non_member_is_named(tmp_path, mode, groups):
    dev = _fake_dev(tmp_path, {"bpf0": mode, "bpf1": 0o606})
    group = _node_group(dev)
    member_of = f"everyone {group}" if groups is None else groups.format(g=group)
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev), "C64H_USER_GROUPS": member_of})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "warn", detail
    failing = detail.split("no rw access: ", 1)[1].split(" (", 1)[0].split()
    assert failing == ["bpf0"], detail
    assert any("access_bpf" in h for h in _hints(proc.stdout)), proc.stdout


def test_bpf_no_nodes_is_missing(tmp_path):
    dev = _fake_dev(tmp_path, {})
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "missing", detail


# ---------------------------------------------------------------------------
# #500 item 3: fix hints name the distro's package manager
# ---------------------------------------------------------------------------


def _os_release(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(body)
    return path


@pytest.mark.parametrize(
    ("body", "iproute_hint", "iptables_hint"),
    [
        pytest.param(
            'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="25.10"\n',
            "sudo apt-get install -y iproute2",
            "sudo apt-get install -y iptables",
            id="ubuntu",
        ),
        pytest.param(
            "ID=linuxmint\nID_LIKE=\"ubuntu debian\"\n",
            "sudo apt-get install -y iproute2",
            "sudo apt-get install -y iptables",
            id="mint-id-like",
        ),
        pytest.param(
            'ID=fedora\nVERSION_ID=42\n',
            "sudo dnf install -y iproute",
            "sudo dnf install -y iptables",
            id="fedora",
        ),
        pytest.param(
            'ID="rocky"\nID_LIKE="rhel centos fedora"\n',
            "sudo dnf install -y iproute",
            "sudo dnf install -y iptables",
            id="rocky-id-like",
        ),
        pytest.param(
            "ID=arch\n",
            "sudo pacman -S --needed iproute2",
            "sudo pacman -S --needed iptables",
            id="arch",
        ),
        pytest.param(
            "ID=void\n",
            "install the package that provides 'iproute2' with your distro's package manager",
            "install the package that provides 'iptables' with your distro's package manager",
            id="unknown",
        ),
    ],
)
def test_linux_hints_follow_the_distro(tmp_path, body, iproute_hint, iptables_hint):
    osr = _os_release(tmp_path, body)
    proc = _check_system("Linux", {"C64H_OS_RELEASE": str(osr)})
    hints = _hints(proc.stdout)
    # Exact match: "iproute" must not pass as a prefix of "iproute2".
    assert f"Install iproute2: {iproute_hint}" in hints, hints
    assert f"Install iptables: {iptables_hint}" in hints, hints
    if "apt-get" not in iproute_hint:
        assert not any("apt-get" in h for h in hints), hints


# ---------------------------------------------------------------------------
# #500 item 3: setup-dev-env.sh's Ubuntu-25 gate reads the (faked) os-release
# ---------------------------------------------------------------------------


def _setup_program(osr: Path, force: int, then: str = "") -> str:
    return (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + "sudo() { printf 'SUDO|%s\\n' \"$*\"; return 0; }\n"
        + f"OS=Linux\nFORCE={force}\nDRY_RUN=0\nNO_SYSTEM_PACKAGES=0\n"
        + _section(SETUP, "# ---------- environment checks", "# ---------- Stage 2")
        + "\ncheck_os\n"
        + then
    )


@pytest.mark.parametrize(
    ("body", "force", "rc"),
    [
        pytest.param('ID=ubuntu\nVERSION_ID="25.10"\n', 0, 0, id="ubuntu-25.10"),
        pytest.param('ID=ubuntu\nVERSION_ID="25.04"\n', 0, 0, id="ubuntu-25.04"),
        pytest.param('ID=ubuntu\nVERSION_ID="24.04"\n', 0, 2, id="ubuntu-24.04-refused"),
        pytest.param('ID=ubuntu\nVERSION_ID="24.04"\n', 1, 0, id="ubuntu-24.04-forced"),
        pytest.param("ID=fedora\nVERSION_ID=42\n", 0, 2, id="fedora-refused"),
        pytest.param("ID=fedora\nVERSION_ID=42\n", 1, 0, id="fedora-forced"),
    ],
)
def test_setup_ubuntu_25_gate(tmp_path, body, force, rc):
    osr = _os_release(tmp_path, body)
    proc = _run(_setup_program(osr, force), {"C64H_OS_RELEASE": str(osr)})
    assert proc.returncode == rc, proc.stdout + proc.stderr


def test_setup_forced_non_apt_distro_skips_apt_get(tmp_path):
    osr = _os_release(tmp_path, "ID=fedora\nVERSION_ID=42\n")
    proc = _run(
        _setup_program(osr, 1, "stage_system_packages\n"),
        {"C64H_OS_RELEASE": str(osr)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "package manager=dnf" in proc.stdout, proc.stdout
    assert "SUDO|" not in proc.stdout, proc.stdout
    assert "apt-get install" not in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="25.10"\n', id="with-id-like"),
        pytest.param('ID=ubuntu\nVERSION_ID="25.04"\n', id="id-only"),
    ],
)
def test_setup_ubuntu_runs_apt_get(tmp_path, body):
    osr = _os_release(tmp_path, body)
    proc = _run(
        _setup_program(osr, 0, "stage_system_packages\n"),
        {"C64H_OS_RELEASE": str(osr)},
    )
    assert proc.returncode == 0, proc.stderr
    sudo_calls = [l for l in proc.stdout.splitlines() if l.startswith("SUDO|")]
    assert any(c.startswith("SUDO|apt-get install -y ") and " flex " in c for c in sudo_calls), sudo_calls


# ---------------------------------------------------------------------------
# #500 item 2: --smoke is opt-in, and the smoke round-trip itself
# ---------------------------------------------------------------------------


def _check_smoke(
    smoke: int, fake_py: Path, repo_root: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    program = (
        _VERIFY_STUBS
        + f"SMOKE={smoke}\nREPO_ROOT={shlex.quote(str(repo_root))}\n"
        + f"select_py_bin() {{ printf '%s' {shlex.quote(str(fake_py))}; }}\n"
        + _section(VERIFY, "# ---------- Section 2b", "# ---------- Section 3")
        + "\ncheck_smoke\n"
    )
    proc = _run(program, env)
    assert proc.returncode == 0, proc.stderr
    return proc


def _fake_python(tmp_path: Path, line: str, rc: int) -> tuple[Path, Path]:
    marker = tmp_path / "ran"
    py = tmp_path / "fake-python"
    py.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\necho {shlex.quote(line)}\nexit {rc}\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    return py, marker


def test_smoke_is_off_by_default(tmp_path):
    py, marker = _fake_python(tmp_path, "ok: fine", 0)
    proc = _check_smoke(0, py, REPO)
    assert _rows(proc.stdout) == []
    assert not marker.exists(), "the smoke runner ran without --smoke"


def test_smoke_pass_and_fail_rows(tmp_path):
    py, marker = _fake_python(tmp_path, "ok: launched VICE (pid 1)", 0)
    proc = _check_smoke(1, py, REPO)
    assert marker.exists()
    assert _rows(proc.stdout) == [["VICE launch + memory round-trip", "ok", "launched VICE (pid 1)", "1"]]

    failing = tmp_path / "failing"
    failing.mkdir()
    py2, _ = _fake_python(failing, "fail: no monitor", 1)
    proc = _check_smoke(1, py2, REPO)
    assert _rows(proc.stdout) == [["VICE launch + memory round-trip", "missing", "no monitor", "1"]]


def _load_smoke():
    spec = importlib.util.spec_from_file_location("vice_smoke_under_test", SMOKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeConfig:
    def __init__(self, **kw):
        self.kw = kw


class _FakeProc:
    instances: list["_FakeProc"] = []

    def __init__(self, config):
        self.config = config
        self.started = False
        self.stopped = False
        self._proc = None
        _FakeProc.instances.append(self)

    @property
    def pid(self):
        return 4242

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _transport_cls(corrupt: bool = False, fail_write: bool = False):
    class _FakeTransport:
        closed = False
        mem: dict[int, int] = {}

        def __init__(self, port):
            self.port = port

        def write_memory(self, addr, data):
            if fail_write:
                raise OSError("monitor went away")
            for i, b in enumerate(data):
                type(self).mem[addr + i] = b

        def read_memory(self, addr, length):
            out = bytes(type(self).mem.get(addr + i, 0) for i in range(length))
            return bytes([out[0] ^ 1]) + out[1:] if corrupt else out

        def close(self):
            type(self).closed = True

    return _FakeTransport


def _smoke(mod, **kw):
    _FakeProc.instances.clear()
    return mod.run_smoke(
        vice_process_cls=_FakeProc, vice_config_cls=_FakeConfig, port=6555, connect_timeout=1, **kw
    )


def test_smoke_round_trip_uses_the_harness_launcher_headless():
    mod = _load_smoke()
    t = _transport_cls()
    passed, detail = _smoke(mod, transport_cls=t)
    assert passed, detail
    (vice,) = _FakeProc.instances
    assert vice.config.kw["sound"] is False and vice.config.kw["minimize"] is True
    assert vice.config.kw["port"] == 6555
    assert vice.started and vice.stopped
    assert t.closed
    assert bytes(t.mem[mod.SMOKE_ADDR + i] for i in range(len(mod.SMOKE_PATTERN))) == mod.SMOKE_PATTERN


def test_smoke_detects_a_read_back_mismatch():
    mod = _load_smoke()
    passed, detail = _smoke(mod, transport_cls=_transport_cls(corrupt=True))
    assert not passed
    assert "mismatch" in detail
    assert _FakeProc.instances[0].stopped


def test_smoke_stops_its_own_vice_on_error():
    mod = _load_smoke()
    passed, detail = _smoke(mod, transport_cls=_transport_cls(fail_write=True))
    assert not passed
    assert "monitor went away" in detail
    assert _FakeProc.instances[0].stopped


# ---------------------------------------------------------------------------
# #500 item 1 (found in the VM run): a headless GTK3 VICE, and pipefail
# ---------------------------------------------------------------------------

# A stand-in x64sc (a shell script, not VICE) that behaves like the GTK3
# build in the Ubuntu 25.10 VM: `--help` without a display prints only the
# GTK warning and exits 1; `-console --help` prints the full ~1900-line list
# with the ethernet flag near the end, which is what made an
# `x64sc ... | grep -q` pipeline exit 141 under pipefail.
_FAKE_HEADLESS_X64SC = r"""#!/bin/sh
case "$*" in
  --version) echo "x64sc (VICE 3.10)"; exit 0 ;;
  "-console --help")
     i=0; while [ $i -lt 1900 ]; do echo "        -filler$i   padding option padding option padding"; i=$((i+1)); done
     echo "        -ethernetcart <Type>"
     echo "        -binarymonitor"
     echo "        -remotemonitor"
     i=0; while [ $i -lt 1900 ]; do echo "        -tail$i   padding option padding option padding"; i=$((i+1)); done
     exit 0 ;;
  --help) echo "(x64sc:1): Gtk-WARNING **: cannot open display: " >&2; exit 1 ;;
  *) exit 1 ;;
esac
"""


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    x = bindir / "x64sc"
    x.write_text(_FAKE_HEADLESS_X64SC)
    x.chmod(0o755)
    c = bindir / "c1541"
    c.write_text("#!/bin/sh\necho 'c1541 (VICE 3.10)'\n")
    c.chmod(0o755)
    return bindir


def test_setup_detects_a_headless_vice_build(tmp_path):
    bindir = _fake_bin(tmp_path)
    program = (
        "set -u\nset -o pipefail\n"
        + _section(SETUP, "# ---------- Stage 2", "stage_vice_build() {")
        + '\nvice_already_installed; echo "RC=$?"\n'
    )
    proc = _run(program, {"PATH": f"{bindir}:/usr/bin:/bin"})
    assert "RC=0" in proc.stdout, proc.stdout + proc.stderr


def test_verify_reads_the_help_of_a_headless_vice_build(tmp_path):
    bindir = _fake_bin(tmp_path)
    program = (
        "set -u\n"
        + "record() { printf 'ROW|%s|%s|%s|%s\\n' \"$2\" \"$3\" \"$4\" \"$5\"; }\n"
        + "add_hint() { printf 'HINT|%s\\n' \"$1\"; }\n"
        + 'have_cmd() { command -v "$1" >/dev/null 2>&1; }\n'
        + _section(VERIFY, "# ---------- Section 1: VICE", "# ---------- Section 2")
        + "\ncheck_vice\n"
    )
    proc = _run(program, {"PATH": f"{bindir}:/usr/bin:/bin"})
    rows = {r[0]: r[1] for r in _rows(proc.stdout)}
    assert rows["ethernet cart support"] == "ok", proc.stdout
    assert rows["binary monitor support"] == "ok", proc.stdout


def test_setup_harness_stage_installs_dev_extras(tmp_path):
    """Stage 3 installs ``.[dev]`` and does not skip a venv that lacks pytest."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "calls"
    repo = tmp_path / "repo"
    (repo / "src" / "c64_test_harness").mkdir(parents=True)
    # python: the harness resolves to this repo; pytest is absent.
    (venv / "bin" / "python").write_text(
        "#!/bin/sh\n"
        f'echo "python $*" >> {shlex.quote(str(log))}\n'
        'case "$*" in\n'
        f"  *os.path.dirname*) echo {shlex.quote(str(repo / 'src' / 'c64_test_harness'))} ;;\n"
        "  *'import pytest'*) exit 1 ;;\n"
        "esac\nexit 0\n"
    )
    (venv / "bin" / "pip").write_text(f'#!/bin/sh\necho "pip $*" >> {shlex.quote(str(log))}\n')
    for f in ("python", "pip"):
        (venv / "bin" / f).chmod(0o755)
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + f"DRY_RUN=0\nNO_HARNESS=0\nVENV_DIR={shlex.quote(str(venv))}\n"
        + f"REPO_ROOT={shlex.quote(str(repo))}\n"
        + _section(SETUP, "# ---------- Stage 3", "# ---------- Stage 4")
        + "\nstage_harness_install\n"
    )
    proc = _run(program)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines()
    assert f"pip install -e {repo}[dev]" in calls, calls


# A fresh Mac's first python3 on PATH is Apple's 3.9 (/usr/bin/python3, Xcode
# CLT), below requires-python >= 3.10; stage 3 used it anyway.
_FAKE_PY = r"""#!/bin/sh
case "$1" in
  -c) exec "$REAL_PY" -c 'import sys; sys.version_info = (@VER@); exec(sys.argv[1])' "$2" ;;
  -m) echo "@NAME@ $*" >> "$CALLS"
      d="$4"; mkdir -p "$d/bin"
      printf '#!/bin/sh\nexit 0\n' > "$d/bin/python"
      printf '#!/bin/sh\necho "pip $*" >> "$CALLS"\n' > "$d/bin/pip"
      chmod 755 "$d/bin/python" "$d/bin/pip" ;;
esac
"""


def _stage3_with(
    tmp_path: Path,
    interpreters: dict[str, str],
    *,
    existing_venv: str | None = None,
    dry_run: bool = False,
    os_name: str = "Linux",
    brew_python: str | None = None,
) -> tuple[subprocess.CompletedProcess, Path, list[str]]:
    """Run stage 3 with only *interpreters* (name -> "3, 9, 6") ahead of /usr/bin.

    *existing_venv* pre-creates the venv with an interpreter of that version.
    *brew_python* installs a fake Homebrew ``python3`` of that version, off
    ``PATH``, at the path ``BREW_PYTHON3`` names (``os_name="Darwin"`` only
    looks there); ``None`` leaves that path empty.
    """
    import sys

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, ver in interpreters.items():
        f = bindir / name
        f.write_text(_FAKE_PY.replace("@VER@", ver).replace("@NAME@", name))
        f.chmod(0o755)
    # /usr/bin and /bin hold a real python3 on macOS; shadow them with a
    # PATH of only the fake bin dir plus the tools the script itself needs.
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("bash", "mkdir", "chmod", "dirname", "cat"):
        real = shutil.which(tool)
        if real:
            (tools / tool).symlink_to(real)
    venv = tmp_path / "venv"
    repo = tmp_path / "repo"
    calls = tmp_path / "calls"
    calls.touch()
    if existing_venv is not None:
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.write_text(_FAKE_PY.replace("@VER@", existing_venv).replace("@NAME@", "venv-python"))
        py.chmod(0o755)
        pip = venv / "bin" / "pip"
        pip.write_text('#!/bin/sh\necho "pip $*" >> "$CALLS"\n')
        pip.chmod(0o755)
    brew_py = tmp_path / "homebrew" / "bin" / "python3"
    if brew_python is not None:
        brew_py.parent.mkdir(parents=True)
        brew_py.write_text(_FAKE_PY.replace("@VER@", brew_python).replace("@NAME@", "brew-python3"))
        brew_py.chmod(0o755)
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + f"DRY_RUN={int(dry_run)}\nNO_HARNESS=0\nVENV_DIR={shlex.quote(str(venv))}\n"
        + f"REPO_ROOT={shlex.quote(str(repo))}\nOS={os_name}\n"
        + f"BREW_PYTHON3={shlex.quote(str(brew_py))}\n"
        + "run_cmd() { shift; \"$@\"; }\n"
        + _section(SETUP, "# ---------- Stage 3", "# ---------- Stage 4")
        + "\nstage_harness_install\n"
    )
    proc = _run(program, {"PATH": f"{bindir}:{tools}", "CALLS": str(calls), "REAL_PY": sys.executable})
    assert proc.returncode == 0, proc.stderr
    venv_calls = [c for c in calls.read_text().splitlines() if " -m venv " in c]
    return proc, venv, venv_calls


def test_stage3_builds_the_venv_with_a_python_that_meets_requires_python(tmp_path):
    _proc, venv, venv_calls = _stage3_with(tmp_path, {"python3": "3, 9, 6", "python3.13": "3, 13, 16"})
    assert venv_calls == [f"python3.13 -m venv --system-site-packages {venv}"], venv_calls


def test_stage3_prefers_the_newest_python_over_a_qualifying_python3(tmp_path):
    _proc, venv, venv_calls = _stage3_with(tmp_path, {"python3": "3, 11, 9", "python3.13": "3, 13, 16"})
    assert venv_calls == [f"python3.13 -m venv --system-site-packages {venv}"], venv_calls


def test_stage3_accepts_python_3_10(tmp_path):
    _proc, venv, venv_calls = _stage3_with(tmp_path, {"python3.10": "3, 10, 0"})
    assert venv_calls == [f"python3.10 -m venv --system-site-packages {venv}"], venv_calls


def test_stage3_refuses_to_reuse_a_venv_below_requires_python(tmp_path):
    """A 3.9 venv left by the old installer used to be reused, and pip then failed."""
    proc, venv, _venv_calls = _stage3_with(tmp_path, {"python3.13": "3, 13, 16"}, existing_venv="3, 9, 6")
    out = proc.stdout + proc.stderr
    assert f"rm -rf {venv}" in out, out
    assert not [c for c in (tmp_path / "calls").read_text().splitlines() if c.startswith("pip ")]


def test_stage3_reuses_a_venv_that_meets_requires_python(tmp_path):
    proc, _venv, venv_calls = _stage3_with(tmp_path, {"python3.13": "3, 13, 16"}, existing_venv="3, 13, 16")
    assert venv_calls == []
    assert "reusing" in proc.stdout, proc.stdout


def test_stage3_dry_run_does_not_show_a_venv_it_would_refuse(tmp_path):
    proc, _venv, _calls = _stage3_with(tmp_path, {"python3": "3, 9, 6"}, dry_run=True)
    assert "-m venv" not in proc.stdout, proc.stdout
    assert "no python3 >= 3.10" in proc.stdout, proc.stdout


def test_stage3_refuses_when_no_python_meets_requires_python(tmp_path):
    proc, venv, venv_calls = _stage3_with(tmp_path, {"python3": "3, 9, 6"})
    assert venv_calls == [], venv_calls
    assert not venv.exists()
    assert "no python3 >= 3.10" in proc.stdout + proc.stderr, proc.stdout


# macOS: Homebrew's unversioned python3, whatever release it is today (owner,
# 2026-10-21: Python is unpinned, fix forward). A versioned python3.1x on PATH
# used to win even when it was older than brew's current one.
def test_stage3_macos_builds_the_venv_on_brews_python3(tmp_path):
    _proc, venv, venv_calls = _stage3_with(
        tmp_path, {"python3.14": "3, 14, 0", "python3": "3, 9, 6"}, os_name="Darwin", brew_python="3, 15, 0"
    )
    assert venv_calls == [f"brew-python3 -m venv {venv}"], venv_calls


def test_stage3_macos_refuses_a_brew_python3_below_requires_python(tmp_path):
    proc, venv, venv_calls = _stage3_with(
        tmp_path, {"python3.13": "3, 13, 16"}, os_name="Darwin", brew_python="3, 9, 6"
    )
    assert venv_calls == [], venv_calls
    assert not venv.exists()
    assert "brew install python" in proc.stdout + proc.stderr, proc.stdout


def test_stage3_macos_without_brew_python3_does_not_fall_back_to_path(tmp_path):
    proc, venv, venv_calls = _stage3_with(tmp_path, {"python3.13": "3, 13, 16"}, os_name="Darwin")
    assert venv_calls == [], venv_calls
    assert not venv.exists()
    assert "brew install python" in proc.stdout + proc.stderr, proc.stdout


def test_stage3_macos_dry_run_shows_brews_python3(tmp_path):
    proc, venv, _calls = _stage3_with(
        tmp_path, {"python3.14": "3, 14, 0"}, os_name="Darwin", brew_python="3, 15, 0", dry_run=True
    )
    brew_py = tmp_path / "homebrew" / "bin" / "python3"
    assert f"{brew_py} -m venv {venv}" in proc.stdout, proc.stdout


def test_setup_exits_not_ready_when_an_install_stage_failed(tmp_path):
    """A failed stage 3 used to end READY/0: verify fell back to another venv."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("bash", "mkdir", "dirname"):
        (bindir / tool).symlink_to(shutil.which(tool))
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + f"DRY_RUN=0\nNO_HARNESS=0\nVENV_DIR={shlex.quote(str(tmp_path / 'venv'))}\n"
        + f"REPO_ROOT={shlex.quote(str(tmp_path))}\n"
        + "run_cmd() { shift; \"$@\"; }\n"
        + _section(SETUP, "# ---------- Stage 3", "# ---------- Stage 4")
        + _section(SETUP, "# ---------- Stage 6", "# ---------- main")
        + "stage_harness_install\nVERIFY_EXIT=0\nprint_summary\n"
        + 'setup_exit_code; echo "RC=$?"\n'
    )
    proc = _run(program, {"PATH": str(bindir)})
    assert "RC=1" in proc.stdout, proc.stdout + proc.stderr
    assert "install step" in proc.stdout, proc.stdout


def test_setup_counts_one_failure_once(tmp_path):
    """No Homebrew is one failure, though stage 1 prints three [fail] lines for it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "bash").symlink_to(shutil.which("bash"))
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + "DRY_RUN=0\n"
        + _section(SETUP, "# ---------- Stage 1", "# ---------- Stage 2")
        + "(trap 'echo \"FAILURES=$STAGE_FAILURES\"' EXIT; stage_system_packages_macos)\n"
    )
    proc = _run(program, {"PATH": str(bindir)})
    assert proc.stdout.count("[fail]") == 3, proc.stdout
    assert "FAILURES=1" in proc.stdout, proc.stdout


def test_setup_macos_stage1_installs_a_python_for_the_venv(tmp_path):
    calls = tmp_path / "brew-calls"
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + "DRY_RUN=0\n"
        + f'brew() {{ echo "$*" >> {shlex.quote(str(calls))}; }}\n'
        + _section(SETUP, "# ---------- Stage 1", "# ---------- Stage 2")
        + "stage_system_packages_macos\n"
    )
    proc = _run(program)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    installs = [c for c in calls.read_text().splitlines() if c.startswith("install ")]
    assert "install python" in installs, installs
    assert not [c for c in installs if c.startswith("install python@")], installs


def test_setup_verifies_the_venv_it_built(tmp_path):
    """verify must check VENV_DIR, not fall back to the canonical venv."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("#!/bin/sh\n")
    (venv / "bin" / "python3").chmod(0o755)
    (tmp_path / "scripts").mkdir()
    verify = tmp_path / "scripts" / "verify-dev-env.sh"
    verify.write_text('#!/bin/sh\necho "VERIFIED=${VIRTUAL_ENV:-unset}"\n')
    verify.chmod(0o755)
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + f"DRY_RUN=0\nVENV_DIR={shlex.quote(str(venv))}\nREPO_ROOT={shlex.quote(str(tmp_path))}\n"
        + _section(SETUP, "# ---------- Stage 6", "# ---------- summary")
        + "run_verify\n"
    )
    proc = _run(program)
    assert f"VERIFIED={venv}" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.parametrize(("verify", "rc"), [(0, 0), (1, 1), (2, 3), (7, 3)])
def test_setup_exit_mirrors_verify_when_no_stage_failed(verify, rc):
    program = (
        "set -u\n"
        + _section(SETUP, "# ---------- logging helpers", "# ---------- arg parsing")
        + "DRY_RUN=0\n"
        + _section(SETUP, "# ---------- Stage 6", "# ---------- main")
        + f"VERIFY_EXIT={verify}\nprint_summary\n"
        + 'setup_exit_code; echo "RC=$?"\n'
    )
    proc = _run(program)
    assert f"RC={rc}" in proc.stdout, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# #510 review: --smoke must exercise this checkout's harness, not whatever
# the venv's editable .pth points at; and -console is only a fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("inherited", ["", "/elsewhere/src"], ids=["unset", "preset"])
def test_smoke_runs_against_this_checkouts_src(tmp_path, inherited):
    """The venv's editable install may point at another checkout (the
    canonical one, from a worktree); the smoke run must put
    ``$REPO_ROOT/src`` first on PYTHONPATH."""
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True)
    py = tmp_path / "fake-python"
    py.write_text('#!/bin/sh\necho "ok: PYTHONPATH=$PYTHONPATH"\nexit 0\n')
    py.chmod(0o755)
    proc = _check_smoke(1, py, repo, {"PYTHONPATH": inherited} if inherited else None)
    ((_label, status, detail, _crit),) = _rows(proc.stdout)
    assert status == "ok", detail
    entries = detail.split("PYTHONPATH=", 1)[1].split(":")
    assert entries[0] == f"{repo}/src", detail
    if inherited:
        assert inherited in entries, detail


def test_smoke_ok_line_names_the_harness_that_ran():
    import c64_test_harness

    mod = _load_smoke()
    passed, detail = _smoke(mod, transport_cls=_transport_cls())
    assert passed, detail
    assert f"harness {c64_test_harness.__file__}" in detail, detail


# Stand-in x64sc whose plain --help lists the flags and whose -console form
# fails: a machine with a display, where the fallback must not decide.
_FAKE_DISPLAY_X64SC = r"""#!/bin/sh
case "$*" in
  --version) echo "x64sc (VICE 3.10)"; exit 0 ;;
  --help)
     echo "        -ethernetcart <Type>"
     echo "        -binarymonitor"
     echo "        -remotemonitor"
     exit 0 ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""


def _display_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    x = bindir / "x64sc"
    x.write_text(_FAKE_DISPLAY_X64SC)
    x.chmod(0o755)
    c = bindir / "c1541"
    c.write_text("#!/bin/sh\necho 'c1541 (VICE 3.10)'\n")
    c.chmod(0o755)
    return bindir


def test_verify_keeps_plain_help_when_it_lists_the_flags(tmp_path):
    bindir = _display_bin(tmp_path)
    program = (
        "set -u\n"
        + "record() { printf 'ROW|%s|%s|%s|%s\\n' \"$2\" \"$3\" \"$4\" \"$5\"; }\n"
        + "add_hint() { printf 'HINT|%s\\n' \"$1\"; }\n"
        + 'have_cmd() { command -v "$1" >/dev/null 2>&1; }\n'
        + _section(VERIFY, "# ---------- Section 1: VICE", "# ---------- Section 2")
        + "\ncheck_vice\n"
    )
    proc = _run(program, {"PATH": f"{bindir}:/usr/bin:/bin"})
    rows = {r[0]: r[1] for r in _rows(proc.stdout)}
    assert rows["ethernet cart support"] == "ok", proc.stdout
    assert rows["binary monitor support"] == "ok", proc.stdout
    assert rows["text monitor support"] == "ok", proc.stdout


def test_setup_keeps_plain_help_when_it_lists_the_flags(tmp_path):
    bindir = _display_bin(tmp_path)
    program = (
        "set -u\nset -o pipefail\n"
        + _section(SETUP, "# ---------- Stage 2", "stage_vice_build() {")
        + '\nvice_already_installed; echo "RC=$?"\n'
    )
    proc = _run(program, {"PATH": f"{bindir}:/usr/bin:/bin"})
    assert "RC=0" in proc.stdout, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# The patched VICE (scripts/build-vice-eth.sh) and the x64sc sudoers row
# ---------------------------------------------------------------------------


def _vice_eth_rows(
    tmp_path: Path, check_rc: int, *, x64sc: bool = True, bpf_mode: int = 0o606
) -> tuple[dict[str, list[str]], list[str]]:
    fake = tmp_path / "build-vice-eth.sh"
    fake.write_text(f"#!/bin/sh\necho 'build-vice-eth: state {check_rc}'\nexit {check_rc}\n")
    fake.chmod(0o755)
    stock = tmp_path / "x64sc"
    if x64sc:
        stock.write_text("#!/bin/sh\n")
        stock.chmod(0o755)
    proc = _check_system(
        "Darwin",
        {
            "C64H_DEV_DIR": str(_fake_dev(tmp_path, {"bpf0": bpf_mode})),
            "C64H_BUILD_VICE_ETH": str(fake),
            "C64H_X64SC_PATH": str(stock),
        },
    )
    rows = {r[0]: r[1:] for r in _rows(proc.stdout)}
    return rows, _hints(proc.stdout)


@pytest.mark.parametrize(("check_rc", "status"), [(0, "ok"), (1, "warn"), (2, "missing"), (5, "unknown")])
def test_patched_vice_row_follows_build_check(tmp_path, check_rc, status):
    rows, _hints_ = _vice_eth_rows(tmp_path, check_rc)
    assert rows["patched VICE (ethernet without root)"][0] == status


def test_stale_patched_vice_hints_a_rebuild(tmp_path):
    _rows_, hints = _vice_eth_rows(tmp_path, 1)
    assert any("scripts/build-vice-eth.sh" in h and "Rebuild" in h for h in hints), hints


@pytest.mark.parametrize("check_rc", [0, 1])
def test_a_patched_build_makes_the_x64sc_rule_unnecessary(tmp_path, check_rc):
    rows, hints = _vice_eth_rows(tmp_path, check_rc)
    status, detail, _crit = rows["NOPASSWD sudo for x64sc"]
    assert status == "ok"
    assert "not needed" in detail
    assert not any("NOPASSWD rule for" in h for h in hints), hints


def test_without_a_patched_build_the_x64sc_rule_is_required(tmp_path):
    rows, hints = _vice_eth_rows(tmp_path, 2)
    assert rows["NOPASSWD sudo for x64sc"][0] == "warn"
    assert any("build-vice-eth.sh" in h and "NOPASSWD rule for" in h for h in hints), hints


def test_no_x64sc_and_no_patched_build_has_no_sudo_row(tmp_path):
    rows, _hints_ = _vice_eth_rows(tmp_path, 2, x64sc=False)
    assert "NOPASSWD sudo for x64sc" not in rows


def test_a_patched_build_without_bpf_access_still_needs_the_x64sc_rule(tmp_path):
    """The harness falls back to stock + sudo when no node is usable."""
    rows, _hints_ = _vice_eth_rows(tmp_path, 0, bpf_mode=0o600)
    assert rows["NOPASSWD sudo for x64sc"][0] == "warn"


# ---------------------------------------------------------------------------
# The bridge helper rows: installed root-owned copies or the repo's (#541)
# ---------------------------------------------------------------------------

_BRIDGE_SCRIPTS = ("setup-bridge-feth-macos.sh", "teardown-bridge-feth-macos.sh",
                   "cleanup-bridge-feth-macos.sh")


def _helpers(directory: Path) -> Path:
    directory.mkdir(parents=True)
    for name in _BRIDGE_SCRIPTS:
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)
    return directory


def _bridge_rows(tmp_path: Path, allowed: list[Path], *, libexec: bool = True,
                 env: dict[str, str] | None = None):
    lib = _helpers(tmp_path / "libexec") if libexec else tmp_path / "nolibexec"
    repo = tmp_path / "repo"
    _helpers(repo / "scripts")
    # Authorises only a bare run of the listed paths, as a "" rule does.
    cases = "|".join(shlex.quote(str(p)) for p in allowed) or "/never"
    stub = (
        'nopasswd_for() { [ "${2:-}" = noargs ] || return 1; '
        f'case "$1" in {cases}) return 0 ;; esac; return 1; }}\n'
    )
    proc = _check_system(
        "Darwin",
        {"C64H_DEV_DIR": str(_fake_dev(tmp_path, {"bpf0": 0o606})),
         "C64H_BRIDGE_LIBEXEC": str(lib), "C64H_REPO_ROOT": str(repo), **(env or {})},
        stubs=stub,
    )
    rows = {r[0]: r[1:] for r in _rows(proc.stdout) if r[0].startswith("NOPASSWD sudo for ") and "x64sc" not in r[0]}
    return rows, _hints(proc.stdout), lib, repo


def test_bridge_rows_accept_the_installed_copies(tmp_path):
    lib = tmp_path / "libexec"
    rows, hints, lib, _repo = _bridge_rows(tmp_path, [lib / n for n in _BRIDGE_SCRIPTS])
    assert set(rows) == {f"NOPASSWD sudo for {n}" for n in _BRIDGE_SCRIPTS}, rows
    for name in _BRIDGE_SCRIPTS:
        status, detail, _crit = rows[f"NOPASSWD sudo for {name}"]
        assert status == "ok", detail
        assert str(lib / name) in detail
    assert not any("sudoers drop-in" in h for h in hints), hints


def test_bridge_rows_accept_repo_rules_beside_installed_copies(tmp_path):
    repo = tmp_path / "repo"
    rows, _hints_, _lib, repo = _bridge_rows(tmp_path, [repo / "scripts" / n for n in _BRIDGE_SCRIPTS])
    for name in _BRIDGE_SCRIPTS:
        status, detail, _crit = rows[f"NOPASSWD sudo for {name}"]
        assert status == "ok" and str(repo / "scripts" / name) in detail, detail


def test_an_unauthorised_bridge_row_names_the_installed_copy(tmp_path):
    lib = tmp_path / "libexec"
    rows, hints, lib, repo = _bridge_rows(tmp_path, [lib / _BRIDGE_SCRIPTS[0]])
    assert rows[f"NOPASSWD sudo for {_BRIDGE_SCRIPTS[0]}"][0] == "ok"
    status, detail, _crit = rows[f"NOPASSWD sudo for {_BRIDGE_SCRIPTS[1]}"]
    assert status == "warn"
    assert str(lib / _BRIDGE_SCRIPTS[1]) in detail and str(repo) not in detail, detail
    assert any("sudoers" in h for h in hints), hints


def test_without_installed_copies_the_rows_use_the_repo(tmp_path):
    repo = tmp_path / "repo"
    rows, _hints_, _lib, repo = _bridge_rows(tmp_path, [], libexec=False)
    status, detail, _crit = rows[f"NOPASSWD sudo for {_BRIDGE_SCRIPTS[0]}"]
    assert status == "warn" and str(repo / "scripts" / _BRIDGE_SCRIPTS[0]) in detail, detail


def test_the_helper_dir_override_is_the_only_place_looked(tmp_path):
    lib = tmp_path / "libexec"
    other = _helpers(tmp_path / "other")
    rows, _hints_, _lib, _repo = _bridge_rows(
        tmp_path, [lib / n for n in _BRIDGE_SCRIPTS], env={"C64_BRIDGE_HELPER_DIR": str(other)}
    )
    status, detail, _crit = rows[f"NOPASSWD sudo for {_BRIDGE_SCRIPTS[0]}"]
    assert status == "warn" and str(other / _BRIDGE_SCRIPTS[0]) in detail, detail


def test_bridge_rows_prefer_libexec_when_both_are_authorised(tmp_path):
    lib, repo = tmp_path / "libexec", tmp_path / "repo"
    rows, _h, lib, _r = _bridge_rows(
        tmp_path, [lib / _BRIDGE_SCRIPTS[0], repo / "scripts" / _BRIDGE_SCRIPTS[0]])
    _status, detail, _crit = rows[f"NOPASSWD sudo for {_BRIDGE_SCRIPTS[0]}"]
    assert detail.endswith(str(lib / _BRIDGE_SCRIPTS[0])), detail


def test_a_whitespace_only_helper_dir_means_unset(tmp_path):
    lib = tmp_path / "libexec"
    rows, _h, lib, _r = _bridge_rows(
        tmp_path, [lib / n for n in _BRIDGE_SCRIPTS], env={"C64_BRIDGE_HELPER_DIR": "  "})
    assert rows[f"NOPASSWD sudo for {_BRIDGE_SCRIPTS[0]}"][0] == "ok"


def test_a_missing_repo_script_gets_no_row(tmp_path):
    """Matches bridge_helper_candidates: an absent script is no candidate."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    proc = _check_system(
        "Darwin",
        {"C64H_DEV_DIR": str(_fake_dev(tmp_path, {"bpf0": 0o606})),
         "C64H_BRIDGE_LIBEXEC": str(tmp_path / "nolibexec"), "C64H_REPO_ROOT": str(repo)},
    )
    labels = [r[0] for r in _rows(proc.stdout)]
    assert not any(f"NOPASSWD sudo for {n}" in labels for n in _BRIDGE_SCRIPTS), labels
