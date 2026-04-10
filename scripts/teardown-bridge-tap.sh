#!/usr/bin/env bash
# teardown-bridge-tap.sh — Remove bridge and TAP interfaces for inter-VICE ethernet testing.
#
# Reverses setup-bridge-tap.sh.  Idempotent — safe to run if already torn down.
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

echo
echo "Done. Bridge and TAP interfaces torn down."
