#!/usr/bin/env bash
# Bridge networking emergency cleanup (macOS) — scoped to the harness
# port ranges. Uses scripts/cleanup_vice_ports.py (cross-platform, macOS
# path uses lsof + ps) to target only VICE processes bound to known
# test-harness ports. NEVER uses pkill by name.
#
# macOS counterpart to scripts/cleanup-bridge-networking.sh (Linux).
# Unlike setup/teardown (happy-path, owned by the harness), this is a
# last-resort cleanup for when a test has crashed mid-run and left
# VICE / feth / bridge10 state behind. Safe to run at any time.
#
# What this does NOT do:
#   - No pf rules to remove (macOS setup does not touch pf).
#   - No dnsmasq to kill (macOS setup does not spawn it).
#   - Does not touch bridge0 (the system bridge). Our bridge is bridge10.
#
# See docs/bridge_networking.md "Reference pattern for VICE agents" for
# the canonical lifecycle and feedback_no_pkill.md for rationale.
#
# Usage:
#   sudo ./scripts/cleanup-bridge-feth-macos.sh

set -u  # don't set -e: we want to keep going through all cleanup steps

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BRIDGE="bridge10"
FETH0="feth0"
FETH1="feth1"
HARNESS_PORT_RANGES="6511:6531,6560:6580"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "[fail] this script is macOS-only (uname: $(uname))"
    echo "       for Linux use scripts/cleanup-bridge-networking.sh"
    exit 2
fi

echo "=== c64-test-harness bridge networking cleanup (macOS) ==="
echo

# --- 1. Kill harness-bound x64sc processes (scoped) ----------------------
echo "[1/3] killing any lingering harness-bound x64sc processes (scoped)..."
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/cleanup_vice_ports.py" ]; then
    python3 "$SCRIPT_DIR/cleanup_vice_ports.py" --range "$HARNESS_PORT_RANGES" || true
else
    # Bash fallback for macOS: resolve listener PIDs with lsof, verify
    # ucomm with ps, then SIGTERM -> SIGKILL after 2s grace. Same scoped
    # semantics as the Python helper.
    echo "  (python3 / helper unavailable, using bash fallback)"
    scope_ports() {
        for p in $(seq 6511 6531) $(seq 6560 6580); do
            echo "$p"
        done
    }
    resolve_pid() {
        local port="$1"
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1
    }
    ucomm_of() {
        local pid="$1"
        ps -p "$pid" -o ucomm= 2>/dev/null | tr -d '[:space:]'
    }
    pids_to_kill=()
    for port in $(scope_ports); do
        pid="$(resolve_pid "$port" || true)"
        if [ -n "${pid:-}" ]; then
            comm="$(ucomm_of "$pid")"
            if [ "$comm" = "x64sc" ]; then
                echo "[cleanup] port $port pid $pid ($comm) -> SIGTERM"
                kill -TERM "$pid" 2>/dev/null || true
                pids_to_kill+=("$pid")
            fi
        fi
    done
    if [ ${#pids_to_kill[@]} -gt 0 ]; then
        sleep 2
        for pid in "${pids_to_kill[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                echo "[cleanup] pid $pid -> SIGKILL (still alive)"
                kill -KILL "$pid" 2>/dev/null || true
            fi
        done
    fi
fi
echo

# --- 2. Tear down bridge + feth pair ---------------------------------------
# Order matters: detach members before destroying the bridge so destroy
# has no dangling refs. "deletem" is silent if the member is already
# detached, but "destroy" on a bridge with members can warn.
echo "[2/3] Tearing down $BRIDGE and feth interfaces..."

iface_exists() {
    ifconfig "$1" >/dev/null 2>&1
}

if iface_exists "$BRIDGE"; then
    BRIDGE_INFO="$(ifconfig "$BRIDGE" 2>/dev/null || true)"
    for FETH in "$FETH0" "$FETH1"; do
        if echo "$BRIDGE_INFO" | grep -q "member: $FETH"; then
            ifconfig "$BRIDGE" deletem "$FETH" 2>/dev/null || true
            echo "  [unbridged] $FETH"
        fi
    done
    ifconfig "$BRIDGE" down 2>/dev/null || true
    if ifconfig "$BRIDGE" destroy 2>/dev/null; then
        echo "  [removed] $BRIDGE"
    else
        echo "  WARNING: $BRIDGE destroy failed (still exists?)"
    fi
else
    echo "  [ok] $BRIDGE already absent"
fi

for FETH in "$FETH0" "$FETH1"; do
    if iface_exists "$FETH"; then
        ifconfig "$FETH" down 2>/dev/null || true
        if ifconfig "$FETH" destroy 2>/dev/null; then
            echo "  [removed] $FETH"
        else
            echo "  WARNING: $FETH destroy failed (still exists?)"
        fi
    else
        echo "  [ok] $FETH already absent"
    fi
done
echo

# --- 3. Remove stale temp vicerc files ------------------------------------
echo "[3/3] Removing stale /tmp/vice_eth_*.rc files..."
shopt -s nullglob
rc_files=(/tmp/vice_eth_*.rc)
if [[ ${#rc_files[@]} -gt 0 ]]; then
    for f in "${rc_files[@]}"; do
        rm -f "$f" && echo "  [removed] $f"
    done
else
    echo "  no stale vicerc files"
fi
shopt -u nullglob
echo

echo "=== Cleanup complete ==="
