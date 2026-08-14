"""macOS BPF-capability probe and the sudo-elevation decision it drives.

Background (issue #144 follow-up): the harness used to elevate *every*
macOS ethernet launch via ``sudo -n``, justified by a claim that the
kernel refuses non-root capture on feth(4) even with ``/dev/bpf*`` at mode
666. That claim was a misreading of the ``cs8900_activate`` segfault,
which #144 root-caused to a VICE build with ethernet compiled out. A
non-root VICE captures fine once the BPF nodes are reachable.

So elevation is now conditional on the BPF nodes actually being out of
reach. These tests pin that decision matrix.
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends import vice_lifecycle
from c64_test_harness.backends.vice_lifecycle import (
    ViceConfig,
    _should_run_as_root,
    bpf_capture_available,
)


# --------------------------------------------------------------- probe

def test_bpf_probe_true_off_darwin(monkeypatch):
    """The BPF gate is macOS-only; elsewhere capture is never blocked by it."""
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "linux")
    # Even with no /dev/bpf* at all.
    monkeypatch.setattr(vice_lifecycle.glob, "glob", lambda pat: [])
    assert bpf_capture_available() is True


def test_bpf_probe_true_when_root(monkeypatch):
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 0)
    monkeypatch.setattr(vice_lifecycle.glob, "glob", lambda pat: [])
    assert bpf_capture_available() is True


def test_bpf_probe_true_when_enough_nodes_are_rw(monkeypatch):
    """A rig that ran `chmod o+rw /dev/bpf*` makes capture reachable."""
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        vice_lifecycle.glob, "glob", lambda pat: ["/dev/bpf0", "/dev/bpf1"]
    )
    monkeypatch.setattr(vice_lifecycle.os, "access", lambda node, mode: True)
    assert bpf_capture_available() is True


def test_bpf_probe_false_when_only_one_node_is_rw(monkeypatch):
    """One VICE opens two BPF devices, so a single node is not enough."""
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        vice_lifecycle.glob, "glob", lambda pat: ["/dev/bpf0", "/dev/bpf1"]
    )
    monkeypatch.setattr(
        vice_lifecycle.os, "access", lambda node, mode: node == "/dev/bpf1"
    )
    assert bpf_capture_available() is False


def test_bpf_probe_min_nodes_is_explicit(monkeypatch):
    """Callers can ask whether N concurrent instances would fit."""
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        vice_lifecycle.glob, "glob",
        lambda pat: [f"/dev/bpf{i}" for i in range(4)],
    )
    monkeypatch.setattr(vice_lifecycle.os, "access", lambda node, mode: True)
    assert bpf_capture_available(min_nodes=4) is True
    # Two concurrent VICEs need 4 nodes; a third would not fit.
    assert bpf_capture_available(min_nodes=6) is False


def test_bpf_probe_false_when_all_nodes_root_only(monkeypatch):
    """Stock macOS: /dev/bpf* are root-only, so an unprivileged VICE can't capture."""
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        vice_lifecycle.glob, "glob", lambda pat: ["/dev/bpf0", "/dev/bpf1"]
    )
    monkeypatch.setattr(vice_lifecycle.os, "access", lambda node, mode: False)
    assert bpf_capture_available() is False


def test_bpf_probe_false_when_no_nodes_exist(monkeypatch):
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 501)
    monkeypatch.setattr(vice_lifecycle.glob, "glob", lambda pat: [])
    assert bpf_capture_available() is False


def test_bpf_probe_checks_read_and_write(monkeypatch):
    """Read-only access is not enough — pcap needs to write to the node."""
    monkeypatch.setattr(vice_lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(vice_lifecycle.os, "geteuid", lambda: 501)
    monkeypatch.setattr(vice_lifecycle.glob, "glob", lambda pat: ["/dev/bpf0"])
    seen: list[int] = []

    def fake_access(node, mode):
        seen.append(mode)
        return False

    monkeypatch.setattr(vice_lifecycle.os, "access", fake_access)
    bpf_capture_available()
    assert seen == [os.R_OK | os.W_OK]


# ------------------------------------------------- elevation decision

def _patch_bpf(monkeypatch, *, available: bool, platform: str = "darwin"):
    monkeypatch.setattr(vice_lifecycle.sys, "platform", platform)
    monkeypatch.setattr(
        vice_lifecycle, "bpf_capture_available", lambda: available
    )


@pytest.mark.parametrize("available", [True, False])
def test_explicit_true_always_wins(monkeypatch, available):
    _patch_bpf(monkeypatch, available=available)
    cfg = ViceConfig(ethernet=True, run_as_root=True)
    assert _should_run_as_root(cfg) is True


@pytest.mark.parametrize("available", [True, False])
def test_explicit_false_always_wins(monkeypatch, available):
    """The escape hatch for a rig that opens BPF by other means."""
    _patch_bpf(monkeypatch, available=available)
    cfg = ViceConfig(ethernet=True, run_as_root=False)
    assert _should_run_as_root(cfg) is False


def test_auto_no_elevation_when_bpf_reachable(monkeypatch):
    """The regression this change is about: a prepared rig needs no sudo."""
    _patch_bpf(monkeypatch, available=True)
    cfg = ViceConfig(ethernet=True)
    assert _should_run_as_root(cfg) is False


def test_auto_elevates_when_bpf_unreachable(monkeypatch):
    """Stock macOS still elevates — the capability, not the platform, decides."""
    _patch_bpf(monkeypatch, available=False)
    cfg = ViceConfig(ethernet=True)
    assert _should_run_as_root(cfg) is True


def test_auto_never_elevates_without_ethernet(monkeypatch):
    _patch_bpf(monkeypatch, available=False)
    cfg = ViceConfig(ethernet=False)
    assert _should_run_as_root(cfg) is False


def test_auto_never_elevates_off_darwin(monkeypatch):
    _patch_bpf(monkeypatch, available=False, platform="linux")
    cfg = ViceConfig(ethernet=True)
    assert _should_run_as_root(cfg) is False


# ------------------------------------------- ethernet binary resolution

def test_ethernet_binary_empty_when_unset(monkeypatch):
    monkeypatch.delenv(vice_lifecycle.ETHERNET_VICE_BIN_ENV, raising=False)
    assert vice_lifecycle.ethernet_vice_binary() == ""


def test_ethernet_binary_reads_env_and_strips(monkeypatch):
    monkeypatch.setenv(vice_lifecycle.ETHERNET_VICE_BIN_ENV, "  /opt/eth/x64sc \n")
    assert vice_lifecycle.ethernet_vice_binary() == "/opt/eth/x64sc"


def test_resolve_prefers_ethernet_build_when_ethernet_on():
    cfg = ViceConfig(executable="x64sc", ethernet=True,
                     ethernet_executable="/opt/eth/x64sc")
    assert vice_lifecycle.resolve_vice_executable(cfg) == "/opt/eth/x64sc"


def test_resolve_ignores_ethernet_build_without_ethernet():
    """Non-ethernet runs keep using the everyday PATH binary."""
    cfg = ViceConfig(executable="x64sc", ethernet=False,
                     ethernet_executable="/opt/eth/x64sc")
    assert vice_lifecycle.resolve_vice_executable(cfg) == "x64sc"


def test_resolve_falls_back_when_no_ethernet_build_configured():
    cfg = ViceConfig(executable="x64sc", ethernet=True, ethernet_executable="")
    assert vice_lifecycle.resolve_vice_executable(cfg) == "x64sc"


def test_config_default_picks_up_env(monkeypatch):
    """Existing ethernet tests inherit the build without per-test edits."""
    monkeypatch.setenv(vice_lifecycle.ETHERNET_VICE_BIN_ENV, "/opt/eth/x64sc")
    cfg = ViceConfig(ethernet=True)
    assert cfg.ethernet_executable == "/opt/eth/x64sc"
    assert vice_lifecycle.resolve_vice_executable(cfg) == "/opt/eth/x64sc"


def test_explicit_field_beats_env(monkeypatch):
    monkeypatch.setenv(vice_lifecycle.ETHERNET_VICE_BIN_ENV, "/opt/eth/x64sc")
    cfg = ViceConfig(ethernet=True, ethernet_executable="/other/x64sc")
    assert vice_lifecycle.resolve_vice_executable(cfg) == "/other/x64sc"
