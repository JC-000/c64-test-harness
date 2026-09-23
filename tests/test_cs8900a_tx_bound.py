"""Bounded CS8900a hardware waits, run on the simulated chip (#234, #235, #236, #238).

Under VICE the emulated chip asserts ``Rdy4TxNOW`` at once and RESET is
instantaneous, so no VICE test can fail on any of these.  The protection
is here and in the structural pins of ``test_cs8900a_register_pins.py``:

* **#236** -- every TX site gives up after
  :data:`~c64_test_harness.bridge_ping.CS8900A_TX_READY_MAX_POLLS` polls
  and stores :data:`~c64_test_harness.bridge_ping.RESULT_TX_NOT_READY`
  instead of spinning forever on a wedged chip.  All nine call sites of
  ``_emit_tx_frame`` are reached, each on a chip that readies exactly the
  bids before it.
* **#238** -- ``frame_len`` odd, zero, or above the maximum is refused at
  emit time on every public route to the copy loop.
* **#438** -- ``build_tx_code(allow_odd_frame_len=True)`` copies an odd
  length ip65's way instead: TxLength the true length, the copy count
  rounded up a byte, one pad byte in the last word.  The refusal stays the
  default because that path has never run on silicon.
* **#404** -- the maximum is 1514 (a standard Ethernet frame without CRC):
  above 256 the copy loop counts pages in ``X`` and bytes in ``Y``, and the
  whole frame reaches TX; at or below 256 the emitted bytes are unchanged.
* **#234** -- :func:`~c64_test_harness.bridge_ping.build_cs8900a_reset_code`
  resets the chip through SelfCTL, waits a bounded time for RESET to
  self-clear, and re-initialises; a RESET that never clears returns
  :data:`~c64_test_harness.bridge_ping.RESULT_RESET_TIMEOUT`.
* **#235** -- ``build_tx_code``'s docstring states what ``0x01`` does and
  does not mean.

Hardware facts are pinned as literals (65,536 polls, ``0x04``/``0x05``,
SelfCTL ``$0114`` bit 6) so a wrong constant cannot validate itself.
"""
from __future__ import annotations

import hashlib

import pytest

from c64_test_harness import bridge_ping as bp
from c64_test_harness.bridge_ping import (
    Asm,
    build_arp_request_frame,
    build_echo_request_frame,
)
from cs8900a_sim import BUSST_RDY4TXNOW, PP_BUSST, Cpu6502, Cs8900aSim

LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300
MAC_A = bytes.fromhex("02c640000001")
MAC_B = bytes.fromhex("c05627b11638")
IP_A, IP_B = bytes([10, 0, 66, 201]), bytes([10, 0, 66, 1])

MAX_POLLS = 65536          # #234's measured wedge window, literal on purpose
TX_NOT_READY = 0x04
RESET_TIMEOUT = 0x05
PP_SELFCTL, PP_RXCTL, PP_LINECTL, PP_IA = 0x0114, 0x0104, 0x0112, 0x0158

ECHO = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B, identifier=0x1234, sequence=1)
ARP_REQ = build_arp_request_frame(MAC_A, IP_A, IP_B)
# What the responders receive: an echo request to IP_B, an ARP request for IP_B.
ECHO_TO_B = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B).frame
ARP_FOR_B = build_arp_request_frame(MAC_A, IP_A, IP_B)


def _run(code: bytes, chip: Cs8900aSim, preload: dict[int, bytes] | None = None,
         max_steps: int = 3_000_000) -> Cpu6502:
    cpu = Cpu6502(chip)
    for addr, data in (preload or {}).items():
        cpu.load(addr, data)
    cpu.load(LOAD, code)
    cpu.mem[RESULT] = 0xAA
    cpu.jsr(LOAD, max_steps=max_steps)
    return cpu


# ===========================================================================
# #236: every TX site is bounded
# ===========================================================================

def _echo_reply_for(echo_frame: bytes) -> bytes:
    f = bytearray(echo_frame)
    f[0:6], f[6:12] = echo_frame[6:12], echo_frame[0:6]
    f[26:30], f[30:34] = echo_frame[30:34], echo_frame[26:30]
    f[34] = 0
    cksum = int.from_bytes(f[36:38], "big") + 0x0800
    f[36:38] = ((cksum & 0xFFFF) + (cksum >> 16)).to_bytes(2, "big")
    return bytes(f)


ECHO_REPLY = _echo_reply_for(ECHO.frame)


class Site:
    """One ``_emit_tx_frame`` call site and how to reach it.

    ``ready`` bids are satisfied before the site under test, so on a chip
    with ``tx_ready_budget=ready`` the wedge lands exactly on it.  ``rx``
    is queued so a fully ready chip runs the routine to a terminating exit
    (the simulated TOD never advances, so a routine left polling never
    returns); ``ok`` is ``(result, frames sent)`` on that ready chip.
    """

    def __init__(self, build, rx, ready, ok):
        self.build, self.rx, self.ready, self.ok = build, rx, ready, ok


# Together these reach all nine _emit_tx_frame call sites: build_tx_code;
# the ARP and echo sites of both ping builders; the reply sites of the three
# responders; and _emit_arp_responder (reached from each [arp] responder).
SITES = {
    "build_tx_code": Site(
        lambda: bp.build_tx_code(LOAD, TX_BUF, len(ECHO.frame), RESULT), [], 0, (0x01, 1)),
    "build_ping_and_wait_code/echo": Site(
        lambda: bp.build_ping_and_wait_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1),
        [ECHO_REPLY], 0, (0x01, 1)),
    "build_ping_and_wait_code/arp": Site(
        lambda: bp.build_ping_and_wait_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 0, (0x01, 2)),
    "build_ping_and_wait_code/echo-after-arp": Site(
        lambda: bp.build_ping_and_wait_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 1, (0x01, 2)),
    "build_ping_and_wait_tod_code/arp": Site(
        lambda: bp.build_ping_and_wait_tod_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 0, (0x01, 2)),
    "build_ping_and_wait_tod_code/echo-after-arp": Site(
        lambda: bp.build_ping_and_wait_tod_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 1, (0x01, 2)),
    "build_icmp_responder_code/reply": Site(
        lambda: bp.build_icmp_responder_code(LOAD, RX_BUF, IP_B, RESULT),
        [ECHO_TO_B], 0, (0x01, 1)),
    "build_icmp_responder_code/arp-reply": Site(
        lambda: bp.build_icmp_responder_code(LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B),
        [ARP_FOR_B, ECHO_TO_B], 0, (0x01, 2)),
    "build_read_and_respond_echo_request_code/reply": Site(
        lambda: bp.build_read_and_respond_echo_request_code(LOAD, RX_BUF, IP_B, RESULT),
        [ECHO_TO_B], 0, (0x01, 1)),
    "build_read_and_respond_echo_request_code/arp-reply": Site(
        lambda: bp.build_read_and_respond_echo_request_code(
            LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B),
        [ARP_FOR_B], 0, (bp.RESULT_ARP_REPLY_SENT, 1)),
    "build_icmp_responder_tod_code/reply": Site(
        lambda: bp.build_icmp_responder_tod_code(LOAD, RX_BUF, IP_B, RESULT),
        [ECHO_TO_B], 0, (0x01, 1)),
    "build_icmp_responder_tod_code/arp-reply": Site(
        lambda: bp.build_icmp_responder_tod_code(LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B),
        [ARP_FOR_B, ECHO_TO_B], 0, (0x01, 2)),
}


def _site_run(name: str, budget: int | None):
    site = SITES[name]
    chip = Cs8900aSim(rx_queue=list(site.rx), tx_ready_budget=budget)
    cpu = _run(site.build(), chip, {TX_BUF: ECHO.frame, ARP_BUF: ARP_REQ})
    return cpu, chip


@pytest.mark.parametrize("name", sorted(SITES))
def test_a_wedged_chip_returns_not_ready_instead_of_hanging(name: str) -> None:
    """Rdy4TxNOW never asserts for the site under test: the routine must
    return, store 0x04, poll exactly 65,536 times, and copy nothing.

    Before #236 this raised ``SimError: step budget ... exhausted`` -- the
    6510 spinning on BusST, which on hardware is a stopped machine."""
    ready = SITES[name].ready
    cpu, chip = _site_run(name, ready)
    assert cpu.mem[RESULT] == TX_NOT_READY, (
        f"{name}: result {cpu.mem[RESULT]:#04x}, expected RESULT_TX_NOT_READY"
    )
    assert len(chip.tx_frames) == ready, f"{name}: {len(chip.tx_frames)} frame(s) sent"
    assert chip.tx_bids == ready + 1, f"{name}: the wedged bid was never made"
    # Each ready bid is satisfied on its first poll; the wedged one uses the budget.
    assert chip.busst_hi_reads == ready + MAX_POLLS, (
        f"{name}: {chip.busst_hi_reads} BusST polls, expected {ready} + {MAX_POLLS}"
    )


@pytest.mark.parametrize("name", sorted(SITES))
def test_a_ready_chip_still_completes_at_every_site(name: str) -> None:
    """The bound is not allowed to cost a frame, or change the result, when
    the chip is ready: every site transmits on its first poll."""
    cpu, chip = _site_run(name, None)
    assert (cpu.mem[RESULT], len(chip.tx_frames)) == SITES[name].ok
    assert chip.busst_hi_reads == len(chip.tx_frames)


class _ReadyOnPoll(Cs8900aSim):
    """Rdy4TxNOW asserts on the *k*-th BusST high-byte read and stays set."""

    def __init__(self, k: int) -> None:
        super().__init__()
        self.k = k

    def _pp_read(self, pp: int) -> int:
        if pp == PP_BUSST and self.clockport:
            return BUSST_RDY4TXNOW if self.busst_hi_reads >= self.k else 0x0018
        return super()._pp_read(pp)


def _emit_one_tx(max_polls: int) -> bytes:
    a = Asm(org=LOAD)
    a.emit(0xA9, 0x01, 0x8D, 0x01, 0xDE)          # clockport on
    bp._emit_tx_frame(a, TX_BUF, 60, "t", "fail", max_polls=max_polls)
    a.emit(0xA9, 0x01, 0x8D, RESULT & 0xFF, RESULT >> 8, 0x60)
    a.label("fail")
    a.emit(0xA9, TX_NOT_READY, 0x8D, RESULT & 0xFF, RESULT >> 8, 0x60)
    return a.build()


@pytest.mark.parametrize("n", [1, 2, 255, 256, 257, 511, 65535, 65536])
def test_the_poll_bound_is_exact(n: int) -> None:
    """X:Y encodes *n* polls exactly: ready on poll *n* transmits, never
    ready gives up after exactly *n* reads.  The 256-boundary cases are
    where an 8-bit counter, or an off-by-one in the ``lo``/``hi`` split,
    would show."""
    chip = Cs8900aSim(tx_ready_budget=0)
    cpu = _run(_emit_one_tx(n), chip, {TX_BUF: ECHO.frame})
    assert (cpu.mem[RESULT], chip.busst_hi_reads, chip.tx_frames) == (TX_NOT_READY, n, [])

    chip = _ReadyOnPoll(n)
    cpu = _run(_emit_one_tx(n), chip, {TX_BUF: ECHO.frame})
    assert cpu.mem[RESULT] == 0x01 and chip.busst_hi_reads == n
    assert chip.tx_frames == [ECHO.frame[:60]]


@pytest.mark.parametrize("n", [0, -1, 65537])
def test_a_poll_bound_outside_1_to_65536_is_refused(n: int) -> None:
    with pytest.raises(ValueError, match="max_polls"):
        _emit_one_tx(n)


def test_tx_bound_constants_are_the_documented_literals() -> None:
    assert bp.CS8900A_TX_READY_MAX_POLLS == MAX_POLLS
    assert bp.RESULT_TX_NOT_READY == TX_NOT_READY
    codes = {0x01, 0x02, 0xFF, bp.RESULT_ARP_REPLY_SENT}
    assert TX_NOT_READY not in codes and RESET_TIMEOUT not in codes | {TX_NOT_READY}


# ===========================================================================
# #238 / #404: frame_len must be even and 2..1514, refused at emit time
# ===========================================================================

MAX_FRAME_LEN = 1514       # Ethernet frame without CRC, literal on purpose
BAD_LENS = [0, 1, 61, 255, 257, 259, 1513, 1515, 1516, 1600, 65536, -2]
GOOD_LENS = [2, 60, 62, 254, 256, 258, 300, 510, 512, 514, 1066, 1514]

LEN_ROUTES = {
    "build_tx_code": lambda n: bp.build_tx_code(LOAD, TX_BUF, n, RESULT),
    "build_ping_and_wait_code.tx_frame_len": lambda n: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, n, RX_BUF, RESULT, 1, 1),
    "build_ping_and_wait_code.arp_frame_len": lambda n: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1, arp_frame_buf=ARP_BUF, arp_frame_len=n),
    "build_ping_and_wait_tod_code.tx_frame_len": lambda n: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, n, RX_BUF, RESULT, 1, 1),
    "build_ping_and_wait_tod_code.arp_frame_len": lambda n: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1, arp_frame_buf=ARP_BUF, arp_frame_len=n),
}


@pytest.mark.parametrize("n", BAD_LENS)
@pytest.mark.parametrize("route", sorted(LEN_ROUTES))
def test_a_frame_len_the_copy_loop_cannot_honour_is_refused(route: str, n: int) -> None:
    """Odd: one frame escapes, then the 6510 hangs with SEI in force
    (measured on hardware in #238).  Above 1514: longer than an Ethernet
    frame, which the chip refuses (#404).  Both must fail loudly here."""
    with pytest.raises(ValueError, match=r"even.*1514|1514.*even"):
        LEN_ROUTES[route](n)


def test_the_udp_live_test_frame_is_one_the_tx_code_delivers_whole() -> None:
    """#304: ``tests/test_rrnet_udp_send_live.py`` used to build a 1066-byte
    frame, which never left the chip (the old copy loop stopped at
    ``1066 & 0xFF = 42`` bytes) and which #238 now refuses.  Its datagram
    must be one ``build_tx_code`` accepts **and** copies out whole.

    The live module is Linux + gate only, so its frame is rebuilt here from
    the module's own constants and run through the simulated chip: the bytes
    that reach TX must be the whole UDP datagram with the whole payload.
    And the live test must no longer be an xfail, or a pass would prove
    nothing again.
    """
    import ast
    from pathlib import Path

    import test_rrnet_udp_send_live as live

    frame = bp.build_udp_frame(
        src_mac=live.C64_MAC, dst_mac=live.HOST_MAC_BROADCAST,
        src_ip=live.C64_IP, dst_ip=live.HOST_IP,
        src_port=live.SRC_PORT, dst_port=live.DST_PORT, payload=live.PAYLOAD,
    )
    chip = Cs8900aSim()
    cpu = _run(bp.build_tx_code(LOAD, TX_BUF, len(frame), RESULT), chip, {TX_BUF: frame})
    assert len(frame) == live.FRAME_LEN
    assert cpu.mem[RESULT] == 0x01
    assert chip.tx_frames == [frame]
    sent = chip.tx_frames[0]
    assert int.from_bytes(sent[38:40], "big") == 8 + len(live.PAYLOAD)   # UDP length
    assert sent[42:42 + len(live.PAYLOAD)] == live.PAYLOAD

    src = Path(live.__file__).read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name.startswith("test_c64_sends_"))
    marks = [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in fn.decorator_list]
    assert not any("xfail" in m for m in marks), marks
    payloads = [ast.unparse(k.value) for c in ast.walk(fn)
                if isinstance(c, ast.Call) and ast.unparse(c.func) == "build_udp_frame"
                for k in c.keywords if k.arg == "payload"]
    assert payloads == ["PAYLOAD"], payloads


@pytest.mark.parametrize("n", GOOD_LENS)
@pytest.mark.parametrize("route", sorted(LEN_ROUTES))
def test_even_lengths_up_to_1514_are_accepted(route: str, n: int) -> None:
    LEN_ROUTES[route](n)


@pytest.mark.parametrize("n", [61, 257, 1513])
def test_an_odd_length_refusal_tells_the_caller_to_pad(n: int) -> None:
    """Odd support is #438; until then the error says what to do instead."""
    with pytest.raises(ValueError, match=r"pad an odd frame by one byte"):
        bp.build_tx_code(LOAD, TX_BUF, n, RESULT)


def test_the_maximum_is_the_documented_literal() -> None:
    assert bp.CS8900A_TX_MAX_FRAME_LEN == MAX_FRAME_LEN


def _pattern(n: int) -> bytes:
    """Bytes that differ page to page, so a loop that re-copies page 0 (a
    missing ``INC $FC``) or skips a page cannot produce the same frame."""
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:n]


# 256 is the last length the 8-bit loop handles; 258 is the first page+tail
# frame, 510/512/514 straddle a page with and without a tail, 1514 is the max.
# Long buffers sit clear of RX_BUF/RESULT, which a 1514-byte frame at TX_BUF overlaps.
LONG_ARP, LONG_TX = 0x6000, 0x7000
WHOLE_LENS = [2, 60, 254, 256, 258, 260, 384, 510, 512, 514, 768, 1024, 1066, 1512, 1514]


@pytest.mark.parametrize("n", WHOLE_LENS)
def test_the_whole_frame_reaches_tx_and_nothing_more(n: int) -> None:
    """Every byte, in order, exactly once: TxLength is ``n``, one frame is
    complete, and no RTDATA write is left over to start a phantom frame.
    Before #404 a length above 256 was refused; before #238 it copied
    ``n & 0xFF`` bytes and never completed the frame."""
    frame = _pattern(n)
    chip = Cs8900aSim()
    cpu = _run(bp.build_tx_code(LOAD, LONG_TX, n, RESULT), chip, {LONG_TX: frame})
    assert cpu.mem[RESULT] == 0x01
    assert chip.txlen == n
    assert chip.tx_frames == [frame]
    assert chip._tx_buf == bytearray(), f"{len(chip._tx_buf)} byte(s) copied past the frame"
    assert chip.busst_hi_reads == 1


@pytest.mark.parametrize("n", [258, 512, 1514])
def test_a_long_frame_goes_out_whole_through_the_ping_builders(n: int) -> None:
    """The ping builders share the copy loop: a long ARP-slot frame and a
    long echo-slot frame both leave whole, in order, then the reply matches."""
    arp_frame, tx_frame = _pattern(n + 2)[2:], _pattern(n)[::-1]
    for build in (bp.build_ping_and_wait_code, bp.build_ping_and_wait_tod_code):
        chip = Cs8900aSim(rx_queue=[ECHO_REPLY])
        code = build(LOAD, LONG_TX, n, RX_BUF, RESULT, 0x1234, 1,
                     arp_frame_buf=LONG_ARP, arp_frame_len=n)
        cpu = _run(code, chip, {LONG_TX: tx_frame, LONG_ARP: arp_frame})
        assert cpu.mem[RESULT] == 0x01, build.__name__
        assert chip.tx_frames == [arp_frame, tx_frame], build.__name__
        assert chip._tx_buf == bytearray()


# Emitted bytes at or below 256 are pinned to master f927012 (pre-#404),
# including the two lengths at the top of the 8-bit loop.
_SHORT_DIGESTS: dict[tuple[str, int], tuple[str, int]] = {
    ("tx", 2): ("67d7d54bba34d49ed51d6f2fa711400e487f325992d45dd24e4a756c5ffad7f9", 99),
    ("ping", 2): ("4064b0ef83c276732e9e4374ff76139bde1316c45abcb60dbabfcdf1e376eb8b", 352),
    ("tod", 2): ("51220cac02bdbaf4b32b264ff79d69d054b8da7ca58b00f1b61c6d353364f86b", 476),
    ("tx", 60): ("d8a9c81805d81c745979be81f76547e0ab94e4a73f8f85be7c571a2d247e2aa5", 99),
    ("ping", 60): ("3db7d6105cc44f19f882fb39f754456eeb49e9e7b67de8ca00be9175b69c0a6c", 352),
    ("tod", 60): ("7000dd85e59d481243e83056367b5432ddf405e2928c890de8a604a9d80c8d8c", 476),
    ("tx", 254): ("e5e3d55000d7a7741fa77648c54e73a98c3e206e1a61496171637f5a7ace29d1", 99),
    ("ping", 254): ("a6ed144d995555e2283acde2cece325fcb5c2ab4159e2e7b2b3f3d7498e024d7", 352),
    ("tod", 254): ("7af3fc52ebb682e906b3568a836645165cdd37f4a7ee7f11182bddc956c8400e", 476),
    ("tx", 256): ("753473b0b2ffa0cbd8a7c3b67ca919cce42a93a4f19bba7aa4297686b19942c0", 99),
    ("ping", 256): ("77837526d98e1533a22f0ae48be4b29b3bb57791dbbb3252c830e99f3d5b2440", 352),
    ("tod", 256): ("fb73e78fe96837c5d2d2aa1a993019276b28fddfa862229b62009dca974709db", 476),
}

_SHORT_CALLS = {
    "tx": lambda n: bp.build_tx_code(LOAD, TX_BUF, n, RESULT),
    "ping": lambda n: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, n, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF, arp_frame_len=n),
    "tod": lambda n: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, n, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF, arp_frame_len=n),
}


@pytest.mark.parametrize("key", sorted(_SHORT_DIGESTS))
def test_frames_up_to_256_emit_masters_exact_bytes(key: tuple[str, int]) -> None:
    """The 8-bit loop is what #238 measured on silicon; #404 leaves it alone."""
    code = _SHORT_CALLS[key[0]](key[1])
    assert (hashlib.sha256(code).hexdigest(), len(code)) == _SHORT_DIGESTS[key]


# ===========================================================================
# #438: an odd length, behind the opt-in, is copied with one pad byte
# ===========================================================================

#: Odd lengths that exercise every arm of the copy loop once rounded up:
#: 3/61 the 8-bit loop, 255 rounding to a full page, 257/259 page+tail,
#: 511 rounding to an exact two-page copy (no tail at all), 1513 the max.
ODD_LENS = [3, 61, 255, 257, 259, 511, 513, 1023, 1513]

#: The byte sitting immediately after the frame in RAM.  ip65's ``adjustcnt``
#: rounds the copy count up, so exactly this byte fills the last word.
PAD_SENTINEL = 0xA5


class _TxWordLog(Cs8900aSim):
    """Records every half-word written into RTDATA, before the sim truncates
    the frame to TxLength -- so a test can see the pad byte and count the
    words the loop actually copied."""

    def __init__(self) -> None:
        super().__init__()
        self.tx_halves: list[int] = []

    def _tx_write(self, reg: int, value: int) -> None:
        self.tx_halves.append(value)
        super()._tx_write(reg, value)


@pytest.mark.parametrize("n", ODD_LENS)
def test_an_odd_frame_goes_out_whole_with_the_true_txlength(n: int) -> None:
    """ip65's ``send``: TxLength is the **true** odd length, and ``adjustcnt``
    rounds the copy to ``ceil(n/2)`` words so one pad byte fills the last
    word.  Before this the emitted loop compared ``Y`` against an odd
    ``CPY`` value it steps over two at a time, so it never terminated: on
    silicon (#238) one frame went out and the 6510 hung forever with SEI in
    force, and here the run exhausts the step budget."""
    frame = _pattern(n)
    chip = _TxWordLog()
    code = bp.build_tx_code(LOAD, LONG_TX, n, RESULT, allow_odd_frame_len=True)
    cpu = _run(code, chip, {LONG_TX: frame + bytes([PAD_SENTINEL])})
    assert cpu.mem[RESULT] == 0x01
    assert chip.txlen == n, "TxLength must be the true length, not the padded count"
    assert chip.tx_frames == [frame]
    assert chip._tx_buf == bytearray(), f"{len(chip._tx_buf)} byte(s) copied past the frame"
    # Exactly ceil(n/2) words: the frame, then one pad byte read from RAM.
    assert bytes(chip.tx_halves) == frame + bytes([PAD_SENTINEL])
    assert chip.busst_hi_reads == 1


@pytest.mark.parametrize("n", [2, 60, 254, 256, 258, 512, 1512, 1514])
def test_the_odd_opt_in_changes_nothing_for_an_even_length(n: int) -> None:
    """The opt-in only relaxes the parity refusal: for an even length the
    emitted bytes are the ones #404 measured on silicon, to the byte."""
    assert bp.build_tx_code(LOAD, TX_BUF, n, RESULT, allow_odd_frame_len=True) == \
        bp.build_tx_code(LOAD, TX_BUF, n, RESULT)


@pytest.mark.parametrize("n", [3, 61, 257, 1513])
def test_an_odd_length_is_still_refused_without_the_opt_in(n: int) -> None:
    """The padded copy is pinned on the simulated chip only -- no silicon has
    transmitted an odd TxLength under it -- so the default stays a refusal."""
    with pytest.raises(ValueError, match=r"pad an odd frame by one byte"):
        bp.build_tx_code(LOAD, TX_BUF, n, RESULT)


def test_the_ping_builders_have_no_odd_length_opt_in() -> None:
    """The opt-in's blast radius is ``build_tx_code`` alone: the ping
    builders transmit into a reply-matching loop and are not the instrument
    a silicon check would use."""
    for build in (bp.build_ping_and_wait_code, bp.build_ping_and_wait_tod_code):
        with pytest.raises(TypeError):
            build(LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1,
                  allow_odd_frame_len=True)          # type: ignore[call-arg]


@pytest.mark.parametrize("n", [0, 1, -1, 1515, 1601, 65537])
def test_the_length_range_is_enforced_even_with_the_odd_opt_in(n: int) -> None:
    """``allow_odd_frame_len`` relaxes parity, never the 2..1514 bound."""
    with pytest.raises(ValueError, match="1514"):
        bp.build_tx_code(LOAD, TX_BUF, n, RESULT, allow_odd_frame_len=True)


@pytest.mark.parametrize("n", ODD_LENS)
def test_an_odd_frame_build_tx_code_still_fits_one_rest_put(n: int) -> None:
    """CLAUDE.md hardware-safety rule 2: the upload stays a zero-attachment
    PUT on a leak-prone device."""
    assert len(bp.build_tx_code(LOAD, TX_BUF, n, RESULT, allow_odd_frame_len=True)) <= 128


@pytest.mark.parametrize("n", [258, 512, 1514])
def test_a_long_frame_build_tx_code_still_fits_one_rest_put(n: int) -> None:
    """At or under 128 bytes the upload is a zero-attachment PUT on a
    leak-prone device (CLAUDE.md hardware-safety rule 2)."""
    assert len(bp.build_tx_code(LOAD, TX_BUF, n, RESULT)) <= 128


# ===========================================================================
# #235: the result byte's contract is written down where callers read it
# ===========================================================================

def test_build_tx_code_docstring_states_the_real_contract() -> None:
    doc = " ".join((bp.build_tx_code.__doc__ or "").split())
    assert "on success" not in doc, "0x01 is not a success flag (#235)"
    assert "Rdy4TxNOW" in doc and "delivery" in doc
    assert "RESULT_TX_NOT_READY" in doc
    assert "even" in doc and "1514" in doc, "the #238/#404 precondition belongs on the public builder"


# ===========================================================================
# #234: chip reset through SelfCTL, bounded, then re-init
# ===========================================================================

def _reset_run(chip: Cs8900aSim, **kw) -> Cpu6502:
    return _run(bp.build_cs8900a_reset_code(LOAD, RESULT, **kw), chip)


def _writes_after_reset(chip: Cs8900aSim) -> list[tuple[int, bool, int]]:
    i = next(i for i, (pp, hi, v) in enumerate(chip.pp_writes)
             if pp == PP_SELFCTL and not hi and v & 0x40)
    return chip.pp_writes[:i], chip.pp_writes[i + 1:]


def test_reset_writes_selfctl_reset_waits_for_it_to_clear_then_reinitialises() -> None:
    chip = Cs8900aSim(reset_clears_after=3)
    cpu = _reset_run(chip, mac=MAC_A)
    assert cpu.mem[RESULT] == 0x01
    assert chip.resets == 1
    assert chip.selfctl_reads == 4, "three reads with RESET set, one clear"
    before, after = _writes_after_reset(chip)
    assert before == [], "nothing is programmed before the reset it would be lost to"
    assert (PP_SELFCTL, True, 0) not in chip.pp_writes, "ip65 writes only SelfCTL's low byte"
    # ip65's init order after the reset: RxCTL, Individual Address, LineCTL.
    order = [pp for pp, _, _ in after]
    assert order[:2] == [PP_RXCTL, PP_RXCTL]
    assert order[2:8] == [PP_IA, PP_IA, PP_IA + 2, PP_IA + 2, PP_IA + 4, PP_IA + 4]
    assert order[8:] == [PP_LINECTL]
    assert after[0][2] | after[1][2] << 8 == bp.CS8900A_RXCTL_VALUE
    assert bytes(v for pp, _, v in after[2:8]) == MAC_A
    assert after[8][2] & 0xC0 == 0xC0, "SerRxON | SerTxON"


def test_reset_without_mac_programs_rxctl_and_linectl_only() -> None:
    chip = Cs8900aSim()
    cpu = _reset_run(chip, rxctl_value=bp.CS8900A_RXCTL_VALUE_IP65)
    _, after = _writes_after_reset(chip)
    assert cpu.mem[RESULT] == 0x01
    assert [pp for pp, _, _ in after] == [PP_RXCTL, PP_RXCTL, PP_LINECTL]
    assert after[0][2] | after[1][2] << 8 == 0x0D05


def test_a_reset_that_never_clears_returns_timeout_and_does_not_reinitialise() -> None:
    chip = Cs8900aSim(reset_clears_after=None)
    cpu = _reset_run(chip, mac=MAC_A)
    assert cpu.mem[RESULT] == RESET_TIMEOUT
    assert chip.selfctl_reads == MAX_POLLS
    _, after = _writes_after_reset(chip)
    assert after == [], "nothing is programmed into a chip still in reset"


@pytest.mark.parametrize("n", [1, 256, 257])
def test_reset_bound_is_exact(n: int) -> None:
    chip = Cs8900aSim(reset_clears_after=n - 1)     # clear on read n
    assert _reset_run(chip, max_polls=n).mem[RESULT] == 0x01
    assert chip.selfctl_reads == n
    chip = Cs8900aSim(reset_clears_after=n)         # clear on read n+1: too late
    assert _reset_run(chip, max_polls=n).mem[RESULT] == RESET_TIMEOUT
    assert chip.selfctl_reads == n


def test_reset_then_a_bounded_tx_is_the_readiness_probe() -> None:
    """#234 asked for a probe that tells a wedged chip from a ready one
    without hanging: reset, then transmit through the bounded poll.  A
    ready chip sends; a wedged one reports 0x04."""
    for budget, want in ((None, 0x01), (0, TX_NOT_READY)):
        chip = Cs8900aSim(tx_ready_budget=budget)
        cpu = Cpu6502(chip)
        cpu.load(TX_BUF, ECHO.frame)
        cpu.load(LOAD, bp.build_cs8900a_reset_code(LOAD, RESULT))
        cpu.jsr(LOAD)
        assert cpu.mem[RESULT] == 0x01
        cpu.load(0x6000, bp.build_tx_code(0x6000, TX_BUF, len(ECHO.frame), RESULT))
        cpu.jsr(0x6000, max_steps=3_000_000)
        assert cpu.mem[RESULT] == want


def test_reset_rejects_a_bad_mac_and_a_bad_bound() -> None:
    with pytest.raises(ValueError):
        bp.build_cs8900a_reset_code(LOAD, RESULT, mac=b"\x00" * 5)
    with pytest.raises(ValueError, match="max_polls"):
        bp.build_cs8900a_reset_code(LOAD, RESULT, max_polls=0)


def test_reset_constants_are_the_documented_literals() -> None:
    assert bp.CS8900A_RESET_MAX_POLLS == MAX_POLLS
    assert bp.RESULT_RESET_TIMEOUT == RESET_TIMEOUT
