"""Live turbo-sweep test: x25519_clamp correctness at multiple U64 CPU speeds.

Gated by three env vars — U64_HOST, U64_ALLOW_MUTATE, and X25519_PRG, which
must point at an ``x25519.prg`` build (issue #245: it used to be a hard-coded
path into a Linux home directory, so on any other machine the module skipped
silently and nobody noticed).

    U64_HOST=<host> U64_ALLOW_MUTATE=1 X25519_PRG=/path/to/x25519.prg \
        python3 -m pytest tests/test_u64_turbo_bench_live.py -v

**Upload budget.** This is the heaviest upload loop in the repository: four
turbo speeds x three vectors = **12 full-PRG ``run_prg`` uploads per
session**, each preceded by a ``reboot()`` — which does not collect ``/Temp``
attachments, because ``/Temp`` is a firmware RAM disk that only a power-on
clears. On firmware without upstream #686 (the C64 Ultimate on 1.1.0) that is
12 attachments against a budget the standing hardware-safety clause in
CLAUDE.md says to treat as a handful. Point this at the U64E, or at nothing.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.device_lock import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    set_reu,
    set_turbo_mhz,
    snapshot_state,
    restore_state,
)
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_text


# ---------------------------------------------------------------------------
# Environment gates
# ---------------------------------------------------------------------------

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

#: Env var naming the x25519 PRG to upload. No default: a path that happens
#: to resolve on one machine is not a gate (#245).
_PRG_ENV = "X25519_PRG"


def _resolve_prg_path() -> tuple[Path | None, str]:
    """Resolve the PRG under test, or say why the module is skipping.

    Returns ``(path, "")`` when armed, and ``(None, reason)`` otherwise.
    The two skip reasons are deliberately distinguishable: an unset
    variable is a decision, an unresolvable one is an environment defect
    that the caller meant to avoid and should be told about.
    """
    raw = os.environ.get(_PRG_ENV)
    if not raw:
        return None, (
            f"{_PRG_ENV} not set — point it at an x25519.prg build to arm "
            "this module (12 run_prg uploads per session; see its docstring)"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        return None, (
            f"{_PRG_ENV}={path} does not name a readable file — "
            "environment defect, not a deliberate skip"
        )
    return path, ""


_PRG_PATH, _PRG_SKIP_REASON = _resolve_prg_path()

pytestmark = [
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
    pytest.mark.skipif(not _ALLOW_MUTATE, reason="U64_ALLOW_MUTATE not set"),
    pytest.mark.skipif(_PRG_PATH is None, reason=_PRG_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Addresses (from labels.txt)
# ---------------------------------------------------------------------------

X25519_CLAMP = 0x1509
X25_SCALAR = 0x19A0
MAIN_LOOP = 0x082A
SENTINEL = 0x0350
TRAMPOLINE = 0x0360


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------

def _clamp_ref(scalar: bytes) -> bytes:
    s = bytearray(scalar)
    s[0] &= 0xF8
    s[31] = (s[31] & 0x7F) | 0x40
    return bytes(s)


# Trampoline: JSR clamp; LDA #$42; STA sentinel; JMP * (park)
_TRAMPOLINE_CODE = bytes([
    0x20, X25519_CLAMP & 0xFF, (X25519_CLAMP >> 8) & 0xFF,  # JSR $1509
    0xA9, 0x42,                                               # LDA #$42
    0x8D, SENTINEL & 0xFF, (SENTINEL >> 8) & 0xFF,           # STA $0350
    0x4C, (TRAMPOLINE + 8) & 0xFF, ((TRAMPOLINE + 8) >> 8) & 0xFF,  # JMP $0368
])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    assert _HOST is not None
    lock = DeviceLock(_HOST)
    try:
        lock.acquire_or_raise(timeout=120.0)
    except DeviceLockTimeout as e:
        pytest.skip(str(e))
    try:
        yield Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
    finally:
        lock.release()


@pytest.fixture(scope="module")
def prg_data() -> bytes:
    assert _PRG_PATH is not None  # guaranteed by the module-level skipif
    return _PRG_PATH.read_bytes()


@pytest.fixture(scope="module")
def original_state(client: Ultimate64Client):
    snap = snapshot_state(client)
    # x25519 program requires 512 KB REU for lookup tables
    set_reu(client, enabled=True, size="512 KB")
    time.sleep(0.5)
    yield snap
    restore_state(client, snap)
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Core helper: one clamp per fresh PRG load (no re-trigger races)
# ---------------------------------------------------------------------------

def _run_clamp_fresh(
    client: Ultimate64Client,
    transport: Ultimate64Transport,
    prg_data: bytes,
    scalar: bytes,
    mhz: int,
) -> bytes:
    """Reboot U64, set turbo, load x25519.prg, run clamp, return result.

    Full reboot between runs clears REU DMA controller state — a soft
    reset (what run_prg does internally) leaves stale DMA state that
    causes hangs when switching turbo speeds.
    """
    # Full reboot for clean FPGA/DMA state
    client.reboot()
    time.sleep(8.0)

    # Re-enable REU (reboot may reset config) and set turbo
    set_reu(client, enabled=True, size="512 KB")
    set_turbo_mhz(client, mhz)
    time.sleep(0.3)
    client.run_prg(prg_data)
    time.sleep(2.0)

    # Verify program started via main_loop bytes (not stale screen text)
    boot_deadline = time.monotonic() + 120.0
    while time.monotonic() < boot_deadline:
        ml = transport.read_memory(MAIN_LOOP, 3)
        if ml == bytes([0x4C, 0x2A, 0x08]):
            break
        time.sleep(0.5)
    else:
        raise TimeoutError(f"PRG boot timeout (main_loop={ml.hex()})")

    time.sleep(0.5)  # settle after init

    # Write scalar, trampoline, zero sentinel
    write_bytes(transport, X25_SCALAR, scalar)
    write_bytes(transport, TRAMPOLINE, _TRAMPOLINE_CODE)
    write_bytes(transport, SENTINEL, bytes([0x00]))

    # DMA flush
    _ = transport.read_memory(SENTINEL, 1)

    # Hijack main_loop → JMP $0360
    write_bytes(transport, MAIN_LOOP, bytes([0x4C, 0x60, 0x03]))

    # Poll sentinel
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if transport.read_memory(SENTINEL, 1)[0] == 0x42:
            break
        time.sleep(0.05)
    else:
        diag = transport.read_memory(SENTINEL, 1)
        raise TimeoutError(f"clamp timeout (sentinel={diag.hex()})")

    return read_bytes(transport, X25_SCALAR, 32)


# ---------------------------------------------------------------------------
# Test vectors
# ---------------------------------------------------------------------------

_VECTORS = [
    ("realistic", bytes([
        0x77, 0x07, 0x6D, 0x0A, 0x73, 0x18, 0xA5, 0x7D,
        0x3C, 0x16, 0xC1, 0x72, 0x51, 0xB2, 0x66, 0x45,
        0xDF, 0x4C, 0x2F, 0x87, 0xEB, 0xC0, 0x99, 0x2A,
        0xB1, 0x77, 0xFB, 0xA5, 0x1D, 0xB9, 0x2C, 0x2A,
    ])),
    ("all-zeros", bytes(32)),
    ("all-0xFF", bytes([0xFF] * 32)),
]


# ---------------------------------------------------------------------------
# Parametrized test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mhz", [48, 16, 4, 1])
def test_clamp_at_turbo_speed(
    client: Ultimate64Client,
    prg_data: bytes,
    original_state,  # noqa: ARG001
    mhz: int,
) -> None:
    transport = Ultimate64Transport(host=_HOST, password=_PW, timeout=10.0)
    try:
        for label, scalar in _VECTORS:
            expected = _clamp_ref(scalar)
            result = _run_clamp_fresh(client, transport, prg_data, scalar, mhz)
            assert result == expected, (
                f"clamp mismatch at {mhz} MHz ({label}):\n"
                f"  expected: {expected.hex()}\n"
                f"  got:      {result.hex()}"
            )
    finally:
        transport.close()
