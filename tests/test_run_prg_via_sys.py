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
from c64_test_harness.transport import TimeoutError
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


def _transport() -> MockTransport:
    return MockTransport(screen_codes=_ready_screen())


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
