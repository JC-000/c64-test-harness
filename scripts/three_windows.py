#!/usr/bin/env python3
"""Launch three VICE windows and write user-chosen words into screen memory.

Uses ViceInstanceManager to start 3 concurrent VICE instances, then
enters an interactive loop where the user types a word and it appears
directly in each emulator's screen RAM ($0400).
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import time

from c64_test_harness import ScreenGrid, wait_for_text, write_bytes
from c64_test_harness.backends.vice_lifecycle import ViceConfig
from c64_test_harness.backends.vice_manager import ViceInstanceManager

# ---------------------------------------------------------------------------
# Screen memory layout
# ---------------------------------------------------------------------------

SCREEN_BASE = 0x0400
COLOR_BASE = 0xD800
COLS = 40
ROWS = 25

# C64 color codes
COLORS = [1, 7, 3]  # white, yellow, cyan — one per instance
BORDER_COLOR_ADDR = 0xD020
BG_COLOR_ADDR = 0xD021


def ascii_to_screen_codes(text: str) -> list[int]:
    """Convert ASCII text to C64 screen codes."""
    result = []
    for ch in text.upper():
        c = ord(ch)
        if ord("A") <= c <= ord("Z"):
            result.append(c - ord("A") + 1)
        elif ord("0") <= c <= ord("9"):
            result.append(c - ord("0") + 0x30)
        elif ch == " ":
            result.append(0x20)
        elif ch == "!":
            result.append(0x21)
        elif ch == "?":
            result.append(0x3F)
        elif ch == ".":
            result.append(0x2E)
        elif ch == "-":
            result.append(0x2D)
        elif ch == "@":
            result.append(0x00)
        else:
            result.append(0x20)  # fallback to space
    return result


def clear_screen(transport, color: int = 0) -> None:
    """Fill screen RAM with spaces and set uniform color."""
    spaces = bytes([0x20] * (COLS * ROWS))
    write_bytes(transport, SCREEN_BASE, spaces)
    colors = bytes([color] * (COLS * ROWS))
    write_bytes(transport, COLOR_BASE, colors)


def write_centered(transport, row: int, text: str, color: int = 1) -> None:
    """Write text centered on the given row in screen memory."""
    codes = ascii_to_screen_codes(text)
    col = max(0, (COLS - len(codes)) // 2)
    addr = SCREEN_BASE + row * COLS + col
    color_addr = COLOR_BASE + row * COLS + col
    write_bytes(transport, addr, bytes(codes))
    write_bytes(transport, color_addr, bytes([color] * len(codes)))


def draw_banner(transport, instance_id: int, color: int) -> None:
    """Draw a banner identifying this instance."""
    # Set border and background
    write_bytes(transport, BG_COLOR_ADDR, bytes([0]))  # black background
    write_bytes(transport, BORDER_COLOR_ADDR, bytes([color]))

    clear_screen(transport, color)
    write_centered(transport, 2, f"INSTANCE {instance_id}", color)
    write_centered(transport, 3, f"PORT {transport.port}", color)
    write_centered(transport, 5, "--- READY ---", color)
    write_centered(transport, 24, "WAITING FOR INPUT...", color)


def write_word_to_all(instances, word: str, colors: list[int]) -> None:
    """Write the user's word to all 3 instances."""
    for i, inst in enumerate(instances):
        color = colors[i]
        # Clear the message area (rows 8-18)
        for row in range(8, 19):
            addr = SCREEN_BASE + row * COLS
            write_bytes(inst.transport, addr, bytes([0x20] * COLS))

        # Write the word large and centered
        write_centered(inst.transport, 12, word, color)

        # Add instance-specific decoration
        deco = f"-- INSTANCE {i} --"
        write_centered(inst.transport, 10, deco, color)
        write_centered(inst.transport, 14, deco, color)

        # Update status line
        row_addr = SCREEN_BASE + 24 * COLS
        write_bytes(inst.transport, row_addr, bytes([0x20] * COLS))
        write_centered(inst.transport, 24, f"SHOWING - {word}", color)


def main() -> int:
    print("=== Three VICE Windows Demo ===")
    print("Launching 3 VICE instances...\n")

    config = ViceConfig(warp=True, ntsc=True, sound=False)

    with ViceInstanceManager(
        config=config,
        port_range_start=6510,
        port_range_end=6513,
    ) as mgr:
        instances = []
        for i in range(3):
            inst = mgr.acquire()
            instances.append(inst)
            print(f"  Instance {i}: port {inst.port}, PID {inst.process.pid}")

        # Wait for BASIC READY prompt on each
        print("\nWaiting for VICE to boot...")
        for i, inst in enumerate(instances):
            grid = wait_for_text(inst.transport, "READY.", timeout=30, verbose=False)
            if grid is None:
                print(f"  FATAL: Instance {i} did not reach READY prompt")
                return 1
            print(f"  Instance {i}: booted")

        # Draw banners
        print("\nDrawing banners...")
        for i, inst in enumerate(instances):
            draw_banner(inst.transport, i, COLORS[i])
        print("  Done — check your 3 VICE windows!\n")

        # Interactive loop
        print("Type a word to display in all 3 windows.")
        print("Type 'quit' or Ctrl+C to exit.\n")

        try:
            while True:
                try:
                    word = input("Enter a word> ").strip()
                except EOFError:
                    break
                if not word or word.lower() == "quit":
                    break
                if len(word) > 38:
                    print("  (truncated to 38 chars)")
                    word = word[:38]
                write_word_to_all(instances, word, COLORS)
                print(f"  Written '{word.upper()}' to all 3 instances")
        except KeyboardInterrupt:
            print("\n")

        print("Shutting down VICE instances...")
        for inst in instances:
            mgr.release(inst)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
