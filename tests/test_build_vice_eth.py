"""scripts/build-vice-eth.sh: the patched VICE follows Homebrew's vice.

Run against a fake ``brew`` (a shell script answering ``cat``, ``list`` and
``--prefix`` from files in a temp dir) and fake ``x64sc`` binaries that
print a ``-features`` table, so nothing is built and Homebrew is never
touched.  The real build is exercised by
``test_vice_elevation.test_the_patched_build_reports_every_row_the_parser_reads``
when one is installed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build-vice-eth.sh"
PATCH = REPO / "vice" / "rawnet-bpf-permissions.patch"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the script is macOS only")

# The parts of Homebrew's vice formula the script edits, in their real shape.
_FORMULA = '''class Vice < Formula
  desc "Versatile Commodore Emulator"
  url "https://downloads.sourceforge.net/project/vice-emu/releases/vice-3.10.tar.gz"

  def install
    system "./autogen.sh"

    system "./configure", "--disable-arch",
                          "--enable-ethernet",
                          *std_configure_args
    system "make", "install"
  end
end
'''

_STOCK_FEATURES = """HAVE_DEBUG                no   Enable debugging code.
HAVE_RAWNET               yes  Enable raw ethernet emulation.
HAVE_PCAP                 yes  Use the PCAP library.
HAVE_USBSID               no   Enable USBSID support
"""
_PATCHED_FEATURES = _STOCK_FEATURES + (
    "RAWNET_BPF_PERMISSIONS    yes  Raw ethernet without root when a /dev/bpf* device is read-write.\n"
)

_FAKE_BREW = r"""#!/bin/bash
case "$1" in
  cat) cat "$FAKE/formula.rb" ;;
  list) [ -e "$FAKE/installed" ] || exit 1; echo "${2##*/} 3.10" ;;
  --prefix) case "$2" in vice) echo "$FAKE/vice" ;; *) echo "$FAKE/vice-eth" ;; esac ;;
  --repository) echo "$FAKE/tap" ;;
  *) echo "unexpected brew $*" >&2; exit 99 ;;
esac
"""


def _sha(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def _x64sc(path: Path, features: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat <<'X'\n" + features + "X\n")
    path.chmod(0o755)


def _world(tmp_path: Path, *, formula: str = _FORMULA) -> Path:
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    brew = fake / "bin" / "brew"
    brew.write_text(_FAKE_BREW)
    brew.chmod(0o755)
    (fake / "formula.rb").write_text(formula)
    _x64sc(fake / "vice" / "bin" / "x64sc", _STOCK_FEATURES)
    return fake


def _run(fake: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{fake / 'bin'}:/usr/bin:/bin",
        "HOME": str(fake),
        "FAKE": str(fake),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
    )


def _install(fake: Path, *, formula_text: str | None = None, features: str = _PATCHED_FEATURES) -> None:
    """Make the fake keg look like a build made from *formula_text*."""
    generated = _run(fake, "--print-formula")
    assert generated.returncode == 0, generated.stderr
    keg = fake / "vice-eth"
    (keg / ".brew").mkdir(parents=True)
    (keg / ".brew" / "vice-eth.rb").write_text(formula_text or generated.stdout)
    _x64sc(keg / "bin" / "x64sc", features)
    (fake / "installed").touch()


def test_formula_is_homebrews_plus_the_patch(tmp_path):
    fake = _world(tmp_path)
    proc = _run(fake, "--print-formula")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith("class ViceEth < Formula\n")
    assert "keg_only" in out
    assert f"c64h-source-formula-sha256: {_sha(_FORMULA.encode())}" in out
    assert f"c64h-patch-sha256: {_sha(PATCH.read_bytes())}" in out
    patch_at = out.index("  patch do\n")
    assert patch_at < out.index("  def install\n")
    assert f'url "file://{PATCH}"' in out
    assert 'system "./configure", "--disable-usbsid", "--disable-arch",' in out
    # Everything else is Homebrew's, unchanged.
    for line in _FORMULA.splitlines()[1:]:
        if "./configure" not in line:
            assert line in out.splitlines(), line


@pytest.mark.parametrize(
    "broken",
    [
        _FORMULA.replace("class Vice < Formula", "class Vice < AbstractFormula"),
        _FORMULA.replace("  def install", "  def install_it"),
        _FORMULA.replace('system "./configure", ', 'system "./configure.sh", '),
    ],
)
def test_a_formula_of_another_shape_is_refused(tmp_path, broken):
    proc = _run(_world(tmp_path, formula=broken), "--print-formula")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "update generate_formula" in proc.stderr


def test_check_reports_not_built(tmp_path):
    proc = _run(_world(tmp_path), "--check")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "not built" in proc.stdout


def test_check_passes_a_build_from_the_current_formula(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    proc = _run(fake, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "current" in proc.stdout


def test_check_flags_a_build_left_behind_by_brew_upgrade(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    (fake / "formula.rb").write_text(_FORMULA.replace("vice-3.10", "vice-3.11"))
    proc = _run(fake, "--check")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "vice formula changed" in proc.stdout


def test_check_flags_a_build_from_an_older_patch(tmp_path):
    fake = _world(tmp_path)
    generated = _run(fake, "--print-formula").stdout
    old = generated.replace(_sha(PATCH.read_bytes()), "0" * 64)
    _install(fake, formula_text=old)
    proc = _run(fake, "--check")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "patch changed" in proc.stdout


def test_check_flags_a_build_without_the_marker(tmp_path):
    fake = _world(tmp_path)
    _install(fake, features=_STOCK_FEATURES)
    proc = _run(fake, "--check")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "RAWNET_BPF_PERMISSIONS" in proc.stdout


def test_check_flags_features_that_differ_from_homebrews_vice(tmp_path):
    fake = _world(tmp_path)
    _install(fake, features=_PATCHED_FEATURES.replace("HAVE_USBSID               no", "HAVE_USBSID               yes"))
    proc = _run(fake, "--check")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "diverged" in proc.stdout
    assert "HAVE_USBSID yes" in proc.stdout


def test_build_does_nothing_when_current(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    proc = _run(fake)
    # The fake brew fails on install/reinstall/tap-new, so rc 0 means none ran.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "current" in proc.stdout
    assert "unexpected brew" not in proc.stderr
