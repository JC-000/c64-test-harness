#!/usr/bin/env bash
# probe-vice-feth.sh — one-shot smoke test that VICE's pcap driver can
# launch against a macOS feth interface and serve the binary monitor.
#
# Prerequisites (run first):
#   sudo ./scripts/setup-bridge-feth-macos.sh
#
# Usage:
#   sudo ./scripts/probe-vice-feth.sh          # runs VICE as root for BPF
#   ./scripts/probe-vice-feth.sh --no-sudo     # assumes ChmodBPF or 666 bpf
#
#   --vice-bin PATH        use PATH instead of the x64sc on $PATH (also
#                          settable via the VICE_BIN environment variable).
#                          Rarely needed: the x64sc on $PATH is normally the
#                          right one.  Homebrew's bottle reports HAVE_RAWNET
#                          yes / HAVE_PCAP yes to 'x64sc -features' and links
#                          libpcap -- issue #144's "ethernet compiled out"
#                          reading was a misdiagnosis of the crash an
#                          UNELEVATED launch produces (VICE selects a pcap
#                          driver only at euid 0, so unelevated it leaves
#                          rawnet_arch_driver NULL and SIGSEGVs).
#   --skip-ethernet-check  bypass the ethernet-capability pre-flight
#
# Expected outcome: VICE launches, binary monitor accepts a TCP connection
# on 127.0.0.1:6510, a minimal MON_CMD_PING request gets a valid response,
# VICE is killed cleanly, and /tmp/vice-probe-feth.out contains no pcap
# errors. Exit 0 on success, 1 on any failure, with a detailed diagnosis.
#
# This script does NOT tear down the bridge — that's teardown-bridge-feth-macos.sh.

set -u -o pipefail

FETH="feth0"
MONITOR_PORT=6510
MONITOR_HOST="127.0.0.1"
VICE_OUT="/tmp/vice-probe-feth.out"
VICE_PID=""
PROBE_TIMEOUT=8
USE_SUDO=1
SKIP_ETHERNET_CHECK=0
VICE_BIN="${VICE_BIN:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-sudo) USE_SUDO=0 ;;
        --skip-ethernet-check) SKIP_ETHERNET_CHECK=1 ;;
        --vice-bin)
            if [[ $# -lt 2 ]]; then
                echo "--vice-bin requires a path argument" >&2
                exit 2
            fi
            VICE_BIN="$2"
            shift ;;
        -h|--help)
            sed -n '2,22p' "$0"
            exit 0 ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2 ;;
    esac
    shift
done

if [[ "$(uname)" != "Darwin" ]]; then
    echo "[fail] macOS-only script"
    exit 2
fi

# --- preflight --------------------------------------------------------------

if ! ifconfig "$FETH" >/dev/null 2>&1; then
    echo "[fail] $FETH does not exist — run sudo ./scripts/setup-bridge-feth-macos.sh first"
    exit 1
fi

if [[ -n "$VICE_BIN" ]]; then
    if [[ ! -x "$VICE_BIN" ]]; then
        echo "[fail] --vice-bin/\$VICE_BIN '$VICE_BIN' is not an executable file"
        exit 1
    fi
elif command -v x64sc >/dev/null 2>&1; then
    VICE_BIN="$(command -v x64sc)"
else
    echo "[fail] x64sc not on PATH — is VICE installed? (brew install vice)"
    exit 1
fi

echo "[probe] VICE binary: $VICE_BIN"

# --- ethernet-capability pre-flight (issue #144) -----------------------------
# The -ethernetcart / -ethernetiodriver command-line options are compiled in
# unconditionally, but the *rawnet driver registry* is only populated when
# VICE was built with ethernet support. Homebrew's arm64 x64sc links libpcap
# yet registers no driver, so activating the cart dereferences NULL and
# segfaults in rawnet_arch_pre_reset+8 — an opaque crash that looks like a BPF
# permissions problem but is not.
#
# Detect it before the cart is ever activated: ask VICE to parse
# `-ethernetiodriver pcap` followed by a deliberately invalid option. VICE
# parses left to right and bails at the first error, so the message tells us
# which build we have, and it exits immediately either way -- no window, no
# cart activation, no crash:
#
#   ethernet compiled out -> "Argument 'pcap' not valid for option ..."
#   ethernet capable      -> "Unknown option '-zz-not-a-real-option'."
#
# We only fail on the *specific* pcap rejection, so any unforeseen third
# outcome falls through to the real probe rather than blocking it.
if [[ "$SKIP_ETHERNET_CHECK" == "1" ]]; then
    echo "[warn] ethernet-capability pre-flight skipped (--skip-ethernet-check)"
else
    ETH_CHECK_OUT="$("$VICE_BIN" -ethernetiodriver pcap -zz-not-a-real-option 2>&1)" || true
    if grep -qF "not valid for option \`-ethernetiodriver'" <<<"$ETH_CHECK_OUT"; then
        echo "[fail] this VICE build has ethernet support compiled out"
        echo "       $VICE_BIN rejects the 'pcap' rawnet driver:"
        echo "         $(grep -F "not valid for option" <<<"$ETH_CHECK_OUT" | head -1)"
        echo
        echo "       The rawnet driver registry is empty, so enabling the cart"
        echo "       segfaults in rawnet_arch_pre_reset."
        echo "       This is NOT a /dev/bpf permissions problem — VICE never reads"
        echo "       those nodes."
        echo
        echo "       Most likely cause: this probe is running unelevated. VICE"
        echo "       registers the pcap driver only when archdep_rawnet_capability()"
        echo "       holds -- geteuid() == 0 on macOS. Re-run as:"
        echo "         sudo $0"
        echo
        echo "       If it persists as root, the build genuinely lacks rawnet."
        echo "       Check with:"
        echo "         $VICE_BIN -features | grep -E 'HAVE_RAWNET|HAVE_PCAP'"
        echo "       and if so, name a build that has it:"
        echo "         $0 --vice-bin /path/to/ethernet-x64sc"
        exit 1
    fi
    echo "[ok]   ethernet-capability pre-flight passed (pcap driver registered)"
fi
echo "[probe] interface:   $FETH"
echo "[probe] monitor:     $MONITOR_HOST:$MONITOR_PORT"
echo "[probe] log:         $VICE_OUT"
echo "[probe] use sudo:    $USE_SUDO"
echo

# --- launch VICE ------------------------------------------------------------
# We launch without a ROM image — VICE boots its internal BASIC. With
# -binarymonitor the monitor TCP server starts during init so we can
# probe it even before the KERNAL prompt appears.

rm -f "$VICE_OUT"

# VICE 3.10 ethernet activation has two quirks that must both be worked
# around (see src/c64_test_harness/backends/vice_lifecycle.py for the
# detailed comment block):
#
#   1. ``-ethernetcart`` is advertised in ``-help`` but rejected at parse
#      time ("Option '-ethernetcart' not valid.").
#   2. ``-ethernetiodriver <name>`` can only accept a value once
#      ``rawnet_arch_init()`` has populated the registered-drivers list,
#      which the Homebrew build only does as part of cart activation.
#
# The only invocation VICE 3.10 will actually accept is an ``-addconfig``
# vicerc that sets ``ETHERNETCART_ACTIVE=1`` + the driver/iface, followed
# by ``-ethernetioif`` / ``-ethernetiodriver`` on the cmdline to force
# the host-side attach.  We write a tmp vicerc inline so this script is
# standalone.
RC_PATH="$(mktemp -t vice_probe_feth.XXXXXX.rc)"
cat >"$RC_PATH" <<EOF
[Version]
ConfigVersion=3.10

[C64SC]
ETHERNETCART_ACTIVE=1
EthernetCartMode=1
EthernetIOIF="$FETH"
EthernetIODriver="pcap"
SaveResourcesOnExit=0
EOF
VICE_ARGS=(
    -addconfig "$RC_PATH"
    -ethernetioif "$FETH"
    -ethernetiodriver pcap
    -binarymonitor
    -binarymonitoraddress "${MONITOR_HOST}:${MONITOR_PORT}"
    +sound
    -logtostdout
)

if [[ "$USE_SUDO" == "1" ]]; then
    # -n makes sudo non-interactive so this script matches the harness's
    # behaviour (ViceProcess.start() also uses `sudo -n`): a missing NOPASSWD
    # entry fails loudly instead of hanging on a password prompt. VICE
    # inherits HOME via sudo's default env_keep, which is enough to locate
    # its data files; we drop -E on purpose for sudoers compatibility.
    sudo -n "$VICE_BIN" "${VICE_ARGS[@]}" >"$VICE_OUT" 2>&1 &
    VICE_PID=$!
else
    "$VICE_BIN" "${VICE_ARGS[@]}" >"$VICE_OUT" 2>&1 &
    VICE_PID=$!
fi

echo "[probe] launched VICE (pid=$VICE_PID)"

cleanup() {
    if [[ -n "$VICE_PID" ]] && kill -0 "$VICE_PID" 2>/dev/null; then
        if [[ "$USE_SUDO" == "1" ]]; then
            sudo -n kill "$VICE_PID" 2>/dev/null || true
            sleep 0.5
            sudo -n kill -9 "$VICE_PID" 2>/dev/null || true
        else
            kill "$VICE_PID" 2>/dev/null || true
            sleep 0.5
            kill -9 "$VICE_PID" 2>/dev/null || true
        fi
    fi
}
trap 'rm -f "$RC_PATH"; cleanup' EXIT

# --- wait for monitor to come up --------------------------------------------

echo -n "[probe] waiting for binary monitor..."
MONITOR_READY=0
for i in $(seq 1 $((PROBE_TIMEOUT * 4))); do
    if ! kill -0 "$VICE_PID" 2>/dev/null; then
        echo " FAILED"
        # Reap the job so we can tell a crash from an orderly exit. A death by
        # SIGSEGV (128+11) here is the issue-#144 signature: the pre-flight
        # above should have caught it, so reaching this point means the cart
        # activation crashed for some other reason.
        wait "$VICE_PID" 2>/dev/null
        VICE_RC=$?
        if [[ "$VICE_RC" == "139" ]]; then
            echo "[fail] VICE segfaulted (SIGSEGV) during startup"
            echo "       A crash while activating the ethernet cart is the"
            echo "       signature of a VICE build without ethernet support"
            echo "       (empty rawnet driver registry) — see issue #144."
            echo "       Re-run with --vice-bin pointing at an ethernet-enabled"
            echo "       x64sc. This is not a /dev/bpf permissions problem."
        else
            echo "[fail] VICE exited before monitor came up (rc=$VICE_RC)"
        fi
        echo "---- VICE output ----"
        cat "$VICE_OUT"
        echo "---- end VICE output ----"
        exit 1
    fi
    if /usr/bin/nc -z "$MONITOR_HOST" "$MONITOR_PORT" 2>/dev/null; then
        MONITOR_READY=1
        break
    fi
    sleep 0.25
done
echo

if [[ "$MONITOR_READY" != "1" ]]; then
    echo "[fail] binary monitor did not come up within ${PROBE_TIMEOUT}s"
    echo "---- VICE output ----"
    cat "$VICE_OUT"
    echo "---- end VICE output ----"
    exit 1
fi

echo "[ok]   binary monitor listening on $MONITOR_HOST:$MONITOR_PORT"

# --- minimal monitor handshake ---------------------------------------------
# MON_CMD_PING (0x81). The harness has a full client, but this script must
# be standalone — we hand-roll the 11-byte request using python3. Any
# python3 will do (system python is fine for a socket ping).
#
# Request format (VICE binary monitor v2):
#   u8  STX=0x02
#   u8  api_version=0x02
#   u32 length (LE) — payload length, 0 for PING
#   u32 request_id (LE) — echoed back
#   u8  command=0x81 (PING)
# Response:
#   u8  STX=0x02
#   u8  api_version=0x02
#   u32 length (LE)
#   u8  response_type=0x81
#   u8  error_code
#   u32 request_id (LE)
#   ... payload ...

/usr/bin/python3 - "$MONITOR_HOST" "$MONITOR_PORT" <<'PY'
import socket, struct, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.create_connection((host, port), timeout=5)
s.settimeout(3.0)

# VICE binary monitor v2 request: STX(1) api(1) length(u32 LE) req_id(u32 LE) cmd(1)
# VICE binary monitor v2 response: STX(1) api(1) length(u32 LE) resp_type(u8)
#                                  err(u8) req_id(u32 LE) body[length]
# VICE emits async events on connect and on state changes (MON_RESPONSE_STOPPED 0x62,
# MON_RESPONSE_RESUMED 0x63, REGISTERS_GET 0x31 etc). Async events use request_id
# 0xFFFFFFFF. We loop-read responses, discard anything that isn't our PING reply,
# and stop when we see response_type=0x81 with our request_id.

def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("short read from monitor (peer closed)")
        buf += chunk
    return buf

MY_REQ_ID = 0xDEADBEEF
MON_CMD_PING = 0x81
MON_RESP_PING = 0x81

req = struct.pack("<BBIIB", 0x02, 0x02, 0, MY_REQ_ID, MON_CMD_PING)
s.sendall(req)

seen_events = []
found_ping = False
err_code = 0
body_len = 0
for _ in range(32):  # bound the loop; 32 async events before PING would be absurd
    hdr = read_exact(s, 12)
    stx, api, length, resp_type, err, resp_req_id = struct.unpack("<BBIBBI", hdr)
    if stx != 0x02 or api != 0x02:
        print(f"[fail] bad framing stx=0x{stx:02x} api=0x{api:02x}", file=sys.stderr)
        sys.exit(1)
    body = read_exact(s, length) if length else b""
    if resp_type == MON_RESP_PING and resp_req_id == MY_REQ_ID:
        found_ping = True
        err_code = err
        body_len = length
        break
    seen_events.append((resp_type, resp_req_id, length))

if seen_events:
    evs = ", ".join(f"0x{t:02x}(id=0x{r:08x},len={l})" for t, r, l in seen_events)
    print(f"[info] discarded {len(seen_events)} async event(s) before PING: {evs}")

if not found_ping:
    print("[fail] did not receive PING response within 32 frames", file=sys.stderr)
    sys.exit(1)

print(f"[ok]   PING round-trip (err=0x{err_code:02x} body_len={body_len})")
s.close()
PY
PING_RC=$?

if [[ "$PING_RC" != "0" ]]; then
    echo "[fail] binary monitor PING failed"
    echo "---- VICE output ----"
    cat "$VICE_OUT"
    echo "---- end VICE output ----"
    exit 1
fi

# --- pcap diagnostics in the log -------------------------------------------

if grep -qi "pcap_open_live failed\|could not open device\|BPF" "$VICE_OUT"; then
    echo "[warn] pcap/BPF error found in VICE log:"
    grep -i "pcap\|BPF" "$VICE_OUT" | head -10
    echo "       (this usually means /dev/bpf* is not user-readable — install"
    echo "       Wireshark's ChmodBPF helper or run as root)"
    exit 1
fi

if grep -qi "ethernet" "$VICE_OUT"; then
    echo "[info] ethernet lines from VICE log:"
    grep -i "ethernet\|pcap\|cs8900" "$VICE_OUT" | head -10
fi

echo
echo "[pass] VICE launched on $FETH via pcap, binary monitor responded to PING"
echo "[pass] log at $VICE_OUT has no pcap/BPF errors"
echo
echo "Next: run the bridge tests with"
echo "  ~/.local/share/c64-test-harness/venv/bin/pytest tests/test_bridge_ping.py -v"
exit 0
