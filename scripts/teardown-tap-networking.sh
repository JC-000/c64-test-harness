#!/usr/bin/env bash
# teardown-tap-networking.sh — Remove TAP interface and routing rules.
#
# Reverses setup-tap-networking.sh.  Idempotent — safe to run if already torn down.
#
# Usage:
#   sudo ./scripts/teardown-tap-networking.sh          # auto-detect outbound interface
#   sudo ./scripts/teardown-tap-networking.sh eth0      # explicit outbound interface

set -euo pipefail

TAP_DEV="tap-c64"
C64_SUBNET="10.0.65.0/24"

if [[ $# -ge 1 ]]; then
    WAN_DEV="$1"
else
    WAN_DEV="$(ip route show default | awk '/default/ {print $5; exit}')"
fi

# --- iptables rules (remove silently if they exist) ---------------------------

if [[ -n "$WAN_DEV" ]]; then
    iptables -t nat -D POSTROUTING -s "$C64_SUBNET" -o "$WAN_DEV" -j MASQUERADE 2>/dev/null && \
        echo "[removed] NAT masquerade rule" || echo "[ok] NAT masquerade rule already absent"

    iptables -D FORWARD -i "$TAP_DEV" -o "$WAN_DEV" -j ACCEPT 2>/dev/null && \
        echo "[removed] FORWARD rule (tap -> wan)" || echo "[ok] FORWARD rule (tap -> wan) already absent"

    iptables -D FORWARD -i "$WAN_DEV" -o "$TAP_DEV" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null && \
        echo "[removed] FORWARD rule (wan -> tap)" || echo "[ok] FORWARD rule (wan -> tap) already absent"
else
    echo "[skip] Could not determine outbound interface — iptables rules not cleaned"
fi

# --- TAP interface ------------------------------------------------------------

if ip link show "$TAP_DEV" &>/dev/null; then
    ip link set "$TAP_DEV" down 2>/dev/null || true
    ip tuntap del dev "$TAP_DEV" mode tap
    echo "[removed] $TAP_DEV"
else
    echo "[ok] $TAP_DEV already absent"
fi

echo
echo "Done. TAP networking torn down."
