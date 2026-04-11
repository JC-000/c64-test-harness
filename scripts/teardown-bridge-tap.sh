#!/usr/bin/env bash
# Bridge networking teardown — reverses setup-bridge-tap.sh.
#
# Symmetric with setup: touches only br-c64, tap-c64-{0,1}, the six
# FORWARD iptables rules, and stale /tmp/vice_eth_*.rc temp files.
# NEVER uses pkill by name; the happy-path VICE lifecycle is owned by
# the Python harness (ViceProcess context manager).
#
# See docs/bridge_networking.md "Reference pattern for VICE agents" for
# the canonical lifecycle and feedback_no_pkill.md for rationale.
#
# teardown-bridge-tap.sh — Remove bridge and TAP interfaces for inter-VICE
# ethernet testing.  Idempotent — safe to run if already torn down.
#
# Usage:
#   sudo ./scripts/teardown-bridge-tap.sh

set -euo pipefail

BRIDGE="br-c64"
TAP0="tap-c64-0"
TAP1="tap-c64-1"

# --- iptables FORWARD rules --------------------------------------------------

for DEV in "$BRIDGE" "$TAP0" "$TAP1"; do
    iptables -D FORWARD -i "$DEV" -j ACCEPT 2>/dev/null && \
        echo "[removed] FORWARD rule ($DEV inbound)" || echo "[ok] FORWARD rule ($DEV inbound) already absent"
    iptables -D FORWARD -o "$DEV" -j ACCEPT 2>/dev/null && \
        echo "[removed] FORWARD rule ($DEV outbound)" || echo "[ok] FORWARD rule ($DEV outbound) already absent"
done

# --- TAP interfaces ----------------------------------------------------------

for TAP_DEV in "$TAP0" "$TAP1"; do
    if ip link show "$TAP_DEV" &>/dev/null; then
        ip link set "$TAP_DEV" down 2>/dev/null || true
        ip tuntap del dev "$TAP_DEV" mode tap
        echo "[removed] $TAP_DEV"
    else
        echo "[ok] $TAP_DEV already absent"
    fi
done

# --- Bridge ------------------------------------------------------------------

if ip link show "$BRIDGE" &>/dev/null; then
    ip link set "$BRIDGE" down 2>/dev/null || true
    ip link del "$BRIDGE" type bridge
    echo "[removed] $BRIDGE"
else
    echo "[ok] $BRIDGE already absent"
fi

# --- Stale vicerc temp files -------------------------------------------------
# Remove stale VICE ethernet vicerc temp files. These are created by
# ViceProcess when ethernet is enabled; a clean harness exit removes
# them already, but teardown should be defensive.
rm -f /tmp/vice_eth_*.rc 2>/dev/null || true

echo
echo "Done. Bridge and TAP interfaces torn down."
