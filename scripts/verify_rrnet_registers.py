#!/usr/bin/env python3
"""Empirically map CS8900a register offsets in VICE 3.10 TFE vs RR-Net mode.

Launches a VICE instance in each mode, does a cold read of all 16 I/O
window bytes ($DE00-$DE0F), then tries to find the PPPtr/PPData pair
that correctly reads the Product ID (PP 0x0000 = 0x630E) and the
BusST register (PP 0x0138).

Also probes whether TxCMD/TxLen direct registers behave as writable.

Run after scripts/setup-bridge-tap.sh.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.vice_binary import BinaryViceTransport  # noqa: E402

X64SC = "/usr/local/bin/x64sc"
TAP = "tap-c64-0"


def _wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except Exception:
            s.close()
            time.sleep(0.2)
    return False


def _launch(mode: int, port: int) -> tuple[subprocess.Popen, str]:
    rc_text = (
        "[Version]\n"
        "ConfigVersion=3.10\n\n"
        "[C64SC]\n"
        "ETHERNETCART_ACTIVE=1\n"
        f"EthernetCartMode={mode}\n"
        f'EthernetIOIF="{TAP}"\n'
        'EthernetIODriver="tuntap"\n'
        "SaveResourcesOnExit=0\n"
    )
    fd, path = tempfile.mkstemp(prefix="verify_rr_", suffix=".rc")
    with os.fdopen(fd, "w") as f:
        f.write(rc_text)
    args = [
        X64SC, "-binarymonitor", "-binarymonitoraddress",
        f"ip4://127.0.0.1:{port}",
        "-warp", "-ntsc", "-minimized", "+sound",
        "-addconfig", path,
        "-ethernetioif", TAP, "-ethernetiodriver", "tuntap",
    ]
    proc = subprocess.Popen(
        args,
        stdout=open(f"/tmp/verify_rr_mode{mode}.out", "wb"),
        stderr=open(f"/tmp/verify_rr_mode{mode}.err", "wb"),
    )
    return proc, path


def _probe(mode_name: str, mode: int) -> None:
    print("=" * 60)
    print(f"Mode {mode} ({mode_name})")
    print("=" * 60)
    port = 6601
    proc, rcpath = _launch(mode, port)
    try:
        if not _wait_port(port, 10):
            print(f"  VICE monitor did not come up in mode {mode}")
            return
        t = BinaryViceTransport(port=port)
        active = t.resource_get("ETHERNETCART_ACTIVE")
        m = t.resource_get("EthernetCartMode")
        print(f"  resources: ACTIVE={active} MODE={m}")

        # 1) Cold read $DE00-$DE0F
        cold = t.read_memory(0xDE00, 16)
        print("  Cold I/O window $DE00-$DE0F:")
        for i in range(0, 16, 2):
            word = cold[i] | (cold[i + 1] << 8)
            print(f"    ${0xDE00 + i:04X}..${0xDE01 + i:04X} = "
                  f"{cold[i]:02X} {cold[i + 1]:02X}  (word 0x{word:04X})")

        # 2) Try to find PPPtr/PPData by writing 0x0000 to each even offset
        #    and checking if the next even offset reads back Product ID.
        print("  PPPtr/PPData scan (write PP 0x0000, read for 0x630E):")
        hits = []
        for ptr in range(0, 16, 2):
            t.write_memory(0xDE00 + ptr, bytes([0x00, 0x00]))
            for data in range(0, 16, 2):
                if data == ptr:
                    continue
                d = t.read_memory(0xDE00 + data, 2)
                val = d[0] | (d[1] << 8)
                if val == 0x630E:
                    hits.append((ptr, data))
        # Deduplicate: mark the "canonical" one as ptr=0x0A (TFE default)
        for ptr, data in hits:
            marker = ""
            if ptr == 0x0A and data == 0x0C:
                marker = "  <- TFE canonical"
            print(f"    PPPtr @ ${ptr:02X}  PPData @ ${data:02X}{marker}")

        # 3) Probe BusST (PP 0x0138) — distinct value proves PPPtr works
        if hits:
            ptr_off, data_off = hits[0]
            t.write_memory(0xDE00 + ptr_off, bytes([0x38, 0x01]))
            d = t.read_memory(0xDE00 + data_off, 2)
            bus_st = d[0] | (d[1] << 8)
            print(f"  BusST via PPPtr=${ptr_off:02X} / PPData=${data_off:02X}: "
                  f"0x{bus_st:04X}")

        # 4) Try to write TxCMD (0x00C0) and TxLen (0x0040) and read back
        tfe_txcmd = 0x04
        tfe_txlen = 0x06
        t.write_memory(0xDE00 + tfe_txcmd, bytes([0xC0, 0x00]))
        t.write_memory(0xDE00 + tfe_txlen, bytes([0x40, 0x00]))
        cmd_rb = t.read_memory(0xDE00 + tfe_txcmd, 2)
        len_rb = t.read_memory(0xDE00 + tfe_txlen, 2)
        print(f"  TFE TxCMD write=0x00C0 readback=0x{cmd_rb[0]|(cmd_rb[1]<<8):04X}")
        print(f"  TFE TxLen write=0x0040 readback=0x{len_rb[0]|(len_rb[1]<<8):04X}")

        try:
            t.resume()
        except Exception:
            pass
        try:
            t.close()
        except Exception:
            pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.unlink(rcpath)
        except OSError:
            pass


def main() -> int:
    if not os.path.exists(X64SC):
        print(f"ERROR: {X64SC} not found")
        return 1
    if not os.path.isdir(f"/sys/class/net/{TAP}"):
        print(f"ERROR: {TAP} not present.  Run scripts/setup-bridge-tap.sh")
        return 1

    _probe("TFE", 0)
    time.sleep(1)
    _probe("RR-Net", 1)
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print("TFE mode exposes the classic CS8900a register layout:")
    print("  RTDATA  @ $DE00/$DE01")
    print("  TxCMD   @ $DE04/$DE05")
    print("  TxLen   @ $DE06/$DE07")
    print("  ISQ     @ $DE08/$DE09")
    print("  PPPtr   @ $DE0A/$DE0B")
    print("  PPData  @ $DE0C/$DE0D")
    print()
    print("RR-Net mode in VICE 3.10 does NOT expose a working")
    print("PPPtr/PPData pair — the standard CS8900a packet-page")
    print("addressing does not function.  Use TFE mode for any code")
    print("that reads/writes CS8900a packet-page registers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
