#!/usr/bin/env bash
# Bridge networking emergency cleanup — scoped to the harness port ranges.
# Uses scripts/cleanup_vice_ports.py (Python helper) to target only VICE
# processes bound to known test-harness ports, falling back to an in-script
# bash port-resolve if Python is unavailable. NEVER uses pkill by name.
#
# See docs/bridge_networking.md "Reference pattern for VICE agents" for the
# canonical lifecycle and feedback_no_pkill.md for rationale.
#
# cleanup-bridge-networking.sh — Idempotent emergency teardown for the
# VICE ethernet bridge test environment.
#
# Kills leftover VICE processes (scoped to harness ports), tears down the
# br-c64 bridge and its tap-c64-0/tap-c64-1 interfaces, removes the iptables
# FORWARD rules added by setup-bridge-tap.sh, and cleans up stale
# /tmp/vice_eth_*.rc files.
#
# Unlike the normal test lifecycle (ViceProcess context manager), this
# script is a last-resort cleanup when a test has crashed mid-run and left
# VICE/TAP state behind.  Safe to run at any time.
#
# Usage:
#   sudo ./scripts/cleanup-bridge-networking.sh

set -u  # don't set -e: we want to keep going through all cleanup steps

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BRIDGE="br-c64"
TAP0="tap-c64-0"
TAP1="tap-c64-1"
HARNESS_PORT_RANGES="6511:6531,6560:6580"

echo "=== c64-test-harness bridge networking cleanup ==="
echo

# --- 1. Kill harness-bound x64sc processes (scoped) ----------------------
echo "[1/5] killing any lingering harness-bound x64sc processes (scoped)..."
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/cleanup_vice_ports.py" ]; then
    python3 "$SCRIPT_DIR/cleanup_vice_ports.py" --range "$HARNESS_PORT_RANGES" || true
else
    # Bash fallback: find listeners in the harness ports, verify comm, kill by PID.
    echo "  (python3 / helper unavailable, using bash fallback)"
    scope_ports() {
        for p in $(seq 6511 6531) $(seq 6560 6580); do
            echo "$p"
        done
    }
    resolve_pid() {
        local port="$1"
        ss -Htlnp "sport = :$port" 2>/dev/null \
            | awk '{print $NF}' \
            | grep -oP 'pid=\K[0-9]+' \
            | head -n1
    }
    for port in $(scope_ports); do
        pid="$(resolve_pid "$port" || true)"
        if [ -n "${pid:-}" ] && [ -r "/proc/$pid/comm" ]; then
            comm="$(cat "/proc/$pid/comm")"
            if [ "$comm" = "x64sc" ]; then
                echo "[cleanup] port $port pid $pid ($comm) -> SIGTERM"
                kill -TERM "$pid" 2>/dev/null || true
            fi
        fi
    done
    sleep 2
    for port in $(scope_ports); do
        pid="$(resolve_pid "$port" || true)"
        if [ -n "${pid:-}" ] && [ -r "/proc/$pid/comm" ]; then
            comm="$(cat "/proc/$pid/comm")"
            if [ "$comm" = "x64sc" ]; then
                echo "[cleanup] port $port pid $pid ($comm) -> SIGKILL (still alive)"
                kill -KILL "$pid" 2>/dev/null || true
            fi
        fi
    done
fi
echo

# --- 2. Kill any dnsmasq bound to the test TAPs (if any) ------------------
echo "[2/5] Killing any dnsmasq processes on $TAP0/$TAP1..."
found_dns=0
if command -v pgrep > /dev/null; then
    while read -r pid; do
        if [[ -n "$pid" ]]; then
            cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
            if echo "$cmdline" | grep -qE "(tap-c64-|br-c64)"; then
                echo "  killing dnsmasq PID $pid: $cmdline"
                kill -TERM "$pid" 2>/dev/null || true
                found_dns=1
            fi
        fi
    done < <(pgrep -x dnsmasq 2>/dev/null)
fi
if [[ "$found_dns" == "0" ]]; then
    echo "  no dnsmasq processes on test TAPs"
fi
echo

# --- 3. Remove iptables FORWARD rules -------------------------------------
echo "[3/5] Removing iptables FORWARD rules..."
removed=0
for DEV in "$BRIDGE" "$TAP0" "$TAP1"; do
    if iptables -D FORWARD -i "$DEV" -j ACCEPT 2>/dev/null; then
        echo "  [removed] FORWARD -i $DEV"
        removed=$((removed + 1))
    fi
    if iptables -D FORWARD -o "$DEV" -j ACCEPT 2>/dev/null; then
        echo "  [removed] FORWARD -o $DEV"
        removed=$((removed + 1))
    fi
done
if [[ "$removed" == "0" ]]; then
    echo "  no FORWARD rules to remove"
fi
echo

# --- 4. Tear down TAP interfaces and bridge -------------------------------
echo "[4/5] Tearing down TAP interfaces and bridge..."
for TAP_DEV in "$TAP0" "$TAP1"; do
    if ip link show "$TAP_DEV" > /dev/null 2>&1; then
        ip link set "$TAP_DEV" down 2>/dev/null || true
        ip tuntap del dev "$TAP_DEV" mode tap 2>/dev/null
        if ip link show "$TAP_DEV" > /dev/null 2>&1; then
            echo "  WARNING: $TAP_DEV still exists"
        else
            echo "  [removed] $TAP_DEV"
        fi
    else
        echo "  [ok] $TAP_DEV already absent"
    fi
done

if ip link show "$BRIDGE" > /dev/null 2>&1; then
    ip link set "$BRIDGE" down 2>/dev/null || true
    ip link del "$BRIDGE" type bridge 2>/dev/null
    if ip link show "$BRIDGE" > /dev/null 2>&1; then
        echo "  WARNING: $BRIDGE still exists"
    else
        echo "  [removed] $BRIDGE"
    fi
else
    echo "  [ok] $BRIDGE already absent"
fi
echo

# --- 5. Remove stale temp vicerc files ------------------------------------
echo "[5/5] Removing stale /tmp/vice_eth_*.rc files..."
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
