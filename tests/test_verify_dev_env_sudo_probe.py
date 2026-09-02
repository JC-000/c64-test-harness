"""``scripts/verify-dev-env.sh``'s ``nopasswd_for`` must agree with
``vice_elevation.parse_sudo_listing``.

The script's header says the shell probe "mirrors" the Python parser so
the two cannot disagree again.  Nothing enforced that, and they drifted:
the Python side learnt about sudoers tags (``SETENV:``), the bare ``*``
wildcard and the ``PASSWD:`` re-tag while the shell kept matching a lone
token only.  These tests use the Python parser as the oracle for every
listing shape, so a change to one side fails here until the other follows.

The probe block is extracted from the script by its ``(begin)``/``(end)``
markers and run under ``bash -c`` with the listing preset, so ``sudo`` is
never invoked.  bash 3.2 (stock macOS) is the target.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from c64_test_harness.backends import vice_elevation as ve

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify-dev-env.sh"
_BEGIN = "# ---------- sudo NOPASSWD probe (begin)"
_END = "# ---------- sudo NOPASSWD probe (end)"

X64SC = "/opt/homebrew/bin/x64sc"
SETUP = "/Users/someone/Documents/c64-test-harness/scripts/setup-bridge-feth-macos.sh"


def _probe_block() -> str:
    text = SCRIPT.read_text()
    start = text.index(_BEGIN)
    end = text.index(_END)
    return text[start:end]


def shell_nopasswd_for(listing: str, path: str) -> bool:
    """What the script's ``nopasswd_for`` says for *path* given *listing*."""
    program = (
        "set -u\n"
        + _probe_block()
        + f"\nSUDO_LISTING={shlex.quote(listing)}\n"
        + "SUDO_LISTING_LOADED=1\n"
        + f"nopasswd_for {shlex.quote(path)}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    assert proc.returncode in (0, 1), (
        f"probe crashed (rc={proc.returncode}): {proc.stderr}"
    )
    return proc.returncode == 0


def python_nopasswd_for(listing: str, path: str) -> bool:
    return ve.parse_sudo_listing(listing).allows(path)


_HEADER = "User someone may run the following commands on host:\n    (ALL) ALL\n"

LISTINGS = [
    pytest.param(f"{_HEADER}    (root) NOPASSWD: {X64SC}\n", X64SC, id="exact"),
    pytest.param(f"{_HEADER}    (root) NOPASSWD: SETENV: {X64SC}\n", X64SC, id="setenv-tag"),
    pytest.param(f"{_HEADER}    (root) NOPASSWD: {X64SC} *\n", X64SC, id="wildcard-args"),
    pytest.param(f"{_HEADER}    (root) NOPASSWD: PASSWD: {X64SC}\n", X64SC, id="passwd-retag"),
    pytest.param(f"{_HEADER}    (root) NOPASSWD: {X64SC} -warp\n", X64SC, id="pinned-args"),
    pytest.param(f"{_HEADER}    (ALL) NOPASSWD: ALL\n", X64SC, id="all"),
    pytest.param(_HEADER, X64SC, id="password-only"),
    pytest.param(
        f"{_HEADER}    (root) NOPASSWD: {SETUP}, {X64SC}\n", X64SC, id="comma-list"
    ),
    pytest.param(
        f"{_HEADER}    (root) NOPASSWD: {X64SC}\n", "/opt/homebrew/bin/brew", id="other-binary"
    ),
    pytest.param(
        f"{_HEADER}    (root) NOPASSWD: /opt/homebrew/bin/brew reinstall --HEAD vice\n",
        "/opt/homebrew/bin/brew",
        id="pinned-brew",
    ),
]


@pytest.mark.parametrize("listing, path", LISTINGS)
def test_shell_probe_agrees_with_parse_sudo_listing(listing, path):
    expected = python_nopasswd_for(listing, path)
    assert shell_nopasswd_for(listing, path) is expected, (
        f"nopasswd_for {path!r} disagrees with parse_sudo_listing "
        f"(python says {expected}) for listing:\n{listing}"
    )


def test_the_probe_block_is_extractable():
    """The markers the test keys on must survive edits to the script."""
    block = _probe_block()
    assert "nopasswd_for()" in block
    assert "load_sudo_listing()" in block


def test_the_probe_never_runs_sudo_when_the_listing_is_preset():
    program = (
        "set -u\n"
        "sudo() { echo 'sudo was invoked' >&2; exit 99; }\n"
        + _probe_block()
        + f"\nSUDO_LISTING={shlex.quote(f'(root) NOPASSWD: {X64SC}')}\n"
        + "SUDO_LISTING_LOADED=1\n"
        + f"nopasswd_for {shlex.quote(X64SC)}\n"
    )
    proc = subprocess.run(["bash", "-c", program], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
