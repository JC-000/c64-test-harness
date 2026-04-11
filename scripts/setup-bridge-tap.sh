#!/usr/bin/env bash
# Bridge networking setup — creates br-c64 + tap-c64-{0,1} for two-VICE
# ethernet tests.  Paired with teardown-bridge-tap.sh (symmetric) and the
# cleanup-bridge-networking.sh emergency recovery script.
#
# See docs/bridge_networking.md "Reference pattern for VICE agents" for
# the canonical lifecycle and feedback_no_pkill.md for rationale.
# Happy-path VICE lifecycle is owned by the Python harness — this script
# NEVER touches VICE processes and NEVER uses pkill by name.
#
# setup-bridge-tap.sh — Create a bridge with two TAP interfaces for
# inter-VICE ethernet testing.
#
# Creates a Linux bridge (br-c64) with two TAP devices (tap-c64-0,
# tap-c64-1) so two VICE instances can communicate over emulated ethernet.
# An IP address is assigned to the bridge for optional host participation.
#
# Must be run with sudo (or as root).  Idempotent — safe to run repeatedly.
#
# Usage:
#   sudo ./scripts/setup-bridge-tap.sh

set -euo pipefail

BRIDGE="br-c64"
BRIDGE_ADDR="10.0.65.1/24"
TAP0="tap-c64-0"
TAP1="tap-c64-1"
# Who should own the TAP devices — the user who invoked sudo, or fallback to current
TAP_USER="${SUDO_USER:-$USER}"

echo "Bridge:      $BRIDGE"
echo "Bridge addr: $BRIDGE_ADDR"
echo "TAP devices: $TAP0, $TAP1 (owner: $TAP_USER)"
echo

# --- Bridge ------------------------------------------------------------------

if ip link show "$BRIDGE" &>/dev/null; then
    echo "[ok] $BRIDGE already exists"
else
    ip link add name "$BRIDGE" type bridge
    echo "[created] $BRIDGE"
fi

# Disable STP for faster link-up
if [[ -f "/sys/devices/virtual/net/$BRIDGE/bridge/stp_state" ]]; then
    CURRENT_STP="$(cat "/sys/devices/virtual/net/$BRIDGE/bridge/stp_state")"
    if [[ "$CURRENT_STP" == "0" ]]; then
        echo "[ok] STP already disabled on $BRIDGE"
    else
        ip link set "$BRIDGE" type bridge stp_state 0
        echo "[disabled] STP on $BRIDGE"
    fi
fi

if ip addr show "$BRIDGE" | grep -q "${BRIDGE_ADDR%/*}"; then
    echo "[ok] $BRIDGE has address $BRIDGE_ADDR"
else
    ip addr add "$BRIDGE_ADDR" dev "$BRIDGE"
    echo "[addr] $BRIDGE_ADDR assigned to $BRIDGE"
fi

if ip link show "$BRIDGE" | grep -q 'state UP'; then
    echo "[ok] $BRIDGE is UP"
else
    ip link set "$BRIDGE" up
    echo "[up] $BRIDGE"
fi

# --- TAP interfaces ----------------------------------------------------------

for TAP_DEV in "$TAP0" "$TAP1"; do
    if ip link show "$TAP_DEV" &>/dev/null; then
        echo "[ok] $TAP_DEV already exists"
    else
        ip tuntap add dev "$TAP_DEV" mode tap user "$TAP_USER"
        echo "[created] $TAP_DEV (owner: $TAP_USER)"
    fi

    # Add to bridge (ignore error if already a member)
    if ip link show "$TAP_DEV" 2>/dev/null | grep -q "master $BRIDGE"; then
        echo "[ok] $TAP_DEV already in $BRIDGE"
    else
        ip link set "$TAP_DEV" master "$BRIDGE"
        echo "[bridge] $TAP_DEV added to $BRIDGE"
    fi

    if ip link show "$TAP_DEV" | grep -q 'state UP'; then
        echo "[ok] $TAP_DEV is UP"
    else
        ip link set "$TAP_DEV" up
        echo "[up] $TAP_DEV"
    fi
done

# --- iptables FORWARD rules --------------------------------------------------
# Allow traffic in/out of bridge and TAP interfaces so frames are not dropped
# when the FORWARD chain policy is DROP (or filtered by other rules).

for DEV in "$BRIDGE" "$TAP0" "$TAP1"; do
    if iptables -C FORWARD -i "$DEV" -j ACCEPT 2>/dev/null; then
        echo "[ok] FORWARD rule ($DEV inbound) exists"
    else
        iptables -A FORWARD -i "$DEV" -j ACCEPT
        echo "[added] FORWARD: $DEV inbound"
    fi
    if iptables -C FORWARD -o "$DEV" -j ACCEPT 2>/dev/null; then
        echo "[ok] FORWARD rule ($DEV outbound) exists"
    else
        iptables -A FORWARD -o "$DEV" -j ACCEPT
        echo "[added] FORWARD: $DEV outbound"
    fi
done

# --- Summary -----------------------------------------------------------------

echo
echo "Done. Bridge $BRIDGE is ready with two TAP interfaces."
echo
echo "  VICE instance 0:  -ethernetioif $TAP0"
echo "  VICE instance 1:  -ethernetioif $TAP1"
echo
echo "Both instances share the same L2 segment via $BRIDGE."
echo "Host can participate at ${BRIDGE_ADDR%/*}."
echo "To tear down: sudo ./scripts/teardown-bridge-tap.sh"
