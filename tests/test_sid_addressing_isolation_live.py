"""``isolated_sid_addressing()`` against the hardware it exists for.

Issue #204, following up #196 / PR #201.  Everything U64-side in that PR
was mocked.  Two claims can only be settled on a device:

1. ``Auto Address Mirroring`` read-back reflects the write.
2. A two-slot map with mirroring off decodes **distinctly** -- and, as
   the positive control that makes the negative result meaningful, the
   stock map with mirroring on does **not**.

The decode is proven on the 6510, never from the host: on the U64 the
host REST/DMA path's view of the I/O window is not a trustworthy
instrument, and 28 of the 32 SID registers are write-only anyway.  A
routine at ``$C900`` configures voice 3 -- sawtooth, TEST clear -- at
three windows in this order: the probe window C (``$D480``, which no
slot occupies) with frequency ``$4000``, then chip A (base A) with
``$2000``, then chip B (base B) with ``$8000``; it then samples OSC3
(``+$1B``) sixteen times at each of A, B and C.  The host reads only the
RAM buffers.

**Why the discriminator is a stride, not a value.**  OSC3 is the top
byte of the 24-bit phase accumulator, so per 16-cycle sampling pass it
advances by exactly ``freq * 16 / 65536``: **2** for ``$2000``, **4** for
``$4000``, **8** for ``$8000``.  A correct decode reads ``(2, 8)`` at
``(A, B)``.  Every wrong decode reads something else: if both windows
reach one chip the config written last wins and both stride **8**; if
A's window also wrote B (or the reverse) the same; and an undecoded
window has no counting stride at all.  A frozen-at-zero design (TEST set
on one chip) would not do, because open bus can read zero too.  C is
configured as well, and first, so that it is a positive detector: a chip
that decodes ``$D480`` without being asked to (a leaked decode) strides a
clean **4** there, which open bus cannot produce, whereas an unconfigured
leaked chip would sit at frequency 0 and read a static value that looks
like open bus.  The stock ``$D400 x4`` map with mirroring on is the trap
the helper guards against, and it reads ``(8, 8, 8)`` here -- three
windows, one voice.  A badline steals ~43 cycles from one pass now and
then (a single 7 or 29/30 in the strides), so the assertion is on the
mode, with at least 13 of 15 strides agreeing.

Limitation, documented rather than tested: the strides prove that A and
B are two different decodes, not *which* chip sits behind each -- a map
with the two slots swapped also reads ``(2, 8)``.  Slot identity is a
separate question this test does not ask.

Three arms per round, interleaved, three rounds per slot pair:

- ``OFF-distinct``: the helper under test, ``{A: $D400, B: $D420}`` with
  ``others="distinct"`` -> expect A=2, B=8, C=no counting stride (open
  bus); a clean 4 at C is a leaked decode, a clean 2 or 8 a mirror.
- ``ON-distinct``: same map, mirroring re-enabled -> A=2, B=8 still (A5
  differs between the slots so it stays decoded) and C=$D480 becomes a
  mirror of A: the C write lands on A's chip and A's later write
  overrides it, so C strides 2.  The mirror mechanism, observed directly.
- ``ON-stock``: the device's own map, all four on ``$D400`` -> every write
  hits every chip and B's wins: 8, 8, 8.  Skipped when the stock map is
  not four identical addresses, since that is what the arm asserts on.

Measured 2026-09-05 on the U64E (fw 3.15, 8580s in both sockets): a PUT
to ``SID Addressing`` takes effect with **no reset** (route_configs.cc
``at_close_config`` reprogramming the decode), so no settle is needed
between arms.

Gate: ``SID_ADDRESSING_LIVE=1``.  Host: ``U64_HOST`` (default
``10.43.23.81``).  Restores the whole ``SID Addressing`` category to the
snapshot taken before the first write and asserts the read-back matches.
"""
from __future__ import annotations

import os
import time
from collections import Counter

import pytest

from c64_test_harness import create_manager, run_subroutine, wait_for_text
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_SID_ADDRESSING,
    _sid_addressing_category,
    check_measurement_environment,
    get_detected_sid_types,
    get_sid_socket_enabled,
    isolated_sid_addressing,
    restore_config_items,
    set_sid_auto_mirroring,
    sid_address_conflicts,
)
from c64_test_harness.backends.ultimate64_schema import (
    SID_AUTO_MIRRORING_ITEM,
    SidSlot,
)

_HOST = os.environ.get("U64_HOST", "10.43.23.81")

pytestmark = pytest.mark.skipif(
    os.environ.get("SID_ADDRESSING_LIVE") != "1",
    reason="SID_ADDRESSING_LIVE=1 not set -- live SID addressing test disabled",
)

#: Clear of every HARNESS_SCRATCH span and of the $0360 trampoline.
CODE_ADDR = 0xC900
BUF_ADDR = 0xCA00
SAMPLES = 16
SAW = 0x20

BASE_A = 0xD400
BASE_B = 0xD420
#: No slot sits here in either map; under mirroring it is A widened over A7.
BASE_C = 0xD480

STRIDE_A = 2   # $2000 * 16 / 65536
STRIDE_B = 8   # $8000 * 16 / 65536
STRIDE_C = 4   # $4000 * 16 / 65536 -- a chip leaking onto $D480 says so
MIN_AGREE = 13  # of 15 strides; one badline may disturb a pass
ROUNDS = 3


def _probe_code(base_a: int, base_b: int, base_c: int) -> bytes:
    def sta(addr: int) -> bytes:
        return bytes([0x8D, addr & 0xFF, addr >> 8])

    def lda(v: int) -> bytes:
        return bytes([0xA9, v])

    def sampler(base: int, buf: int) -> bytes:
        # LDX #0 / l: LDA base+$1B / STA buf,X / INX / CPX #n / BNE l
        # 4 + 5 + 2 + 2 + 3 = 16 cycles per pass.
        return bytes([
            0xA2, 0x00,
            0xAD, (base + 0x1B) & 0xFF, (base + 0x1B) >> 8,
            0x9D, buf & 0xFF, buf >> 8,
            0xE8, 0xE0, SAMPLES, 0xD0, 0xF5,
        ])

    code = bytes([0x78])  # SEI
    # C first, then A, then B: under mirroring a later write to the same
    # chip overrides an earlier one, which is what makes the ON arms'
    # expectations (C mirrors A at 2; everything 8 on the stock map)
    # follow from the order.
    code += lda(0x00) + sta(base_c + 0x0E) + lda(0x40) + sta(base_c + 0x0F)
    code += lda(SAW) + sta(base_c + 0x12)
    code += lda(0x00) + sta(base_a + 0x0E) + lda(0x20) + sta(base_a + 0x0F)
    code += lda(SAW) + sta(base_a + 0x12)
    code += lda(0x00) + sta(base_b + 0x0E) + lda(0x80) + sta(base_b + 0x0F)
    code += lda(SAW) + sta(base_b + 0x12)
    code += sampler(base_a, BUF_ADDR)
    code += sampler(base_b, BUF_ADDR + SAMPLES)
    code += sampler(base_c, BUF_ADDR + 2 * SAMPLES)
    code += bytes([0x58, 0x60])  # CLI / RTS
    return code


def _stride_mode(buf: bytes) -> tuple[int, int]:
    strides = [(b - a) % 256 for a, b in zip(buf, buf[1:])]
    value, count = Counter(strides).most_common(1)[0]
    return value, count


def _probe(target) -> dict[str, tuple[int, int]]:
    """Run the 6510 probe once; return {window: (stride mode, agreement)}."""
    t = target.transport
    t.write_memory(BUF_ADDR, bytes(3 * SAMPLES))
    t.write_memory(CODE_ADDR, _probe_code(BASE_A, BASE_B, BASE_C))
    run_subroutine(target, CODE_ADDR, timeout=10.0, poll_cadence=0.01)
    data = t.read_memory(BUF_ADDR, 3 * SAMPLES)
    return {
        "A": _stride_mode(data[:SAMPLES]),
        "B": _stride_mode(data[SAMPLES:2 * SAMPLES]),
        "C": _stride_mode(data[2 * SAMPLES:]),
    }


def _is_chip(window: tuple[int, int], stride: int) -> bool:
    return window[0] == stride and window[1] >= MIN_AGREE


def _is_open_bus(window: tuple[int, int]) -> bool:
    """No clean counting stride: not C's own (a leaked decode), not A's
    or B's (a mirror).  A static value (stride 0) is open bus too."""
    return not (
        window[0] in (STRIDE_A, STRIDE_B, STRIDE_C) and window[1] >= MIN_AGREE
    )


@pytest.fixture(scope="module")
def target():
    with create_manager(
        backend="u64", u64_hosts=_HOST, lock_timeout=600.0
    ) as mgr:
        with mgr.instance() as tgt:
            client = tgt.client
            check_measurement_environment(client)
            stock = _sid_addressing_category(client)
            assert stock, "SID Addressing category came back empty"
            # BASIC READY is what run_subroutine's SYS trampoline needs.
            client.reset()
            time.sleep(3.0)
            assert wait_for_text(
                tgt.transport, "READY.", timeout=20.0, poll_interval=0.5,
                verbose=False,
            ) is not None, "C64 never reached READY after reset"
            tgt.stock_sid_addressing = stock  # type: ignore[attr-defined]
            try:
                yield tgt
            finally:
                restore_config_items(client, CAT_SID_ADDRESSING, stock)
                final = _sid_addressing_category(client)
                assert final == stock, (
                    f"SID Addressing not restored: {final} != {stock}"
                )


def test_auto_mirroring_readback_reflects_the_write(target) -> None:
    """Issue step 2: the assert inside ``set_sid_auto_mirroring`` is real.

    Both directions, and the read is the raw category rather than the
    helper's own return path, so a helper that compared a cached value
    with itself would still fail here.
    """
    client = target.client
    stock_value = target.stock_sid_addressing[SID_AUTO_MIRRORING_ITEM]
    try:
        set_sid_auto_mirroring(client, False)
        assert _sid_addressing_category(client)[SID_AUTO_MIRRORING_ITEM] == "Disabled"
        set_sid_auto_mirroring(client, True)
        assert _sid_addressing_category(client)[SID_AUTO_MIRRORING_ITEM] == "Enabled"
    finally:
        client.set_config_item(
            CAT_SID_ADDRESSING, SID_AUTO_MIRRORING_ITEM, stock_value
        )
    assert _sid_addressing_category(client)[SID_AUTO_MIRRORING_ITEM] == stock_value


def _skip_if_socket_empty(client, slot: SidSlot) -> None:
    if slot in (SidSlot.SOCKET1, SidSlot.SOCKET2):
        n = 1 if slot is SidSlot.SOCKET1 else 2
        if not get_sid_socket_enabled(client)[n]:
            pytest.skip(f"{slot.value} is disabled on this device")
        kind = get_detected_sid_types(client)[n]
        if kind == "None":
            pytest.skip(f"{slot.value} holds no chip")


@pytest.mark.parametrize(
    "slot_a, slot_b",
    [
        (SidSlot.ULTISID1, SidSlot.ULTISID2),
        (SidSlot.SOCKET1, SidSlot.SOCKET2),
        (SidSlot.SOCKET1, SidSlot.ULTISID2),
    ],
    ids=["ultisid-ultisid", "socket-socket", "socket-ultisid"],
)
def test_isolated_map_decodes_distinctly_and_stock_map_aliases(
    target, slot_a: SidSlot, slot_b: SidSlot,
) -> None:
    client = target.client
    stock = target.stock_sid_addressing
    _skip_if_socket_empty(client, slot_a)
    _skip_if_socket_empty(client, slot_b)

    stock_mirroring = stock[SID_AUTO_MIRRORING_ITEM]
    if stock_mirroring != "Enabled":
        pytest.skip(
            f"stock map has mirroring {stock_mirroring!r}; the ON-stock "
            f"positive control assumes the factory 'Enabled'"
        )
    stock_addresses = {
        item: value for item, value in stock.items() if item.endswith("Address")
    }
    if len(set(stock_addresses.values())) != 1:
        pytest.skip(
            f"stock map is not four identical addresses ({stock_addresses}); "
            f"the ON-stock arm asserts every window strides {STRIDE_B}, which "
            f"only follows when all four slots share one decode"
        )
    table: list[str] = []
    for rnd in range(ROUNDS):
        # -- OFF-distinct: the helper under test -----------------------
        with isolated_sid_addressing(
            client, {slot_a: f"${BASE_A:04X}", slot_b: f"${BASE_B:04X}"}
        ) as amap:
            assert sid_address_conflicts(amap) == [], amap
            assert len({a for a in amap.values() if a != "Unmapped"}) == len(
                [a for a in amap.values() if a != "Unmapped"]
            )
            cat = _sid_addressing_category(client)
            assert cat[SID_AUTO_MIRRORING_ITEM] == "Disabled"
            off = _probe(target)
            table.append(f"r{rnd} OFF-distinct {off}")
            assert _is_chip(off["A"], STRIDE_A), f"A is not its own chip: {off}"
            assert _is_chip(off["B"], STRIDE_B), (
                f"B does not decode distinctly from A: {off} "
                f"(8 at both windows means one chip answered twice)"
            )
            assert _is_open_bus(off["C"]), (
                f"window ${BASE_C:04X}, which no slot occupies, answered "
                f"like a chip with mirroring off: {off} (a clean "
                f"{STRIDE_C} is a leaked decode, a clean {STRIDE_A}/"
                f"{STRIDE_B} a mirror)"
            )

            # -- ON-distinct: mirroring back on, same map ---------------
            set_sid_auto_mirroring(client, True)
            on = _probe(target)
            table.append(f"r{rnd} ON-distinct  {on}")
            assert _is_chip(on["A"], STRIDE_A) and _is_chip(on["B"], STRIDE_B), on
            assert _is_chip(on["C"], STRIDE_A), (
                f"with mirroring on, ${BASE_C:04X} should be a mirror of A "
                f"(stride {STRIDE_A}); got {on}.  The mirror mechanism did "
                f"not engage, so the positive control is not a control."
            )
        # The context manager put the whole category back.
        assert _sid_addressing_category(client) == stock

        # -- ON-stock: the trap ------------------------------------------
        trap = _probe(target)
        table.append(f"r{rnd} ON-stock     {trap}")
        assert (
            _is_chip(trap["A"], STRIDE_B)
            and _is_chip(trap["B"], STRIDE_B)
            and _is_chip(trap["C"], STRIDE_B)
        ), (
            f"stock map with mirroring on did NOT alias: {trap}.  Expected "
            f"stride {STRIDE_B} at all three windows (one decode answering "
            f"everywhere).  Without this the negative result above proves "
            f"nothing."
        )
    print("\n".join(table))
