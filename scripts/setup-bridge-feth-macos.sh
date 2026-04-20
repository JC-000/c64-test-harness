#!/usr/bin/env bash
# Bridge networking setup (macOS) — creates bridge0 + feth0/feth1 for
# two-VICE ethernet tests. Paired with teardown-bridge-feth-macos.sh.
#
# This is the macOS-native counterpart to setup-bridge-tap.sh. Instead of
# Linux TAP devices + a Linux bridge, it uses macOS "feth" peer interfaces
# (fake ethernet pairs) and the BSD "bridge" pseudo-device driven by
# ifconfig. VICE attaches via its pcap driver rather than tuntap because
# macOS has no /dev/net/tun and libpcap-over-BPF is the portable path.
#
# Must be run with sudo (or as root). Idempotent — safe to run repeatedly.
#
# Usage:
#   sudo ./scripts/setup-bridge-feth-macos.sh

set -euo pipefail

# NOTE: we intentionally do NOT use bridge0. On macOS, bridge0 is a
# pre-existing system bridge (Thunderbolt/Internet-Sharing-style) that
# often already has en1/en2 as members. Adding feth peers to it or
# assigning our IP would pollute the system bridge. bridge10 is our
# project-owned name — the BSD bridge driver accepts any bridgeN.
BRIDGE="bridge10"
BRIDGE_ADDR="10.0.65.1"
BRIDGE_MASK="255.255.255.0"
FETH0="feth0"
FETH1="feth1"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "[fail] this script is macOS-only (uname: $(uname))"
    echo "       for Linux use scripts/setup-bridge-tap.sh"
    exit 2
fi

echo "Bridge:      $BRIDGE"
echo "Bridge addr: $BRIDGE_ADDR/$BRIDGE_MASK"
echo "feth pair:   $FETH0 <-> $FETH1"
echo

iface_exists() {
    ifconfig "$1" >/dev/null 2>&1
}

# --- feth pair --------------------------------------------------------------
# feth is a pair of linked pseudo-ethernet interfaces. Creating feth0 and
# feth1 independently then pairing them with "peer" links their rx/tx.

for FETH in "$FETH0" "$FETH1"; do
    if iface_exists "$FETH"; then
        echo "[ok] $FETH already exists"
    else
        ifconfig "$FETH" create
        echo "[created] $FETH"
    fi
done

# Pair the two feth interfaces. "ifconfig feth0 peer feth1" is idempotent:
# if they're already peered, it's a no-op.
if ifconfig "$FETH0" 2>/dev/null | grep -q "peer: $FETH1"; then
    echo "[ok] $FETH0 already peered with $FETH1"
else
    ifconfig "$FETH0" peer "$FETH1"
    echo "[peered] $FETH0 <-> $FETH1"
fi

# Bring both up. feth peers need to be "up" before they'll forward frames.
for FETH in "$FETH0" "$FETH1"; do
    if ifconfig "$FETH" | grep -q 'flags=.*<.*UP'; then
        echo "[ok] $FETH is UP"
    else
        ifconfig "$FETH" up
        echo "[up] $FETH"
    fi
done

# --- bridge -----------------------------------------------------------------

if iface_exists "$BRIDGE"; then
    echo "[ok] $BRIDGE already exists"
else
    ifconfig "$BRIDGE" create
    echo "[created] $BRIDGE"
fi

# Add both feth peers as bridge members. The bridge "addm" subcommand is
# NOT idempotent on BSD — it errors if the member is already attached —
# so we check via "ifconfig bridge0" grep first.
BRIDGE_INFO="$(ifconfig "$BRIDGE" 2>/dev/null || true)"
for FETH in "$FETH0" "$FETH1"; do
    if echo "$BRIDGE_INFO" | grep -q "member: $FETH"; then
        echo "[ok] $FETH already a member of $BRIDGE"
    else
        ifconfig "$BRIDGE" addm "$FETH"
        echo "[bridge] $FETH added to $BRIDGE"
    fi
done

# Assign the host-side IP. "ifconfig bridge0 inet 10.0.65.1 netmask ..." is
# the BSD form (there's no "ip addr add" on macOS).
if ifconfig "$BRIDGE" | grep -qE "inet ${BRIDGE_ADDR}\b"; then
    echo "[ok] $BRIDGE has address $BRIDGE_ADDR"
else
    ifconfig "$BRIDGE" inet "$BRIDGE_ADDR" netmask "$BRIDGE_MASK"
    echo "[addr] $BRIDGE_ADDR assigned to $BRIDGE"
fi

# Bring the bridge up.
if ifconfig "$BRIDGE" | grep -q 'flags=.*<.*UP'; then
    echo "[ok] $BRIDGE is UP"
else
    ifconfig "$BRIDGE" up
    echo "[up] $BRIDGE"
fi

# --- Summary ----------------------------------------------------------------

echo
echo "Done. Bridge $BRIDGE is ready with two feth interfaces."
echo
echo "  VICE instance 0:  -ethernetiodriver pcap -ethernetioif $FETH0"
echo "  VICE instance 1:  -ethernetiodriver pcap -ethernetioif $FETH1"
echo
echo "Both instances share the same L2 segment via $BRIDGE."
echo "Host can participate at $BRIDGE_ADDR."
echo
echo "NOTE: VICE's pcap driver opens /dev/bpf* which is root-only by default"
echo "      on macOS. If you see a 'pcap_open_live' or BPF permission error,"
echo "      install Wireshark's ChmodBPF helper (recommended) or temporarily"
echo "      run 'sudo chmod 666 /dev/bpf*' (resets on reboot)."
echo
echo "To tear down: sudo ./scripts/teardown-bridge-feth-macos.sh"
