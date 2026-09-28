#!/usr/bin/env python3
"""One-shot VICE smoke test for ``verify-dev-env.sh --smoke`` (#500).

Launches one headless VICE through the harness launcher (``ViceProcess`` /
``ViceConfig`` with ``sound=False``, ``minimize=True``; never a bare
``x64sc`` subprocess), writes a short pattern into RAM over the binary
monitor, reads it back, and stops the emulator it started -- its own
``ViceProcess``, never a process found by name.

It is **destructive** in the sense that it starts an emulator, which is
why ``verify-dev-env.sh`` runs it only when ``--smoke`` is passed.

Prints one line (``ok: ...`` or ``fail: ...``) and exits 0 on a
round-trip that matched, 1 otherwise.
"""

from __future__ import annotations

import socket
import sys
import time

#: Cassette buffer: free RAM on a machine sitting at READY.
SMOKE_ADDR = 0x033C
SMOKE_PATTERN = bytes([0xA5, 0x5A, 0x00, 0xFF, 0xC6, 0x40, 0x13, 0x37])
CONNECT_TIMEOUT = 30.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connect(transport_cls, port: int, vice, timeout: float):
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        proc = getattr(vice, "_proc", None)
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"VICE exited (rc={proc.returncode}) before the monitor answered")
        try:
            return transport_cls(port=port)
        except Exception as exc:  # connection refused while VICE boots
            last = exc
            time.sleep(0.5)
    raise ConnectionError(f"binary monitor on port {port} did not answer within {timeout:.0f}s: {last}")


def run_smoke(
    *,
    vice_process_cls=None,
    vice_config_cls=None,
    transport_cls=None,
    port: int | None = None,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> tuple[bool, str]:
    """Run the round-trip; return ``(passed, one-line detail)``.

    The three classes default to the harness's own and are parameters only
    so the unit test can fake the launcher.
    """
    if vice_process_cls is None or vice_config_cls is None or transport_cls is None:
        from c64_test_harness import BinaryViceTransport, ViceConfig, ViceProcess

        vice_process_cls = vice_process_cls or ViceProcess
        vice_config_cls = vice_config_cls or ViceConfig
        transport_cls = transport_cls or BinaryViceTransport

    port = port if port is not None else _free_port()
    config = vice_config_cls(port=port, sound=False, minimize=True, warp=True)
    vice = vice_process_cls(config)
    transport = None
    try:
        vice.start()
        pid = vice.pid
        transport = _connect(transport_cls, port, vice, connect_timeout)
        transport.write_memory(SMOKE_ADDR, SMOKE_PATTERN)
        got = bytes(transport.read_memory(SMOKE_ADDR, len(SMOKE_PATTERN)))
        if got != SMOKE_PATTERN:
            return False, (
                f"read-back mismatch at ${SMOKE_ADDR:04X}: wrote {SMOKE_PATTERN.hex()} "
                f"read {got.hex()} (pid {pid}, port {port})"
            )
        return True, (
            f"launched VICE (pid {pid}, port {port}), wrote and read back "
            f"{len(SMOKE_PATTERN)} bytes at ${SMOKE_ADDR:04X}"
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        # Stops only the process this ViceProcess launched.
        vice.stop()


def main() -> int:
    passed, detail = run_smoke()
    print(("ok: " if passed else "fail: ") + detail)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
