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


def _check_system(uname: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    program = (
        _VERIFY_STUBS
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
    assert "not other-rw: bpf4 " in detail, detail
    for good in ("bpf0", "bpf1", "bpf2", "bpf3"):
        assert good not in detail.split("(")[0], detail
    assert any("bpf4+" in h for h in _hints(proc.stdout)), proc.stdout


def test_bpf_every_failing_node_is_named(tmp_path):
    dev = _fake_dev(tmp_path, {"bpf0": 0o600, "bpf1": 0o606, "bpf10": 0o604, "bpf2": 0o602})
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "warn"
    failing = detail.split("not other-rw: ", 1)[1].split(" (", 1)[0].split()
    assert sorted(failing) == ["bpf0", "bpf10", "bpf2"], detail


def test_bpf_all_nodes_other_rw_is_ok(tmp_path):
    dev = _fake_dev(tmp_path, {"bpf0": 0o606, "bpf1": 0o666, "bpf5": 0o606})
    proc = _check_system("Darwin", {"C64H_DEV_DIR": str(dev)})
    _label, status, detail, _crit = _bpf_row(proc)
    assert status == "ok", detail
    assert "all 3 nodes" in detail, detail
    assert not any("bpf" in h for h in _hints(proc.stdout))


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
