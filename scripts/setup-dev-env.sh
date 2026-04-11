#!/usr/bin/env bash
#
# setup-dev-env.sh -- fresh Ubuntu Desktop 25 installer for c64-test-harness.
#
# Takes a zero-state machine to a state where scripts/verify-dev-env.sh reports
# READY. Every stage is idempotent (re-runs are safe) and opt-out via --no-*
# flags. Every mutating action is gated behind --dry-run so reviewers can
# inspect what would happen without touching the system.
#
# Stages:
#   1. system packages (apt-get)           -- opt-out --no-system-packages
#   2. VICE 3.10 source build + install    -- opt-out --no-vice
#   3. Python harness editable install     -- opt-out --no-harness
#   4. bridge networking setup             -- opt-out --no-bridge
#   5. Ultimate 64 probe (optional)        -- opt-in via $U64_HOST or --u64-host
#   6. final verify-dev-env.sh run
#
# Safety: set -u (not -e) so the script continues through skipped/optional
# stages. set -o pipefail for pipeline error propagation.
#
# Ubuntu Desktop 25 (25.04 / 25.10) is the target. Other distros are warned
# against via check_os() but can proceed with --force.

set -u
set -o pipefail

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
setup-dev-env.sh -- fresh Ubuntu 25 installer for c64-test-harness

USAGE:
  setup-dev-env.sh [OPTIONS]

OPTIONS:
  --dry-run             Print actions without executing (safe; still runs verify)
  --force               Skip the Ubuntu 25 version check
  --no-system-packages  Skip apt-get install step
  --no-vice             Skip VICE source build
  --no-harness          Skip pip install -e .
  --no-bridge           Skip bridge network setup
  --no-u64              Skip U64 probe even if U64_HOST is set
  --u64-host HOST       Probe this U64 host (overrides $U64_HOST)
  --build-dir DIR       Where to put VICE source (default ~/.cache/c64-test-harness/build)
  --sha256 HEX          Expected SHA256 of VICE tarball (optional pin)
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

stage_system_packages() {
    banner "Stage 1 -- system packages"
    if [ "$NO_SYSTEM_PACKAGES" = "1" ]; then
        log_skip "--no-system-packages passed"
        return
    fi

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
        libgtkglext1-dev      # legacy GL extensions; may be missing on 25+
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
    #   --enable-native-gtk3ui  native GTK3 UI (matches current dev machine)
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
    banner "Stage 3 -- Python harness editable install"
    if [ "$NO_HARNESS" = "1" ]; then
        log_skip "--no-harness passed"
        return
    fi

    # Idempotency: if already importable from this repo root, skip.
    if [ "$DRY_RUN" != "1" ] && python3 -c 'import c64_test_harness' >/dev/null 2>&1; then
        local installed_path
        installed_path="$(python3 -c 'import c64_test_harness, os; print(os.path.dirname(c64_test_harness.__file__))' 2>/dev/null || true)"
        local expected_prefix="$REPO_ROOT/src/c64_test_harness"
        if [ -n "$installed_path" ] && [ "$installed_path" = "$expected_prefix" ]; then
            log_skip "c64_test_harness already installed editable from $installed_path"
            return
        fi
        log_warn "c64_test_harness importable from $installed_path (expected $expected_prefix); will reinstall"
    fi

    log_install "pip install --user -e .[dev]"
    if [ "$DRY_RUN" = "1" ]; then
        log_dry "cd $REPO_ROOT && python3 -m pip install --user -e '.[dev]'"
        log_dry "python3 -c 'import c64_test_harness; print(c64_test_harness.__version__)'"
        log_ok "stage 3 (dry-run): would install harness in editable mode"
        return
    fi

    if ! ( cd "$REPO_ROOT" && python3 -m pip install --user -e '.[dev]' ); then
        log_fail "pip install failed"
        return
    fi

    if python3 -c 'import c64_test_harness; print(c64_test_harness.__version__)' 2>/dev/null; then
        log_ok "c64_test_harness import check passed"
    else
        log_fail "c64_test_harness import check failed after install"
    fi
}

# ---------- Stage 4: bridge networking ------------------------------------

stage_bridge_setup() {
    banner "Stage 4 -- bridge networking"
    if [ "$NO_BRIDGE" = "1" ]; then
        log_skip "--no-bridge passed"
        return
    fi

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
    "$verify"
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
