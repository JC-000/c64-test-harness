"""Tests for ``run_prg_via_sys`` and its BASIC-stub parser (issue #211).

Why this load path exists at all: on the U64, ``client.run_prg()``'s
DMA-load drops an external cartridge.  A program it loads reads the whole
``$DE00`` window as zeros even while ``Cartridge Preference`` still says
``External``, so a cartridge driver fails at its first register read --
stock ip65 reports ``INIT DRIVER: FAILED``.  Writing the program into RAM
and typing ``SYS`` keeps the cartridge on the bus.

The hardware behaviour is not reproducible without a U64 and a cartridge;
what is testable here is that the helper writes the right bytes to the
right address and types the right entry point.
"""

from __future__ import annotations

import pytest

from c64_test_harness.execute import parse_basic_sys_address, run_prg_via_sys
from c64_test_harness.transport import TimeoutError, TransportError
from conftest import MockTransport

# "10 SYS2061" as cc65 emits it, then a two-byte payload at $080D.
CC65_STUB = bytes([0x01, 0x08, 0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061" + bytes(
    [0x00, 0x00, 0x00]
)
PAYLOAD = bytes([0xA9, 0x2A, 0x60])          # LDA #$2A / RTS
CC65_PRG = CC65_STUB + PAYLOAD

_READY = [18, 5, 1, 4, 25, 46]               # "READY." in screen codes


def _ready_screen() -> list[int]:
    codes = [32] * 1000
    codes[:len(_READY)] = _READY
    return codes


class _ReflectingTransport(MockTransport):
    """A MockTransport whose reads see its own writes -- the helper verifies
    the program head by reading it back, so a mock that reads zeros would
    fail every test for the wrong reason."""

    def write_memory(self, addr, data, *, override=None):
        super().write_memory(addr, data, override=override)
        buf = self.memory.setdefault(addr, [])
        data = list(data)
        buf[:len(data)] = data


class _HeadClobberedOnceTransport(_ReflectingTransport):
    """First read of the program head does not match what was written, as
    on a U64 that is still walking RAM after reset; subsequent reads are
    faithful.  The trials showed it deterministic within the window, so
    one rewrite is the expected middle case."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.head_reads = 0

    def read_memory(self, addr, length):
        if addr == 0x0801:
            self.head_reads += 1
            if self.head_reads == 1:
                return bytes(length)
        return super().read_memory(addr, length)


class _NeverIntactTransport(_ReflectingTransport):
    def read_memory(self, addr, length):
        return bytes(length)


def _transport() -> MockTransport:
    return _ReflectingTransport(screen_codes=_ready_screen())


# --- stub parsing ----------------------------------------------------------

def test_parses_cc65_stub() -> None:
    assert parse_basic_sys_address(CC65_PRG) == 2061


def test_parses_stub_with_a_space_after_sys() -> None:
    prg = bytes([0x01, 0x08, 0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b" 4096" + bytes(3)
    assert parse_basic_sys_address(prg) == 4096


def test_returns_none_when_there_is_no_sys_token() -> None:
    assert parse_basic_sys_address(bytes([0x01, 0x08]) + b"\x00" * 8) is None


def test_stops_at_the_first_non_digit() -> None:
    """A trailing ``:`` or REM must not be swallowed into the address."""
    prg = bytes([0x01, 0x08, 0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061:" + bytes(3)
    assert parse_basic_sys_address(prg) == 2061


# --- the load path ---------------------------------------------------------

def test_writes_the_body_at_the_load_address_and_types_sys() -> None:
    t = _transport()
    used = run_prg_via_sys(t, CC65_PRG)

    assert used == 2061
    assert (0x0801, list(CC65_PRG[2:])) in t.written_memory, (
        "PRG body must land at the load address from its first two bytes, "
        "with those two bytes stripped"
    )
    typed = "".join(chr(c) for batch in t.injected_keys for c in batch)
    assert "SYS2061" in typed.upper()
    assert typed.endswith("\r"), "the SYS line must be submitted"


def test_resets_and_waits_for_ready_by_default() -> None:
    t = _transport()
    run_prg_via_sys(t, CC65_PRG)
    assert t.reset_calls, "must reset before typing into BASIC"


def test_reset_can_be_skipped() -> None:
    t = _transport()
    run_prg_via_sys(t, CC65_PRG, reset=False)
    assert not t.reset_calls
    assert t.written_memory and t.injected_keys


def test_explicit_sys_addr_overrides_the_stub() -> None:
    t = _transport()
    used = run_prg_via_sys(t, CC65_PRG, sys_addr=49152)
    assert used == 49152
    typed = "".join(chr(c) for batch in t.injected_keys for c in batch)
    assert "SYS49152" in typed.upper()


def test_refuses_a_stubless_prg_without_an_explicit_entry_point() -> None:
    t = _transport()
    stubless = bytes([0x00, 0xC0]) + PAYLOAD
    with pytest.raises(ValueError, match="no SYS token"):
        run_prg_via_sys(t, stubless)
    assert not t.injected_keys, "must not type anything it could not aim"


def test_refuses_a_truncated_prg() -> None:
    with pytest.raises(ValueError, match="too short"):
        run_prg_via_sys(_transport(), bytes([0x01, 0x08]))


def test_raises_when_the_machine_never_reaches_ready() -> None:
    t = MockTransport(screen_codes=[32] * 1000)     # blank screen, no READY.
    with pytest.raises(TimeoutError, match="READY"):
        run_prg_via_sys(t, CC65_PRG, boot_timeout=0.3)
    assert not t.injected_keys, "must not type into a machine that never booted"


# --- parser defects found by adversarial review (2026-09-05) ---------------
#
# The original parser took the FIRST $9E byte anywhere in the PRG body with
# no BASIC-line structure.  Each case below was probed live against that
# implementation and produced the wrong answer noted in its docstring.

def _basic_prg(lines: list[tuple[int, bytes]], load: int = 0x0801) -> bytes:
    """Assemble a tokenised BASIC program: (line_number, token_bytes)."""
    out = bytearray()
    addr = load
    for lineno, toks in lines:
        nxt = addr + 4 + len(toks) + 1
        out += bytes([nxt & 0xFF, nxt >> 8, lineno & 0xFF, lineno >> 8]) + toks + b"\x00"
        addr = nxt
    out += b"\x00\x00"
    return bytes([load & 0xFF, load >> 8]) + bytes(out)


def test_stubless_prg_whose_code_contains_9e_is_not_a_sys() -> None:
    """Old parser returned 12 -> ``SYS12`` -> a jump into zero page."""
    stubless = bytes([0x00, 0xC0]) + bytes([0xA2, 0x9E, 0x31, 0x32, 0x60])
    assert parse_basic_sys_address(stubless) is None


def test_line_number_158_does_not_masquerade_as_the_sys_token() -> None:
    """Line 158 is stored as ``9E 00``; the old parser found it and returned None."""
    prg = _basic_prg([(158, b"\x9e" + b"2061")])
    assert parse_basic_sys_address(prg) == 2061


def test_sys_with_parenthesised_operand() -> None:
    """``SYS(2061)`` is valid BASIC; old parser returned None."""
    prg = _basic_prg([(10, b"\x9e" + b"(2061)")])
    assert parse_basic_sys_address(prg) == 2061


def test_rem_line_before_the_sys_line_is_skipped() -> None:
    """A REM containing $9E followed by a digit fooled the old parser (returned 5)."""
    prg = _basic_prg([(5, b"\x8f" + b"\x9e5 note"), (10, b"\x9e" + b"2061")])
    assert parse_basic_sys_address(prg) == 2061


def test_non_ascii_bytes_after_sys_do_not_raise() -> None:
    """``chr(0xB2).isdigit()`` is True ('²'); old parser raised ValueError from int()."""
    prg = _basic_prg([(10, b"\x9e" + b"\xb22061")])
    assert parse_basic_sys_address(prg) is None


def test_prg_not_loading_at_basic_start_is_not_parsed_as_basic() -> None:
    """A machine-code PRG at $C000 is not a BASIC program, whatever bytes it holds."""
    prg = bytes([0x00, 0xC0]) + bytes([0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061" + bytes(3)
    assert parse_basic_sys_address(prg) is None
    assert parse_basic_sys_address(prg, basic_start=0xC000) == 2061


# --- head clobber after reset (c64-wireguard paired trials, 2026-09-05) ----

def test_head_not_retained_after_reset_is_rewritten_and_sys_still_typed() -> None:
    t = _HeadClobberedOnceTransport(screen_codes=_ready_screen())
    used = run_prg_via_sys(t, CC65_PRG, verify_timeout=5.0)
    assert used == 2061
    writes = [(a, d) for a, d in t.written_memory if a == 0x0801]
    assert len(writes) == 2, "expected the full body, then a head rewrite"
    assert writes[1][1] == list(CC65_PRG[2:66]) or writes[1][1] == list(CC65_PRG[2:]), (
        "the second write must restore the program head"
    )
    assert t.head_reads >= 2, "must re-verify after the rewrite"
    typed = "".join(chr(c) for batch in t.injected_keys for c in batch)
    assert "SYS2061" in typed.upper()


def test_gives_up_loudly_when_the_head_never_reads_back() -> None:
    t = _NeverIntactTransport(screen_codes=_ready_screen())
    with pytest.raises(TransportError, match="never read back intact"):
        run_prg_via_sys(t, CC65_PRG, verify_timeout=0.3)
    assert not t.injected_keys, "must not SYS into a program it could not verify"


def test_clean_write_does_not_retry() -> None:
    t = _transport()
    run_prg_via_sys(t, CC65_PRG)
    assert sum(1 for a, _ in t.written_memory if a == 0x0801) == 1
