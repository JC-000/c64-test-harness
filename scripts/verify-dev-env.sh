#!/usr/bin/env bash
#
# verify-dev-env.sh — non-destructive dev environment check for c64-test-harness
#
# SAFETY: This script is read-only. It never launches VICE (beyond --version /
# --help, which exit immediately), never runs pytest, never mutates network
# state, never touches anything outside the repo and a read-only probe of the
# Ultimate 64 REST API (only if U64_HOST is set and --no-u64 is not passed).
#
# Exit codes:
#   0 — READY (all critical checks passed; optional gaps allowed)
#   1 — NOT READY (one or more critical checks failed)
#   2 — script error (bad args, not in a repo, etc.)
#
# Usage:
#   verify-dev-env.sh [--quiet] [--json] [--no-u64] [--u64-host HOST]

set -u

QUIET=0
JSON=0
NO_U64=0
U64_HOST="${U64_HOST:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --quiet) QUIET=1; shift ;;
        --json) JSON=1; shift ;;
        --no-u64) NO_U64=1; shift ;;
        --u64-host)
            if [ $# -lt 2 ]; then
                echo "error: --u64-host needs a value" >&2
                exit 2
            fi
            U64_HOST="$2"; shift 2 ;;
        --u64-host=*) U64_HOST="${1#*=}"; shift ;;
        -h|--help)
            cat <<'EOF'
verify-dev-env.sh — non-destructive dev environment check

Options:
  --quiet            Suppress section headers; print summary + failures only
  --json             Emit results as a single JSON object
  --no-u64           Skip the Ultimate 64 reachability probe
  --u64-host HOST    U64 hostname/IP to probe (overrides $U64_HOST)
  -h, --help         Show this help

Exit codes: 0=READY, 1=NOT READY, 2=script error
EOF
            exit 0 ;;
        *)
            echo "error: unknown argument: $1" >&2
            exit 2 ;;
    esac
done

# ---------- result collection ---------------------------------------------

# Accumulators (parallel arrays: section / label / status / detail / critical)
R_SECTION=()
R_LABEL=()
R_STATUS=()  # ok / missing / unknown / warn / skipped
R_DETAIL=()
R_CRIT=()    # 1 = critical, 0 = optional
FIX_HINTS=() # section-scoped fix hints

OK=0
MISSING=0
SKIPPED=0
WARN=0
BLOCKER=""

record() {
    # record SECTION LABEL STATUS DETAIL CRITICAL
    R_SECTION+=("$1")
    R_LABEL+=("$2")
    R_STATUS+=("$3")
    R_DETAIL+=("$4")
    R_CRIT+=("$5")
    case "$3" in
        ok)      OK=$((OK+1)) ;;
        missing)
            MISSING=$((MISSING+1))
            if [ "$5" = "1" ] && [ -z "$BLOCKER" ]; then
                BLOCKER="$1: $2"
            fi
            ;;
        skipped) SKIPPED=$((SKIPPED+1)) ;;
        warn)    WARN=$((WARN+1)) ;;
        unknown) WARN=$((WARN+1)) ;;
    esac
}

add_hint() {
    FIX_HINTS+=("$1")
}

# ---------- check helpers -------------------------------------------------

have_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- Section 1: VICE ----------------------------------------------

check_vice() {
    local sec="VICE"
    if have_cmd x64sc; then
        local path
        path="$(command -v x64sc)"
        record "$sec" "x64sc on PATH" ok "$path" 1

        # Version check. Primary: `x64sc --version`. On macOS 26 Tahoe the
        # Homebrew vice 3.10 bottle has an upstream init-order bug where
        # archdep_program_path_set_argv0() is called *after* `--version` is
        # handled, so `proc_pidpath()` failure there hits a NULL argv0_ref
        # and exits 1 without printing the version. Fall back to other
        # signals (brew metadata, Cellar path, -features) when the primary
        # path yields nothing. See docs/development.md "macOS (Homebrew)".
        local ver_out ver ver_source=""
        ver_out="$(x64sc --version 2>&1 | head -5 || true)"
        ver="$(printf '%s\n' "$ver_out" | grep -oE 'VICE[[:space:]]+[0-9]+\.[0-9]+' | head -1)"
        if [ -n "$ver" ]; then
            ver_source="--version"
        elif have_cmd brew; then
            # Fallback 1: `brew list --versions vice` prints e.g. "vice 3.10"
            local brew_ver
            brew_ver="$(brew list --versions vice 2>/dev/null | awk '{print $2}' | head -1)"
            if [ -n "$brew_ver" ]; then
                ver="VICE $brew_ver"
                ver_source="brew"
            fi
        fi
        if [ -z "$ver" ]; then
            # Fallback 2: parse Cellar path from the symlink target, e.g.
            # /opt/homebrew/bin/x64sc -> ../Cellar/vice/3.10/bin/x64sc
            local real_path
            real_path="$(readlink "$path" 2>/dev/null || true)"
            local cellar_ver
            cellar_ver="$(printf '%s' "$real_path" | sed -nE 's#.*/Cellar/vice/([0-9]+\.[0-9]+).*#\1#p')"
            if [ -n "$cellar_ver" ]; then
                ver="VICE $cellar_ver"
                ver_source="Cellar path"
            fi
        fi
        if [ -z "$ver" ]; then
            # Fallback 3: `-features` is another code path that survives the
            # argv[0] issue; if it runs at all, the binary is functional.
            if x64sc -features 2>/dev/null | grep -qi 'HAVE_'; then
                ver="unknown (binary functional)"
                ver_source="-features probe"
            fi
        fi
        if [ -n "$ver" ]; then
            local detail="$ver"
            [ -n "$ver_source" ] && [ "$ver_source" != "--version" ] && detail="$ver [via $ver_source; --version broken on macOS 26]"
            if printf '%s' "$ver" | grep -q '3\.10'; then
                record "$sec" "VICE version" ok "$detail" 1
            elif [ "$ver_source" = "-features probe" ]; then
                record "$sec" "VICE version" warn "$detail" 0
            else
                record "$sec" "VICE version" warn "$ver (expected VICE 3.10)" 0
            fi
        else
            record "$sec" "VICE version" missing "could not parse version" 1
            add_hint "Install VICE 3.10: see docs/development.md"
        fi

        # Help-text probes for ethernet + monitor flags
        local help_out
        help_out="$(x64sc --help 2>&1 || true)"
        if printf '%s' "$help_out" | grep -qiE -- '-ethernetcart|-ethernetioif|-ethernetiodriver'; then
            record "$sec" "ethernet cart support" ok "ethernet flags found in --help" 1
        else
            record "$sec" "ethernet cart support" missing "VICE built without --enable-ethernet" 1
            add_hint "Rebuild VICE 3.10 from source with --enable-ethernet (distro packages usually omit it)"
        fi

        if printf '%s' "$help_out" | grep -qi -- '-binarymonitor'; then
            record "$sec" "binary monitor support" ok "-binarymonitor flag present" 1
        else
            record "$sec" "binary monitor support" missing "-binarymonitor flag not advertised" 1
        fi

        if printf '%s' "$help_out" | grep -qi -- '-remotemonitor'; then
            record "$sec" "text monitor support" ok "-remotemonitor flag present" 0
        else
            record "$sec" "text monitor support" warn "-remotemonitor flag not advertised (needed for warp toggle)" 0
        fi
    else
        record "$sec" "x64sc on PATH" missing "not found" 1
        record "$sec" "VICE version" skipped "(x64sc missing)" 1
        record "$sec" "ethernet cart support" skipped "(x64sc missing)" 1
        record "$sec" "binary monitor support" skipped "(x64sc missing)" 1
        record "$sec" "text monitor support" skipped "(x64sc missing)" 0
        add_hint "Install VICE 3.10 built with --enable-ethernet; see docs/development.md"
    fi

    if have_cmd c1541; then
        local c1541_ver
        c1541_ver="$(c1541 --version 2>&1 | grep -oE 'VICE[[:space:]]+[0-9]+\.[0-9]+' | head -1 || true)"
        [ -z "$c1541_ver" ] && c1541_ver="present"
        record "$sec" "c1541 on PATH" ok "$c1541_ver" 1
    else
        record "$sec" "c1541 on PATH" missing "not found" 1
    fi
}

# ---------- Section 2: Python --------------------------------------------

# Pick the python3 to probe. A stale system python3 (e.g. macOS ships 3.9.6)
# would make the harness look unhealthy even when the real runtime -- a venv
# -- is modern. Priority:
#   1. $VIRTUAL_ENV/bin/python3  (an active venv trumps everything)
#   2. ~/.local/share/c64-test-harness/venv/bin/python3  (harness convention
#      per docs/development.md)
#   3. $REPO_ROOT/.venv/bin/python3  (in-tree venv, common layout)
#   4. system python3 on PATH
# Prints the selected binary path on stdout, empty if nothing is found.
select_py_bin() {
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then
        printf '%s' "$VIRTUAL_ENV/bin/python3"
        return
    fi
    local harness_venv="$HOME/.local/share/c64-test-harness/venv/bin/python3"
    if [ -x "$harness_venv" ]; then
        printf '%s' "$harness_venv"
        return
    fi
    if [ -n "${REPO_ROOT:-}" ] && [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
        printf '%s' "$REPO_ROOT/.venv/bin/python3"
        return
    fi
    if have_cmd python3; then
        command -v python3
        return
    fi
}

# Classify $1 (a python3 path) by origin, for transparent reporting.
py_bin_source() {
    local py_bin="$1"
    if [ -n "${VIRTUAL_ENV:-}" ] && [ "$py_bin" = "$VIRTUAL_ENV/bin/python3" ]; then
        printf 'active venv'
        return
    fi
    case "$py_bin" in
        "$HOME/.local/share/c64-test-harness/venv/"*) printf 'harness venv' ;;
        "${REPO_ROOT:-/nope}/.venv/"*)                printf 'repo .venv' ;;
        *)                                            printf 'system' ;;
    esac
}

check_python() {
    local sec="Python"
    local py_bin py_source
    py_bin="$(select_py_bin)"

    if [ -z "$py_bin" ]; then
        record "$sec" "python3" missing "not found" 1
        record "$sec" "c64_test_harness importable" skipped "(python3 missing)" 1
        record "$sec" "pytest available" skipped "(python3 missing)" 0
        return
    fi

    py_source="$(py_bin_source "$py_bin")"

    local py_ver
    py_ver="$("$py_bin" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    if [ -z "$py_ver" ]; then
        record "$sec" "python3" missing "present at $py_bin but failed to report version" 1
    else
        local major minor
        major="$(printf '%s' "$py_ver" | cut -d. -f1)"
        minor="$(printf '%s' "$py_ver" | cut -d. -f2)"
        if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }; then
            record "$sec" "python3 >= 3.10" ok "$py_ver [$py_source: $py_bin]" 1
        else
            record "$sec" "python3 >= 3.10" missing "found $py_ver at $py_bin [$py_source], need >= 3.10" 1
            if [ "$py_source" = "system" ]; then
                add_hint "System python3 is too old. Create/activate a harness venv (see docs/development.md) -- verify-dev-env.sh will then pick it up automatically."
            fi
        fi
    fi

    # Harness import
    if "$py_bin" -c 'import c64_test_harness' >/dev/null 2>&1; then
        local harness_ver
        harness_ver="$("$py_bin" -c 'import c64_test_harness as c; print(getattr(c, "__version__", "unknown"))' 2>/dev/null || echo unknown)"
        record "$sec" "c64_test_harness importable" ok "version $harness_ver [$py_source]" 1
    else
        record "$sec" "c64_test_harness importable" missing "import failed [$py_source: $py_bin]" 1
        add_hint "From repo root with the harness venv active: pip install -e '.[dev]'"
    fi

    # pytest (dev dep, warn only)
    if "$py_bin" -c 'import pytest' >/dev/null 2>&1; then
        local pt_ver
        pt_ver="$("$py_bin" -c 'import pytest; print(pytest.__version__)' 2>/dev/null || echo '?')"
        record "$sec" "pytest available" ok "$pt_ver" 0
    else
        record "$sec" "pytest available" warn "pytest not installed (dev dep)" 0
        add_hint "Install dev deps: pip install -e '.[dev]'"
    fi
}

# ---------- Section 3: System tools ---------------------------------------

check_system() {
    local sec="System tools"
    # Bridge/TAP networking uses iproute2 + iptables + /dev/net/tun on Linux.
    # On macOS the equivalent is ifconfig + the BSD bridge driver + feth(4),
    # which are all part of the base system (no package install). Skip the
    # Linux-specific checks on Darwin and verify ifconfig instead.
    if [ "$(uname)" = "Darwin" ]; then
        if have_cmd ifconfig; then
            record "$sec" "ifconfig command" ok "$(command -v ifconfig)" 0
        else
            record "$sec" "ifconfig command" missing "ifconfig not found (macOS base tool)" 0
        fi
        if [ -c /dev/bpf0 ]; then
            if [ -r /dev/bpf0 ]; then
                record "$sec" "/dev/bpf0 readable" ok "BPF devices user-readable (pcap ready)" 0
            else
                record "$sec" "/dev/bpf0 readable" warn "BPF devices are root-only (VICE pcap driver needs sudo chmod 666 /dev/bpf* or Wireshark's ChmodBPF helper)" 0
                add_hint "Make /dev/bpf* user-readable: sudo chmod 666 /dev/bpf* (session) or install Wireshark's ChmodBPF (permanent)"
            fi
        else
            record "$sec" "/dev/bpf0 readable" missing "no /dev/bpf* device" 0
        fi
    else
        if have_cmd ip; then
            record "$sec" "ip command" ok "$(command -v ip)" 0
        else
            record "$sec" "ip command" missing "iproute2 not installed" 0
            add_hint "Install iproute2 (Ubuntu: sudo apt-get install iproute2)"
        fi
        if have_cmd iptables; then
            record "$sec" "iptables command" ok "$(command -v iptables)" 0
        else
            record "$sec" "iptables command" missing "iptables not installed" 0
            add_hint "Install iptables (Ubuntu: sudo apt-get install iptables)"
        fi
        if [ -c /dev/net/tun ]; then
            record "$sec" "/dev/net/tun" ok "present" 0
        else
            record "$sec" "/dev/net/tun" missing "TUN/TAP device node missing" 0
            add_hint "Load the tun kernel module: sudo modprobe tun"
        fi
    fi
    if sudo -n true 2>/dev/null; then
        record "$sec" "passwordless sudo" ok "available (informational)" 0
    else
        record "$sec" "passwordless sudo" unknown "not available (not a failure; needed for bridge setup)" 0
    fi
}

# ---------- Section 4: Bridge networking ----------------------------------

check_bridge() {
    local sec="Bridge networking"
    local any_missing=0
    # Bridge layout is platform-dependent. Linux uses a Linux bridge (br-c64)
    # + TAP devices (tap-c64-{0,1}); macOS uses bridge10 + feth peer pairs
    # (feth0/feth1). Both sets of rows are non-critical — the ethernet test
    # subset (5 files) skips cleanly when the bridge isn't set up.
    if [ "$(uname)" = "Darwin" ]; then
        for iface in bridge10 feth0 feth1; do
            if ifconfig "$iface" >/dev/null 2>&1; then
                record "$sec" "$iface" ok "present" 0
            else
                record "$sec" "$iface" missing "not found" 0
                any_missing=1
            fi
        done
        if [ "$any_missing" = "1" ]; then
            add_hint "Run: sudo ./scripts/setup-bridge-feth-macos.sh"
        fi
    else
        if [ -d /sys/class/net/br-c64 ]; then
            record "$sec" "br-c64 bridge" ok "present" 0
        else
            record "$sec" "br-c64 bridge" missing "not found" 0
            any_missing=1
        fi
        if [ -d /sys/class/net/tap-c64-0 ]; then
            record "$sec" "tap-c64-0" ok "present" 0
        else
            record "$sec" "tap-c64-0" missing "not found" 0
            any_missing=1
        fi
        if [ -d /sys/class/net/tap-c64-1 ]; then
            record "$sec" "tap-c64-1" ok "present" 0
        else
            record "$sec" "tap-c64-1" missing "not found" 0
            any_missing=1
        fi
        if [ "$any_missing" = "1" ]; then
            add_hint "Run: sudo ./scripts/setup-bridge-tap.sh"
        fi
    fi
}

# ---------- Section 5: Ultimate 64 ---------------------------------------

U64_CHECKED=false
U64_REACHABLE="null"
U64_VERSION="null"

check_u64() {
    local sec="Ultimate 64"
    if [ "$NO_U64" = "1" ]; then
        record "$sec" "U64 probe" skipped "--no-u64 passed" 0
        return
    fi
    if [ -z "$U64_HOST" ]; then
        record "$sec" "U64 probe" skipped "set U64_HOST or pass --u64-host to enable" 0
        return
    fi
    U64_CHECKED=true
    if ! have_cmd curl; then
        record "$sec" "curl available" missing "curl not installed (needed for U64 probe)" 0
        return
    fi
    local url="http://${U64_HOST}/v1/version"
    local body
    if body="$(curl -sf -m 3 "$url" 2>/dev/null)"; then
        U64_REACHABLE="true"
        record "$sec" "U64 reachable at $U64_HOST" ok "HTTP 200 from /v1/version" 0
        # Best-effort version extraction (no jq dependency)
        local ver
        ver="$(printf '%s' "$body" | python3 -c 'import sys,json;
try:
    d=json.loads(sys.stdin.read())
    for k in ("version","firmware","fw","fw_version"):
        if k in d: print(d[k]); break
    else:
        print("")
except Exception:
    print("")' 2>/dev/null || true)"
        if [ -n "$ver" ]; then
            U64_VERSION="$ver"
            record "$sec" "U64 firmware" ok "$ver" 0
        else
            record "$sec" "U64 firmware" unknown "could not parse version from /v1/version" 0
        fi
    else
        U64_REACHABLE="false"
        record "$sec" "U64 reachable at $U64_HOST" missing "HTTP probe failed (timeout/connection refused)" 0
    fi
}

# ---------- Section 6: Repo state ----------------------------------------

REPO_ROOT=""
REPO_BRANCH=""
REPO_SHA=""

check_repo() {
    local sec="Repo"
    # Walk up from script location to find pyproject.toml with c64-test-harness
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
    if [ -n "$REPO_ROOT" ]; then
        record "$sec" "c64-test-harness repo" ok "$REPO_ROOT" 1
        if have_cmd git && [ -d "$REPO_ROOT/.git" ] || git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
            REPO_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
            REPO_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')"
            record "$sec" "git state" ok "branch: $REPO_BRANCH, commit: $REPO_SHA" 0
        else
            record "$sec" "git state" unknown "not a git checkout" 0
        fi
    else
        record "$sec" "c64-test-harness repo" missing "run this script from inside a c64-test-harness checkout" 1
    fi
}

# ---------- Run all checks ------------------------------------------------

check_repo
check_vice
check_python
check_system
check_bridge
check_u64

# ---------- Output --------------------------------------------------------

status_glyph() {
    # printf %b interprets octal escapes, which are portable across
    # bash 3.2 (macOS) and bash 4+ (Linux). `\u` escapes are bash 4.2+
    # only and render as literal "\u2713" on the stock macOS bash.
    case "$1" in
        ok) printf '%b' '\342\234\223' ;;       # U+2713 check
        missing) printf '%b' '\342\234\227' ;;  # U+2717 cross
        skipped|unknown) printf '?' ;;
        warn) printf '%b' '\342\232\240' ;;     # U+26A0 warn
        *) printf '?' ;;
    esac
}

# Determine overall state
OVERALL="READY"
OVERALL_REASON=""
# critical_missing = any row with status=missing and critical=1
CRIT_MISSING=0
for i in "${!R_STATUS[@]}"; do
    if [ "${R_STATUS[$i]}" = "missing" ] && [ "${R_CRIT[$i]}" = "1" ]; then
        CRIT_MISSING=$((CRIT_MISSING+1))
    fi
done

if [ "$CRIT_MISSING" -gt 0 ]; then
    OVERALL="NOT READY"
    OVERALL_REASON="$BLOCKER"
elif [ "$MISSING" -gt 0 ] || [ "$WARN" -gt 0 ]; then
    OVERALL="READY (with optional gaps)"
fi

if [ "$JSON" = "1" ]; then
    # Defer to python for clean JSON
    python3 - "$OK" "$MISSING" "$SKIPPED" "$WARN" "$OVERALL" "$OVERALL_REASON" \
             "$U64_CHECKED" "$U64_REACHABLE" "$U64_VERSION" \
             "$REPO_ROOT" "$REPO_BRANCH" "$REPO_SHA" <<'PY' "${R_SECTION[@]}" "||" "${R_LABEL[@]}" "||" "${R_STATUS[@]}" "||" "${R_DETAIL[@]}" "||" "${R_CRIT[@]}"
import json, sys
ok, missing, skipped, warn, overall, reason = sys.argv[1:7]
u64_checked, u64_reachable, u64_version = sys.argv[7:10]
repo_root, repo_branch, repo_sha = sys.argv[10:13]
rest = sys.argv[13:]
# Split on '||' sentinel
groups = []
cur = []
for x in rest:
    if x == "||":
        groups.append(cur); cur = []
    else:
        cur.append(x)
groups.append(cur)
sections, labels, statuses, details, crits = groups
rows = []
for s, l, st, d, c in zip(sections, labels, statuses, details, crits):
    rows.append({"section": s, "label": l, "status": st, "detail": d, "critical": c == "1"})
def by_section(name):
    return [r for r in rows if r["section"] == name]
def to_bool(rows_):
    return {r["label"]: r["status"] == "ok" for r in rows_}
out = {
    "vice": to_bool(by_section("VICE")),
    "python": to_bool(by_section("Python")),
    "system": to_bool(by_section("System tools")),
    "bridge": to_bool(by_section("Bridge networking")),
    "u64": {
        "checked": u64_checked == "true",
        "reachable": None if u64_reachable == "null" else (u64_reachable == "true"),
        "version": None if u64_version == "null" else u64_version,
    },
    "repo": {
        "root": repo_root or None,
        "branch": repo_branch or None,
        "commit": repo_sha or None,
    },
    "rows": rows,
    "summary": {
        "ok": int(ok), "missing": int(missing), "skipped": int(skipped), "warn": int(warn),
        "overall": overall, "blocker": reason or None,
    },
}
print(json.dumps(out, indent=2))
PY
else
    if [ "$QUIET" = "0" ]; then
        printf 'c64-test-harness dev environment check\n'
        printf '=======================================\n\n'
    fi

    CUR_SECTION=""
    for i in "${!R_STATUS[@]}"; do
        sec="${R_SECTION[$i]}"
        label="${R_LABEL[$i]}"
        status="${R_STATUS[$i]}"
        detail="${R_DETAIL[$i]}"
        if [ "$QUIET" = "1" ] && [ "$status" != "missing" ]; then
            continue
        fi
        if [ "$sec" != "$CUR_SECTION" ]; then
            if [ "$QUIET" = "0" ]; then
                [ -n "$CUR_SECTION" ] && printf '\n'
                printf '[%s]\n' "$sec"
            fi
            CUR_SECTION="$sec"
        fi
        glyph="$(status_glyph "$status")"
        if [ -n "$detail" ]; then
            printf '  %s %s (%s)\n' "$glyph" "$label" "$detail"
        else
            printf '  %s %s\n' "$glyph" "$label"
        fi
    done

    if [ "$QUIET" = "0" ] && [ ${#FIX_HINTS[@]} -gt 0 ]; then
        printf '\n[Fix hints]\n'
        # Deduplicate without associative arrays — macOS ships bash 3.2
        # which doesn't support `declare -A`, and `#!/usr/bin/env bash`
        # resolves to /bin/bash there unless a newer one is on PATH. A
        # newline-delimited seen-list avoids the portability issue and is
        # fine for the handful of hints we emit. (This block is at top-
        # level script scope, so no `local` keyword.)
        seen_hints=""
        for h in "${FIX_HINTS[@]}"; do
            case $'\n'"$seen_hints"$'\n' in
                *$'\n'"$h"$'\n'*) ;;
                *)
                    printf '  -> %s\n' "$h"
                    seen_hints="${seen_hints}${h}"$'\n'
                    ;;
            esac
        done
    fi

    printf '\nSummary: %d ok, %d missing, %d skipped' "$OK" "$MISSING" "$SKIPPED"
    [ "$WARN" -gt 0 ] && printf ', %d warn' "$WARN"
    printf '\n'
    if [ -n "$OVERALL_REASON" ]; then
        printf 'Overall: %s -- %s\n' "$OVERALL" "$OVERALL_REASON"
    else
        printf 'Overall: %s\n' "$OVERALL"
    fi
fi

# ---------- Exit code -----------------------------------------------------

if [ "$CRIT_MISSING" -gt 0 ]; then
    exit 1
fi
exit 0
