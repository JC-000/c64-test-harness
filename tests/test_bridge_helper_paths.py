"""Which copy of a bridge lifecycle script the harness runs and probes (#541).

A bench may install root-owned copies of the bridge helpers in
``/usr/local/libexec/c64-harness`` and name only those in sudoers, because a
NOPASSWD rule on a user-writable repo path is a route to root.  The
``bridge_scripts`` elevation probe and the live cleanup test must then use
those copies; on a bench whose rules name the repo's scripts they must keep
using the repo's.  ``$C64_BRIDGE_HELPER_DIR`` names the directory outright.

Nothing here runs sudo or a bridge script: the sudoers verdict is faked.
"""

from __future__ import annotations

import os

import pytest

import bridge_platform as bp
import conftest
from c64_test_harness.backends import vice_elevation as ve

NAMES = ("setup-bridge-feth-macos.sh", "teardown-bridge-feth-macos.sh",
         "cleanup-bridge-feth-macos.sh")


def _install(directory, names=NAMES):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        script = directory / name
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
    return directory


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """A libexec dir and a repo checkout, both holding the helpers."""
    libexec = _install(tmp_path / "libexec")
    repo = tmp_path / "repo"
    _install(repo / "scripts")
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(libexec))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    return libexec, repo


def _authorise(monkeypatch, allowed):
    calls = []

    def fake(path, *, no_args=False):
        calls.append((path, no_args))
        return path in allowed

    monkeypatch.setattr(bp, "sudo_can_run", fake)
    monkeypatch.setattr(ve, "sudo_can_run", fake)
    return calls


def test_the_authorised_libexec_copy_is_preferred(bench, monkeypatch):
    libexec, repo = bench
    calls = _authorise(monkeypatch, {str(libexec / NAMES[0])})
    path, ok = bp.resolve_bridge_helper(NAMES[0], str(repo))
    assert (path, ok) == (str(libexec / NAMES[0]), True)
    # The helpers run bare, so a rule pinned to "" covers them.
    assert all(no_args for _p, no_args in calls), calls


def test_a_bench_whose_rules_name_the_repo_keeps_the_repo_copy(bench, monkeypatch):
    libexec, repo = bench
    _authorise(monkeypatch, {str(repo / "scripts" / NAMES[2])})
    assert bp.resolve_bridge_helper(NAMES[2], str(repo)) == (
        str(repo / "scripts" / NAMES[2]), True)


def test_unauthorised_everywhere_names_the_installed_copy(bench, monkeypatch):
    libexec, repo = bench
    _authorise(monkeypatch, set())
    assert bp.resolve_bridge_helper(NAMES[0], str(repo)) == (str(libexec / NAMES[0]), False)


def test_no_installed_copy_falls_back_to_the_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _install(repo / "scripts")
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(tmp_path / "absent"))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    _authorise(monkeypatch, set())
    assert bp.resolve_bridge_helper(NAMES[0], str(repo)) == (
        str(repo / "scripts" / NAMES[0]), False)


def test_the_env_override_is_the_only_candidate(bench, tmp_path, monkeypatch):
    libexec, repo = bench
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(bp.BRIDGE_HELPER_DIR_ENV, str(elsewhere))
    # Even an authorised libexec copy is not used: the override says where.
    _authorise(monkeypatch, {str(libexec / NAMES[0])})
    assert bp.resolve_bridge_helper(NAMES[0], str(repo)) == (str(elsewhere / NAMES[0]), False)


def test_no_candidate_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(tmp_path / "absent"))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    _authorise(monkeypatch, set())
    assert bp.resolve_bridge_helper(NAMES[0], None) == (None, False)


# --- the bridge_scripts elevation probe -----------------------------------


@pytest.fixture
def probe_root(bench, monkeypatch):
    libexec, repo = bench
    monkeypatch.setattr(conftest, "_canonical_repo_root", lambda: str(repo))
    return libexec, repo


def test_probe_passes_on_libexec_rules_alone(probe_root, monkeypatch):
    libexec, _repo = probe_root
    _authorise(monkeypatch, {str(libexec / n) for n in NAMES})
    assert conftest._probe_bridge_scripts(NAMES) == (True, "")


def test_probe_remedy_names_the_installed_copies(probe_root, monkeypatch):
    libexec, repo = probe_root
    _authorise(monkeypatch, {str(libexec / NAMES[0])})
    ok, remedy = conftest._probe_bridge_scripts(NAMES)
    assert ok is False
    for name in NAMES[1:]:
        assert f'{libexec / name} ""' in remedy, remedy
    assert str(libexec / NAMES[0]) not in remedy
    assert str(repo) not in remedy


def test_probe_without_any_copy_still_refuses_to_fabricate(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(tmp_path / "absent"))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    monkeypatch.setattr(conftest, "_canonical_repo_root", lambda: None)
    _authorise(monkeypatch, set())
    ok, remedy = conftest._probe_bridge_scripts(NAMES)
    assert ok is False
    assert "canonical checkout cannot be located" in remedy
    assert os.getcwd() not in remedy


def test_setup_hint_names_the_installed_copy(bench):
    libexec, _repo = bench
    assert bp._setup_hint(NAMES[0]) == f"run sudo -n {libexec / NAMES[0]}"


def test_setup_hint_without_an_installed_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(tmp_path / "absent"))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    assert bp._setup_hint(NAMES[0]) == f"run sudo scripts/{NAMES[0]}"


# --- review round 1 -------------------------------------------------------


def test_libexec_wins_when_both_copies_are_authorised(bench, monkeypatch):
    """The copies differ (the installed one is hardened), so the order matters."""
    libexec, repo = bench
    _authorise(monkeypatch, {str(libexec / NAMES[0]), str(repo / "scripts" / NAMES[0])})
    assert bp.resolve_bridge_helper(NAMES[0], str(repo)) == (str(libexec / NAMES[0]), True)


def test_a_missing_repo_script_is_not_a_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(tmp_path / "absent"))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    (tmp_path / "repo" / "scripts").mkdir(parents=True)
    assert bp.bridge_helper_candidates(NAMES[0], str(tmp_path / "repo")) == []


def test_a_whitespace_only_override_means_unset(bench, monkeypatch):
    libexec, repo = bench
    monkeypatch.setenv(bp.BRIDGE_HELPER_DIR_ENV, "  ")
    assert bp.bridge_helper_candidates(NAMES[0], str(repo))[0] == str(libexec / NAMES[0])


def test_libexec_is_searched_on_macos_only(bench, monkeypatch):
    """The Linux live test still runs repo scripts via `sudo -n bash`, which
    no argument-free rule on an installed copy could cover."""
    libexec, repo = bench
    monkeypatch.setattr(bp.sys, "platform", "linux")
    assert bp.bridge_helper_candidates(NAMES[0], str(repo)) == [str(repo / "scripts" / NAMES[0])]


def test_probe_names_the_scripts_it_cannot_find(tmp_path, monkeypatch):
    """A partial install with no checkout: say which are missing."""
    libexec = _install(tmp_path / "libexec", names=NAMES[:1])
    monkeypatch.setattr(bp, "BRIDGE_HELPER_LIBEXEC", str(libexec))
    monkeypatch.delenv(bp.BRIDGE_HELPER_DIR_ENV, raising=False)
    monkeypatch.setattr(conftest, "_canonical_repo_root", lambda: None)
    _authorise(monkeypatch, {str(libexec / NAMES[0])})
    ok, remedy = conftest._probe_bridge_scripts(NAMES)
    assert ok is False
    assert NAMES[1] in remedy and NAMES[2] in remedy, remedy
    assert f"cannot find {NAMES[0]}" not in remedy and f", {NAMES[0]}" not in remedy, remedy


def test_probe_remedy_for_an_installed_copy_pins_digest_and_no_args(probe_root, monkeypatch):
    import hashlib
    libexec, _repo = probe_root
    _authorise(monkeypatch, set())
    _ok, remedy = conftest._probe_bridge_scripts(NAMES[:1])
    digest = hashlib.sha256((libexec / NAMES[0]).read_bytes()).hexdigest()
    assert f'NOPASSWD: sha256:{digest} {libexec / NAMES[0]} ""' in remedy, remedy
