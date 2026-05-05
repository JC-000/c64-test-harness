#!/usr/bin/env python3
"""Empirically verify which VICE 3.10 ethernet activation approach works.

Tests three approaches against tap-c64-0 and prints PASS/FAIL for each:

* Approach A: CLI flags only (``-ethernetcart -ethernetcartmode``)
* Approach B: temp vicerc passed via ``-addconfig`` (loads on top of defaults)
* Approach C: CLI ``-ethernetioif``/``-ethernetiodriver`` +
  runtime ``resource_set(ETHERNETCART_ACTIVE, 1)``

For each approach the script launches VICE, waits for the binary monitor,
reads ``ETHERNETCART_ACTIVE`` / ``EthernetCartMode`` and probes the
CS8900a Product ID (PP 0x0000) via TFE-mode offsets ($DE0A/$DE0C).

Prerequisites:
    sudo scripts/setup-bridge-tap.sh   # creates tap-c64-0
    # (no other x64sc running, no stale /dev/net/tun handles)

Result: Approach B is the working approach on VICE 3.10.  Approach A
is rejected at CLI parse time with ``Option '-ethernetcart' not valid``
(the flag appears in ``-help`` but isn't actually registered).  Approach
C fails because ``EthernetIOIF``/``EthernetIODriver`` are STRING
resources the binary monitor can't write.

Per-run stdout/stderr is captured to /tmp/vice_verify_<tag>.{out,err}.
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
DRIVER = "tuntap"


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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _launch(args: list[str], tag: str) -> subprocess.Popen:
    out = open(f"/tmp/vice_verify_{tag}.out", "wb")
    err = open(f"/tmp/vice_verify_{tag}.err", "wb")
    return subprocess.Popen(args, stdout=out, stderr=err)


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _probe(t: BinaryViceTransport) -> int:
    """TFE-mode Product ID probe via PPPtr=$DE0A / PPData=$DE0C."""
    t.write_memory(0xDE0A, bytes([0x00, 0x00]))
    d = t.read_memory(0xDE0C, 2)
    return d[0] | (d[1] << 8)


def _resources(t: BinaryViceTransport) -> dict:
    r = {}
    for name in ("ETHERNETCART_ACTIVE", "EthernetCartMode"):
        try:
            r[name] = t.resource_get(name)
        except Exception as e:  # noqa: BLE001
            r[name] = f"ERR({e})"
    return r


def _common(port: int) -> list[str]:
    return [
        X64SC,
        "-binarymonitor",
        "-binarymonitoraddress",
        f"ip4://127.0.0.1:{port}",
        "-warp",
        "-ntsc",
        "-minimized",
        "+sound",
    ]


def approach_a(port: int) -> dict:
    args = _common(port) + [
        "-ethernetioif", TAP,
        "-ethernetiodriver", DRIVER,
        "-ethernetcart", "-ethernetcartmode", "0",
    ]
    proc = _launch(args, "A")
    if not _wait_port(port, 8):
        result = {"alive": proc.poll() is None, "exit_code": proc.returncode,
                  "stderr": open("/tmp/vice_verify_A.err").read().strip()[:200]}
        _kill(proc)
        return result
    try:
        t = BinaryViceTransport(port=port)
        res = _resources(t)
        pid = _probe(t)
        try:
            t.resume()
        except Exception:
            pass
        try:
            t.close()
        except Exception:
            pass
        return {"alive": True, "resources": res, "product_id": pid}
    finally:
        _kill(proc)


def approach_b(port: int) -> dict:
    rc_text = (
        "[Version]\n"
        "ConfigVersion=3.10\n"
        "\n"
        "[C64SC]\n"
        "ETHERNETCART_ACTIVE=1\n"
        "EthernetCartMode=0\n"
        f'EthernetIOIF="{TAP}"\n'
        f'EthernetIODriver="{DRIVER}"\n'
        "SaveResourcesOnExit=0\n"
    )
    fd, rcpath = tempfile.mkstemp(prefix="vice_verify_B_", suffix=".rc")
    with os.fdopen(fd, "w") as f:
        f.write(rc_text)
    try:
        args = _common(port) + ["-addconfig", rcpath]
        proc = _launch(args, "B")
        if not _wait_port(port, 8):
            result = {"alive": proc.poll() is None, "exit_code": proc.returncode}
            _kill(proc)
            return result
        try:
            t = BinaryViceTransport(port=port)
            res = _resources(t)
            pid = _probe(t)
            try:
                t.resume()
            except Exception:
                pass
            try:
                t.close()
            except Exception:
                pass
            return {"alive": True, "resources": res, "product_id": pid}
        finally:
            _kill(proc)
    finally:
        try:
            os.unlink(rcpath)
        except OSError:
            pass


def approach_c(port: int) -> dict:
    args = _common(port) + [
        "-ethernetioif", TAP,
        "-ethernetiodriver", DRIVER,
    ]
    proc = _launch(args, "C")
    if not _wait_port(port, 8):
        result = {"alive": proc.poll() is None, "exit_code": proc.returncode}
        _kill(proc)
        return result
    try:
        t = BinaryViceTransport(port=port)
        try:
            t.resource_set("EthernetCartMode", 0)
            t.resource_set("ETHERNETCART_ACTIVE", 1)
            time.sleep(0.3)
            res = _resources(t)
            pid = _probe(t)
            return {"alive": True, "resources": res, "product_id": pid}
        except Exception as e:  # noqa: BLE001
            return {"alive": True, "error": str(e)}
        finally:
            try:
                t.resume()
            except Exception:
                pass
            try:
                t.close()
            except Exception:
                pass
    finally:
        _kill(proc)


def _verdict(name: str, r: dict) -> str:
    pid = r.get("product_id")
    res = r.get("resources", {})
    ok = (
        r.get("alive")
        and isinstance(res, dict)
        and res.get("ETHERNETCART_ACTIVE") in (1, "1")
        and pid == 0x630E
    )
    tag = "PASS" if ok else "FAIL"
    parts = [f"  {name}: {tag}"]
    parts.append(f"     alive={r.get('alive')} product_id="
                 + (f"0x{pid:04X}" if isinstance(pid, int) else str(pid)))
    if res:
        parts.append(f"     resources={res}")
    if "error" in r:
        parts.append(f"     error={r['error']}")
    if "stderr" in r:
        parts.append(f"     stderr={r['stderr']}")
    return "\n".join(parts)


def main() -> int:
    if not os.path.exists(X64SC):
        print(f"ERROR: {X64SC} not found")
        return 1
    if not os.path.isdir(f"/sys/class/net/{TAP}"):
        print(f"ERROR: {TAP} not present.  Run scripts/setup-bridge-tap.sh")
        return 1

    print("=" * 60)
    print("Approach A: CLI flags only")
    print("=" * 60)
    ra = approach_a(_free_port())
    time.sleep(1)

    print("Approach B: temp vicerc -addconfig")
    rb = approach_b(_free_port())
    time.sleep(1)

    print("Approach C: CLI iface/driver + runtime resource_set")
    rc = approach_c(_free_port())

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(_verdict("A", ra))
    print(_verdict("B", rb))
    print(_verdict("C", rc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
