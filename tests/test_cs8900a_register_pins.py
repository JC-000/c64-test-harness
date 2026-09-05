"""Structural pins on the CS8900a register values the builders emit.

PR #213's second commit aligned three things with ip65 -- TxCMD ``0x00C9``,
the RxEvent poll mask ``0x0D``, and the 6510-side MAC programmer -- and
shipped them with no test that could fail if they regressed.  These pins
close that gap.  They are unit tests on purpose: VICE forces ``rx_ok`` and
ignores the register-number bits, so the two-VICE bridge suite passes with
the old ``0x00C0`` / ``0x01`` values as well as the new ones (issues #207,
#213).  Only real silicon fails on them, and hardware is not in the
default suite.

The mask pin is deliberately two-sided: every RxEvent poll (PPPtr
``0x0124``) must mask ``CS8900A_RXEVENT_MASK``, and every BusST poll
(PPPtr ``0x0138``, Rdy4TxNOW) must still mask ``0x01`` -- a global
search-and-replace of ``0x01`` would break the transmit-ready wait.
"""

from __future__ import annotations

import inspect

import pytest

from c64_test_harness import bridge_ping as bp
from c64_test_harness.bridge_ping import (
    CS8900A_RXEVENT_MASK,
    CS8900A_TXCMD_VALUE,
    PPDATA_HI,
    PPDATA_LO,
    PPTR_HI,
    PPTR_LO,
    TXCMD_LO,
    cs8900a_enable_inline_code,
    cs8900a_set_mac_code,
    cs8900a_set_mac_inline_code,
)

# Plausible arguments for every builder, by parameter name.
_ARGS = {
    "load_addr": 0xC000, "frame_buf": 0xC400, "tx_frame_buf": 0xC400,
    "frame_len": 60, "tx_frame_len": 60, "rx_buf": 0x8000,
    "result_addr": 0xC0FF, "identifier": 0x1234, "sequence": 1,
    "my_ip": bytes([10, 0, 0, 1]), "deadline_tenths": 50, "batch_size": 500,
    "meta_addr": 0xC0E0, "rx_meta": 0xC0E0, "expect_id": 0x1234, "expect_seq": 1,
    "value": None, "mask": None,
}


def _builders():
    """Every ``build_*_code`` in bridge_ping, called with plausible args."""
    out = {}
    for name, fn in inspect.getmembers(bp, inspect.isfunction):
        if not (name.startswith("build_") and name.endswith("_code")):
            continue
        kwargs = {}
        for p in inspect.signature(fn).parameters.values():
            if p.default is not inspect.Parameter.empty:
                continue
            assert p.name in _ARGS and _ARGS[p.name] is not None, (
                f"{name}: no plausible value for required parameter {p.name!r}; "
                "extend _ARGS so this builder stays under test"
            )
            kwargs[p.name] = _ARGS[p.name]
        out[name] = bytes(fn(**kwargs))
    assert out, "no builders found -- the discovery is broken, not the code"
    return out


def _pptr_set(offset: int) -> bytes:
    """LDA #lo / STA PPTR_LO / LDA #hi / STA PPTR_HI for a PacketPage offset."""
    return bytes([
        0xA9, offset & 0xFF, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, offset >> 8, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
    ])


def _polls_by_pptr(code: bytes) -> dict[int, list[int]]:
    """Map each PacketPage offset to the AND #imm masks of the PPData-hi
    polls that follow it.

    A poll reads whatever PPPtr was set to most recently, so each
    ``LDA $DE05 / AND #imm`` is attributed to the nearest preceding PPPtr
    set of *any* offset -- not the nearest preceding set of the offset
    being asked about.  The difference matters: a drop path may set PPPtr
    to RxEvent and ``JMP`` backwards, after which the next poll in byte
    order belongs to the TX path's BusST set, not to it.
    """
    sets = []          # (pos, offset)
    for i in range(len(code) - 9):
        if (code[i] == 0xA9 and code[i + 2] == 0x8D
                and code[i + 3] == PPTR_LO & 0xFF and code[i + 4] == PPTR_LO >> 8
                and code[i + 5] == 0xA9 and code[i + 7] == 0x8D
                and code[i + 8] == PPTR_HI & 0xFF and code[i + 9] == PPTR_HI >> 8):
            sets.append((i, code[i + 1] | (code[i + 6] << 8)))
    poll = bytes([0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8, 0x29])
    out: dict[int, list[int]] = {}
    pos = 0
    while True:
        j = code.find(poll, pos)
        if j < 0:
            return out
        prior = [off for p, off in sets if p < j]
        if prior:
            out.setdefault(prior[-1], []).append(code[j + len(poll)])
        pos = j + 1


def test_every_transmit_writes_txcmd_with_its_register_number() -> None:
    """TxCMD low byte must be $C9 (TxStart-after-full-frame + regnum 9)."""
    sta_txcmd = bytes([0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8])
    seen = 0
    for name, code in _builders().items():
        pos = 0
        while True:
            i = code.find(sta_txcmd, pos)
            if i < 0:
                break
            assert code[i - 2] == 0xA9, f"{name}: STA TxCMD not preceded by LDA #imm"
            assert code[i - 1] == CS8900A_TXCMD_VALUE & 0xFF, (
                f"{name}: writes TxCMD ${code[i - 1]:02X}; must be "
                f"${CS8900A_TXCMD_VALUE & 0xFF:02X} -- a bare $C0 drops the "
                "register number and is the same omission as the old RxCTL $00D8"
            )
            seen += 1
            pos = i + 1
    assert seen >= 4, f"only {seen} TxCMD writes found across the builders"


def test_rxevent_polls_use_the_ip65_mask_and_busst_polls_do_not() -> None:
    """RxEvent polls mask $0D; Rdy4TxNOW polls on BusST keep masking $01."""
    rx_seen = tx_seen = 0
    for name, code in _builders().items():
        polls = _polls_by_pptr(code)
        for m in polls.get(0x0124, []):
            rx_seen += 1
            assert m == CS8900A_RXEVENT_MASK, (
                f"{name}: RxEvent poll masks ${m:02X}; must be "
                f"${CS8900A_RXEVENT_MASK:02X} (RxOK|IndividualAdr|Broadcast) or "
                "frames signalled without RxOK are invisible on silicon"
            )
        for m in polls.get(0x0138, []):
            tx_seen += 1
            assert m == 0x01, (
                f"{name}: BusST Rdy4TxNOW poll masks ${m:02X}; must stay $01 -- "
                "bit 8 alone is the ready flag"
            )
    assert rx_seen >= 3 and tx_seen >= 3, (
        f"found {rx_seen} RxEvent polls and {tx_seen} BusST polls; expected both"
    )


def test_txcmd_and_mask_constants_have_the_ip65_values() -> None:
    assert CS8900A_TXCMD_VALUE == 0x00C9
    assert CS8900A_RXEVENT_MASK == 0x0D
    assert bp.CS8900A_RXCTL_VALUE_IP65 == 0x0D05
    assert bp.CS8900A_RXCTL_VALUE == bp.CS8900A_RXCTL_VALUE_IP65 | 0x0080, (
        "the harness default is ip65's value plus PromiscuousA and nothing else"
    )


def test_set_mac_inline_programs_three_ia_words_in_wire_order() -> None:
    mac = bytes([0x02, 0xC6, 0x40, 0x00, 0x00, 0x77])
    code = cs8900a_set_mac_inline_code(mac)
    expected = b"".join(
        _pptr_set(0x0158 + i * 2) + bytes([
            0xA9, mac[i * 2], 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
            0xA9, mac[i * 2 + 1], 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        ])
        for i in range(3)
    )
    assert code == expected, "IA must be written as three little-endian words at PP $0158/$015A/$015C"
    assert code[-1] != 0x60, "inline form must not end in RTS"


def test_set_mac_blob_is_clockport_then_inline_then_rts() -> None:
    mac = bytes(range(6))
    blob = cs8900a_set_mac_code(mac)
    assert blob == bp._clockport_enable_bytes() + cs8900a_set_mac_inline_code(mac) + b"\x60"


@pytest.mark.parametrize("bad", [b"", bytes(5), bytes(7)])
def test_set_mac_rejects_anything_but_six_bytes(bad: bytes) -> None:
    with pytest.raises(ValueError):
        cs8900a_set_mac_inline_code(bad)
    with pytest.raises(ValueError):
        cs8900a_set_mac_code(bad)


def test_enable_inline_programs_rxctl_before_enabling_the_line() -> None:
    """RxCTL must be set before SerRxON/SerTxON, or the first frames arrive
    against the reset filter."""
    code = cs8900a_enable_inline_code()
    rxctl, linectl = code.find(_pptr_set(0x0104)), code.find(_pptr_set(0x0112))
    assert rxctl >= 0 and linectl >= 0, "enable sequence must touch both RxCTL and LineCTL"
    assert rxctl < linectl
    assert code[:len(bp._clockport_enable_bytes())] == bp._clockport_enable_bytes(), (
        "the clockport enable must come first"
    )
