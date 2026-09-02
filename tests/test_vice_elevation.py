"""Raw-network capability and the elevation decision it drives.

These tests pin the *real* gate VICE applies, which is not the one the
harness used to model.  ``archdep_rawnet_capability()`` (VICE 3.10,
``src/arch/shared/archdep_rawnet_capability.c``) returns true for
``geteuid() == 0``, plus a Linux-only ``CAP_NET_RAW`` branch.  It never
looks at ``/dev/bpf*``.  Its result gates *driver selection* in
``rawnetarch.c:set_ethernet_driver()``, so an unelevated macOS VICE ends
up with ``rawnet_arch_driver == NULL`` and dies in
``rawnet_arch_pre_reset()`` — SIGSEGV, no log output.

So the harness must ask "will this child have rawnet capability?" and,
when the answer is no, refuse to launch and hand the operator the exact
command instead of a crash with no diagnostics.

Everything here is mock/tmp-file based: nothing spawns an emulator.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from c64_test_harness.backends import vice_elevation as ve
from c64_test_harness.backends import vice_lifecycle
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess


# --------------------------------------------------------------- helpers

#: Enough of a Mach-O/ELF payload to look like a real file; only the
#: marker strings matter to the scanner.
_FILLER = b"\x00\x01\x02\x03" * 512


def _fake_x64sc(tmp_path, name: str, *, ethernet: bool):
    path = tmp_path / name
    body = _FILLER
    if ethernet:
        body += b"\x00ETHERNET_DRIVER\x00" + _FILLER + b"\x00ETHERNETCART_ACTIVE\x00"
    else:
        body += b"\x00SoundDeviceName\x00" + _FILLER
    path.write_bytes(body + _FILLER)
    path.chmod(0o755)
    return str(path)


def _no_sudo(monkeypatch):
    monkeypatch.setattr(ve, "sudo_can_run", lambda binary: False)


def _yes_sudo(monkeypatch):
    monkeypatch.setattr(ve, "sudo_can_run", lambda binary: True)


def _as_uid(monkeypatch, uid: int):
    monkeypatch.setattr(ve.os, "geteuid", lambda: uid)


def _stub_features(monkeypatch, *, rawnet=True, pcap=True, tuntap=False):
    """Answer the build probe without exec'ing anything.

    Needed wherever a test patches ``subprocess.Popen``: ``subprocess`` is
    one module object, so a Popen sentinel would also fire inside
    ``subprocess.run`` when ``vice_features`` shells out to ``-features``.
    """
    stub = lambda path: ve.ViceFeatures(rawnet, pcap, tuntap, "-features", True)
    monkeypatch.setattr(ve, "vice_features", stub)
    # vice_lifecycle imported the name directly, so patch it there too.
    monkeypatch.setattr(vice_lifecycle, "vice_features", stub)


# ------------------------------------------------- build capability scan

def test_build_scan_accepts_a_binary_with_rawnet_strings(tmp_path):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    assert ve.vice_binary_supports_ethernet(exe) is True


def test_build_scan_rejects_a_binary_built_without_rawnet(tmp_path):
    exe = _fake_x64sc(tmp_path, "x64sc-noeth", ethernet=False)
    assert ve.vice_binary_supports_ethernet(exe) is False


def test_build_scan_rejects_a_missing_binary(tmp_path):
    assert ve.vice_binary_supports_ethernet(str(tmp_path / "nope")) is False


def test_build_scan_sees_a_rewritten_binary(tmp_path):
    """Caching must key on the file, not just the path."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)
    assert ve.vice_binary_supports_ethernet(exe) is False
    _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    assert ve.vice_binary_supports_ethernet(exe) is True


@pytest.mark.skipif(
    not os.path.exists("/opt/homebrew/bin/x64sc"),
    reason="no Homebrew x64sc on this host",
)
def test_homebrew_x64sc_is_ethernet_capable():
    """Ground truth, not an assumption.

    Issue #144 concluded Homebrew's bottle has "ethernet compiled out".
    It does not: the binary carries the full rawnet resource surface and
    links libpcap.  The crash it was blamed for is the unelevated NULL
    driver.  This test fails loudly if that ever stops being true.
    """
    assert ve.vice_binary_supports_ethernet("/opt/homebrew/bin/x64sc") is True


# ------------------------------------------------- archdep_rawnet mirror

def test_root_has_rawnet_capability(monkeypatch):
    _as_uid(monkeypatch, 0)
    assert ve.rawnet_capability() is True


def test_unprivileged_macos_has_no_rawnet_capability(monkeypatch):
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    assert ve.rawnet_capability() is False


def test_capability_ignores_dev_bpf_permissions(monkeypatch, tmp_path):
    """The defect this module replaces.

    A rig that ran ``chmod o+rw /dev/bpf*`` still has no rawnet
    capability: VICE never inspects those nodes.  Verified live — with
    ``/dev/bpf0`` at ``crw----rw-`` and uid 501, VICE still refuses the
    pcap driver.
    """
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    # World-writable BPF nodes everywhere.
    monkeypatch.setattr(
        ve.os, "access", lambda *a, **k: pytest.fail("must not stat /dev/bpf*")
    )
    assert ve.rawnet_capability() is False


def test_linux_cap_net_raw_grants_capability(monkeypatch):
    """VICE's Linux-only CAP_NET_RAW branch."""
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "linux")
    monkeypatch.setattr(ve, "_has_cap_net_raw", lambda: True)
    assert ve.rawnet_capability() is True


def test_as_root_argument_answers_for_the_child(monkeypatch):
    """We ask about the child we are about to launch, not about ourselves."""
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    assert ve.rawnet_capability(as_root=True) is True
    assert ve.rawnet_capability(as_root=False) is False


# ----------------------------------------------------- driver gate rules

def test_pcap_driver_is_gated():
    assert ve.driver_requires_root("pcap") is True


def test_tuntap_driver_is_not_gated():
    """``rawnetarch.c`` selects tuntap without consulting the capability."""
    assert ve.driver_requires_root("tuntap") is False


def test_default_driver_is_pcap_on_macos(monkeypatch):
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    assert ve.driver_requires_root("") is True


def test_default_driver_is_tuntap_on_linux(monkeypatch):
    monkeypatch.setattr(ve.sys, "platform", "linux")
    assert ve.driver_requires_root("") is False


# ------------------------------------------------------- the launch plan

def _eth_argv(binary="/opt/eth/x64sc"):
    return [binary, "-addconfig", "/tmp/x.rc", "-ethernetioif", "feth0",
            "-ethernetiodriver", "pcap"]


def test_plan_leaves_a_non_ethernet_launch_alone(monkeypatch):
    _as_uid(monkeypatch, 501)
    _no_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=False)
    plan = ve.plan_vice_launch(cfg, ["/opt/eth/x64sc", "-warp"])
    assert plan.argv == ["/opt/eth/x64sc", "-warp"]
    assert plan.sudo_wrapped is False
    assert plan.elevated is False


def test_plan_wraps_with_sudo_when_elevation_is_obtainable(monkeypatch):
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _yes_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    plan = ve.plan_vice_launch(cfg, _eth_argv())
    assert plan.argv[:2] == ["sudo", "-n"]
    assert plan.argv[2:] == _eth_argv()
    assert plan.sudo_wrapped is True
    assert plan.elevated is True


def test_plan_does_not_wrap_when_already_root(monkeypatch):
    _as_uid(monkeypatch, 0)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    monkeypatch.setattr(
        ve, "sudo_can_run", lambda b: pytest.fail("must not shell out to sudo as root")
    )
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    plan = ve.plan_vice_launch(cfg, _eth_argv())
    assert plan.argv == _eth_argv()
    assert plan.sudo_wrapped is False
    assert plan.elevated is True


def test_plan_does_not_elevate_for_tuntap(monkeypatch):
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "linux")
    monkeypatch.setattr(
        ve, "sudo_can_run", lambda b: pytest.fail("tuntap needs no elevation")
    )
    cfg = ViceConfig(ethernet=True, ethernet_driver="tuntap")
    plan = ve.plan_vice_launch(cfg, _eth_argv("/usr/bin/x64sc"))
    assert plan.sudo_wrapped is False
    assert plan.elevated is False


def test_plan_raises_when_elevation_is_unobtainable(monkeypatch):
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    with pytest.raises(ve.ViceElevationRequiredError) as excinfo:
        ve.plan_vice_launch(cfg, _eth_argv())
    assert excinfo.value.binary == "/opt/eth/x64sc"


def test_elevation_error_carries_a_runnable_command(monkeypatch):
    """The point of the exception: an operator can paste and run this."""
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    with pytest.raises(ve.ViceElevationRequiredError) as excinfo:
        ve.plan_vice_launch(cfg, _eth_argv())
    err = excinfo.value
    # The command runs the very launch we refused, elevated, with no
    # ``-n`` so an interactive sudo may prompt.
    assert err.argv[0] == "sudo"
    assert "-n" not in err.argv
    assert err.argv[1:] == _eth_argv()
    assert err.command.startswith("sudo /opt/eth/x64sc ")
    # It must never be bash-wrapped: NOPASSWD matches sudo's first
    # non-flag argv, so a wrapper defeats the sudoers entry.
    assert "bash" not in err.command
    # And it must say what to authorise, naming the exact path.
    assert "/opt/eth/x64sc" in err.sudoers_entry
    assert "NOPASSWD" in err.sudoers_entry
    assert err.sudoers_entry in str(err)
    assert err.command in str(err)


def test_plan_refuses_an_unelevated_ethernet_launch_pinned_off(monkeypatch):
    """``run_as_root=False`` used to be a way to ask for a SIGSEGV."""
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _yes_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap", run_as_root=False)
    with pytest.raises(ve.ViceElevationRequiredError):
        ve.plan_vice_launch(cfg, _eth_argv())


def test_escape_hatch_downgrades_the_refusal_to_a_warning(monkeypatch, caplog):
    """Deliberate opt-out for a capability we cannot see (e.g. file caps)."""
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    monkeypatch.setenv(ve.ALLOW_UNELEVATED_ENV, "1")
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    with caplog.at_level("WARNING"):
        plan = ve.plan_vice_launch(cfg, _eth_argv())
    assert plan.sudo_wrapped is False
    assert plan.elevated is False
    assert any(ve.ALLOW_UNELEVATED_ENV in r.message for r in caplog.records)


def test_plan_honours_explicit_run_as_root_without_ethernet(monkeypatch):
    _as_uid(monkeypatch, 501)
    _yes_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=False, run_as_root=True)
    plan = ve.plan_vice_launch(cfg, ["/usr/bin/x64sc", "-warp"])
    assert plan.argv[:2] == ["sudo", "-n"]


# ------------------------------------------------------- the sudo probe

#: Real ``sudo -n -l`` output from this bench, trimmed. Note the
#: ``(ALL) ALL`` line: the user may run anything as root *with a
#: password*, so mere permission proves nothing about an unattended run.
_SUDO_LISTING = """\
Matching Defaults entries for someone on Offensive-Bias:
    env_reset, env_keep+=BLOCKSIZE, lecture_file=/etc/sudo_lecture, !log_allowed

User someone may run the following commands on Offensive-Bias:
    (ALL) ALL
    (root) NOPASSWD: /Users/someone/Documents/c64-test-harness/scripts/setup-bridge-feth-macos.sh, \
/Users/someone/Documents/c64-test-harness/scripts/teardown-bridge-feth-macos.sh
    (root) NOPASSWD: /opt/homebrew/bin/x64sc
    (root) NOPASSWD: /opt/homebrew/bin/brew reinstall --HEAD vice, /opt/homebrew/bin/brew install --HEAD vice
"""


def _sudo_listing(monkeypatch, text: str, *, rc: int = 0, calls: list | None = None):
    ve.sudo_authorisation.cache_clear()

    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, rc, text, "")

    monkeypatch.setattr(ve.subprocess, "run", fake_run)


def test_permission_to_run_is_not_authorisation_to_run_unattended(monkeypatch):
    """The bug this test exists for.

    ``sudo -n -l -- <cmd>`` exits 0 for any command the user may run at
    all, including under a password-requiring ``(ALL) ALL``. On this
    bench it returns 0 for /bin/ls. Only a NOPASSWD rule means the
    harness can elevate unattended.
    """
    _sudo_listing(monkeypatch, _SUDO_LISTING)
    assert ve.sudo_can_run("/opt/homebrew/bin/x64sc") is True
    # Permitted via (ALL) ALL, but it would prompt -- so: no.
    assert ve.sudo_can_run("/Users/someone/.local/opt/vice-3.10-ethernet/bin/x64sc") is False
    assert ve.sudo_can_run("/bin/ls") is False


def test_nopasswd_rule_may_list_several_commands(monkeypatch):
    _sudo_listing(monkeypatch, _SUDO_LISTING)
    for path in (
        "/Users/someone/Documents/c64-test-harness/scripts/setup-bridge-feth-macos.sh",
        "/Users/someone/Documents/c64-test-harness/scripts/teardown-bridge-feth-macos.sh",
    ):
        assert ve.sudo_can_run(path) is True


def test_an_argument_restricted_rule_does_not_authorise_the_binary(monkeypatch):
    """`NOPASSWD: /opt/homebrew/bin/brew reinstall --HEAD vice` authorises
    that one command line, not brew in general."""
    _sudo_listing(monkeypatch, _SUDO_LISTING)
    assert ve.sudo_can_run("/opt/homebrew/bin/brew") is False


def test_blanket_nopasswd_all_authorises_everything(monkeypatch):
    _sudo_listing(monkeypatch, "User x may run the following commands:\n    (ALL) NOPASSWD: ALL\n")
    assert ve.sudo_can_run("/anything/at/all") is True


def test_no_nopasswd_rules_means_no_unattended_elevation(monkeypatch):
    _sudo_listing(monkeypatch, "User x may run the following commands:\n    (ALL) ALL\n")
    assert ve.sudo_can_run("/opt/homebrew/bin/x64sc") is False


def test_sudo_listing_is_read_once(monkeypatch):
    calls: list = []
    _sudo_listing(monkeypatch, _SUDO_LISTING, calls=calls)
    for _ in range(4):
        ve.sudo_can_run("/opt/homebrew/bin/x64sc")
    assert len(calls) == 1
    assert calls[0][0] == ["sudo", "-n", "-l"]


def test_sudo_probe_survives_a_missing_sudo(monkeypatch):
    ve.sudo_authorisation.cache_clear()

    def boom(argv, **kwargs):
        raise OSError("no sudo")

    monkeypatch.setattr(ve.subprocess, "run", boom)
    assert ve.sudo_can_run("/opt/eth/x64sc") is False


def test_sudo_probe_survives_a_refused_listing(monkeypatch):
    _sudo_listing(monkeypatch, "", rc=1)
    assert ve.sudo_can_run("/opt/eth/x64sc") is False


def test_sudo_probe_never_prompts(monkeypatch):
    """A probe that blocks on a password prompt would hang the suite."""
    calls: list = []
    _sudo_listing(monkeypatch, _SUDO_LISTING, calls=calls)
    ve.sudo_can_run("/opt/homebrew/bin/x64sc")
    _, kwargs = calls[0]
    assert kwargs.get("stdin") is subprocess.DEVNULL
    assert kwargs.get("timeout")


# ---------------------------------------------- ethernet binary resolver

def test_resolver_leaves_non_ethernet_runs_on_the_path_binary():
    cfg = ViceConfig(executable="x64sc", ethernet=False, ethernet_executable="")
    assert vice_lifecycle.resolve_vice_executable(cfg) == "x64sc"


def test_resolver_accepts_an_ethernet_capable_path_binary(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    cfg = ViceConfig(executable=exe, ethernet=True, ethernet_executable="")
    assert vice_lifecycle.resolve_vice_executable(cfg) == exe


def test_resolver_prefers_the_configured_ethernet_build(tmp_path):
    path_exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    eth_exe = _fake_x64sc(tmp_path, "x64sc-eth", ethernet=True)
    cfg = ViceConfig(executable=path_exe, ethernet=True, ethernet_executable=eth_exe)
    assert vice_lifecycle.resolve_vice_executable(cfg) == eth_exe


def test_resolver_raises_instead_of_silently_falling_back(tmp_path):
    """The #144 shape: ethernet on, binary cannot do ethernet, tests pass
    vacuously against emulated CS8900 registers."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)
    cfg = ViceConfig(executable=exe, ethernet=True, ethernet_executable="")
    with pytest.raises(vice_lifecycle.ViceEthernetBinaryError) as excinfo:
        vice_lifecycle.resolve_vice_executable(cfg)
    msg = str(excinfo.value)
    assert vice_lifecycle.ETHERNET_VICE_BIN_ENV in msg
    assert "ethernet_executable" in msg


def test_resolver_raises_when_the_binary_does_not_exist(tmp_path):
    cfg = ViceConfig(executable=str(tmp_path / "absent"), ethernet=True)
    with pytest.raises(vice_lifecycle.ViceEthernetBinaryError):
        vice_lifecycle.resolve_vice_executable(cfg)


def test_resolver_raises_when_the_configured_build_is_not_ethernet(tmp_path):
    good = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    bad = _fake_x64sc(tmp_path, "x64sc-eth", ethernet=False)
    cfg = ViceConfig(executable=good, ethernet=True, ethernet_executable=bad)
    with pytest.raises(vice_lifecycle.ViceEthernetBinaryError):
        vice_lifecycle.resolve_vice_executable(cfg)


# ------------------------------------------------ ViceProcess integration

def test_start_refuses_before_spawning_anything(tmp_path, monkeypatch):
    """No process may be created when the child could not capture."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    _stub_features(monkeypatch)
    monkeypatch.setattr(
        vice_lifecycle.subprocess, "Popen",
        lambda *a, **k: pytest.fail("must not spawn VICE"),
    )
    cfg = ViceConfig(
        executable=exe, ethernet=True, ethernet_driver="pcap",
        ethernet_interface="feth0", monitor=False,
    )
    proc = ViceProcess(cfg)
    with pytest.raises(ve.ViceElevationRequiredError):
        proc.start()


def test_start_cleans_up_the_temp_vicerc_when_it_refuses(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    _stub_features(monkeypatch)
    monkeypatch.setattr(
        vice_lifecycle.subprocess, "Popen",
        lambda *a, **k: pytest.fail("must not spawn VICE"),
    )
    cfg = ViceConfig(
        executable=exe, ethernet=True, ethernet_driver="pcap",
        ethernet_interface="feth0", monitor=False,
    )
    proc = ViceProcess(cfg)
    with pytest.raises(ve.ViceElevationRequiredError):
        proc.start()
    assert proc._tmp_vicerc is None


def test_start_refuses_an_ethernet_less_binary_before_spawning(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)
    _as_uid(monkeypatch, 0)  # elevation is not the problem here
    _stub_features(monkeypatch, rawnet=False)
    monkeypatch.setattr(
        vice_lifecycle.subprocess, "Popen",
        lambda *a, **k: pytest.fail("must not spawn VICE"),
    )
    cfg = ViceConfig(executable=exe, ethernet=True, monitor=False)
    proc = ViceProcess(cfg)
    with pytest.raises(vice_lifecycle.ViceEthernetBinaryError):
        proc.start()


def test_public_api_exports_the_new_names():
    import c64_test_harness as pkg

    for name in (
        "ViceEthernetError",
        "ViceEthernetBinaryError",
        "ViceElevationRequiredError",
        "ViceLaunchPlan",
        "plan_vice_launch",
        "vice_binary_supports_ethernet",
    ):
        assert name in pkg.__all__
        assert getattr(pkg, name) is not None


def test_the_bpf_heuristic_is_gone():
    """Its presence is what made the old decision look defensible."""
    assert not hasattr(vice_lifecycle, "bpf_capture_available")
    assert not hasattr(vice_lifecycle, "BPF_NODES_PER_VICE")


# =====================================================================
# Capability probing via ``x64sc -features`` (owner design change:
# stand up the Homebrew binary intelligently rather than requiring a
# separately-built VICE).
# =====================================================================

_FEATURES_OUT = """\
HAVE_DEBUG                no   Enable debugging code.
HAVE_RAWNET               yes  Enable raw ethernet emulation.
HAVE_PCAP                 yes  Use the PCAP library.
HAVE_TUNTAP               no   Support for TUN/TAP virtual network interface.
"""

_FEATURES_NO_RAWNET = """\
HAVE_DEBUG                no   Enable debugging code.
HAVE_RAWNET               no   Enable raw ethernet emulation.
HAVE_PCAP                 no   Use the PCAP library.
HAVE_TUNTAP               no   Support for TUN/TAP virtual network interface.
"""


def _fake_features(monkeypatch, output: str, *, calls: list | None = None, rc: int = 0):
    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, output, "")

    monkeypatch.setattr(ve.subprocess, "run", fake_run)


def test_features_probe_reads_the_binarys_own_report(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)  # scan would say no
    calls: list = []
    _fake_features(monkeypatch, _FEATURES_OUT, calls=calls)
    feat = ve.vice_features(exe)
    assert feat.rawnet is True
    assert feat.pcap is True
    assert feat.tuntap is False
    assert feat.source == "-features"
    # It asks the binary, with the documented flags, and nothing else.
    #
    # ``-default`` is load-bearing, not decoration: without it the
    # child reads the ambient vicerc, and one carrying
    # ``LogToStdout=0`` sends the feature rows somewhere we are not
    # reading -- measured at 0 rows against 36. This pins the argv;
    # ``test_the_features_probe_ignores_an_ambient_vicerc`` pins the
    # effect against a real binary, because an argv assertion alone
    # cannot tell you the flag does what it is here for.
    assert calls == [[exe, "-default", "-features"]]


def test_features_probe_reports_a_build_without_rawnet(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)  # scan would say yes
    _fake_features(monkeypatch, _FEATURES_NO_RAWNET)
    assert ve.vice_features(exe).rawnet is False


def test_features_probe_is_cached_per_binary(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    calls: list = []
    _fake_features(monkeypatch, _FEATURES_OUT, calls=calls)
    for _ in range(5):
        ve.vice_features(exe)
    assert len(calls) == 1


def test_features_probe_falls_back_to_the_binary_scan(tmp_path, monkeypatch):
    """A binary we cannot exec still gets an honest answer."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)

    def boom(argv, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(ve.subprocess, "run", boom)
    feat = ve.vice_features(exe)
    assert feat.rawnet is True
    assert feat.source == "scan"


def test_scan_fallback_does_not_claim_driver_knowledge(tmp_path, monkeypatch):
    """The byte scan cannot tell pcap from tuntap; it must not pretend."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    monkeypatch.setattr(
        ve.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    assert ve.vice_features(exe).drivers_known is False


def test_supports_ethernet_uses_the_features_probe(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _fake_features(monkeypatch, _FEATURES_NO_RAWNET)
    assert ve.vice_binary_supports_ethernet(exe) is False


@pytest.mark.skipif(
    not os.path.exists("/opt/homebrew/bin/x64sc"),
    reason="no Homebrew x64sc on this host",
)
def test_homebrew_x64sc_reports_rawnet_and_pcap():
    """Ground truth for the design decision to drop the custom build."""
    feat = ve.vice_features("/opt/homebrew/bin/x64sc")
    assert feat.source == "-features"
    assert feat.rawnet is True
    assert feat.pcap is True


# -------------------------------------------- driver-vs-build agreement

def test_resolver_rejects_a_driver_the_build_lacks(tmp_path, monkeypatch):
    """Asking for tuntap on a pcap-only build is the same NULL-driver
    SIGSEGV by another route."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _fake_features(monkeypatch, _FEATURES_OUT)  # HAVE_TUNTAP no
    cfg = ViceConfig(executable=exe, ethernet=True, ethernet_driver="tuntap")
    with pytest.raises(vice_lifecycle.ViceEthernetBinaryError) as excinfo:
        vice_lifecycle.resolve_vice_executable(cfg)
    assert "tuntap" in str(excinfo.value)


def test_resolver_accepts_a_driver_the_build_has(tmp_path, monkeypatch):
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _fake_features(monkeypatch, _FEATURES_OUT)
    cfg = ViceConfig(executable=exe, ethernet=True, ethernet_driver="pcap")
    assert vice_lifecycle.resolve_vice_executable(cfg) == exe


def test_resolver_does_not_second_guess_the_scan_fallback(tmp_path, monkeypatch):
    """No driver knowledge means no driver refusal."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    monkeypatch.setattr(
        ve.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    cfg = ViceConfig(executable=exe, ethernet=True, ethernet_driver="tuntap")
    assert vice_lifecycle.resolve_vice_executable(cfg) == exe


def test_unset_ethernet_bin_is_the_normal_case(tmp_path, monkeypatch):
    """$VICE_ETHERNET_BIN is an override, not a requirement."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    monkeypatch.delenv(vice_lifecycle.ETHERNET_VICE_BIN_ENV, raising=False)
    _fake_features(monkeypatch, _FEATURES_OUT)
    cfg = ViceConfig(executable=exe, ethernet=True)
    assert cfg.ethernet_executable == ""
    assert vice_lifecycle.resolve_vice_executable(cfg) == exe


# ------------------------------------------- the path sudo actually sees

def test_sudo_wrap_uses_an_absolute_binary(tmp_path, monkeypatch):
    """``sudo -n x64sc`` would fail: sudo's secure_path does not include
    /opt/homebrew/bin, so the bare name must be resolved first."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    monkeypatch.setattr(ve.shutil, "which", lambda name: exe if name == "x64sc" else None)
    probed: list[str] = []
    monkeypatch.setattr(ve, "sudo_can_run", lambda b: probed.append(b) or True)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    plan = ve.plan_vice_launch(cfg, ["x64sc", "-warp"])
    assert plan.argv == ["sudo", "-n", exe, "-warp"]
    # And authorisation was checked for that same absolute path.
    assert probed == [exe]


def test_elevation_error_names_the_symlink_not_its_target(tmp_path, monkeypatch):
    """sudoers matches the literal command path after PATH lookup and does
    not follow symlinks, so /opt/homebrew/bin/x64sc is what must be
    authorised — not the Cellar path behind it."""
    target = _fake_x64sc(tmp_path, "cellar-x64sc", ethernet=True)
    link = tmp_path / "bin-x64sc"
    link.symlink_to(target)
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    with pytest.raises(ve.ViceElevationRequiredError) as excinfo:
        ve.plan_vice_launch(cfg, [str(link), "-warp"])
    err = excinfo.value
    assert err.binary == str(link)
    assert "cellar-x64sc" not in err.sudoers_entry
    assert "cellar-x64sc" not in err.command


# --------------------------------------------- -features row parsing


class TestFeaturesRowParsing:
    """``len(parts) >= 2`` is the guard that reads a feature row.

    Every fixture in this module mirrors VICE 3.10's four-column layout
    (``NAME  yes  Description...``), so the boundary the guard actually
    defends -- a row with exactly two tokens -- is never exercised.  A
    mutation run confirmed it: changing ``>= 2`` to ``> 2`` left all 56
    tests here green, because six-token rows satisfy both.
    """

    def test_a_bare_two_token_row_is_still_a_feature_row(
        self, tmp_path, monkeypatch
    ):
        """``NAME value`` with no description must parse.

        This is the whole point of ``>= 2``.  Nothing guarantees VICE
        keeps the description column, and a build that dropped it would
        silently demote every probe to the image-scan fallback --
        reporting ``drivers_known=False`` and refusing drivers the binary
        actually has.
        """
        exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)  # scan says no
        _fake_features(monkeypatch, "HAVE_RAWNET yes\nHAVE_PCAP yes\n")
        feat = ve.vice_features(exe)
        assert feat.source == "-features", (
            "a two-token row was not recognised, so the probe fell back to "
            "the image scan"
        )
        assert feat.rawnet is True and feat.pcap is True

    def test_a_single_token_row_is_ignored_not_fatal(
        self, tmp_path, monkeypatch
    ):
        """The other side of the boundary: one token is not a row.

        The malformed row is ``HAVE_PCAP`` rather than ``HAVE_RAWNET``
        deliberately.  A malformed *rawnet* row is not merely ignored --
        it leaves ``"rawnet"`` out of ``values``, which is the documented
        trigger for the image-scan fallback, so the test would be
        measuring the fallback rather than the parse.
        """
        exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)
        _fake_features(monkeypatch, "HAVE_RAWNET yes\nHAVE_PCAP\n")
        feat = ve.vice_features(exe)
        assert feat.source == "-features", "the probe should not have fallen back"
        assert feat.rawnet is True, "the well-formed row must still count"
        assert feat.pcap is False, "a one-token row must contribute nothing"


@pytest.mark.skipif(
    not os.path.exists("/opt/homebrew/bin/x64sc"),
    reason="no Homebrew x64sc on this host",
)
def test_the_features_fixtures_match_the_real_output_shape():
    """The fixtures encode an assumption about VICE that nothing checks.

    Every mocked test here feeds ``_fake_features`` a string the author
    typed.  If VICE's real ``-features`` layout differed -- a different
    row name, a colon, a leading indent -- the mocks would agree with
    each other and with a parser written to the same misunderstanding,
    and no test would notice.  This anchors the fixture shape to the
    binary, the way ``test_vice_wire_format_live`` anchors the response
    frame.
    """
    proc = subprocess.run(
        ["/opt/homebrew/bin/x64sc", "-features"],
        capture_output=True, text=True, timeout=60,
    )
    rows = {
        parts[0]: parts[1]
        for line in proc.stdout.splitlines()
        if (parts := line.split()) and len(parts) >= 2
    }
    missing = sorted(set(ve._FEATURE_ROWS) - set(rows))
    assert not missing, (
        f"the parser looks for {missing}, which real -features output does "
        f"not contain -- every fixture in this module is the wrong shape"
    )
    assert set(rows[name] for name in ve._FEATURE_ROWS) <= {"yes", "no"}, (
        "a feature value was neither 'yes' nor 'no'; the parser's "
        "`== \"yes\"` test would silently read it as False"
    )


@pytest.mark.skipif(
    not os.path.exists("/opt/homebrew/bin/x64sc"),
    reason="no Homebrew x64sc on this host",
)
def test_the_features_probe_ignores_an_ambient_vicerc(tmp_path, monkeypatch):
    """``-features`` must not be silenced by the operator's own config.

    The probe shells out to the binary, and without ``-default`` that
    child reads ``$HOME/.config/vice/vicerc`` like any other launch.  A
    vicerc carrying ``LogToStdout=0`` sends the feature rows somewhere we
    are not reading; ``values`` comes back empty and
    :func:`_probe_features` falls through to the image scan, reporting
    ``drivers_known=False`` and refusing drivers the binary really has.

    Measured on this bench: 0 feature rows without ``-default``, 36 with.
    That is the same ambient-config lesson as ``ViceProcess.start()``'s
    own ``-default``, one file over.

    Hermetic: ``HOME`` is redirected at *tmp_path*, so this never reads or
    writes the operator's real configuration.
    """
    vice_dir = tmp_path / ".config" / "vice"
    vice_dir.mkdir(parents=True)
    (vice_dir / "vicerc").write_text(
        "[Version]\nConfigVersion=3.10\n\n[C64SC]\nLogToStdout=0\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    # The probe is cached on the binary's identity, not on $HOME, so a
    # result cached by an earlier test would mask this entirely.
    ve._probe_features.cache_clear()

    feat = ve.vice_features("/opt/homebrew/bin/x64sc")
    ve._probe_features.cache_clear()

    assert feat.source == "-features", (
        "an ambient vicerc silenced the probe and it fell back to the "
        "image scan, which cannot report driver support"
    )
    assert feat.drivers_known is True


# ------------------------------------------- plan / listing / path nits


def test_plan_does_not_refuse_run_as_root_false_when_already_root(monkeypatch):
    """Root already holds the capability; ``run_as_root=False`` only
    means "do not sudo", which as root is exactly what happens anyway.
    ``needs_root`` used to ignore the current euid and refuse."""
    _as_uid(monkeypatch, 0)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    monkeypatch.setattr(
        ve, "sudo_can_run", lambda b: pytest.fail("must not shell out to sudo as root")
    )
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap", run_as_root=False)
    plan = ve.plan_vice_launch(cfg, _eth_argv())
    assert plan.argv == _eth_argv()
    assert plan.sudo_wrapped is False
    assert plan.elevated is True


def test_a_wildcard_argument_rule_authorises_the_binary(monkeypatch):
    """``NOPASSWD: /path/x64sc *`` authorises *any* argument list, so it
    covers the launch; it was being dropped as an argument-pinned rule."""
    _sudo_listing(
        monkeypatch,
        "User x may run the following commands:\n"
        "    (root) NOPASSWD: /opt/homebrew/bin/x64sc *\n",
    )
    assert ve.sudo_can_run("/opt/homebrew/bin/x64sc") is True


def test_a_setenv_tagged_rule_authorises_the_binary(monkeypatch):
    """sudoers tags may follow NOPASSWD: (``NOPASSWD: SETENV: /path``);
    the tag is not part of the command."""
    _sudo_listing(
        monkeypatch,
        "User x may run the following commands:\n"
        "    (root) NOPASSWD: SETENV: /opt/homebrew/bin/x64sc\n",
    )
    assert ve.sudo_can_run("/opt/homebrew/bin/x64sc") is True


def test_a_specific_argument_rule_still_does_not_authorise(monkeypatch):
    """Only the bare wildcard widens; a pinned argv is still one command."""
    _sudo_listing(
        monkeypatch,
        "User x may run the following commands:\n"
        "    (root) NOPASSWD: /opt/homebrew/bin/x64sc -warp\n"
        "    (root) NOPASSWD: /opt/homebrew/bin/brew reinstall *\n",
    )
    assert ve.sudo_can_run("/opt/homebrew/bin/x64sc") is False
    assert ve.sudo_can_run("/opt/homebrew/bin/brew") is False


def test_a_relative_binary_is_resolved_to_an_absolute_path(tmp_path, monkeypatch):
    """``launch_path("./x64sc")`` returned "./x64sc", and the sudoers line
    built from it -- ``NOPASSWD: ./x64sc`` -- is one visudo rejects.
    sudo itself also matches an absolute command path."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    monkeypatch.chdir(tmp_path)
    assert ve.launch_path("./x64sc") == exe

    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    _no_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    with pytest.raises(ve.ViceElevationRequiredError) as excinfo:
        ve.plan_vice_launch(cfg, ["./x64sc", "-warp"])
    err = excinfo.value
    assert err.binary == exe
    assert err.sudoers_entry.endswith(f"NOPASSWD: {exe}")
    assert "./x64sc" not in err.sudoers_entry


def test_the_remedy_command_names_the_resolved_binary(tmp_path, monkeypatch):
    """``binary`` and ``sudoers_entry`` already used the resolved path;
    the pasteable ``sudo ...`` command was built from the caller's
    spelling.  ``sudo x64sc`` fails on macOS (secure_path lacks
    /opt/homebrew/bin), so the remedy must use the same absolute path
    the sudoers line names."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=True)
    _as_uid(monkeypatch, 501)
    monkeypatch.setattr(ve.sys, "platform", "darwin")
    monkeypatch.setattr(ve.shutil, "which", lambda name: exe if name == "x64sc" else None)
    _no_sudo(monkeypatch)
    cfg = ViceConfig(ethernet=True, ethernet_driver="pcap")
    with pytest.raises(ve.ViceElevationRequiredError) as excinfo:
        ve.plan_vice_launch(cfg, ["x64sc", "-warp"])
    err = excinfo.value
    assert err.binary == exe
    assert err.argv == ["sudo", exe, "-warp"]
    assert err.command == f"sudo {exe} -warp"
    assert f"NOPASSWD: {exe}" in err.sudoers_entry


def test_the_resolver_hint_names_only_knobs_that_exist(tmp_path):
    """The remedy used to point at ``HarnessConfig.vice_ethernet_executable``
    / TOML ``[vice] ethernet_executable``.  Neither exists: nothing maps
    HarnessConfig.vice_* into ViceConfig.  The two knobs that do work are
    the env var and ``ViceConfig(ethernet_executable=...)``."""
    exe = _fake_x64sc(tmp_path, "x64sc", ethernet=False)
    cfg = ViceConfig(executable=exe, ethernet=True, ethernet_executable="")
    with pytest.raises(vice_lifecycle.ViceEthernetBinaryError) as excinfo:
        vice_lifecycle.resolve_vice_executable(cfg)
    msg = str(excinfo.value)
    assert f"{vice_lifecycle.ETHERNET_VICE_BIN_ENV}=/path/to/x64sc" in msg
    assert "ViceConfig(ethernet_executable=" in msg
    assert "HarnessConfig" not in msg
    assert "[vice]" not in msg
    assert "TOML" not in msg
