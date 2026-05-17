#!/usr/bin/env python3
"""Demo: load 6502 code into VICE memory, execute it, and verify results.

Demonstrates the full workflow without any PRG file:
  - Load machine code directly into RAM
  - Execute subroutines via jsr()
  - Modify data between runs
  - Patch code at runtime

Requires ``x64sc`` on PATH.
"""

import sys

from c64_test_harness import (
    ViceConfig,
    ViceInstanceManager,
    load_code,
    jsr,
    read_bytes,
    write_bytes,
    wait_for_text,
)

# 6502 program (8 bytes at $C000):
#   LDA $C100      ; AD 00 C1  — load input byte
#   ASL A          ; 0A        — multiply by 2
#   STA $C101      ; 8D 01 C1  — store result
#   RTS            ; 60
CODE = bytes([0xAD, 0x00, 0xC1, 0x0A, 0x8D, 0x01, 0xC1, 0x60])
CODE_ADDR = 0xC000
INPUT_ADDR = 0xC100
OUTPUT_ADDR = 0xC101

# Default per-jsr timeout. Required at warp=True: without it, a missed
# breakpoint event would hang the test indefinitely.
JSR_TIMEOUT = 15.0


def check(name: str, expected: int, actual: int, counters: dict) -> None:
    if actual == expected:
        print(f"{name} PASS: result = {actual}")
        counters["passed"] += 1
    else:
        print(f"{name} FAIL: expected {expected}, got {actual}")
        counters["failed"] += 1


def main() -> int:
    counters = {"passed": 0, "failed": 0}

    config = ViceConfig(warp=True, sound=False)

    with ViceInstanceManager(config=config) as mgr:
        inst = mgr.acquire()
        print(f"VICE PID={inst.pid}, port={inst.port}")

        transport = inst.transport

        # Wait for BASIC prompt
        grid = wait_for_text(transport, "READY.", timeout=30.0)
        if grid is None:
            print("FATAL: BASIC READY prompt did not appear")
            return 1
        print("C64 booted, BASIC READY.")

        # Safety: write JMP $0339 at $0339 so CPU loops harmlessly
        # after jsr() returns (prevents crash when BASIC ROM is banked out).
        write_bytes(transport, 0x0339, bytes([0x4C, 0x39, 0x03]))

        # Load subroutine into RAM
        load_code(transport, CODE_ADDR, CODE)
        print(f"Loaded {len(CODE)} bytes at ${CODE_ADDR:04X}")

        # --- Test 1: 42 * 2 = 84 ---
        write_bytes(transport, INPUT_ADDR, bytes([42]))
        jsr(transport, CODE_ADDR, timeout=JSR_TIMEOUT)
        result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
        check("Test 1", 84, result, counters)

        # --- Test 2: 100 * 2 = 200 ---
        write_bytes(transport, INPUT_ADDR, bytes([100]))
        jsr(transport, CODE_ADDR, timeout=JSR_TIMEOUT)
        result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
        check("Test 2", 200, result, counters)

        # --- Test 3: Patch ASL ($0A) -> LSR ($4A), 84 / 2 = 42 ---
        write_bytes(transport, CODE_ADDR + 3, bytes([0x4A]))  # patch opcode
        write_bytes(transport, INPUT_ADDR, bytes([84]))
        jsr(transport, CODE_ADDR, timeout=JSR_TIMEOUT)
        result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
        check("Test 3", 42, result, counters)

        # --- Test 4: Edge case — 0 / 2 = 0 ---
        write_bytes(transport, INPUT_ADDR, bytes([0]))
        jsr(transport, CODE_ADDR, timeout=JSR_TIMEOUT)
        result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
        check("Test 4", 0, result, counters)

        mgr.release(inst)

    print()
    if counters["failed"] == 0:
        print("All tests passed!")
        return 0
    print(f"{counters['failed']} test(s) failed, {counters['passed']} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
