#!/usr/bin/env python3
"""Play a SID file on VICE or Ultimate 64.

Usage:
    python3 examples/play_sid.py --vice mysong.sid
    python3 examples/play_sid.py --u64 192.168.1.81 mysong.sid
    python3 examples/play_sid.py --vice --self-test   # uses built-in test SID

Requires a .sid file (PSID v1/v2 with explicit load_addr; IRQ-driven SIDs
not supported on VICE — use --u64 for those).
"""

from __future__ import annotations

import argparse
import sys
import time

from c64_test_harness import (
    DeviceLock,
    DeviceLockTimeout,
    SidFile,
    Ultimate64Error,
    Ultimate64Transport,
    ViceConfig,
    ViceInstanceManager,
    build_test_psid,
    play_sid,
    stop_sid_vice,
    wait_for_text,
)


def _build_self_test_sid() -> SidFile:
    """Synthesize a test PSID with sentinel+counter at $0360/$0361."""
    init_code = bytes([0xA9, 0x42, 0x8D, 0x60, 0x03])  # LDA #$42 ; STA $0360
    play_code = bytes([0xEE, 0x61, 0x03])              # INC $0361
    raw = build_test_psid(
        load_addr=0x1000, init_code=init_code, play_code=play_code,
        name="SELF-TEST", author="HARNESS", released="2026",
    )
    return SidFile.from_bytes(raw)


def _print_sid_info(sid: SidFile) -> None:
    print(f"Name:     {sid.name}")
    print(f"Author:   {sid.author}")
    print(f"Released: {sid.released}")
    print(f"Songs:    {sid.songs} (start song {sid.start_song})")
    print(f"Load:     ${sid.effective_load_addr:04X}")
    print(f"Init:     ${sid.init_addr:04X}")
    print(f"Play:     ${sid.play_addr:04X}")


def _run_vice(sid: SidFile, self_test: bool) -> None:
    config = ViceConfig(warp=False, sound=False)
    with ViceInstanceManager(config=config) as mgr:
        with mgr.instance() as vm:
            transport = vm.transport
            print("Waiting for BASIC READY prompt...")
            wait_for_text(transport, "READY.", timeout=30.0)

            if self_test:
                # Zero sentinels so we can verify init/play ran.
                transport.write_memory(0x0360, bytes([0x00, 0x00]))

            print(f"Playing SID (song 0)...")
            play_sid(transport, sid, song=0)

            if self_test:
                # Report counter increments every 100ms for 3 seconds.
                # Each read_memory auto-pauses the CPU via the binary
                # monitor; we explicitly resume() afterward so the
                # IRQ-driven play routine keeps bumping the counter.
                print("Sampling $0360/$0361 every 100ms for 3s:")
                for i in range(30):
                    time.sleep(0.1)
                    mem = transport.read_memory(0x0360, 2)
                    transport.resume()
                    print(
                        f"  t={i*0.1:>4.1f}s  "
                        f"$0360=${mem[0]:02X}  $0361=${mem[1]:02X}"
                    )
                # One final read after a longer resumed window — if
                # the IRQ is driving play, the counter should have moved.
                time.sleep(0.5)
                mem = transport.read_memory(0x0360, 2)
                print(
                    f"  final (after +0.5s): "
                    f"$0360=${mem[0]:02X}  $0361=${mem[1]:02X}"
                )
            else:
                print("Playing for 3 seconds...")
                time.sleep(3.0)

            print("Stopping playback (restoring KERNAL IRQ vector)...")
            stop_sid_vice(transport)
            print("Done.")


def _run_u64(host: str, sid: SidFile) -> None:
    # Queue access via DeviceLock so multiple agents sharing one U64 don't
    # collide. acquire_or_raise gives a structured DeviceLockTimeout with a
    # diagnosed-state message (queued / wedged / unreachable).
    lock = DeviceLock(host)
    try:
        lock.acquire_or_raise(timeout=60.0)
    except DeviceLockTimeout as exc:
        print(f"Skipping U64 path: {exc}")
        return
    try:
        transport = Ultimate64Transport(host=host, timeout=8.0)
        try:
            print(f"Playing SID on Ultimate 64 at {host}...")
            try:
                play_sid(transport, sid, song=0)
            except Ultimate64Error as exc:
                # Ultimate64Client.sid_play targets /v1/runners:sid_play + PUT,
                # but firmware 3.14 exposes /v1/runners:sidplay + POST. If we
                # hit the 404 we report it clearly rather than silently failing.
                print(f"HTTP error from device: {exc}")
                print(
                    "Note: Ultimate64Client.sid_play targets an endpoint that "
                    "differs from firmware 3.14's REST API — this needs a client "
                    "fix to switch to POST /v1/runners:sidplay."
                )
                return
            print("Playing for 5 seconds...")
            time.sleep(5.0)
            print("Resetting device to stop audio...")
            transport.client.reset()
            print("Done.")
        finally:
            transport.close()
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Play a SID file on VICE or Ultimate 64.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--vice", action="store_true",
        help="Launch VICE via ViceInstanceManager and play there.",
    )
    group.add_argument(
        "--u64", metavar="HOST",
        help="Connect to an Ultimate 64 at HOST and play there.",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Use a built-in test SID (no file needed).",
    )
    parser.add_argument(
        "sid_path", nargs="?",
        help="Path to .sid file (omit when --self-test is used).",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        sid = _build_self_test_sid()
    elif args.sid_path:
        sid = SidFile.load(args.sid_path)
    else:
        parser.error("must provide a .sid file path or --self-test")
        return 2

    _print_sid_info(sid)
    print()

    if args.vice:
        _run_vice(sid, self_test=args.self_test)
    else:
        _run_u64(args.u64, sid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
