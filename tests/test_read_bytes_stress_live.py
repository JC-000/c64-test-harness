"""Stress reproducer for GitHub issue #88: flaky ``read_bytes``.

Issue #88 reports that `c64_test_harness.read_bytes` occasionally returns
a corrupt 32-byte payload during long, complex test flows that mix
``jsr()``, ``read_bytes()``, and ``write_bytes()`` heavily. The mismatch
pattern (mostly +/-1 in alternating bytes, with one +/-0x80 outlier) is
too clean to be random ISR/keystroke corruption. The reporter's tight
1000-iter `write_bytes -> read_bytes` loop (`tools/diag_read_consistency.py`
in c64-nist-curves) did NOT reproduce, so this stress test deliberately
varies the access pattern to maximise the chance of triggering whatever
race the long-haul flow exposes.

Variants exercised in a single test, sequentially against one VICE
instance:

  A. Tight `write_bytes -> read_bytes` of 32-byte random payloads.
     (Baseline: should mirror reporter's negative result.)
  B. Mixed read sizes (1, 4, 8, 16, 32 bytes) against a precomputed
     reference page so a partial-byte glitch is detectable.
  C. Heavy event traffic: write -> set_checkpoint -> read -> delete_checkpoint.
     Targets hypothesis #1 (fragmented mem-get response when async events
     arrive in the receive stream).
  D. Full mimic of the failing flow: write A and B at distinct addrs,
     jsr() to a routine we install (a simple "copy 32 bytes from src to
     dst" loop), read result, compare. The jsr() path drives checkpoints
     and STOPPED events between every read, which is the exact cocktail
     suggested by the reporter as the trigger.

On any mismatch the test re-reads the same address immediately and
records both reads. If `read1 != read2` and one of them matches the
expected value, that confirms the harness-side flake.

Gating: opt-in via env var ``READ_BYTES_STRESS=1``. Skipped when unset.
Iteration counts can be tuned via:
  - ``READ_BYTES_STRESS_ITERS_A`` (default 5000)
  - ``READ_BYTES_STRESS_ITERS_B`` (default 5000)
  - ``READ_BYTES_STRESS_ITERS_C`` (default 3000)
  - ``READ_BYTES_STRESS_ITERS_D`` (default 3000)
  - ``READ_BYTES_STRESS_WALL_CAP_S`` (default 1200; hard wall-clock cap)
"""
from __future__ import annotations

import os
import random
import shutil
import time
from dataclasses import dataclass

import pytest

from c64_test_harness import (
    ViceConfig,
    ViceInstanceManager,
    read_bytes,
    write_bytes,
    set_breakpoint,
    delete_breakpoint,
    jsr,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("READ_BYTES_STRESS"),
    reason="long-running stress reproducer; opt-in with READ_BYTES_STRESS=1",
)


@dataclass
class Mismatch:
    variant: str
    iteration: int
    addr: int
    length: int
    expected: bytes
    got1: bytes
    got2: bytes  # immediate re-read

    def diff_positions(self) -> list[tuple[int, int, int]]:
        n = min(len(self.expected), len(self.got1))
        return [
            (i, self.expected[i], self.got1[i])
            for i in range(n)
            if self.expected[i] != self.got1[i]
        ]

    def render(self) -> str:
        diffs = self.diff_positions()
        diff_summary = ", ".join(
            f"b{i}: exp={e:#04x} got={g:#04x} d={(g - e) & 0xff:+d}"
            for i, e, g in diffs[:16]
        )
        if len(diffs) > 16:
            diff_summary += f", ... ({len(diffs)} total)"
        return (
            f"[{self.variant}] iter={self.iteration} addr=${self.addr:04X} "
            f"len={self.length}\n"
            f"  expected = {self.expected.hex()}\n"
            f"  got1     = {self.got1.hex()}\n"
            f"  got2     = {self.got2.hex()}\n"
            f"  re-read agrees with got1?  {self.got1 == self.got2}\n"
            f"  re-read agrees with expected? {self.got2 == self.expected}\n"
            f"  diff(got1 vs expected) = {diff_summary}"
        )


def _check(
    variant: str,
    iteration: int,
    transport,
    addr: int,
    length: int,
    expected: bytes,
    bucket: list[Mismatch],
) -> None:
    """Read addr/length and compare; on mismatch, immediately re-read."""
    got1 = read_bytes(transport, addr, length)
    if got1 != expected[:length]:
        # Re-read: critical diagnostic per the issue's recommendation.
        got2 = read_bytes(transport, addr, length)
        bucket.append(
            Mismatch(variant, iteration, addr, length, expected[:length], got1, got2)
        )


def _variant_a_tight_write_read(
    transport,
    rng: random.Random,
    iterations: int,
    addr: int,
    bucket: list[Mismatch],
    wall_deadline: float,
) -> int:
    """Tight write->read of 32-byte random payloads (mirrors reporter's diag)."""
    done = 0
    for i in range(iterations):
        if time.monotonic() >= wall_deadline:
            break
        payload = bytes(rng.randint(0, 255) for _ in range(32))
        write_bytes(transport, addr, payload)
        _check("A", i, transport, addr, 32, payload, bucket)
        done = i + 1
    return done


def _variant_b_mixed_sizes(
    transport,
    rng: random.Random,
    iterations: int,
    base_addr: int,
    bucket: list[Mismatch],
    wall_deadline: float,
) -> int:
    """Alternating sizes (1, 4, 8, 16, 32) into adjacent slots within a page."""
    sizes = (1, 4, 8, 16, 32)
    page = bytearray(rng.randint(0, 255) for _ in range(256))
    write_bytes(transport, base_addr, bytes(page))
    done = 0
    for i in range(iterations):
        if time.monotonic() >= wall_deadline:
            break
        # Pick a random slot inside the page so unaligned and aligned reads mix.
        slot_off = rng.randint(0, 256 - 32)
        size = rng.choice(sizes)
        # Sometimes rewrite the slot first (to mimic write+read interleaving).
        if rng.random() < 0.5:
            new_chunk = bytes(rng.randint(0, 255) for _ in range(size))
            write_bytes(transport, base_addr + slot_off, new_chunk)
            page[slot_off:slot_off + size] = new_chunk
        expected = bytes(page[slot_off:slot_off + size])
        _check("B", i, transport, base_addr + slot_off, size, expected, bucket)
        done = i + 1
    return done


def _variant_c_event_traffic(
    transport,
    rng: random.Random,
    iterations: int,
    addr: int,
    bucket: list[Mismatch],
    wall_deadline: float,
) -> int:
    """Heavy checkpoint set/delete between reads.

    Each iteration:
      - write 32 random bytes
      - set 1-3 checkpoints at innocuous addresses (no STOPPED expected, but
        the checkpoint-info responses go through the same recv path)
      - read back
      - delete the checkpoints

    If the bug is a fragmented mem-get response when async events arrive,
    this is the cocktail most likely to expose it.
    """
    done = 0
    # Innocuous checkpoint targets: areas the CPU will not normally touch
    # during BASIC idle, so we don't expect STOPPED to actually fire.
    checkpoint_addrs = [0x9000, 0x9100, 0x9200, 0x9300, 0x9400]
    for i in range(iterations):
        if time.monotonic() >= wall_deadline:
            break
        payload = bytes(rng.randint(0, 255) for _ in range(32))
        write_bytes(transport, addr, payload)

        bp_count = rng.randint(1, 3)
        bps: list[int] = []
        for _ in range(bp_count):
            bp_addr = rng.choice(checkpoint_addrs)
            bp_id = set_breakpoint(transport, bp_addr)
            bps.append(bp_id)

        _check("C", i, transport, addr, 32, payload, bucket)

        for bp_id in bps:
            delete_breakpoint(transport, bp_id)
        done = i + 1
    return done


# A "memcpy 32 bytes" routine in 6502 assembly. Self-contained at $C100;
# expects source pointer at $FB/$FC and destination pointer at $FD/$FE.
#
#       LDY #$00
# loop: LDA ($FB),Y
#       STA ($FD),Y
#       INY
#       CPY #$20
#       BNE loop
#       RTS
_MEMCPY32_CODE = bytes([
    0xA0, 0x00,           # LDY #$00
    0xB1, 0xFB,           # LDA ($FB),Y
    0x91, 0xFD,           # STA ($FD),Y
    0xC8,                 # INY
    0xC0, 0x20,           # CPY #$20
    0xD0, 0xF7,           # BNE loop  (-9)
    0x60,                 # RTS
])
_MEMCPY32_ADDR = 0xC100


def _variant_d_jsr_roundtrip(
    transport,
    rng: random.Random,
    iterations: int,
    src_addr: int,
    dst_addr: int,
    bucket: list[Mismatch],
    wall_deadline: float,
) -> int:
    """Full mimic of the failing flow.

    write src; set ZP pointers; jsr(memcpy32); read dst; compare.

    This drives a STOPPED event (the jsr trampoline checkpoint), then a
    delete_breakpoint, then a read_bytes — the exact ordering that the
    issue's hypothesis #1 implicates.
    """
    # Install the routine once.
    write_bytes(transport, _MEMCPY32_ADDR, _MEMCPY32_CODE)

    done = 0
    for i in range(iterations):
        if time.monotonic() >= wall_deadline:
            break
        payload = bytes(rng.randint(0, 255) for _ in range(32))
        write_bytes(transport, src_addr, payload)

        # ZP pointers: $FB/$FC = src, $FD/$FE = dst
        write_bytes(transport, 0xFB, bytes([
            src_addr & 0xFF, (src_addr >> 8) & 0xFF,
            dst_addr & 0xFF, (dst_addr >> 8) & 0xFF,
        ]))

        jsr(transport, _MEMCPY32_ADDR, timeout=10.0)

        _check("D", i, transport, dst_addr, 32, payload, bucket)
        done = i + 1
    return done


def test_read_bytes_stress_for_issue_88(capsys):
    """Stress reproducer for issue #88. See module docstring."""
    if shutil.which("x64sc") is None:
        pytest.skip("x64sc not on PATH")

    iters_a = int(os.environ.get("READ_BYTES_STRESS_ITERS_A", "5000"))
    iters_b = int(os.environ.get("READ_BYTES_STRESS_ITERS_B", "5000"))
    iters_c = int(os.environ.get("READ_BYTES_STRESS_ITERS_C", "3000"))
    iters_d = int(os.environ.get("READ_BYTES_STRESS_ITERS_D", "3000"))
    wall_cap_s = float(os.environ.get("READ_BYTES_STRESS_WALL_CAP_S", "1200"))

    rng = random.Random(0xC0DE)
    bucket: list[Mismatch] = []

    cfg = ViceConfig(warp=True, sound=False)
    # Use a port range distinct from the module-scoped binary_transport
    # fixture (6511..6531) and bridge fixtures (6560..6580) to avoid
    # overlap during parallel pytest runs.
    with ViceInstanceManager(
        config=cfg,
        port_range_start=6585,
        port_range_end=6605,
    ) as mgr:
        with mgr.instance() as inst:
            t = inst.transport
            t0 = time.monotonic()
            wall_deadline = t0 + wall_cap_s

            # Wait briefly for BASIC idle so the CPU isn't churning init.
            # ViceInstanceManager pauses VICE on connect already (the binary
            # monitor enters with the CPU stopped). That's actually the safe
            # state for our stress test: writes/reads against a paused CPU.
            # No `resume()` here — we want a quiet bus.

            phase_a_t0 = time.monotonic()
            done_a = _variant_a_tight_write_read(
                t, rng, iters_a, 0xC000, bucket, wall_deadline,
            )
            phase_a_dt = time.monotonic() - phase_a_t0
            mism_after_a = len(bucket)

            phase_b_t0 = time.monotonic()
            done_b = _variant_b_mixed_sizes(
                t, rng, iters_b, 0xC400, bucket, wall_deadline,
            )
            phase_b_dt = time.monotonic() - phase_b_t0
            mism_after_b = len(bucket)

            phase_c_t0 = time.monotonic()
            done_c = _variant_c_event_traffic(
                t, rng, iters_c, 0xC800, bucket, wall_deadline,
            )
            phase_c_dt = time.monotonic() - phase_c_t0
            mism_after_c = len(bucket)

            phase_d_t0 = time.monotonic()
            done_d = _variant_d_jsr_roundtrip(
                t, rng, iters_d, 0xC000, 0xC200, bucket, wall_deadline,
            )
            phase_d_dt = time.monotonic() - phase_d_t0
            mism_after_d = len(bucket)

            total_dt = time.monotonic() - t0

    # Always print the per-phase summary so a passing run still gives the
    # supervisor signal about how much we exercised.
    summary = (
        "\n=== read_bytes stress summary (issue #88) ===\n"
        f" Variant A (tight write/read 32B):  iters={done_a}/{iters_a} "
        f"  dt={phase_a_dt:.1f}s  mism={mism_after_a}\n"
        f" Variant B (mixed sizes):           iters={done_b}/{iters_b} "
        f"  dt={phase_b_dt:.1f}s  mism={mism_after_b - mism_after_a}\n"
        f" Variant C (event traffic):         iters={done_c}/{iters_c} "
        f"  dt={phase_c_dt:.1f}s  mism={mism_after_c - mism_after_b}\n"
        f" Variant D (write/jsr/read):        iters={done_d}/{iters_d} "
        f"  dt={phase_d_dt:.1f}s  mism={mism_after_d - mism_after_c}\n"
        f" total wall={total_dt:.1f}s  total mismatches={len(bucket)}"
    )
    print(summary)

    if bucket:
        report = "\n\n".join(m.render() for m in bucket[:10])
        if len(bucket) > 10:
            report += f"\n\n... and {len(bucket) - 10} more mismatches."
        pytest.fail(
            f"Reproduced issue #88: {len(bucket)} mismatch(es).\n{summary}\n\n{report}"
        )
