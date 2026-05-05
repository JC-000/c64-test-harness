#!/usr/bin/env bash
# Bridge networking teardown (macOS) — reverses setup-bridge-feth-macos.sh.
#
# Symmetric with setup: touches only bridge0, feth0, feth1. Idempotent.
# Does NOT kill VICE processes — happy-path VICE lifecycle is owned by the
# Python harness (ViceProcess context manager).
#
# Usage:
#   sudo ./scripts/teardown-bridge-feth-macos.sh

set -euo pipefail

BRIDGE="bridge10"  # see setup-bridge-feth-macos.sh — avoids system bridge0
FETH0="feth0"
FETH1="feth1"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "[fail] this script is macOS-only (uname: $(uname))"
    exit 2
fi

iface_exists() {
    ifconfig "$1" >/dev/null 2>&1
}

# --- bridge -----------------------------------------------------------------

if iface_exists "$BRIDGE"; then
    # Remove feth members first so destroy has no attached refs. BSD "deletem"
    # silently no-ops if the member isn't attached.
    BRIDGE_INFO="$(ifconfig "$BRIDGE" 2>/dev/null || true)"
    for FETH in "$FETH0" "$FETH1"; do
        if echo "$BRIDGE_INFO" | grep -q "member: $FETH"; then
            ifconfig "$BRIDGE" deletem "$FETH" 2>/dev/null || true
            echo "[unbridged] $FETH"
        fi
    done
    ifconfig "$BRIDGE" down 2>/dev/null || true
    ifconfig "$BRIDGE" destroy
    echo "[removed] $BRIDGE"
else
    echo "[ok] $BRIDGE already absent"
fi

# --- feth pair --------------------------------------------------------------

for FETH in "$FETH0" "$FETH1"; do
    if iface_exists "$FETH"; then
        ifconfig "$FETH" down 2>/dev/null || true
        ifconfig "$FETH" destroy
        echo "[removed] $FETH"
    else
        echo "[ok] $FETH already absent"
    fi
done

# --- Stale vicerc temp files ------------------------------------------------
rm -f /tmp/vice_eth_*.rc 2>/dev/null || true

echo
echo "Done. Bridge and feth interfaces torn down."
