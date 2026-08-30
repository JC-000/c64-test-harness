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

def test_sudo_probe_asks_about_the_exact_binary(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ve.subprocess, "run", fake_run)
    assert ve.sudo_can_run("/opt/eth/x64sc") is True
    assert seen == [["sudo", "-n", "-l", "--", "/opt/eth/x64sc"]]


def test_sudo_probe_is_false_when_not_permitted(monkeypatch):
    monkeypatch.setattr(
        ve.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "sorry"),
    )
    assert ve.sudo_can_run("/opt/eth/x64sc") is False


def test_sudo_probe_survives_a_missing_sudo(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("no sudo")

    monkeypatch.setattr(ve.subprocess, "run", boom)
    assert ve.sudo_can_run("/opt/eth/x64sc") is False


def test_sudo_probe_never_prompts(monkeypatch):
    """A probe that blocks on a password prompt would hang the suite."""
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ve.subprocess, "run", fake_run)
    ve.sudo_can_run("/opt/eth/x64sc")
    assert captured.get("stdin") is subprocess.DEVNULL
    assert captured.get("timeout")


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
    # It asks the binary, with the documented flag, and nothing else.
    assert calls == [[exe, "-features"]]


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
