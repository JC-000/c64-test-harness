#!/usr/bin/env bash
#
# setup-dev-env.sh -- fresh dev-env installer for c64-test-harness.
#
# Takes a zero-state machine to a state where scripts/verify-dev-env.sh reports
# READY. Every stage is idempotent (re-runs are safe) and opt-out via --no-*
# flags. Every mutating action is gated behind --dry-run so reviewers can
# inspect what would happen without touching the system.
#
# Stages:
#   1. system packages (apt-get / brew)     -- opt-out --no-system-packages
#   2. VICE 3.10 source build + install     -- opt-out --no-vice (Linux only;
#                                              macOS uses Homebrew bottle from
#                                              stage 1)
#   3. Python harness editable install      -- opt-out --no-harness
#   4. bridge networking setup              -- opt-out --no-bridge
#   5. Ultimate 64 probe (optional)         -- opt-in via $U64_HOST or --u64-host
#   6. final verify-dev-env.sh run
#
# Safety: set -u (not -e) so the script continues through skipped/optional
# stages. set -o pipefail for pipeline error propagation.
#
# Supported platforms:
#   - Ubuntu Desktop 25 (25.04 / 25.10): full apt-get + VICE source build path.
#   - macOS (Darwin): Homebrew-based install of the `vice` bottle; bridge
#     setup dispatches to setup-bridge-feth-macos.sh.
# Other distros are warned against via check_os() but can proceed with --force.

set -u
set -o pipefail

# ---------- OS detection --------------------------------------------------

# Detected once, used by stages that branch between Ubuntu (apt-get, Linux
# bridge/TAP) and macOS (Homebrew, bridge10/feth0/feth1). The Linux path is
# the long-standing default; macOS branches are wrapped around it so
# behaviour on Ubuntu is unchanged.
OS="$(uname)"

# ---------- defaults ------------------------------------------------------

DRY_RUN=0
FORCE=0
NO_SYSTEM_PACKAGES=0
NO_VICE=0
NO_HARNESS=0
NO_BRIDGE=0
NO_U64=0
U64_HOST="${U64_HOST:-}"
BUILD_DIR="${BUILD_DIR:-$HOME/.cache/c64-test-harness/build}"
# Dedicated venv for the harness. PEP 668 / externally-managed-environment
# (Ubuntu 23+) blocks `pip install --user` against system Python, and Ubuntu
# 25 enforces it, so we install into a dedicated venv instead. XDG-ish path.
VENV_DIR="${VENV_DIR:-$HOME/.local/share/c64-test-harness/venv}"
EXPECTED_SHA256=""

REPO_ROOT=""

# VICE 3.10 upstream tarball. Verified with HEAD request at PR authoring time;
# SourceForge 302-redirects to a mirror. A SHA256 pin is not hardcoded here
# because no upstream-published checksum was found at authoring time -- pass
# --sha256 HEX to enforce one, or verify manually.
VICE_VERSION="3.10"
VICE_TARBALL_URL="https://sourceforge.net/projects/vice-emu/files/releases/vice-${VICE_VERSION}.tar.gz/download"
VICE_TARBALL_NAME="vice-${VICE_VERSION}.tar.gz"
VICE_SRC_DIRNAME="vice-${VICE_VERSION}"

# ---------- logging helpers -----------------------------------------------

log()       { printf '[%s] %s\n' "$1" "$2"; }
log_install(){ log "install" "$1"; }
log_ok()    { log "ok" "$1"; }
log_skip()  { log "skip" "$1"; }
log_warn()  { log "warn" "$1"; }
log_fail()  { log "fail" "$1"; }
log_dry()   { log "dry-run" "$1"; }

banner() {
    printf '\n========================================\n'
    printf '%s\n' "$1"
    printf '========================================\n'
}

# Run a command, or print it under --dry-run. First arg is a label passed
# through to logging for dry-run output; the rest is the argv to execute.
run_cmd() {
    local label="$1"; shift
    if [ "$DRY_RUN" = "1" ]; then
        log_dry "$label: $*"
        return 0
    fi
    log_install "$label: $*"
    "$@"
}

# Same but with sudo. Separated so dry-run output is explicit about sudo usage.
run_sudo() {
    local label="$1"; shift
    if [ "$DRY_RUN" = "1" ]; then
        log_dry "$label: sudo $*"
        return 0
    fi
    log_install "$label: sudo $*"
    sudo "$@"
}

# ---------- arg parsing ---------------------------------------------------

usage() {
    cat <<'EOF'
setup-dev-env.sh -- dev-env installer for c64-test-harness (Ubuntu 25 / macOS)

USAGE:
  setup-dev-env.sh [OPTIONS]

OPTIONS:
  --dry-run             Print actions without executing (safe; still runs verify)
  --force               Skip the Ubuntu 25 version check (ignored on macOS)
  --no-system-packages  Skip the apt-get / brew install step
  --no-vice             Skip VICE source build (no-op on macOS: brew-installed)
  --no-harness          Skip pip install -e .
  --no-bridge           Skip bridge network setup
  --no-u64              Skip U64 probe even if U64_HOST is set
  --u64-host HOST       Probe this U64 host (overrides $U64_HOST)
  --build-dir DIR       Where to put VICE source (default ~/.cache/c64-test-harness/build)
  --sha256 HEX          Expected SHA256 of VICE tarball (optional pin; Linux-only)
  -h, --help            Print this help

EXIT CODES:
  0  verify-dev-env.sh reports READY
  1  verify-dev-env.sh reports NOT READY (one or more stages failed or were skipped)
  2  installer error (bad arg, OS mismatch without --force, not in repo)
  3  verify-dev-env.sh itself is broken (exit 2)
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) DRY_RUN=1; shift ;;
            --force) FORCE=1; shift ;;
            --no-system-packages) NO_SYSTEM_PACKAGES=1; shift ;;
            --no-vice) NO_VICE=1; shift ;;
            --no-harness) NO_HARNESS=1; shift ;;
            --no-bridge) NO_BRIDGE=1; shift ;;
            --no-u64) NO_U64=1; shift ;;
            --u64-host)
                if [ $# -lt 2 ]; then
                    log_fail "--u64-host needs a value"
                    exit 2
                fi
                U64_HOST="$2"; shift 2 ;;
            --u64-host=*) U64_HOST="${1#*=}"; shift ;;
            --build-dir)
                if [ $# -lt 2 ]; then
                    log_fail "--build-dir needs a value"
                    exit 2
                fi
                BUILD_DIR="$2"; shift 2 ;;
            --build-dir=*) BUILD_DIR="${1#*=}"; shift ;;
            --sha256)
                if [ $# -lt 2 ]; then
                    log_fail "--sha256 needs a value"
                    exit 2
                fi
                EXPECTED_SHA256="$2"; shift 2 ;;
            --sha256=*) EXPECTED_SHA256="${1#*=}"; shift ;;
            -h|--help) usage; exit 0 ;;
            *)
                log_fail "unknown argument: $1"
                usage >&2
                exit 2 ;;
        esac
    done
}

# ---------- environment checks --------------------------------------------

check_os() {
    banner "Preflight -- OS detection"
    if [ "$OS" = "Darwin" ]; then
        # macOS: no /etc/os-release; report sw_vers for the record and move on.
        # We don't gate on a specific macOS version -- Homebrew handles the
        # differences between Apple Silicon (/opt/homebrew) and Intel
        # (/usr/local) internally.
        local mac_ver mac_build
        mac_ver="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
        mac_build="$(sw_vers -buildVersion 2>/dev/null || echo unknown)"
        log_ok "detected: macOS $mac_ver (build $mac_build)"
        return
    fi
    if [ ! -r /etc/os-release ]; then
        log_warn "/etc/os-release missing; cannot detect distro"
        if [ "$FORCE" = "0" ]; then
            log_fail "refusing to run on unknown OS; pass --force to override"
            exit 2
        fi
        return
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    local id="${ID:-unknown}"
    local ver="${VERSION_ID:-unknown}"
    log_ok "detected: ID=$id VERSION_ID=$ver"
    if [ "$id" != "ubuntu" ]; then
        log_warn "target is Ubuntu Desktop 25; found $id $ver"
        if [ "$FORCE" = "0" ]; then
            log_fail "non-Ubuntu system; pass --force to override"
            exit 2
        fi
    elif [ "$ver" != "25.04" ] && [ "$ver" != "25.10" ]; then
        log_warn "target is Ubuntu 25.04 or 25.10; found $ver"
        if [ "$FORCE" = "0" ]; then
            log_fail "Ubuntu version mismatch; pass --force to override"
            exit 2
        fi
    else
        log_ok "Ubuntu Desktop 25 confirmed"
    fi
}

check_repo_root() {
    banner "Preflight -- repo root"
    local here
    here="$(cd -- "$(dirname -- "$0")" && pwd)"
    local dir="$here"
    while [ "$dir" != "/" ]; do
        if [ -f "$dir/pyproject.toml" ] && grep -q 'c64-test-harness' "$dir/pyproject.toml" 2>/dev/null; then
            REPO_ROOT="$dir"
            break
        fi
        dir="$(dirname -- "$dir")"
    done
    if [ -z "$REPO_ROOT" ]; then
        log_fail "could not locate c64-test-harness repo root (walked up from $here)"
        exit 2
    fi
    log_ok "repo root: $REPO_ROOT"
    if [ ! -x "$REPO_ROOT/scripts/verify-dev-env.sh" ]; then
        log_fail "scripts/verify-dev-env.sh missing or not executable at $REPO_ROOT"
        exit 2
    fi
}

# ---------- Stage 1: system packages --------------------------------------

# macOS variant of stage 1. The Linux path installs a large build toolchain
# because it then builds VICE 3.10 from source; on macOS we consume the
# Homebrew `vice` bottle instead (which also provides x64sc + c1541 +
# ethernet support), so the only required package is `vice`. Python is
# assumed to be already installed (Homebrew Python or system python3 >=
# 3.10 per verify-dev-env.sh).
#
# Homebrew must NOT be run as root -- doing so corrupts the prefix
# permissions. We never call `sudo brew`. If brew is missing we print a
# hard stop and let the user install it manually per https://brew.sh.
stage_system_packages_macos() {
    if ! command -v brew >/dev/null 2>&1; then
        log_fail "Homebrew (brew) not found on PATH"
        log_fail "Install Homebrew first: https://brew.sh"
        log_fail "Then re-run this script. Do not run Homebrew as root."
        exit 2
    fi
    log_ok "Homebrew found at $(command -v brew)"

    # Minimum macOS package set for the harness. `vice` is the only hard
    # requirement; `curl` is in the base system on every supported macOS.
    local packages=(
        vice                  # x64sc, c1541 -- Homebrew bottle includes ethernet
    )

    if [ "$DRY_RUN" = "1" ]; then
        log_dry "brew update"
        log_dry "brew install ${packages[*]}"
        log_ok "stage 1 (dry-run): would install ${#packages[@]} Homebrew packages"
        return
    fi

    log_install "brew update"
    if ! brew update; then
        log_warn "brew update reported errors; continuing"
    fi

    log_install "brew install ${packages[*]}"
    # `brew install` is idempotent: if the formula is already installed it
    # prints a one-line skip notice and exits 0. We still capture failures
    # per-package so we can report which formula (if any) broke.
    local failed=()
    local pkg
    for pkg in "${packages[@]}"; do
        if ! brew install "$pkg"; then
            failed+=("$pkg")
            log_warn "failed: $pkg (try: brew search $pkg)"
        fi
    done
    if [ "${#failed[@]}" -eq 0 ]; then
        log_ok "all ${#packages[@]} Homebrew packages installed"
    else
        log_fail "could not install: ${failed[*]}"
    fi
}

stage_system_packages() {
    banner "Stage 1 -- system packages"
    if [ "$NO_SYSTEM_PACKAGES" = "1" ]; then
        log_skip "--no-system-packages passed"
        return
    fi

    if [ "$OS" = "Darwin" ]; then
        stage_system_packages_macos
        return
    fi

    # -------- Linux (Ubuntu) path --------
    # Package list derived from:
    #   - VICE 3.10 configure.ac requirements for --enable-ethernet +
    #     --enable-native-gtk3ui + audio codecs
    #   - Ubuntu 25 package names (best-effort mapping -- some names may have
    #     drifted from earlier Ubuntu releases). If apt-get install fails on a
    #     specific package, grep for its base name with `apt-cache search` and
    #     update the list below.
    #
    # Each line tagged with a short rationale so a maintainer can swap in a
    # replacement if a package rename lands in a future Ubuntu.
    local packages=(
        # -- build toolchain --
        build-essential       # gcc, g++, make, libc headers
        gcc                   # C compiler (explicit for clarity)
        make                  # build driver
        autoconf              # VICE ./configure regeneration
        automake              # VICE Makefile.am handling
        libtool               # VICE libltdl
        pkg-config            # pkg-config / .pc lookups during configure

        # -- VICE GTK3 UI deps --
        libsdl2-dev           # SDL2 headers for audio/video
        libsdl2-image-dev     # SDL2_image headers
        libgtk-3-dev          # GTK3 headers (native-gtk3ui)
        libglew-dev           # GLEW for VICE GL backend
        # NOTE: libgtkglext1-dev was in the original list as conservative
        # coverage for legacy GtkGLExt GL bindings. It was deprecated on
        # Ubuntu 24.04 and is not reliably available on Ubuntu 25 -- and
        # VICE 3.10's native GTK3 UI does not need it (the modern GTK3 GL
        # path uses GtkGLArea via GDK, not the legacy GtkGLExt library).
        # Dropped here; add it back if a 25.x apt index resurrects it and
        # your local VICE build actually requires it.
        libxaw7-dev           # Xaw fallback UI

        # -- VICE runtime support --
        libreadline-dev       # monitor line editing
        libpcap-dev           # CS8900a TFE backend (ethernet)
        libpulse-dev          # PulseAudio output
        libasound2-dev        # ALSA output
        libflac-dev           # FLAC sample support
        libvorbis-dev         # Ogg Vorbis support
        libmpg123-dev         # mp3 decoding
        libmp3lame-dev        # mp3 encoding

        # -- harness + bridge tooling --
        git                   # clone + version info
        curl                  # tarball download, U64 probe
        ca-certificates       # HTTPS trust store for curl
        python3               # harness runtime
        python3-pip           # pip install -e .
        python3-venv          # optional venv support for users
        iproute2              # ip/bridge CLI for bridge stage
        iptables              # iptables rules for bridge stage
    )

    log_install "apt-get update"
    if ! run_sudo "apt-update" apt-get update; then
        log_warn "apt-get update reported errors; continuing"
    fi

    log_install "apt-get install -y ${#packages[@]} packages"
    if [ "$DRY_RUN" = "1" ]; then
        log_dry "apt-get install -y ${packages[*]}"
        log_ok "stage 1 (dry-run): would install ${#packages[@]} packages"
        return
    fi

    # Try the full list first; if it fails, try installing one at a time so
    # we can report which package(s) failed without aborting the whole stage.
    if sudo apt-get install -y "${packages[@]}"; then
        log_ok "all ${#packages[@]} packages installed"
        return
    fi

    log_warn "bulk install failed; retrying package-by-package to identify failures"
    local failed=()
    local pkg
    for pkg in "${packages[@]}"; do
        if ! sudo apt-get install -y "$pkg" >/dev/null 2>&1; then
            failed+=("$pkg")
            log_warn "failed: $pkg (try: apt-cache search ${pkg%-dev})"
        fi
    done
    if [ "${#failed[@]}" -eq 0 ]; then
        log_ok "all packages installed after retry"
    else
        log_fail "could not install: ${failed[*]}"
        log_fail "suggested debugging: apt-cache search <base-name> for each"
    fi
}

# ---------- Stage 2: VICE 3.10 build --------------------------------------

vice_already_installed() {
    # Returns 0 if a working VICE 3.10 with ethernet is already on PATH.
    command -v x64sc >/dev/null 2>&1 || return 1
    x64sc --version 2>&1 | grep -q "VICE 3.10" || return 1
    x64sc --help 2>&1 | grep -qiE -- '-ethernetcart' || return 1
    return 0
}

stage_vice_build() {
    banner "Stage 2 -- VICE 3.10 source build"
    if [ "$NO_VICE" = "1" ]; then
        log_skip "--no-vice passed"
        return
    fi
    if [ "$OS" = "Darwin" ]; then
        # On macOS we install VICE via Homebrew in stage 1, which ships a
        # prebuilt bottle with ethernet support enabled. Building from
        # source isn't needed and the upstream tarball's ./configure
        # doesn't support Darwin out of the box without patches.
        log_skip "macOS uses Homebrew-installed VICE (see stage 1); source build not applicable"
        return
    fi
    if vice_already_installed; then
        log_skip "VICE 3.10 with ethernet already installed at $(command -v x64sc)"
        return
    fi

    log_install "build dir: $BUILD_DIR"
    run_cmd "mkdir-build" mkdir -p "$BUILD_DIR"

    local tarball="$BUILD_DIR/$VICE_TARBALL_NAME"
    local srcdir="$BUILD_DIR/$VICE_SRC_DIRNAME"

    # Download
    if [ -f "$tarball" ] && [ "$DRY_RUN" != "1" ]; then
        log_skip "tarball already cached: $tarball"
    else
        log_install "downloading $VICE_TARBALL_URL"
        run_cmd "download" curl -sSL -o "$tarball" "$VICE_TARBALL_URL"
    fi

    # Optional SHA256 verification
    if [ -n "$EXPECTED_SHA256" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            log_dry "sha256sum $tarball | grep -q $EXPECTED_SHA256"
        else
            local actual
            actual="$(sha256sum "$tarball" 2>/dev/null | awk '{print $1}')"
            if [ "$actual" = "$EXPECTED_SHA256" ]; then
                log_ok "sha256 verified: $actual"
            else
                log_fail "sha256 mismatch: expected $EXPECTED_SHA256 got $actual"
                return
            fi
        fi
    else
        log_warn "no --sha256 pin given; skipping integrity verification of $tarball"
    fi

    # Extract
    if [ -d "$srcdir/configure" ] || { [ -d "$srcdir" ] && [ -f "$srcdir/configure" ]; }; then
        log_skip "source already extracted: $srcdir"
    else
        log_install "extracting $tarball"
        run_cmd "extract" tar -xf "$tarball" -C "$BUILD_DIR"
    fi

    # Configure + build + install. Flags rationale:
    #   --enable-ethernet       load-bearing: CS8900a / TFE / RR-Net support
    #   --enable-shared         shared libs (default, kept explicit)
    #   --disable-html-docs     skip doc generation; shaves minutes off build
    #   --enable-native-gtk3ui  native GTK3 UI
    #
    # UI choice: GTK3 was picked to match the binary on the current dev
    # machine (inferred from the installed `x64sc`; the actual build log
    # is not on disk). SDL2 is a reasonable alternative with fewer deps --
    # to switch, replace `--enable-native-gtk3ui` below with
    # `--enable-sdlui2` (and you can drop libgtk-3-dev / libglew-dev /
    # libxaw7-dev from the stage 1 package list). Both UIs work with this
    # test harness, which only talks to VICE via the binary monitor and
    # doesn't care about the UI toolkit.
    local configure_cmd=(
        ./configure
        --enable-ethernet
        --enable-shared
        --disable-html-docs
        --enable-native-gtk3ui
    )
    if [ "$DRY_RUN" = "1" ]; then
        log_dry "cd $srcdir && ${configure_cmd[*]}"
        log_dry "cd $srcdir && make -j\$(nproc)"
        log_dry "cd $srcdir && sudo make install"
        log_dry "hash -r && x64sc --version && x64sc --help | grep -i ethernet"
        log_ok "stage 2 (dry-run): would configure, build, and install VICE 3.10"
        return
    fi

    log_install "configuring VICE"
    if ! ( cd "$srcdir" && "${configure_cmd[@]}" ); then
        log_fail "VICE configure failed (see $srcdir/config.log)"
        return
    fi

    local nproc_val
    nproc_val="$(nproc 2>/dev/null || echo 2)"
    log_install "building VICE with -j$nproc_val (this takes several minutes)"
    if ! ( cd "$srcdir" && make -j"$nproc_val" ); then
        log_fail "VICE make failed"
        return
    fi

    log_install "installing VICE (sudo make install)"
    if ! ( cd "$srcdir" && sudo make install ); then
        log_fail "VICE sudo make install failed"
        return
    fi

    hash -r 2>/dev/null || true
    if vice_already_installed; then
        log_ok "VICE 3.10 with ethernet installed at $(command -v x64sc)"
    else
        log_fail "VICE install completed but verification failed -- check x64sc --help"
    fi
}

# ---------- Stage 3: Python harness install -------------------------------

stage_harness_install() {
    banner "Stage 3 -- Python harness editable install (dedicated venv)"
    if [ "$NO_HARNESS" = "1" ]; then
        log_skip "--no-harness passed"
        return
    fi

    # PEP 668 / externally-managed-environment: Ubuntu 23+ ships
    # /usr/lib/python3*/EXTERNALLY-MANAGED and Ubuntu 25 enforces it, so
    # `pip install --user` against system Python errors out. We create a
    # dedicated venv at $VENV_DIR and install into it with plain
    # `pip install -e` instead.
    #
    # --system-site-packages is used so system-installed libraries (pytest,
    # pyyaml, etc.) remain visible inside the venv -- the harness itself
    # has zero runtime deps so the usual "system pip leaks into venv" risk
    # is low, and the gain is big (no surprise "ModuleNotFoundError: yaml"
    # after creating the venv).
    local expected_pkg="$REPO_ROOT/src/c64_test_harness"

    # Idempotency: venv exists AND harness is importable from it AND resolves
    # to this repo checkout -> skip.
    if [ "$DRY_RUN" != "1" ] && [ -x "$VENV_DIR/bin/python" ]; then
        local installed_path=""
        installed_path="$("$VENV_DIR/bin/python" -c 'import c64_test_harness, os; print(os.path.dirname(c64_test_harness.__file__))' 2>/dev/null || true)"
        if [ -n "$installed_path" ] && [ "$installed_path" = "$expected_pkg" ]; then
            log_skip "c64_test_harness already installed editable in $VENV_DIR (resolves to $installed_path)"
            return
        fi
        if [ -n "$installed_path" ]; then
            log_warn "venv at $VENV_DIR has c64_test_harness from $installed_path (expected $expected_pkg); will reinstall"
        fi
    fi

    if [ "$DRY_RUN" = "1" ]; then
        log_dry "mkdir -p $(dirname "$VENV_DIR")"
        log_dry "python3 -m venv --system-site-packages $VENV_DIR"
        log_dry "$VENV_DIR/bin/pip install --upgrade pip"
        log_dry "$VENV_DIR/bin/pip install -e $REPO_ROOT"
        log_dry "$VENV_DIR/bin/python -c 'import c64_test_harness; print(c64_test_harness.__version__)'"
        log_ok "stage 3 (dry-run): would create venv at $VENV_DIR and install harness editable"
        log_ok "stage 3 (dry-run): activate with: source $VENV_DIR/bin/activate"
        return
    fi

    log_install "creating venv at $VENV_DIR"
    run_cmd "mkdir-venv-parent" mkdir -p "$(dirname "$VENV_DIR")"
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        if ! python3 -m venv --system-site-packages "$VENV_DIR"; then
            log_fail "python3 -m venv failed -- is python3-venv installed?"
            return
        fi
    else
        log_skip "venv already present at $VENV_DIR (reusing)"
    fi

    log_install "upgrading pip inside venv"
    if ! "$VENV_DIR/bin/pip" install --upgrade pip; then
        log_warn "pip upgrade failed; continuing with bundled pip"
    fi

    log_install "pip install -e $REPO_ROOT (inside venv)"
    if ! "$VENV_DIR/bin/pip" install -e "$REPO_ROOT"; then
        log_fail "editable install failed"
        return
    fi

    if "$VENV_DIR/bin/python" -c 'import c64_test_harness; print(c64_test_harness.__version__)' 2>/dev/null; then
        log_ok "installed into venv at $VENV_DIR"
        printf '[next] activate the venv with:\n'
        printf '    source %s/bin/activate\n' "$VENV_DIR"
        printf '[next] or run tests via:\n'
        printf '    %s/bin/python -m pytest tests/\n' "$VENV_DIR"
    else
        log_fail "c64_test_harness import check failed after install into $VENV_DIR"
    fi
}

# ---------- Stage 4: bridge networking ------------------------------------

stage_bridge_setup() {
    banner "Stage 4 -- bridge networking"
    if [ "$NO_BRIDGE" = "1" ]; then
        log_skip "--no-bridge passed"
        return
    fi

    if [ "$OS" = "Darwin" ]; then
        # macOS uses bridge10 + feth0/feth1 (see tests/bridge_platform.py).
        # Interface existence is probed via `ifconfig`; the BSD bridge
        # driver doesn't expose sysfs.
        if ifconfig bridge10 >/dev/null 2>&1 \
           && ifconfig feth0 >/dev/null 2>&1 \
           && ifconfig feth1 >/dev/null 2>&1; then
            log_skip "bridge10 + feth0 + feth1 already present"
            return
        fi

        local setup_script="$REPO_ROOT/scripts/setup-bridge-feth-macos.sh"
        if [ ! -x "$setup_script" ]; then
            log_fail "$setup_script missing or not executable"
            return
        fi

        run_sudo "bridge-setup" "$setup_script"

        if [ "$DRY_RUN" = "1" ]; then
            log_ok "stage 4 (dry-run): would run sudo $setup_script"
            return
        fi

        if ifconfig bridge10 >/dev/null 2>&1 \
           && ifconfig feth0 >/dev/null 2>&1 \
           && ifconfig feth1 >/dev/null 2>&1; then
            log_ok "bridge + feth interfaces present"
        else
            log_fail "bridge setup reported success but interfaces missing"
        fi
        return
    fi

    # -------- Linux path --------
    if [ -d /sys/class/net/br-c64 ] && [ -d /sys/class/net/tap-c64-0 ] && [ -d /sys/class/net/tap-c64-1 ]; then
        log_skip "br-c64 + tap-c64-0 + tap-c64-1 already present"
        return
    fi

    local setup_script="$REPO_ROOT/scripts/setup-bridge-tap.sh"
    if [ ! -x "$setup_script" ]; then
        log_fail "$setup_script missing or not executable"
        return
    fi

    run_sudo "bridge-setup" "$setup_script"

    if [ "$DRY_RUN" = "1" ]; then
        log_ok "stage 4 (dry-run): would run sudo $setup_script"
        return
    fi

    if [ -d /sys/class/net/br-c64 ] && [ -d /sys/class/net/tap-c64-0 ] && [ -d /sys/class/net/tap-c64-1 ]; then
        log_ok "bridge + TAP interfaces present"
    else
        log_fail "bridge setup reported success but interfaces missing"
    fi
}

# ---------- Stage 5: Ultimate 64 probe ------------------------------------

stage_u64_probe() {
    banner "Stage 5 -- Ultimate 64 probe"
    if [ "$NO_U64" = "1" ]; then
        log_skip "--no-u64 passed"
        return
    fi
    if [ -z "$U64_HOST" ]; then
        log_skip "U64_HOST not set and --u64-host not passed"
        return
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl not installed; cannot probe U64"
        return
    fi
    local url="http://${U64_HOST}/v1/version"
    if [ "$DRY_RUN" = "1" ]; then
        log_dry "curl -sf -m 3 $url"
        log_ok "stage 5 (dry-run): would probe $U64_HOST"
        return
    fi
    local body
    if body="$(curl -sf -m 3 "$url" 2>/dev/null)"; then
        log_ok "U64 reachable at $U64_HOST"
        printf '       %s\n' "$body"
    else
        log_warn "U64 at $U64_HOST not reachable (non-fatal)"
    fi
}

# ---------- Stage 6: run verify-dev-env.sh --------------------------------

VERIFY_EXIT=0

run_verify() {
    banner "Stage 6 -- scripts/verify-dev-env.sh"
    local verify="$REPO_ROOT/scripts/verify-dev-env.sh"
    # verify-dev-env.sh is non-destructive; run it even in dry-run mode so the
    # user sees the current state of the machine.
    #
    # verify-dev-env.sh uses plain `python3` to probe for the harness. Since
    # we install into a dedicated venv (stage 3), that `python3` would
    # resolve to system Python and fail the import check even though the
    # install succeeded. Prepend the venv's bin dir to PATH for the verify
    # invocation only -- this mirrors what an activated venv does, and
    # avoids modifying verify-dev-env.sh in this PR.
    if [ -x "$VENV_DIR/bin/python3" ] || [ -x "$VENV_DIR/bin/python" ]; then
        log_install "prepending $VENV_DIR/bin to PATH for verify run"
        PATH="$VENV_DIR/bin:$PATH" "$verify"
    else
        "$verify"
    fi
    VERIFY_EXIT=$?
}

# ---------- summary -------------------------------------------------------

print_summary() {
    banner "Summary"
    case "$VERIFY_EXIT" in
        0)
            log_ok "verify-dev-env.sh says READY"
            ;;
        1)
            log_fail "verify-dev-env.sh says NOT READY -- see output above"
            ;;
        2)
            log_fail "verify-dev-env.sh returned script-error (exit 2)"
            ;;
        *)
            log_fail "verify-dev-env.sh returned unexpected exit code $VERIFY_EXIT"
            ;;
    esac
    if [ "$DRY_RUN" = "1" ]; then
        printf '\n(dry-run: no system changes were made)\n'
    fi
}

# ---------- main ----------------------------------------------------------

main() {
    parse_args "$@"
    if [ "$DRY_RUN" = "1" ]; then
        printf '*** DRY RUN MODE -- no commands will be executed ***\n'
    fi
    check_os
    check_repo_root
    stage_system_packages
    stage_vice_build
    stage_harness_install
    stage_bridge_setup
    stage_u64_probe
    run_verify
    print_summary
    case "$VERIFY_EXIT" in
        0) exit 0 ;;
        1) exit 1 ;;
        2) exit 3 ;;
        *) exit 3 ;;
    esac
}

main "$@"
