"""Integration tests for disk I/O via VICE (binary monitor transport).

Validates the full round-trip: create disk images with DiskImage, boot VICE
with the image attached, and verify the C64 can load PRGs and read SEQ files.

Uses BinaryViceTransport (-binarymonitor) for all VICE communication.

The binary monitor protocol uses a persistent TCP connection.  The CPU stays
stopped between commands, so screen polling helpers must call ``resume()``
between reads to let the CPU run and update the screen.

The library's ``wait_for_text()`` now handles this automatically by calling
``transport.resume()`` between polls.  The ``_binary_wait_for_load_complete()``
helper uses a custom condition (checking LOADING+READY sequence) so it
remains local.  ``_binary_send_text()`` resumes the CPU after injecting
keys so BASIC can process them.
"""

from __future__ import annotations

import os
import shutil
import struct
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.disk import DiskImage, FileType
from c64_test_harness.keyboard import send_text
from c64_test_harness.memory import read_bytes, read_word_le, write_bytes
from c64_test_harness.screen import ScreenGrid, wait_for_text
from c64_test_harness.transport import TransportError

from conftest import connect_binary_transport

# Skip entire module if required tools are missing
pytestmark = [
    pytest.mark.skipif(
        shutil.which("x64sc") is None, reason="x64sc not found on PATH"
    ),
    pytest.mark.skipif(
        shutil.which("c1541") is None, reason="c1541 not found on PATH"
    ),
]

# Dynamic port allocation to avoid conflicts when running tests in parallel
_allocator = PortAllocator(port_range_start=6510, port_range_end=6519)

MONITOR_TIMEOUT = 30
TEXT_TIMEOUT = 30


# ======================================================================
# Binary transport helpers
# ======================================================================




def _binary_wait_for_load_complete(
    transport: BinaryViceTransport,
    timeout: float = TEXT_TIMEOUT,
) -> ScreenGrid | None:
    """Wait for a C64 LOAD to complete via binary transport.

    Detects completion by finding "LOADING" followed by "READY." in the
    screen's continuous text.  Resumes the CPU between polls.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            transport.resume()
            time.sleep(2.0)
            grid = ScreenGrid.from_transport(transport)
            text = grid.continuous_text().upper()
            loading_idx = text.find("LOADING")
            if loading_idx >= 0:
                ready_idx = text.find("READY.", loading_idx + 7)
                if ready_idx > loading_idx:
                    return grid
        except Exception:
            time.sleep(2.0)
    return None


def _binary_send_text(transport: BinaryViceTransport, text: str) -> None:
    """Inject text via keyboard and resume CPU to process it.

    The binary Keyboard Feed command queues keys, but the CPU must be
    running to consume them from the buffer.
    """
    send_text(transport, text)
    transport.resume()
    # Give the C64 time to process the keystrokes
    time.sleep(0.5)


# ======================================================================
# Helpers -- generate C64 programs as raw PRG bytes
# ======================================================================

def make_basic_prg(message: str) -> bytes:
    """Build a tokenized BASIC PRG: ``10 PRINT"<message>"``."""
    load_addr = b"\x01\x08"
    line_num = b"\x0a\x00"          # line 10
    print_token = b"\x99"           # PRINT
    quote = b"\x22"
    msg_bytes = message.encode("ascii")
    eol = b"\x00"
    eop = b"\x00\x00"              # end of program

    line_body = line_num + print_token + quote + msg_bytes + quote + eol
    next_ptr = 0x0801 + 2 + len(line_body)
    next_ptr_bytes = struct.pack("<H", next_ptr)

    return load_addr + next_ptr_bytes + line_body + eop


def make_seq_reader_prg() -> bytes:
    """Build a PRG with BASIC stub + 6502 code that reads a SEQ file.

    Interface:
        $033C  -- trigger/status: write $01 to start; reads $FF=done, $FE=error
        $033E  -- 16-bit LE byte count of data read
        $C000+ -- read buffer

    The program reads a SEQ file named "TESTSEQ" from device 8.
    On start, writes "IDLE" to screen at $0400 and polls $033C.
    """
    # BASIC stub: 10 SYS2061
    stub = bytes([
        0x01, 0x08,         # load address $0801
        0x0B, 0x08,         # next line pointer $080B
        0x0A, 0x00,         # line 10
        0x9E,               # SYS token
        0x32, 0x30, 0x36, 0x31,  # "2061"
        0x00,               # end of line
        0x00, 0x00,         # end of program
    ])
    # Machine code starts at $080D = 2061

    # Screen codes for messages
    IDLE_SC = [9, 4, 12, 5]            # IDLE
    DONE_SC = [4, 15, 14, 5]           # DONE
    ERROR_SC = [5, 18, 18, 15, 18]     # ERROR
    BLANK_SC = [32] * 10               # spaces to clear

    fname = list(b"TESTSEQ")

    code: list[int] = []

    def emit(*bs: int) -> None:
        code.extend(bs)

    def current_addr() -> int:
        return 0x080D + len(code)

    # --- entry: init status bytes ---
    emit(0xA9, 0x00)        # LDA #0
    emit(0x8D, 0x3C, 0x03)  # STA $033C
    emit(0x8D, 0x3E, 0x03)  # STA $033E
    emit(0x8D, 0x3F, 0x03)  # STA $033F

    # Write "IDLE" to screen at $0400
    for i, sc in enumerate(IDLE_SC):
        emit(0xA9, sc)
        emit(0x8D, (0x00 + i) & 0xFF, 0x04)

    # --- poll loop ---
    poll_addr = current_addr()
    emit(0xAD, 0x3C, 0x03)  # LDA $033C
    emit(0xC9, 0x01)        # CMP #$01
    emit(0xD0, 0xF9)        # BNE poll  (branch -7 to LDA $033C)

    # Triggered -- reset status and count
    emit(0xA9, 0x00)        # LDA #0
    emit(0x8D, 0x3C, 0x03)  # STA $033C
    emit(0x8D, 0x3E, 0x03)  # STA $033E
    emit(0x8D, 0x3F, 0x03)  # STA $033F

    # Clear screen line 0 (10 chars)
    for i, sc in enumerate(BLANK_SC):
        emit(0xA9, sc)
        emit(0x8D, (0x00 + i) & 0xFF, 0x04)

    # Set up ZP pointer for buffer at $C000
    emit(0xA9, 0x00)        # LDA #$00
    emit(0x85, 0xFB)        # STA $FB
    emit(0xA9, 0xC0)        # LDA #$C0
    emit(0x85, 0xFC)        # STA $FC

    # SETNAM: A=len, X/Y -> filename address
    fname_addr_placeholder = len(code)
    emit(0xA9, len(fname))  # LDA #<len>
    emit(0xA2, 0x00)        # LDX #<fname (placeholder)
    emit(0xA0, 0x00)        # LDY #>fname (placeholder)
    emit(0x20, 0xBD, 0xFF)  # JSR SETNAM

    # SETLFS: file#1, device 8, secondary 2 (data channel for SEQ read)
    emit(0xA9, 0x01)        # LDA #1  (file number)
    emit(0xA2, 0x08)        # LDX #8  (device)
    emit(0xA0, 0x02)        # LDY #2  (secondary address >= 2 for SEQ)
    emit(0x20, 0xBA, 0xFF)  # JSR SETLFS

    # OPEN
    emit(0x20, 0xC0, 0xFF)  # JSR OPEN
    error_branch_1 = len(code)
    emit(0xB0, 0x00)        # BCS error (placeholder)

    # CHKIN(1)
    emit(0xA2, 0x01)        # LDX #1
    emit(0x20, 0xC6, 0xFF)  # JSR CHKIN
    error_close_branch = len(code)
    emit(0xB0, 0x00)        # BCS error_close (placeholder)

    # --- read loop ---
    # On C64, CHRIN returns the last valid byte when READST signals EOF.
    # We must store the byte FIRST, then check READST.
    read_loop_addr = current_addr()
    emit(0x20, 0xCF, 0xFF)  # JSR CHRIN  -> byte in A

    # Store byte via ZP pointer
    emit(0xA0, 0x00)        # LDY #0
    emit(0x91, 0xFB)        # STA ($FB),Y

    # Increment 16-bit count at $033E
    emit(0xEE, 0x3E, 0x03)  # INC $033E
    emit(0xD0, 0x03)        # BNE +3
    emit(0xEE, 0x3F, 0x03)  # INC $033F

    # Increment ZP pointer
    emit(0xE6, 0xFB)        # INC $FB
    emit(0xD0, 0x02)        # BNE +2
    emit(0xE6, 0xFC)        # INC $FC

    # Check READST for EOF
    emit(0x20, 0xB7, 0xFF)  # JSR READST
    emit(0x29, 0x40)        # AND #$40 (EOF bit)
    done_branch = len(code)
    emit(0xD0, 0x00)        # BNE done (placeholder)

    # Loop back
    rl_lo = read_loop_addr & 0xFF
    rl_hi = (read_loop_addr >> 8) & 0xFF
    emit(0x4C, rl_lo, rl_hi)  # JMP read_loop

    # --- done ---
    done_addr_offset = len(code)

    # CLRCHN + CLOSE
    emit(0x20, 0xCC, 0xFF)  # JSR CLRCHN
    emit(0xA9, 0x01)        # LDA #1
    emit(0x20, 0xC3, 0xFF)  # JSR CLOSE

    # Write "DONE" to screen
    for i, sc in enumerate(DONE_SC):
        emit(0xA9, sc)
        emit(0x8D, (0x00 + i) & 0xFF, 0x04)

    # Set status $FF (success)
    emit(0xA9, 0xFF)
    emit(0x8D, 0x3C, 0x03)  # STA $033C

    # JMP poll
    p_lo = poll_addr & 0xFF
    p_hi = (poll_addr >> 8) & 0xFF
    emit(0x4C, p_lo, p_hi)

    # --- error_close: close file first, then fall through to error ---
    error_close_addr_offset = len(code)
    emit(0x20, 0xCC, 0xFF)  # JSR CLRCHN
    emit(0xA9, 0x01)        # LDA #1
    emit(0x20, 0xC3, 0xFF)  # JSR CLOSE

    # --- error ---
    error_addr_offset = len(code)
    for i, sc in enumerate(ERROR_SC):
        emit(0xA9, sc)
        emit(0x8D, (0x00 + i) & 0xFF, 0x04)

    emit(0xA9, 0xFE)
    emit(0x8D, 0x3C, 0x03)  # STA $033C
    emit(0x4C, p_lo, p_hi)  # JMP poll

    # --- filename data ---
    fname_offset = len(code)
    code.extend(fname)

    # --- Fix up branch targets ---
    _fixup_branch(code, error_branch_1, error_addr_offset)
    _fixup_branch(code, error_close_branch, error_close_addr_offset)
    _fixup_branch(code, done_branch, done_addr_offset)

    # Fix up SETNAM filename address (patch LDX/LDY operands, not opcodes)
    fname_abs = 0x080D + fname_offset
    code[fname_addr_placeholder + 3] = fname_abs & 0xFF       # LDX operand
    code[fname_addr_placeholder + 5] = (fname_abs >> 8) & 0xFF  # LDY operand

    return stub + bytes(code)


def _fixup_branch(code: list[int], branch_offset: int, target_offset: int) -> None:
    """Fix a relative branch at branch_offset+1 to point to target_offset."""
    displacement = target_offset - (branch_offset + 2)
    if displacement < 0:
        displacement = displacement & 0xFF
    code[branch_offset + 1] = displacement


# ======================================================================
# TestPrgLoad -- load and run BASIC programs from disk
# ======================================================================

class TestPrgLoad:
    """Load a PRG from a D64 in VICE, RUN it, verify screen output."""

    @pytest.mark.parametrize("signature", [
        "DISK TEST AAA",
        "DISK TEST BBB",
        "DISK TEST CCC",
    ])
    def test_load_and_run_prg(self, tmp_path: Path, signature: str) -> None:
        prg_data = make_basic_prg(signature)
        prg_path = tmp_path / "testprg.prg"
        prg_path.write_bytes(prg_data)

        disk = DiskImage.create(tmp_path / "test.d64")
        # c1541 stores uppercase ASCII as shifted PETSCII ($C1-$DA), but the
        # C64 keyboard produces unshifted PETSCII ($41-$5A).  Use lowercase
        # ASCII so c1541 writes unshifted codes that match keyboard input.
        disk.write_file(prg_path, "testprg")

        port = _allocator.allocate()
        reservation = _allocator.take_socket(port)
        if reservation is not None:
            reservation.close()
        try:
            config = ViceConfig(
                port=port,
                disk_image=disk,
                drive_unit=8,
            )

            with ViceProcess(config) as vice:
                transport = connect_binary_transport(port, proc=vice)
                try:
                    # Wait for BASIC READY prompt
                    grid = wait_for_text(
                        transport, "READY.", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, "BASIC READY prompt not found"

                    # LOAD from disk -- wait for "LOADING" followed by "READY."
                    _binary_send_text(transport, 'LOAD"TESTPRG",8\r')
                    grid = _binary_wait_for_load_complete(transport)
                    assert grid is not None, "LOAD did not complete"

                    # RUN
                    _binary_send_text(transport, "RUN\r")
                    grid = wait_for_text(
                        transport, signature, timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, \
                        f"Signature '{signature}' not found on screen"
                finally:
                    transport.close()
        finally:
            _allocator.release(port)


# ======================================================================
# TestSeqRead -- read a SEQ file from disk via machine code
# ======================================================================

class TestSeqRead:
    """Write a SEQ file to D64, read it back via C64 machine code."""

    def test_read_seq_file(self, tmp_path: Path) -> None:
        original_data = os.urandom(200)

        seq_path = tmp_path / "seqdata.bin"
        seq_path.write_bytes(original_data)

        reader_prg = make_seq_reader_prg()
        reader_path = tmp_path / "reader.prg"
        reader_path.write_bytes(reader_prg)

        disk = DiskImage.create(tmp_path / "test.d64")
        disk.write_file(seq_path, "testseq", FileType.SEQ)
        disk.write_file(reader_path, "reader")

        port = _allocator.allocate()
        reservation = _allocator.take_socket(port)
        if reservation is not None:
            reservation.close()
        try:
            config = ViceConfig(
                port=port,
                disk_image=disk,
                drive_unit=8,
            )

            with ViceProcess(config) as vice:
                transport = connect_binary_transport(port, proc=vice)
                try:
                    grid = wait_for_text(
                        transport, "READY.", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, "BASIC READY prompt not found"

                    # Load reader PRG
                    _binary_send_text(transport, 'LOAD"READER",8\r')
                    grid = _binary_wait_for_load_complete(transport)
                    assert grid is not None, "LOAD did not complete"

                    # Run reader -- it displays "IDLE" when ready
                    _binary_send_text(transport, "RUN\r")
                    grid = wait_for_text(
                        transport, "IDLE", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, \
                        "Reader program did not display IDLE on screen"

                    # Trigger SEQ read -- write flag then resume CPU
                    write_bytes(transport, 0x033C, [0x01])
                    transport.resume()

                    grid = wait_for_text(
                        transport, "DONE", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, \
                        "DONE not found -- SEQ read may have failed"

                    # Verify byte count and buffer contents
                    count = read_word_le(transport, 0x033E)
                    assert count == len(original_data), \
                        f"Byte count mismatch: got {count}, expected {len(original_data)}"

                    buffer = read_bytes(transport, 0xC000, count)
                    assert buffer == original_data
                finally:
                    transport.close()
        finally:
            _allocator.release(port)


# ======================================================================
# TestSeqModify -- modify SEQ data on disk, re-read in VICE
# ======================================================================

class TestSeqModify:
    """Modify a SEQ file on disk image, verify C64 reads the updated data."""

    @pytest.mark.parametrize("offset,length", [
        (0, 10),       # modify beginning
        (95, 20),      # modify middle
        (180, 20),     # modify near end
    ])
    def test_modify_seq_and_reread(
        self, tmp_path: Path, offset: int, length: int
    ) -> None:
        base_data = os.urandom(200)

        modified = bytearray(base_data)
        modified[offset:offset + length] = os.urandom(length)
        modified = bytes(modified)

        seq_path = tmp_path / "seqdata.bin"
        seq_path.write_bytes(modified)

        reader_prg = make_seq_reader_prg()
        reader_path = tmp_path / "reader.prg"
        reader_path.write_bytes(reader_prg)

        disk = DiskImage.create(tmp_path / "test.d64")
        disk.write_file(seq_path, "testseq", FileType.SEQ)
        disk.write_file(reader_path, "reader")

        port = _allocator.allocate()
        reservation = _allocator.take_socket(port)
        if reservation is not None:
            reservation.close()
        try:
            config = ViceConfig(
                port=port,
                disk_image=disk,
                drive_unit=8,
            )

            with ViceProcess(config) as vice:
                transport = connect_binary_transport(port, proc=vice)
                try:
                    grid = wait_for_text(
                        transport, "READY.", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, "BASIC READY prompt not found"

                    _binary_send_text(transport, 'LOAD"READER",8\r')
                    grid = _binary_wait_for_load_complete(transport)
                    assert grid is not None, "LOAD did not complete"

                    _binary_send_text(transport, "RUN\r")
                    grid = wait_for_text(
                        transport, "IDLE", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, \
                        "Reader program did not display IDLE on screen"

                    # Trigger SEQ read -- write flag then resume CPU
                    write_bytes(transport, 0x033C, [0x01])
                    transport.resume()

                    grid = wait_for_text(
                        transport, "DONE", timeout=TEXT_TIMEOUT, verbose=False,
                    )
                    assert grid is not None, \
                        "DONE not found -- SEQ read may have failed"

                    count = read_word_le(transport, 0x033E)
                    assert count == len(modified), \
                        f"Byte count mismatch: got {count}, expected {len(modified)}"

                    buffer = read_bytes(transport, 0xC000, count)
                    assert buffer == modified
                    assert buffer != base_data, \
                        "Buffer matches unmodified base data"
                finally:
                    transport.close()
        finally:
            _allocator.release(port)
