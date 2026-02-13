#!/usr/bin/env python3
"""Demo: load 6502 code into VICE memory, execute it, and verify results.

Demonstrates the full workflow without any PRG file:
  - Load machine code directly into RAM
  - Execute subroutines via jsr()
  - Modify data between runs
  - Patch code at runtime

Requires ``x64sc`` on PATH.
"""

from c64_test_harness import (
    ViceProcess,
    ViceConfig,
    ViceTransport,
    wait_for_text,
    load_code,
    jsr,
    read_bytes,
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

passed = 0
failed = 0


def check(name: str, expected: int, actual: int) -> None:
    global passed, failed
    if actual == expected:
        print(f"{name} PASS: result = {actual}")
        passed += 1
    else:
        print(f"{name} FAIL: expected {expected}, got {actual}")
        failed += 1


config = ViceConfig(warp=True, sound=False)

with ViceProcess(config) as vice:
    vice.start()
    transport = ViceTransport(port=config.port)

    # Wait for BASIC prompt
    wait_for_text(transport, "READY.", timeout=30)
    print("C64 booted, BASIC READY.")

    # Load subroutine into RAM
    load_code(transport, CODE_ADDR, CODE)
    print(f"Loaded {len(CODE)} bytes at ${CODE_ADDR:04X}")

    # --- Test 1: 42 * 2 = 84 ---
    transport.write_memory( INPUT_ADDR, bytes([42]))
    jsr(transport, CODE_ADDR)
    result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
    check("Test 1", 84, result)

    # --- Test 2: 100 * 2 = 200 ---
    transport.write_memory( INPUT_ADDR, bytes([100]))
    jsr(transport, CODE_ADDR)
    result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
    check("Test 2", 200, result)

    # --- Test 3: Patch ASL ($0A) -> LSR ($4A), 84 / 2 = 42 ---
    transport.write_memory( CODE_ADDR + 3, bytes([0x4A]))  # patch opcode
    transport.write_memory( INPUT_ADDR, bytes([84]))
    jsr(transport, CODE_ADDR)
    result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
    check("Test 3", 42, result)

    # --- Test 4: Edge case — 0 / 2 = 0 ---
    transport.write_memory( INPUT_ADDR, bytes([0]))
    jsr(transport, CODE_ADDR)
    result = read_bytes(transport, OUTPUT_ADDR, 1)[0]
    check("Test 4", 0, result)

    print()
    if failed == 0:
        print("All tests passed!")
    else:
        print(f"{failed} test(s) failed, {passed} passed.")
