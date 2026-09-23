"""Live turbo-sweep test: x25519_clamp correctness at multiple U64 CPU speeds.

Gated by three env vars — U64_HOST, U64_ALLOW_MUTATE, and X25519_PRG, which
must point at an ``x25519.prg`` build (issue #245: it used to be a hard-coded
path into a Linux home directory, so on any other machine the module skipped
silently and nobody noticed).

The build must also have kept its ``labels.txt`` beside the PRG: this module
reads ``x25519_clamp``, ``x25_scalar`` and ``main_loop`` out of it at import
rather than carrying literals, which went stale once and cost a full session
of uploads to notice (issue #439).

    U64_HOST=<host> U64_ALLOW_MUTATE=1 X25519_PRG=/path/to/x25519.prg \
        python3 -m pytest tests/test_u64_turbo_bench_live.py -v

**Upload budget.** This is the heaviest upload loop in the repository: four
turbo speeds x three vectors = **12 full-PRG ``run_prg`` uploads per
session**, each preceded by a ``reboot()`` — which does not collect ``/Temp``
attachments, because ``/Temp`` is a firmware RAM disk that only a power-on
clears. On firmware without upstream #686 (the C64 Ultimate on 1.1.0) that is
12 attachments against a device on which no safe number of uploads is known.
Point this at the U64E, or at nothing.
"""

from __future__ import annotations

import functools
import os
import time
from pathlib import Path

import pytest

from c64_test_harness import Labels
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
from live_fixture_teardown import (
    attempt_steps,
    raise_teardown_failures,
    teardown_then_release,
)


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

# ---------------------------------------------------------------------------
# Addresses: derived from the build, never hard-coded (#439)
# ---------------------------------------------------------------------------
#
# These used to be literals copied out of a ``labels.txt`` by hand. The
# x25519 link layout then moved (that project's v0.14-v0.16, 2026-09-06/07)
# and the literals did not: ``main_loop`` became ``$082D`` while the module
# still polled ``$082A``, which holds ``20 44 08`` (``JSR $0844``). Every
# speed failed ``PRG boot timeout`` *after* its uploads — 12 ``run_prg``
# calls and 12 ``reboot()``s spent to discover a stale constant. So the
# build under test decides (the #245 principle), and it decides at import
# time: the ``pytestmark`` skip below is evaluated at collection, before any
# fixture runs and therefore before the first upload.

#: The x25519 Makefile writes ``$(BUILD_DIR)/labels.txt`` beside
#: ``$(BUILD_DIR)/x25519.prg``, so the listing is the PRG's sibling.
_LABELS_FILENAME = "labels.txt"

#: Build symbols this module cannot run without, by their ca65 names.
_REQUIRED_LABELS = ("x25519_clamp", "x25_scalar", "main_loop")


def _resolve_labels(prg_path: Path | None) -> tuple[Labels | None, str]:
    """Resolve build addresses from the ``labels.txt`` beside the PRG.

    Parsing is :meth:`c64_test_harness.Labels.from_file`'s (#463); this
    function only adds the policy. Returns ``(labels, "")`` when every
    required symbol resolved, and ``(None, reason)`` otherwise. The three
    failure modes are reported distinguishably — an absent listing, a
    listing that is not one, and a listing missing a symbol are three
    different things to go fix — and none of them falls back to a literal.
    """
    if prg_path is None:
        return None, f"no {_PRG_ENV} resolved, so there is no {_LABELS_FILENAME} to read"

    path = prg_path.parent / _LABELS_FILENAME
    if not path.is_file():
        return None, (
            f"{path} not found — the x25519 build writes {_LABELS_FILENAME} "
            f"next to the PRG; this module reads its addresses from the build "
            f"rather than hard-coding them (#439)"
        )

    try:
        labels = Labels.from_file(path)
        text = path.read_text()
    except OSError as exc:  # unreadable is an environment defect, not a decision
        return None, f"{path} could not be read: {exc}"

    # ``Labels`` drops lines it cannot parse without a word, so an HTML 404
    # saved as labels.txt parses to nothing. Say so, and quote what is there.
    if not labels:
        unparseable = [line for line in text.splitlines() if line.strip()]
        sample = unparseable[0] if unparseable else "<empty file>"
        return None, (
            f"{path} contains no ca65 label lines "
            f"({len(unparseable)} unparseable; first: {sample!r}) — expected "
            f"lines of the form 'al C:00082D .main_loop'"
        )

    missing = [name for name in _REQUIRED_LABELS if name not in labels]
    if missing:
        reason = (
            f"{path} is missing the symbol(s) {', '.join(missing)} — the PRG "
            f"at {prg_path} is not the x25519 build this module drives"
        )
        if len(missing) == len(_REQUIRED_LABELS):
            # None of them: the file may not be a listing at all, with one
            # stray line ``Labels`` accepted. Show what it is.
            first = next(line for line in text.splitlines() if line.strip())
            reason += f" (first line: {first!r})"
        return None, reason

    return labels, ""


_LABELS, _LABELS_SKIP_REASON = _resolve_labels(_PRG_PATH)

# Addresses that belong to the build. ``None`` when the labels did not
# resolve, in which case the module-level skip below has already fired.
X25519_CLAMP = None if _LABELS is None else _LABELS["x25519_clamp"]
X25_SCALAR = None if _LABELS is None else _LABELS["x25_scalar"]
MAIN_LOOP = None if _LABELS is None else _LABELS["main_loop"]

# Addresses the harness chooses. These are free RAM the test picks itself,
# not build labels, so they are not looked up in the listing.
SENTINEL = 0x0350

#: The trampoline's page. The trampoline sits here at main_loop's low byte,
#: so the hijack rewrites one byte of the running ``JMP main_loop`` (#477;
#: see :func:`_hijack_code`). $CD00-$CEFF is outside HARNESS_SCRATCH and the
#: x25519 image, and an 11-byte trampoline never leaves it.
TRAMPOLINE_PAGE = 0xCD00
TRAMPOLINE = None if MAIN_LOOP is None else TRAMPOLINE_PAGE | (MAIN_LOOP & 0xFF)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
# Declared after the addresses so the label resolution can gate the module.
# Every one of these is evaluated at collection, which is the point: a stale
# or unreadable build stops the session before the ``client`` fixture, and so
# before the first of its 12 uploads (#439).

pytestmark = [
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
    pytest.mark.skipif(not _ALLOW_MUTATE, reason="U64_ALLOW_MUTATE not set"),
    pytest.mark.skipif(_PRG_PATH is None, reason=_PRG_SKIP_REASON),
    pytest.mark.skipif(_LABELS is None, reason=_LABELS_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------

def _clamp_ref(scalar: bytes) -> bytes:
    s = bytearray(scalar)
    s[0] &= 0xF8
    s[31] = (s[31] & 0x7F) | 0x40
    return bytes(s)


def _jmp_bytes(target: int) -> bytes:
    """``JMP target``, assembled little-endian.

    This module needs these bytes in two places: the parked ``main_loop``
    the boot check waits for (a self-``JMP``) and the trampoline's own park.
    Spelling either of them as a literal is what #439 was: the literal kept
    saying ``4C 2A 08`` after the label moved to ``$082D``, so the poll
    could never match and the failure surfaced only after the uploads.
    """
    return bytes([0x4C, target & 0xFF, (target >> 8) & 0xFF])


def _hijack_code() -> tuple[int, bytes]:
    """``(address, data)`` that turns ``JMP main_loop`` into ``JMP TRAMPOLINE``.

    One byte: the high operand byte, at ``main_loop + 2``. The 6510 is
    executing that ``JMP`` while the DMA write lands, and the write can halt
    it between the operand fetches; a write of both operand bytes can then
    run a torn ``JMP`` -- old low byte, new high byte, ``JMP $032D`` for the
    old ``$0360`` trampoline, into harness scratch. #426 measured the
    mechanism on the TOD test; ``tests/torn_fetch.py`` models it (#477).

    Reads ``TRAMPOLINE`` and ``MAIN_LOOP`` at call time, so moving either
    moves the hijack with it (#439), and refuses a pair whose low bytes
    differ rather than write a hijack that can tear.
    """
    if TRAMPOLINE & 0xFF != MAIN_LOOP & 0xFF:
        raise ValueError(
            f"TRAMPOLINE ${TRAMPOLINE:04X} must share main_loop's low byte "
            f"(${MAIN_LOOP:04X}): a hijack that rewrites both operand bytes of "
            f"a running JMP can be fetched torn (#477)"
        )
    return MAIN_LOOP + 2, bytes([TRAMPOLINE >> 8])


def _trampoline_code(clamp_address: int) -> bytes:
    """JSR clamp; LDA #$42; STA sentinel; JMP * (park).

    Takes the clamp entry point as an argument so it comes from the build's
    ``labels.txt`` rather than from a literal that the next link-layout
    change silently invalidates.
    """
    park = TRAMPOLINE + 8
    return bytes([
        0x20, clamp_address & 0xFF, (clamp_address >> 8) & 0xFF,  # JSR clamp
        0xA9, 0x42,                                               # LDA #$42
        0x8D, SENTINEL & 0xFF, (SENTINEL >> 8) & 0xFF,           # STA sentinel
        *_jmp_bytes(park),                                        # JMP park
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
    client = None
    failures: list = []
    try:
        client = Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
        yield client
    finally:
        steps = [] if client is None else [("client.close()", client.close)]
        failures = teardown_then_release(steps, lock.release)
    raise_teardown_failures("client teardown", failures)


@pytest.fixture(scope="module")
def prg_data() -> bytes:
    assert _PRG_PATH is not None  # guaranteed by the module-level skipif
    return _PRG_PATH.read_bytes()


@pytest.fixture(scope="module")
def original_state(client: Ultimate64Client):
    snap = snapshot_state(client)
    failures: list = []
    try:
        # x25519 program requires 512 KB REU for lookup tables
        set_reu(client, enabled=True, size="512 KB")
        time.sleep(0.5)
        yield snap
    finally:
        # Every exit puts the snapshot back; a failed restore is reported (#391).
        failures = attempt_steps([
            ("restore_state(client, snap)", functools.partial(restore_state, client, snap)),
        ])
        time.sleep(0.5)
    raise_teardown_failures("original_state restore", failures)


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

    # Verify program started via main_loop bytes (not stale screen text).
    # The expected bytes are assembled from the build's own main_loop
    # address, so a relinked PRG moves the poll with it (#439).
    parked = _jmp_bytes(MAIN_LOOP)
    ml = b""
    boot_deadline = time.monotonic() + 120.0
    while time.monotonic() < boot_deadline:
        ml = transport.read_memory(MAIN_LOOP, 3)
        if ml == parked:
            break
        time.sleep(0.5)
    else:
        raise TimeoutError(
            f"PRG boot timeout (main_loop=${MAIN_LOOP:04X} read {ml.hex()}, "
            f"expected {parked.hex()})"
        )

    time.sleep(0.5)  # settle after init

    # Write scalar, trampoline, zero sentinel
    write_bytes(transport, X25_SCALAR, scalar)
    write_bytes(transport, TRAMPOLINE, _trampoline_code(X25519_CLAMP))
    write_bytes(transport, SENTINEL, bytes([0x00]))

    # DMA flush
    _ = transport.read_memory(SENTINEL, 1)

    # Hijack main_loop -> JMP TRAMPOLINE, high byte only (#477)
    write_bytes(transport, *_hijack_code())

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
