"""scripts/build-vice-eth.sh: the patched VICE follows Homebrew's vice.

Run against a fake ``brew`` (a shell script answering ``info``, ``cat``,
``list`` and ``--prefix`` from files in a temp dir), a fake homebrew-core
served from a ``file://`` URL (``C64H_HOMEBREW_CORE_RAW_URL``) and fake
``x64sc`` binaries that print a ``-features`` table, so nothing is built,
nothing leaves the machine and Homebrew is never touched.  Like Homebrew in
API mode, the fake ``brew cat vice`` fails unless homebrew/core is tapped.  The real build is exercised by
``test_vice_elevation.test_the_patched_build_reports_every_row_the_parser_reads``
when one is installed.
"""

from __future__ import annotations

import hashlib
import json
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

# What homebrew-core carries today: the same formula plus the bottle block
# that a bottle rebuild rewrites without touching the source build.
_BOTTLE = """  bottle do
    rebuild 1
    sha256 arm64_tahoe: "6418080055eb645bc56525d0d902c9df649addb24ff8cef9756c149e6cfdbb29"
  end

"""
_FORMULA_WITH_BOTTLE = _FORMULA.replace("\n  def install", "\n" + _BOTTLE + "  def install")

_HEAD = "804538d1aabdd3003aa61638eaba5aa6115308e2"

_STOCK_FEATURES = """HAVE_DEBUG                no   Enable debugging code.
HAVE_RAWNET               yes  Enable raw ethernet emulation.
HAVE_PCAP                 yes  Use the PCAP library.
HAVE_USBSID               no   Enable USBSID support
"""
_PATCHED_FEATURES = _STOCK_FEATURES + (
    "RAWNET_BPF_PERMISSIONS    yes  Raw ethernet without root when a /dev/bpf* device is read-write.\n"
)

# install/reinstall stand in for Homebrew: they copy the tap's formula into
# the keg's .brew/ (as Homebrew does) and write a patched x64sc.
_FAKE_BREW = r"""#!/bin/bash
case "$1" in
  info) cat "$FAKE/info.json" ;;
  cat) [ -d "$FAKE/core-tap" ] || { echo "Error: vice's source doesn't exist on disk." >&2; exit 1; }
       cat "$FAKE/core-tap/Formula/v/vice.rb" ;;
  list) [ -e "$FAKE/installed" ] || exit 1; echo "${2##*/} 3.10" ;;
  --prefix) case "$2" in vice) echo "$FAKE/vice" ;; *) echo "$FAKE/vice-eth" ;; esac ;;
  --repository) case "$2" in homebrew/core) echo "$FAKE/core-tap" ;; *) echo "$FAKE/tap" ;; esac ;;
  tap-new) mkdir -p "$FAKE/tap" ;;
  install|reinstall)
    echo "$*" >> "$FAKE/installs"
    mkdir -p "$FAKE/vice-eth/.brew" "$FAKE/vice-eth/bin"
    cp "$FAKE/tap/Formula/vice-eth.rb" "$FAKE/vice-eth/.brew/vice-eth.rb"
    cp "$FAKE/patched-x64sc" "$FAKE/vice-eth/bin/x64sc"
    touch "$FAKE/installed" ;;
  *) echo "unexpected brew $*" >&2; exit 99 ;;
esac
"""


def _sha(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def _x64sc(path: Path, features: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat <<'X'\n" + features + "X\n")
    path.chmod(0o755)


def _publish(
    fake: Path,
    formula: str,
    *,
    head: str = _HEAD,
    checksum: str | None = None,
    served: str | None = None,
    pinned: bool = True,
) -> None:
    """Make *formula* Homebrew's current vice: the API JSON names it at
    *head* with its sha256 (or *checksum*), and the fake homebrew-core
    serves *served* (default: *formula*) at that commit."""
    info: dict = {"name": "vice", "tap": "homebrew/core"}
    if pinned:
        info.update(
            ruby_source_path="Formula/v/vice.rb",
            tap_git_head=head,
            ruby_source_checksum={"sha256": checksum or _sha(formula.encode())},
        )
    (fake / "info.json").write_text(json.dumps({"formulae": [info], "casks": []}))
    blob = fake / "core" / head / "Formula" / "v" / "vice.rb"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text(formula if served is None else served)


def _tap_core(fake: Path, formula: str) -> None:
    """A local homebrew/core tap, so `brew cat vice` answers."""
    f = fake / "core-tap" / "Formula" / "v" / "vice.rb"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(formula)


def _world(tmp_path: Path, *, formula: str = _FORMULA) -> Path:
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    brew = fake / "bin" / "brew"
    brew.write_text(_FAKE_BREW)
    brew.chmod(0o755)
    _publish(fake, formula)
    _x64sc(fake / "vice" / "bin" / "x64sc", _STOCK_FEATURES)
    _x64sc(fake / "patched-x64sc", _PATCHED_FEATURES)
    return fake


def _run(fake: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{fake / 'bin'}:/usr/bin:/bin",
        "HOME": str(fake),
        "FAKE": str(fake),
        "C64H_HOMEBREW_CORE_RAW_URL": f"file://{fake / 'core'}",
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
    (fake / "tap" / "patches").mkdir(parents=True, exist_ok=True)
    (fake / "tap" / "patches" / PATCH.name).write_bytes(PATCH.read_bytes())
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
    # The tap's copy of the patch, not this checkout's: the installed
    # formula must not depend on where the repo (or a worktree) lives.
    assert f'url "file://{fake}/tap/patches/{PATCH.name}"' in out
    assert str(REPO) not in out
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
    _publish(fake, _FORMULA.replace("vice-3.10", "vice-3.11"), head="1" * 40)
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


def test_check_counts_a_build_without_the_marker_as_not_patched(tmp_path):
    """The harness reads such a binary as stock and wants root: exit 2, so
    verify does not call the x64sc sudo rule unnecessary."""
    fake = _world(tmp_path)
    _install(fake, features=_STOCK_FEATURES)
    proc = _run(fake, "--check")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "not patched" in proc.stdout


def test_check_flags_a_tap_without_the_patch_copy(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    (fake / "tap" / "patches" / PATCH.name).unlink()
    proc = _run(fake, "--check")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "reinstall would fail" in proc.stdout


def test_check_says_when_it_could_not_compare_with_vice(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    (fake / "vice" / "bin" / "x64sc").unlink()
    proc = _run(fake, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not compared" in proc.stdout


def test_build_installs_from_the_tap_and_then_passes_check(tmp_path):
    fake = _world(tmp_path)
    proc = _run(fake)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (fake / "tap" / "patches" / PATCH.name).read_bytes() == PATCH.read_bytes()
    assert (fake / "installs").read_text().split() == [
        "install", "--build-from-source", "c64-test-harness/local/vice-eth"
    ]
    assert "current" in proc.stdout


def test_force_reinstalls_a_current_build(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    proc = _run(fake, "--force")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (fake / "installs").read_text().split()[0] == "reinstall"


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
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "current" in proc.stdout
    assert not (fake / "installs").exists()


# ---------------------------------------------------------------------------
# Where the formula comes from: homebrew-core at the API's pinned commit,
# verified against the API's checksum; `brew cat` only with a core tap.
# ---------------------------------------------------------------------------


def test_formula_is_fetched_at_the_api_pin_without_a_core_tap(tmp_path):
    """Homebrew in API mode has no core tap, so `brew cat vice` fails."""
    fake = _world(tmp_path)
    # Something else at another commit: only the pinned one may be read.
    _publish(fake, _FORMULA.replace("vice-3.10", "vice-9.99"), head="f" * 40)
    _publish(fake, _FORMULA)
    proc = _run(fake, "--print-formula")
    assert proc.returncode == 0, proc.stderr
    assert "vice-3.10.tar.gz" in proc.stdout
    assert f"c64h-source-formula-sha256: {_sha(_FORMULA.encode())}" in proc.stdout


def test_check_works_without_a_core_tap(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    proc = _run(fake, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "current" in proc.stdout


def test_a_download_that_does_not_match_the_api_checksum_is_refused(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    _publish(fake, _FORMULA, served=_FORMULA.replace("--enable-ethernet", "--evil"))
    proc = _run(fake, "--print-formula")
    assert proc.returncode != 0
    assert "--evil" not in proc.stdout
    assert "sha256" in proc.stderr
    check = _run(fake, "--check")
    assert check.returncode == 3, check.stdout + check.stderr
    assert "could not compare" in check.stdout


def test_a_checksum_mismatch_is_not_papered_over_by_the_core_tap(tmp_path):
    fake = _world(tmp_path)
    _tap_core(fake, _FORMULA)
    _publish(fake, _FORMULA, served=_FORMULA.replace("--enable-ethernet", "--evil"))
    proc = _run(fake, "--print-formula")
    assert proc.returncode != 0
    assert "class ViceEth" not in proc.stdout


def test_an_unfetchable_formula_without_a_core_tap_fails_closed(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    (fake / "core" / _HEAD / "Formula" / "v" / "vice.rb").unlink()
    proc = _run(fake, "--print-formula")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout == ""
    assert "could not fetch" in proc.stderr
    # --check neither passes nor calls the build stale: it could not look.
    check = _run(fake, "--check")
    assert check.returncode == 3, check.stdout + check.stderr
    assert "could not compare" in check.stdout


def test_an_api_entry_without_a_checksum_is_not_trusted(tmp_path):
    fake = _world(tmp_path)
    info = json.loads((fake / "info.json").read_text())
    del info["formulae"][0]["ruby_source_checksum"]
    (fake / "info.json").write_text(json.dumps(info))
    proc = _run(fake, "--print-formula")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("why", ["unfetchable", "unpinned", "unchecksummed"])
def test_brew_cat_is_the_fallback_when_homebrew_core_is_tapped(tmp_path, why):
    fake = _world(tmp_path)
    tapped = _FORMULA.replace("vice-3.10", "vice-3.10-tap")
    _tap_core(fake, tapped)
    if why == "unfetchable":
        (fake / "core" / _HEAD / "Formula" / "v" / "vice.rb").unlink()
    elif why == "unpinned":
        _publish(fake, _FORMULA, pinned=False)
    else:
        info = json.loads((fake / "info.json").read_text())
        del info["formulae"][0]["ruby_source_checksum"]
        (fake / "info.json").write_text(json.dumps(info))
    proc = _run(fake, "--print-formula")
    assert proc.returncode == 0, proc.stderr
    assert "vice-3.10-tap.tar.gz" in proc.stdout
    assert "brew cat" in proc.stderr


def test_the_bottle_block_is_not_part_of_the_build(tmp_path):
    """A bottle rebuild changes only the bottle block, which a from-source
    build never reads: a build made before it stays current, and the
    generated formula carries no bottle of vice's."""
    fake = _world(tmp_path)
    _install(fake)
    _publish(fake, _FORMULA_WITH_BOTTLE)
    out = _run(fake, "--print-formula").stdout
    assert "bottle do" not in out and "rebuild 1" not in out
    assert f"c64h-source-formula-sha256: {_sha(_FORMULA.encode())}" in out
    proc = _run(fake, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "current" in proc.stdout


def test_build_with_an_unverifiable_formula_writes_and_installs_nothing(tmp_path):
    fake = _world(tmp_path)
    _install(fake)
    before = (fake / "vice-eth" / ".brew" / "vice-eth.rb").read_text()
    (fake / "tap" / "Formula").mkdir(parents=True)
    (fake / "tap" / "Formula" / "vice-eth.rb").write_text(before)
    _publish(fake, _FORMULA, served=_FORMULA.replace("--enable-ethernet", "--evil"))
    proc = _run(fake, "--force")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not (fake / "installs").exists()
    assert (fake / "tap" / "Formula" / "vice-eth.rb").read_text() == before
