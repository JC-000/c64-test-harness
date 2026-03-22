#!/usr/bin/env bash
# setup-tap-networking.sh — Configure TAP interface and routing for C64 ethernet testing.
#
# Creates a TAP device (tap-c64) owned by the calling user, assigns it an IP,
# enables IPv4 forwarding, and sets up NAT so the emulated C64 can reach the
# internet through the host's default route.
#
# Must be run with sudo (or as root).  Idempotent — safe to run repeatedly.
#
# Usage:
#   sudo ./scripts/setup-tap-networking.sh          # auto-detect outbound interface
#   sudo ./scripts/setup-tap-networking.sh eth0      # explicit outbound interface

set -euo pipefail

TAP_DEV="tap-c64"
TAP_ADDR="10.0.65.1/24"
C64_SUBNET="10.0.65.0/24"
# Who should own the TAP device — the user who invoked sudo, or fallback to current
TAP_USER="${SUDO_USER:-$USER}"

# Outbound interface: explicit arg, or auto-detect from default route
if [[ $# -ge 1 ]]; then
    WAN_DEV="$1"
else
    WAN_DEV="$(ip route show default | awk '/default/ {print $5; exit}')"
fi

if [[ -z "$WAN_DEV" ]]; then
    echo "ERROR: Could not determine outbound interface. Pass it as an argument." >&2
    exit 1
fi

echo "TAP device:  $TAP_DEV (owner: $TAP_USER)"
echo "TAP address: $TAP_ADDR"
echo "Outbound:    $WAN_DEV"
echo "C64 subnet:  $C64_SUBNET"
echo

# --- TAP interface -----------------------------------------------------------

if ip link show "$TAP_DEV" &>/dev/null; then
    echo "[ok] $TAP_DEV already exists"
else
    ip tuntap add dev "$TAP_DEV" mode tap user "$TAP_USER"
    echo "[created] $TAP_DEV (owner: $TAP_USER)"
fi

if ip link show "$TAP_DEV" | grep -q 'state UP'; then
    echo "[ok] $TAP_DEV is UP"
else
    ip link set "$TAP_DEV" up
    echo "[up] $TAP_DEV"
fi

if ip addr show "$TAP_DEV" | grep -q "${TAP_ADDR%/*}"; then
    echo "[ok] $TAP_DEV has address $TAP_ADDR"
else
    ip addr add "$TAP_ADDR" dev "$TAP_DEV"
    echo "[addr] $TAP_ADDR assigned to $TAP_DEV"
fi

# --- IP forwarding ------------------------------------------------------------

CURRENT_FWD="$(cat /proc/sys/net/ipv4/ip_forward)"
if [[ "$CURRENT_FWD" == "1" ]]; then
    echo "[ok] IPv4 forwarding is enabled"
else
    sysctl -w net.ipv4.ip_forward=1 >/dev/null
    echo "[enabled] IPv4 forwarding"
fi

# --- iptables NAT & forwarding -----------------------------------------------

if iptables -t nat -C POSTROUTING -s "$C64_SUBNET" -o "$WAN_DEV" -j MASQUERADE 2>/dev/null; then
    echo "[ok] NAT masquerade rule exists"
else
    iptables -t nat -A POSTROUTING -s "$C64_SUBNET" -o "$WAN_DEV" -j MASQUERADE
    echo "[added] NAT masquerade: $C64_SUBNET -> $WAN_DEV"
fi

if iptables -C FORWARD -i "$TAP_DEV" -o "$WAN_DEV" -j ACCEPT 2>/dev/null; then
    echo "[ok] FORWARD rule (tap -> wan) exists"
else
    iptables -A FORWARD -i "$TAP_DEV" -o "$WAN_DEV" -j ACCEPT
    echo "[added] FORWARD: $TAP_DEV -> $WAN_DEV"
fi

if iptables -C FORWARD -i "$WAN_DEV" -o "$TAP_DEV" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; then
    echo "[ok] FORWARD rule (wan -> tap, established) exists"
else
    iptables -A FORWARD -i "$WAN_DEV" -o "$TAP_DEV" -m state --state RELATED,ESTABLISHED -j ACCEPT
    echo "[added] FORWARD: $WAN_DEV -> $TAP_DEV (RELATED,ESTABLISHED)"
fi

echo
echo "Done. The C64 can use IP 10.0.65.2 with gateway 10.0.65.1."
echo "To tear down: sudo ./scripts/teardown-tap-networking.sh"
