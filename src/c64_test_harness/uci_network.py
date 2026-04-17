"""UCI (Ultimate Command Interface) network driver for Ultimate 64.

Generates 6502 machine-code routines that talk to the UCI I/O registers
at $DF1C-$DF1F for TCP/UDP socket networking, and provides high-level
helpers that inject and execute those routines on a live U64 via DMA.

UCI register map
~~~~~~~~~~~~~~~~
- $DF1B (r/w)   OUR_DEVICE: device number assignment
- $DF1C (read)  STATUS:    bit7=DATA_AV, bit6=STAT_AV,
                            bit5+4=STATE, bit3=ERROR,
                            bit2=ABORT_PENDING, bit1=DATA_ACC,
                            bit0=CMD_BUSY
- $DF1C (write) CONTROL:   bit3=CLR_ERR, bit2=ABORT, bit1=NEXT_DATA,
                            bit0=PUSH_CMD
- $DF1D (write) CMD_DATA:  command/parameter byte input
- $DF1D (read)  ID:        UCI identification byte (0xC9)
- $DF1E (read)  RESP_DATA: response data bytes
- $DF1F (read)  STATUS_DATA: status string bytes

Command protocol:
0. Abort pending state: write $04 to $DF1C (best practice)
1. Wait idle (STATE==0 and CMD_BUSY==0)
2. Write target byte, command byte, params to $DF1D
3. Push command: write $01 to $DF1C
4. Wait not busy: poll $DF1C bit0
5. Check error: bit3 set => write $08 to $DF1C, flag error
6. Read response: while bit7 set, read $DF1E
7. Read status: while bit6 set, read $DF1F
8. Acknowledge: write $02 to $DF1C

Response formats:
- GET_IPADDR returns 12 bytes: IP(4) + Netmask(4) + Gateway(4)
- GET_NETADDR returns 6-byte MAC address
- SOCKET_READ returns [len_lo] [len_hi] [data...]
- SOCKET_WRITE returns [written_lo] [written_hi]
- Status strings: "00,OK" on success, "84,UNRESOLVED HOST" etc. on error
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport
    from .backends.ultimate64_client import Ultimate64Client


# ---------------------------------------------------------------------------
# Delay-loop fence parameters (see `_build_fence` below).
#
# At stock 1 MHz the FPGA behind $DF1C-$DF1F is slow enough that back-to-back
# 6502 bus cycles leave ample settling time. At U64 turbo speeds (8, 24,
# 48 MHz) the CPU starts outrunning the FPGA: writes get double-latched,
# reads return stale/glitched values, and the UCI protocol corrupts.
#
# The fix (ported verbatim from the c64-https `fix/uci-nop-fencing` PR) is
# a nested delay-loop macro that burns ~52 µs of wall-clock time after
# *every* read or write to a UCI register, giving the FPGA time to latch
# writes AND settle reads before the CPU acts on the value.
#
# Tuned empirically via binary search at 48 MHz on real U64E hardware:
#     OUTER=3  INNER=121 (~1830 cycles, ~38.1 µs at 48 MHz) = FAIL
#     OUTER=3  INNER=122 (~1845 cycles, ~38.4 µs at 48 MHz) = PASS (minimum)
#     OUTER=5  INNER=100 (~2525 cycles, ~52.6 µs at 48 MHz) = chosen (35% margin)
#
# At 1 MHz the same loop costs ~2.5 ms per access — negligible for
# networking. 18 bytes per fence site.
#
# The fence is opt-in via the ``turbo_safe`` keyword on every builder / helper
# — existing 1 MHz callers are unaffected.
# ---------------------------------------------------------------------------
UCI_FENCE_OUTER = 5    # outer loop iterations
UCI_FENCE_INNER = 100  # inner loop iterations

# Settle iterations burned *after* every PUSH_CMD write, *before* the first
# CMD_BUSY poll. At turbo speeds the FPGA may not have asserted CMD_BUSY by
# the time the CPU reaches the poll loop; the resulting "not busy on first
# read" causes the command to be lost. 255 * 5 ≈ 1275 cycles ≈ 27 µs at 48 MHz
# (≈ 1.3 ms at 1 MHz), which is enough slack for the FPGA to latch the
# command. Mirrors c64-https `uci_push_wait`.
UCI_PUSH_SETTLE_ITERS = 0xFF

# ---------------------------------------------------------------------------
# UCI I/O registers
# ---------------------------------------------------------------------------
UCI_DEVICE_REG         = 0xDF1B   # r/w: device number assignment
UCI_CONTROL_STATUS_REG = 0xDF1C   # read: status / write: control
UCI_CMD_DATA_REG       = 0xDF1D   # write: command bytes / read: ID
UCI_RESP_DATA_REG      = 0xDF1E   # read: response data
UCI_STATUS_DATA_REG    = 0xDF1F   # read: status string

# UCI identification byte (read from $DF1D)
UCI_IDENTIFIER = 0xC9

# ---------------------------------------------------------------------------
# Status register bit masks (read side of $DF1C)
# ---------------------------------------------------------------------------
BIT_DATA_AV  = 0x80    # bit 7 — response data available
BIT_STAT_AV  = 0x40    # bit 6 — status data available
BIT_ERROR    = 0x08    # bit 3 — error flag
BIT_CMD_BUSY = 0x01    # bit 0 — command busy

# State field (bits 5:4 of status register)
STATE_BITS      = 0x30    # mask for state bits
STATE_IDLE      = 0x00    # idle
STATE_BUSY      = 0x10    # busy processing
STATE_LAST_DATA = 0x20    # last data available
STATE_MORE_DATA = 0x30    # more data available

# Combined mask for idle check: STATE bits + CMD_BUSY
_IDLE_MASK = STATE_BITS | BIT_CMD_BUSY

# ---------------------------------------------------------------------------
# Control register bits (write side of $DF1C)
# ---------------------------------------------------------------------------
CMD_PUSH      = 0x01    # bit 0 — push command
CMD_NEXT_DATA = 0x02    # bit 1 — next data / acknowledge
CMD_ABORT     = 0x04    # bit 2 — abort command
CMD_CLR_ERR   = 0x08    # bit 3 — clear error

# ---------------------------------------------------------------------------
# UCI target IDs
# ---------------------------------------------------------------------------
TARGET_DOS1    = 0x01
TARGET_DOS2    = 0x02
TARGET_NETWORK = 0x03
TARGET_CONTROL = 0x04

# ---------------------------------------------------------------------------
# Network commands
# ---------------------------------------------------------------------------
NET_CMD_IDENTIFY            = 0x01
NET_CMD_GET_INTERFACE_COUNT = 0x02
NET_CMD_GET_NETADDR         = 0x04
NET_CMD_GET_IPADDR          = 0x05
NET_CMD_SET_IPADDR          = 0x06
NET_CMD_TCP_CONNECT         = 0x07
NET_CMD_UDP_CONNECT         = 0x08
NET_CMD_SOCKET_CLOSE        = 0x09
NET_CMD_SOCKET_READ         = 0x10
NET_CMD_SOCKET_WRITE        = 0x11
NET_CMD_TCP_LISTENER_START  = 0x12
NET_CMD_TCP_LISTENER_STOP   = 0x13
NET_CMD_GET_LISTENER_STATE  = 0x14
NET_CMD_GET_LISTENER_SOCKET = 0x15

# ---------------------------------------------------------------------------
# Data queue limits
# ---------------------------------------------------------------------------
DATA_QUEUE_MAX   = 896
STATUS_QUEUE_MAX = 256

# ---------------------------------------------------------------------------
# Listener states
# ---------------------------------------------------------------------------
NOT_LISTENING = 0x00
LISTENING     = 0x01
CONNECTED     = 0x02
BIND_ERROR    = 0x03
PORT_IN_USE   = 0x04

# ---------------------------------------------------------------------------
# 6502 opcodes used in generated routines
# ---------------------------------------------------------------------------
_LDA_IMM = 0xA9
_LDA_ABS = 0xAD
_LDX_IMM = 0xA2
_LDY_IMM = 0xA0
_STA_ABS = 0x8D
_STY_ABS = 0x8C
_AND_IMM = 0x29
_BNE     = 0xD0
_BEQ     = 0xF0
_INY     = 0xC8
_DEX     = 0xCA
_JMP_ABS = 0x4C
_TAX     = 0xAA
_TXA     = 0x8A
_LDA_ABS_Y = 0xB9
_STA_ABS_Y = 0x99
_PHA     = 0x48
_PLA     = 0x68
_RTS = 0x60  # 6502 RTS opcode — used to end UCI routines dispatched via SYS.

# ---------------------------------------------------------------------------
# Default memory layout for injected routines
# ---------------------------------------------------------------------------
_CODE_ADDR     = 0xC000   # routine code
_DATA_ADDR     = 0xC100   # hostname / write data buffer
_RESP_ADDR     = 0xC200   # response data from UCI
_STATUS_ADDR   = 0xC300   # status string from UCI
_RESP_LEN_ADDR = 0xC3F0   # 2-byte LE response length
_STAT_LEN_ADDR = 0xC3F2   # 2-byte LE status length
_SENTINEL_ADDR = 0xC3FE   # completion sentinel
_ERROR_ADDR    = 0xC3FF   # error flag

_SENTINEL_DONE = 0x42     # magic value written on completion

# Timeout for polling sentinel (seconds)
_DEFAULT_TIMEOUT = 10.0
_POLL_INTERVAL   = 0.05

# Default addresses for socket operations
_SOCKET_ID_ADDR = 0xC100
_DATA_BUF_ADDR  = 0xC101
_DATA_LEN_ADDR  = 0xC1FF
_HOST_ADDR      = 0xC100


def _lo(addr: int) -> int:
    return addr & 0xFF


def _hi(addr: int) -> int:
    return (addr >> 8) & 0xFF


# ---------------------------------------------------------------------------
# Turbo-safety delay fence (6502 emitter)
# ---------------------------------------------------------------------------

def _build_fence() -> list[int]:
    """6502 fragment: nested delay loop ~52 us at 48 MHz, ~2.5 ms at 1 MHz.

    Preserves A and X across the delay so callers that staged a status byte
    in A (e.g. immediately after ``LDA $DF1C``) can still run ``AND #mask``
    with the correct value afterwards.  18 bytes total.

    Layout (byte offsets within the emitted fence)::

        +0:  PHA                ; save A                     (1 byte)
        +1:  TXA                                             (1 byte)
        +2:  PHA                ; save X                     (1 byte)
        +3:  LDX #UCI_FENCE_OUTER                            (2 bytes)
        +5:  LDY #UCI_FENCE_INNER  ; outer                   (2 bytes)
        +7:  DEY                   ; inner                   (1 byte)
        +8:  BNE inner  ; -3                                 (2 bytes)
        +10: DEX                                             (1 byte)
        +11: BNE outer  ; -8                                 (2 bytes)
        +13: PLA                                             (1 byte)
        +14: TAX                ; restore X                  (1 byte)
        +15: PLA                ; restore A                  (1 byte)
        +16: (next instruction)

    Matches c64-https `uci_fence` macro semantics (preserves A/X, ~52 us
    at 48 MHz), implemented with LDY/DEY/BNE to avoid importing SBC into
    the builder opcode set.
    """
    _DEY = 0x88
    return [
        _PHA,                           # +0   save A
        _TXA,                           # +1
        _PHA,                           # +2   save X
        _LDX_IMM, UCI_FENCE_OUTER,      # +3..+4
        # outer (+5):
        _LDY_IMM, UCI_FENCE_INNER,      # +5..+6
        # inner (+7):
        _DEY,                           # +7
        _BNE, 0xFD,                     # +8..+9   rel=-3 back to DEY
        _DEX,                           # +10
        _BNE, 0xF8,                     # +11..+12 rel=-8 back to outer LDY
        _PLA,                           # +13
        _TAX,                           # +14      restore X
        _PLA,                           # +15      restore A
    ]


# Size of the fence emitted by _build_fence() — exposed so builders can
# compute branch offsets that jump over fence expansions when turbo-safe.
_FENCE_BYTES = 16


# ---------------------------------------------------------------------------
# Generic UCI command routine builder fragments
# ---------------------------------------------------------------------------

def _build_abort_preamble() -> list[int]:
    """6502 fragment: send ABORT to clear any pending UCI state.

    Best practice: issue ABORT ($04) before starting a new command
    sequence to ensure a clean slate.  A brief delay loop (LDX #$FF;
    DEX; BNE) gives the firmware time to process the abort before
    we proceed.
    """
    return [
        _LDA_IMM, CMD_ABORT,
        _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        # Brief delay loop for abort to take effect
        _LDX_IMM, 0xFF,
        _DEX,             # DEX
        _BNE, 0xFD,       # BNE -3 (back to DEX)
    ]


def _build_wait_idle() -> list[int]:
    """6502 fragment: wait until UCI is idle (STATE==0 and CMD_BUSY==0).

    Polls $DF1C, masks with 0x31, loops until zero.
    """
    # loop: LDA $DF1C(3); AND #$31(2); BNE loop(2) = 7 bytes
    # BNE at byte 5, next=7, target=0: offset = 0-7 = -7 = 0xF9
    return [
        _LDA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _AND_IMM, _IDLE_MASK,
        _BNE, 0xF9,
    ]


def _build_push_and_wait() -> list[int]:
    """6502 fragment: push command then wait for not-busy."""
    # LDA #$01(2); STA $DF1C(3); LDA $DF1C(3); AND #$01(2); BNE wait(2)
    # wait loop at byte 5; BNE at byte 10; next=12; target=5; offset=-7=0xF9
    return [
        _LDA_IMM, CMD_PUSH,
        _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _LDA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _AND_IMM, BIT_CMD_BUSY,
        _BNE, 0xF9,
    ]


def _build_check_error(error_addr: int) -> list[int]:
    """6502 fragment: check error bit, set error_addr=$FF if set, clear error.

    Byte layout (17 bytes):
    0: LDA $DF1C(3)=3; AND #$08(2)=5; BEQ +10(2)=7;
    7: LDA #$FF(2)=9; STA error_addr(3)=12; LDA #$08(2)=14; STA $DF1C(3)=17
    BEQ at 5, next=7, skip 10 bytes to 17 (end).
    """
    return [
        _LDA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _AND_IMM, BIT_ERROR,
        _BEQ, 10,
        _LDA_IMM, 0xFF,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
        _LDA_IMM, CMD_CLR_ERR,
        _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
    ]


def _build_read_response(resp_addr: int, resp_len_addr: int) -> list[int]:
    """6502 fragment: read response data into resp_addr, count into resp_len_addr.

    Each byte read is acknowledged with CMD_NEXT_DATA ($02) written to
    $DF1C so the UCI advances to the next data byte (or returns to idle
    when all data has been consumed).

    Byte layout (32 bytes):
    0: LDY #$00(2)=2; STA resp_len(3)=5; STA resp_len+1(3)=8
    8: LDA $DF1C(3)=11; AND #$80(2)=13; BEQ +14(2)=15
    15: LDA $DF1E(3)=18; STA resp,Y(3)=21; INY(1)=22
    22: LDA #$02(2)=24; STA $DF1C(3)=27; BNE -21(2)=29
    29: STY resp_len(3)=32
    BEQ at 13, next=15, target=29(STY): offset = 29-15 = 14
    BNE at 27, next=29, target=8(loop): offset = 8-29 = -21 = 0xEB
    (BNE is always taken because A=$02 after the STA)
    """
    return [
        _LDY_IMM, 0x00,
        _STA_ABS, _lo(resp_len_addr), _hi(resp_len_addr),
        _STA_ABS, _lo(resp_len_addr + 1), _hi(resp_len_addr + 1),
        # loop:
        _LDA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _AND_IMM, BIT_DATA_AV,
        _BEQ, 14,
        _LDA_ABS, _lo(UCI_RESP_DATA_REG), _hi(UCI_RESP_DATA_REG),
        _STA_ABS_Y, _lo(resp_addr), _hi(resp_addr),
        _INY,
        _LDA_IMM, CMD_NEXT_DATA,
        _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _BNE, 0xEB,
        # done:
        _STY_ABS, _lo(resp_len_addr), _hi(resp_len_addr),
    ]


def _build_read_status(status_addr: int, stat_len_addr: int) -> list[int]:
    """6502 fragment: read status string into status_addr, count into stat_len_addr.

    Same structure as _build_read_response but reads from $DF1F and
    checks BIT_STAT_AV (bit 6).  Each byte is acknowledged with
    CMD_NEXT_DATA so the UCI advances through the status queue.
    """
    return [
        _LDY_IMM, 0x00,
        _STA_ABS, _lo(stat_len_addr), _hi(stat_len_addr),
        _STA_ABS, _lo(stat_len_addr + 1), _hi(stat_len_addr + 1),
        # loop:
        _LDA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _AND_IMM, BIT_STAT_AV,
        _BEQ, 14,
        _LDA_ABS, _lo(UCI_STATUS_DATA_REG), _hi(UCI_STATUS_DATA_REG),
        _STA_ABS_Y, _lo(status_addr), _hi(status_addr),
        _INY,
        _LDA_IMM, CMD_NEXT_DATA,
        _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
        _BNE, 0xEB,
        # done:
        _STY_ABS, _lo(stat_len_addr), _hi(stat_len_addr),
    ]


def _build_acknowledge() -> list[int]:
    """6502 fragment: wait for UCI to return to idle after draining data/status.

    The read_response and read_status loops now include NEXT_DATA
    acknowledgment with each byte read, so by the time they finish UCI
    is typically idle.  We just wait for idle confirmation here; writing
    NEXT_DATA to an already-idle UCI can cause it to enter a non-idle
    state, so we avoid it.
    """
    return _build_wait_idle()


# ---------------------------------------------------------------------------
# Turbo-safe fragment emitters.
#
# Each of these takes the absolute program counter `pc` at which the fragment
# will be placed, because fence expansion blows the 8-bit branch range
# (-128..+127) on backward loops — those are rewritten as absolute JMPs.
# Forward branches stay as short BEQ/BNE offsets, which we compute from the
# final emitted length.
#
# `fence = True` interleaves the delay-loop fence after every read/write of
# a UCI register ($DF1C-$DF1F). With `fence = False` the turbo-safe variants
# still emit JMP trampolines and long-safe branches, making them strictly
# equivalent to the 1 MHz fragments just with slightly more code — this is
# useful when a caller wants a uniform code path regardless of speed.
# ---------------------------------------------------------------------------

def _build_wait_idle_tsx(pc: int, fence: bool = True) -> list[int]:
    """Turbo-safe variant of ``_build_wait_idle``.

    Emits::

        loop (pc):
            LDA  $DF1C
            <fence>
            AND  #_IDLE_MASK
            BEQ  done
            JMP  loop
        done:
    """
    out: list[int] = []
    loop_addr = pc
    # LDA $DF1C
    out.extend([_LDA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_AND_IMM, _IDLE_MASK])
    # BEQ done — skip the JMP trampoline (3 bytes).
    out.extend([_BEQ, 0x03])
    # JMP loop
    out.extend([_JMP_ABS, _lo(loop_addr), _hi(loop_addr)])
    return out


def _build_push_and_wait_tsx(pc: int, fence: bool = True) -> list[int]:
    """Turbo-safe variant of ``_build_push_and_wait``.

    Emits PUSH_CMD + a fixed settle delay + a wait_not_busy loop that uses
    JMP trampolines (since the fence is too wide for short BNE back)::

        LDA #PUSH_CMD
        STA $DF1C
        <fence>
        LDX #UCI_PUSH_SETTLE_ITERS
    settle:
        DEX
        BNE settle
    busy_loop:
        LDA $DF1C
        <fence>
        AND #BIT_CMD_BUSY
        BEQ done
        JMP busy_loop
    done:
    """
    out: list[int] = []
    # LDA #CMD_PUSH; STA $DF1C
    out.extend([_LDA_IMM, CMD_PUSH])
    out.extend([_STA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    # LDX #n; settle: DEX; BNE settle
    out.extend([_LDX_IMM, UCI_PUSH_SETTLE_ITERS & 0xFF])
    out.append(_DEX)
    out.extend([_BNE, 0xFD])  # -3 back to DEX

    # busy_loop absolute address:
    busy_loop_abs = pc + len(out)
    # LDA $DF1C
    out.extend([_LDA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_AND_IMM, BIT_CMD_BUSY])
    # BEQ done (skip 3-byte JMP)
    out.extend([_BEQ, 0x03])
    out.extend([_JMP_ABS, _lo(busy_loop_abs), _hi(busy_loop_abs)])
    return out


def _build_check_error_tsx(
    pc: int, error_addr: int, fence: bool = True,
) -> list[int]:
    """Turbo-safe ``_build_check_error`` with fence between LDA and AND."""
    out: list[int] = []
    out.extend([_LDA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_AND_IMM, BIT_ERROR])
    # Error path size is 10 bytes (LDA #$FF + STA + LDA #$08 + STA) + fence
    err_block_len = (
        2                   # LDA #$FF
        + 3                 # STA error_addr
        + 2                 # LDA #CMD_CLR_ERR
        + 3                 # STA $DF1C
        + (_FENCE_BYTES if fence else 0)
    )
    out.extend([_BEQ, err_block_len])
    # error block:
    out.extend([_LDA_IMM, 0xFF])
    out.extend([_STA_ABS, _lo(error_addr), _hi(error_addr)])
    out.extend([_LDA_IMM, CMD_CLR_ERR])
    out.extend([_STA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    return out


def _build_read_response_tsx(
    pc: int,
    resp_addr: int,
    resp_len_addr: int,
    fence: bool = True,
) -> list[int]:
    """Turbo-safe ``_build_read_response`` — fence every UCI read/write,
    use JMP back for the main loop.

    Layout::

        LDY #$00
        STA  resp_len_lo
        STA  resp_len_hi
    loop (pc_loop):
        LDA  $DF1C
        <fence>
        AND  #BIT_DATA_AV
        BEQ  done_trampoline
        JMP  read
    done_trampoline:
        JMP  done
    read:
        LDA  $DF1E
        <fence>
        STA  resp_addr,Y
        INY
        LDA  #CMD_NEXT_DATA
        STA  $DF1C
        <fence>
        JMP  loop
    done:
        STY  resp_len
    """
    out: list[int] = []
    # Preamble: LDY #0; STA resp_len; STA resp_len+1
    out.extend([_LDY_IMM, 0x00])
    out.extend([_STA_ABS, _lo(resp_len_addr), _hi(resp_len_addr)])
    out.extend([_STA_ABS, _lo(resp_len_addr + 1),
                _hi(resp_len_addr + 1)])

    loop_abs = pc + len(out)
    # LDA $DF1C
    out.extend([_LDA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_AND_IMM, BIT_DATA_AV])
    # BEQ done_trampoline (3 bytes ahead — skip the "JMP read" trampoline)
    out.extend([_BEQ, 0x03])
    # JMP read  (will patch target after we know where read is)
    jmp_read_pos = len(out)
    out.extend([_JMP_ABS, 0x00, 0x00])  # placeholder
    # done_trampoline: JMP done (placeholder, patched at end)
    jmp_done_pos = len(out)
    out.extend([_JMP_ABS, 0x00, 0x00])  # placeholder

    # read:
    read_abs = pc + len(out)
    out[jmp_read_pos + 1] = _lo(read_abs)
    out[jmp_read_pos + 2] = _hi(read_abs)
    # LDA $DF1E
    out.extend([_LDA_ABS, _lo(UCI_RESP_DATA_REG),
                _hi(UCI_RESP_DATA_REG)])
    if fence:
        out.extend(_build_fence())
    # STA resp_addr,Y
    out.extend([_STA_ABS_Y, _lo(resp_addr), _hi(resp_addr)])
    out.append(_INY)
    # LDA #CMD_NEXT_DATA; STA $DF1C
    out.extend([_LDA_IMM, CMD_NEXT_DATA])
    out.extend([_STA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    # JMP loop
    out.extend([_JMP_ABS, _lo(loop_abs), _hi(loop_abs)])

    # done:
    done_abs = pc + len(out)
    out[jmp_done_pos + 1] = _lo(done_abs)
    out[jmp_done_pos + 2] = _hi(done_abs)
    # STY resp_len
    out.extend([_STY_ABS, _lo(resp_len_addr), _hi(resp_len_addr)])

    return out


def _build_read_status_tsx(
    pc: int,
    status_addr: int,
    stat_len_addr: int,
    fence: bool = True,
) -> list[int]:
    """Turbo-safe ``_build_read_status`` — same shape as the response
    reader but reads $DF1F and tests BIT_STAT_AV."""
    out: list[int] = []
    out.extend([_LDY_IMM, 0x00])
    out.extend([_STA_ABS, _lo(stat_len_addr), _hi(stat_len_addr)])
    out.extend([_STA_ABS, _lo(stat_len_addr + 1),
                _hi(stat_len_addr + 1)])

    loop_abs = pc + len(out)
    out.extend([_LDA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_AND_IMM, BIT_STAT_AV])
    out.extend([_BEQ, 0x03])
    jmp_read_pos = len(out)
    out.extend([_JMP_ABS, 0x00, 0x00])
    jmp_done_pos = len(out)
    out.extend([_JMP_ABS, 0x00, 0x00])

    read_abs = pc + len(out)
    out[jmp_read_pos + 1] = _lo(read_abs)
    out[jmp_read_pos + 2] = _hi(read_abs)
    out.extend([_LDA_ABS, _lo(UCI_STATUS_DATA_REG),
                _hi(UCI_STATUS_DATA_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_STA_ABS_Y, _lo(status_addr), _hi(status_addr)])
    out.append(_INY)
    out.extend([_LDA_IMM, CMD_NEXT_DATA])
    out.extend([_STA_ABS, _lo(UCI_CONTROL_STATUS_REG),
                _hi(UCI_CONTROL_STATUS_REG)])
    if fence:
        out.extend(_build_fence())
    out.extend([_JMP_ABS, _lo(loop_abs), _hi(loop_abs)])

    done_abs = pc + len(out)
    out[jmp_done_pos + 1] = _lo(done_abs)
    out[jmp_done_pos + 2] = _hi(done_abs)
    out.extend([_STY_ABS, _lo(stat_len_addr), _hi(stat_len_addr)])

    return out


def _emit_write_cmd_data_tsx(val: int, fence: bool = True) -> list[int]:
    """Fenced LDA #val; STA $DF1D — used to push command/target/param bytes."""
    out = [_LDA_IMM, val & 0xFF,
           _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG)]
    if fence:
        out.extend(_build_fence())
    return out


def _emit_write_cmd_from_mem_tsx(src_addr: int, fence: bool = True) -> list[int]:
    """Fenced LDA src_addr; STA $DF1D — used to push a byte sourced from
    regular C64 memory (e.g. socket ID pre-staged by the host).
    """
    out = [_LDA_ABS, _lo(src_addr), _hi(src_addr),
           _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG)]
    if fence:
        out.extend(_build_fence())
    return out


def _build_abort_preamble_tsx(fence: bool = True) -> list[int]:
    """Turbo-safe abort preamble — same as the plain version but with a fence
    after the control-register write so the abort is fully latched before
    the ``LDX #$FF / DEX`` settle loop runs.
    """
    out = [
        _LDA_IMM, CMD_ABORT,
        _STA_ABS, _lo(UCI_CONTROL_STATUS_REG),
        _hi(UCI_CONTROL_STATUS_REG),
    ]
    if fence:
        out.extend(_build_fence())
    out.extend([
        _LDX_IMM, 0xFF,
        _DEX,
        _BNE, 0xFD,
    ])
    return out


def _build_sentinel(sentinel_addr: int, value: int = _SENTINEL_DONE) -> list[int]:
    """6502 fragment: write sentinel value."""
    return [
        _LDA_IMM, value,
        _STA_ABS, _lo(sentinel_addr), _hi(sentinel_addr),
    ]


def _build_park(code_so_far_len: int, base: int = _CODE_ADDR) -> list[int]:
    """6502 fragment: JMP to self (park CPU)."""
    park = base + code_so_far_len + 3  # +3 for the JMP instruction itself
    # BUT we add sentinel before park, so caller must include sentinel length
    # Actually, caller adds this after sentinel, so code_so_far_len includes sentinel
    return [_JMP_ABS, _lo(park), _hi(park)]


# ---------------------------------------------------------------------------
# Public assembly builders
# ---------------------------------------------------------------------------

def build_uci_probe(
    result_addr: int = _RESP_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build 6502 routine: read UCI ID byte, store at *result_addr*, set sentinel.

    The ID register is at $DF1D (read). A value of 0xC9 confirms UCI
    is present on the hardware.

    :param turbo_safe: If ``True``, insert the delay-loop fence after the
        ``LDA $DF1D`` so the FPGA has time to settle the ID byte before the
        CPU stores it. See :func:`build_uci_command` for background.
    """
    code: list[int] = []
    # Read UCI ID
    code.extend([_LDA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG)])
    if turbo_safe:
        code.extend(_build_fence())
    code.extend([_STA_ABS, _lo(result_addr), _hi(result_addr)])
    # Set sentinel + RTS (routine is dispatched via SYS, returns to BASIC)
    code.extend(_build_sentinel(sentinel_addr))
    code.append(_RTS)
    return bytes(code)


def build_uci_command(
    target: int = TARGET_NETWORK,
    cmd: int = NET_CMD_GET_INTERFACE_COUNT,
    params: bytes | list[int] = b"",
    resp_addr: int = _RESP_ADDR,
    status_addr: int = _STATUS_ADDR,
    resp_len_addr: int = _RESP_LEN_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build generic UCI command routine.

    Sends *target* + *cmd* + *params*, reads response and status, sets sentinel.
    Error flag is stored at *error_addr* ($00 = ok, $FF = error).

    :param turbo_safe: If ``True``, emit the delay-loop fence after every UCI
        register access and convert loop-back short branches to JMP trampolines.
        This is required when the U64 CPU runs at turbo speed (8/24/48 MHz) —
        the FPGA behind ``$DF1C``-``$DF1F`` needs ~38 µs to latch writes and
        settle reads. At stock 1 MHz the plain (unfenced) path is faster and
        just as correct. Defaults to ``False`` for backward compatibility.
    """
    if isinstance(params, list):
        params = bytes(params)

    code: list[int] = []

    def pc() -> int:
        return code_addr + len(code)

    # Clear error flag
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    if turbo_safe:
        code.extend(_build_abort_preamble_tsx())
    else:
        code.extend(_build_abort_preamble())

    # Wait for idle
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_wait_idle())

    # Write target byte
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(target))
    else:
        code.extend([
            _LDA_IMM, target,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Write command byte
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(cmd))
    else:
        code.extend([
            _LDA_IMM, cmd,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Write parameter bytes
    for b in params:
        if turbo_safe:
            code.extend(_emit_write_cmd_data_tsx(b))
        else:
            code.extend([
                _LDA_IMM, b,
                _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            ])

    # Push command and wait
    if turbo_safe:
        code.extend(_build_push_and_wait_tsx(pc()))
    else:
        code.extend(_build_push_and_wait())

    # Check error
    if turbo_safe:
        code.extend(_build_check_error_tsx(pc(), error_addr))
    else:
        code.extend(_build_check_error(error_addr))

    # Read response data
    if turbo_safe:
        code.extend(_build_read_response_tsx(pc(), resp_addr, resp_len_addr))
    else:
        code.extend(_build_read_response(resp_addr, resp_len_addr))

    # Read status string
    if turbo_safe:
        code.extend(_build_read_status_tsx(pc(), status_addr, stat_len_addr))
    else:
        code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_acknowledge())

    # Sentinel + RTS (routine is dispatched via SYS, returns to BASIC)
    code.extend(_build_sentinel(sentinel_addr))
    code.append(_RTS)

    return bytes(code)


def build_get_ip(
    result_addr: int = _RESP_ADDR,
    status_addr: int = _STATUS_ADDR,
    resp_len_addr: int = _RESP_LEN_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build routine: GET_IP_ADDRESS, stores IP string at *result_addr*.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    return build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_IPADDR,
        params=bytes([0x00]),  # interface index 0
        resp_addr=result_addr,
        status_addr=status_addr,
        resp_len_addr=resp_len_addr,
        stat_len_addr=stat_len_addr,
        error_addr=error_addr,
        sentinel_addr=sentinel_addr,
        code_addr=code_addr,
        turbo_safe=turbo_safe,
    )


def build_tcp_connect(
    host_addr: int = _HOST_ADDR,
    port: int = 80,
    result_addr: int = _RESP_ADDR,
    status_addr: int = _STATUS_ADDR,
    resp_len_addr: int = _RESP_LEN_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build routine: TCP_SOCKET_CONNECT.

    The hostname must be pre-loaded at *host_addr* as a null-terminated
    ASCII string.  *port* is encoded little-endian in the command params.
    The socket ID is stored in the first byte of *result_addr*.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    return _build_connect_routine(
        NET_CMD_TCP_CONNECT, host_addr, port,
        result_addr, status_addr, resp_len_addr,
        stat_len_addr, error_addr, sentinel_addr, code_addr,
        turbo_safe=turbo_safe,
    )


def build_udp_connect(
    host_addr: int = _HOST_ADDR,
    port: int = 53,
    result_addr: int = _RESP_ADDR,
    status_addr: int = _STATUS_ADDR,
    resp_len_addr: int = _RESP_LEN_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build routine: UDP_SOCKET_CONNECT (same structure as TCP).

    :param turbo_safe: see :func:`build_uci_command`.
    """
    return _build_connect_routine(
        NET_CMD_UDP_CONNECT, host_addr, port,
        result_addr, status_addr, resp_len_addr,
        stat_len_addr, error_addr, sentinel_addr, code_addr,
        turbo_safe=turbo_safe,
    )


def _build_connect_routine(
    cmd: int,
    host_addr: int,
    port: int,
    result_addr: int,
    status_addr: int,
    resp_len_addr: int,
    stat_len_addr: int,
    error_addr: int,
    sentinel_addr: int,
    code_addr: int,
    turbo_safe: bool = False,
) -> bytes:
    """Build TCP or UDP connect routine with hostname from C64 memory."""
    port_lo = port & 0xFF
    port_hi = (port >> 8) & 0xFF

    code: list[int] = []

    def pc() -> int:
        return code_addr + len(code)

    # Clear error flag
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    if turbo_safe:
        code.extend(_build_abort_preamble_tsx())
    else:
        code.extend(_build_abort_preamble())

    # Wait for idle
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_wait_idle())

    # Write target
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(TARGET_NETWORK))
    else:
        code.extend([
            _LDA_IMM, TARGET_NETWORK,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Write command
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(cmd))
    else:
        code.extend([
            _LDA_IMM, cmd,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Write port (LE)
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(port_lo))
        code.extend(_emit_write_cmd_data_tsx(port_hi))
    else:
        code.extend([
            _LDA_IMM, port_lo,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            _LDA_IMM, port_hi,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Write hostname bytes from host_addr until null terminator
    if turbo_safe:
        # Turbo-safe hostname loop — fence after each STA $DF1D, JMP back
        # for loop (short branch can't reach past a fence expansion).
        #
        #   LDY #0
        #   loop:
        #     LDA host,Y       (3)
        #     BEQ +3           (2)  -> skip JMP write_host  (i.e. reached null)
        #     JMP write_host   (3)
        #     ; null: write terminator and fall through
        #     STA $DF1D        (3)
        #     <fence>
        #     JMP after_host
        #   write_host:
        #     STA $DF1D        (3)
        #     <fence>
        #     INY              (1)
        #     BEQ +3           (2)  -> Y wrapped 255->0, bail
        #     JMP loop         (3)
        #     ; fall through on Y wrap
        #   after_host:
        host_loop_abs = pc()
        code.extend([_LDY_IMM, 0x00])
        loop_abs = pc()
        code.extend([_LDA_ABS_Y, _lo(host_addr), _hi(host_addr)])
        # BEQ +3 -> skip "JMP write_host" (3 bytes)
        code.extend([_BEQ, 0x03])
        jmp_write_pos = len(code)
        code.extend([_JMP_ABS, 0x00, 0x00])  # patched below
        # Null path: write null terminator, fence, JMP after_host
        code.extend([_STA_ABS, _lo(UCI_CMD_DATA_REG),
                     _hi(UCI_CMD_DATA_REG)])
        code.extend(_build_fence())
        jmp_after_pos = len(code)
        code.extend([_JMP_ABS, 0x00, 0x00])  # patched at end
        # write_host:
        write_host_abs = pc()
        code[jmp_write_pos + 1] = _lo(write_host_abs)
        code[jmp_write_pos + 2] = _hi(write_host_abs)
        code.extend([_STA_ABS, _lo(UCI_CMD_DATA_REG),
                     _hi(UCI_CMD_DATA_REG)])
        code.extend(_build_fence())
        code.append(_INY)
        code.extend([_BEQ, 0x03])  # Y wrapped — bail
        code.extend([_JMP_ABS, _lo(loop_abs), _hi(loop_abs)])
        # after_host:
        after_abs = pc()
        code[jmp_after_pos + 1] = _lo(after_abs)
        code[jmp_after_pos + 2] = _hi(after_abs)
        # Keep `host_loop_abs` referenced for future debug; silence linter:
        _ = host_loop_abs
    else:
        # 1 MHz path — short branches are fine.
        # LDY #$00(2); LDA host,Y(3); BEQ +6(2); STA $DF1D(3); INY(1); BNE -11(2)
        # STA $DF1D(3) — write null terminator
        code.extend([
            _LDY_IMM, 0x00,
            # loop:
            _LDA_ABS_Y, _lo(host_addr), _hi(host_addr),
            _BEQ, 6,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            _INY,
            _BNE, 0xF5,   # -11
            # done_host: write the null terminator
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Push command and wait
    if turbo_safe:
        code.extend(_build_push_and_wait_tsx(pc()))
    else:
        code.extend(_build_push_and_wait())

    # Check error
    if turbo_safe:
        code.extend(_build_check_error_tsx(pc(), error_addr))
    else:
        code.extend(_build_check_error(error_addr))

    # Read response (socket ID in first byte)
    if turbo_safe:
        code.extend(_build_read_response_tsx(pc(), result_addr, resp_len_addr))
    else:
        code.extend(_build_read_response(result_addr, resp_len_addr))

    # Read status
    if turbo_safe:
        code.extend(_build_read_status_tsx(pc(), status_addr, stat_len_addr))
    else:
        code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_acknowledge())

    # Sentinel + RTS (routine is dispatched via SYS, returns to BASIC)
    code.extend(_build_sentinel(sentinel_addr))
    code.append(_RTS)

    return bytes(code)


def build_socket_write(
    socket_id_addr: int = _SOCKET_ID_ADDR,
    data_addr: int = _DATA_BUF_ADDR,
    data_len_addr: int = _DATA_LEN_ADDR,
    status_addr: int = _STATUS_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build routine: SOCKET_WRITE.

    Socket ID is read from *socket_id_addr* (1 byte).
    Data is at *data_addr*, length (1 byte, max 255) at *data_len_addr*.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    code: list[int] = []

    def pc() -> int:
        return code_addr + len(code)

    # Clear error
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    if turbo_safe:
        code.extend(_build_abort_preamble_tsx())
    else:
        code.extend(_build_abort_preamble())

    # Wait idle
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_wait_idle())

    # Target
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(TARGET_NETWORK))
    else:
        code.extend([
            _LDA_IMM, TARGET_NETWORK,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Command
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(NET_CMD_SOCKET_WRITE))
    else:
        code.extend([
            _LDA_IMM, NET_CMD_SOCKET_WRITE,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Socket ID param
    if turbo_safe:
        code.extend(_emit_write_cmd_from_mem_tsx(socket_id_addr))
    else:
        code.extend([
            _LDA_ABS, _lo(socket_id_addr), _hi(socket_id_addr),
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    if turbo_safe:
        # Turbo-safe payload loop — fence after each STA $DF1D, JMP back
        # because short BNE can't reach over a fence expansion.
        #
        #   LDA data_len; TAX; LDY #0
        #   loop:
        #     TXA
        #     BEQ +3   -> skip JMP write  (X=0 means done)
        #     JMP write
        #     JMP done            ; fall-through when no more bytes
        #   write:
        #     LDA data,Y
        #     STA $DF1D
        #     <fence>
        #     INY
        #     DEX
        #     JMP loop
        #   done:
        code.extend([_LDA_ABS, _lo(data_len_addr), _hi(data_len_addr)])
        code.append(_TAX)
        code.extend([_LDY_IMM, 0x00])
        loop_abs = pc()
        code.append(_TXA)
        code.extend([_BEQ, 0x03])  # skip JMP write
        jmp_write_pos = len(code)
        code.extend([_JMP_ABS, 0x00, 0x00])  # patched to write
        jmp_done_pos = len(code)
        code.extend([_JMP_ABS, 0x00, 0x00])  # patched to done
        # write:
        write_abs = pc()
        code[jmp_write_pos + 1] = _lo(write_abs)
        code[jmp_write_pos + 2] = _hi(write_abs)
        code.extend([_LDA_ABS_Y, _lo(data_addr), _hi(data_addr)])
        code.extend([_STA_ABS, _lo(UCI_CMD_DATA_REG),
                     _hi(UCI_CMD_DATA_REG)])
        code.extend(_build_fence())
        code.append(_INY)
        code.append(_DEX)
        code.extend([_JMP_ABS, _lo(loop_abs), _hi(loop_abs)])
        # done:
        done_abs = pc()
        code[jmp_done_pos + 1] = _lo(done_abs)
        code[jmp_done_pos + 2] = _hi(done_abs)
    else:
        # Write data bytes using X as counter, Y as index — 1 MHz path.
        code.extend([
            _LDA_ABS, _lo(data_len_addr), _hi(data_len_addr),
            _TAX,
            _LDY_IMM, 0x00,
            # loop:
            _TXA,
            _BEQ, 10,     # skip to done: LDA(3)+STA(3)+INY(1)+DEX(1)+BNE(2) = 10
            _LDA_ABS_Y, _lo(data_addr), _hi(data_addr),
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            _INY,
            _DEX,
            _BNE, 0xF3,   # -13 back to TXA
            # done:
        ])

    # Push and wait
    if turbo_safe:
        code.extend(_build_push_and_wait_tsx(pc()))
    else:
        code.extend(_build_push_and_wait())

    # Check error
    if turbo_safe:
        code.extend(_build_check_error_tsx(pc(), error_addr))
    else:
        code.extend(_build_check_error(error_addr))

    # Read status
    if turbo_safe:
        code.extend(_build_read_status_tsx(pc(), status_addr, stat_len_addr))
    else:
        code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_acknowledge())

    # Sentinel + RTS (routine is dispatched via SYS, returns to BASIC)
    code.extend(_build_sentinel(sentinel_addr))
    code.append(_RTS)

    return bytes(code)


def build_socket_read(
    socket_id_addr: int = _SOCKET_ID_ADDR,
    result_addr: int = _RESP_ADDR,
    max_len: int = 255,
    actual_len_addr: int = _RESP_LEN_ADDR,
    status_addr: int = _STATUS_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build routine: SOCKET_READ.

    Params: socket_id, length (2 bytes LE).
    Response data goes to *result_addr*, actual length to *actual_len_addr*.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    len_lo = max_len & 0xFF
    len_hi = (max_len >> 8) & 0xFF

    code: list[int] = []

    def pc() -> int:
        return code_addr + len(code)

    # Clear error
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    if turbo_safe:
        code.extend(_build_abort_preamble_tsx())
    else:
        code.extend(_build_abort_preamble())

    # Wait idle
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_wait_idle())

    # Target + command
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(TARGET_NETWORK))
        code.extend(_emit_write_cmd_data_tsx(NET_CMD_SOCKET_READ))
    else:
        code.extend([
            _LDA_IMM, TARGET_NETWORK,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            _LDA_IMM, NET_CMD_SOCKET_READ,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Socket ID
    if turbo_safe:
        code.extend(_emit_write_cmd_from_mem_tsx(socket_id_addr))
    else:
        code.extend([
            _LDA_ABS, _lo(socket_id_addr), _hi(socket_id_addr),
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Length (2 bytes LE)
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(len_lo))
        code.extend(_emit_write_cmd_data_tsx(len_hi))
    else:
        code.extend([
            _LDA_IMM, len_lo,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            _LDA_IMM, len_hi,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Push and wait
    if turbo_safe:
        code.extend(_build_push_and_wait_tsx(pc()))
    else:
        code.extend(_build_push_and_wait())

    # Check error
    if turbo_safe:
        code.extend(_build_check_error_tsx(pc(), error_addr))
    else:
        code.extend(_build_check_error(error_addr))

    # Read response
    if turbo_safe:
        code.extend(_build_read_response_tsx(pc(), result_addr, actual_len_addr))
    else:
        code.extend(_build_read_response(result_addr, actual_len_addr))

    # Read status
    if turbo_safe:
        code.extend(_build_read_status_tsx(pc(), status_addr, stat_len_addr))
    else:
        code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_acknowledge())

    # Sentinel + RTS (routine is dispatched via SYS, returns to BASIC)
    code.extend(_build_sentinel(sentinel_addr))
    code.append(_RTS)

    return bytes(code)


def build_socket_close(
    socket_id_addr: int = _SOCKET_ID_ADDR,
    status_addr: int = _STATUS_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
    turbo_safe: bool = False,
) -> bytes:
    """Build routine: SOCKET_CLOSE.

    Socket ID is read from *socket_id_addr* (1 byte).

    :param turbo_safe: see :func:`build_uci_command`.
    """
    code: list[int] = []

    def pc() -> int:
        return code_addr + len(code)

    # Clear error
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    if turbo_safe:
        code.extend(_build_abort_preamble_tsx())
    else:
        code.extend(_build_abort_preamble())

    # Wait idle
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_wait_idle())

    # Target + command
    if turbo_safe:
        code.extend(_emit_write_cmd_data_tsx(TARGET_NETWORK))
        code.extend(_emit_write_cmd_data_tsx(NET_CMD_SOCKET_CLOSE))
    else:
        code.extend([
            _LDA_IMM, TARGET_NETWORK,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
            _LDA_IMM, NET_CMD_SOCKET_CLOSE,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Socket ID
    if turbo_safe:
        code.extend(_emit_write_cmd_from_mem_tsx(socket_id_addr))
    else:
        code.extend([
            _LDA_ABS, _lo(socket_id_addr), _hi(socket_id_addr),
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Push and wait
    if turbo_safe:
        code.extend(_build_push_and_wait_tsx(pc()))
    else:
        code.extend(_build_push_and_wait())

    # Check error
    if turbo_safe:
        code.extend(_build_check_error_tsx(pc(), error_addr))
    else:
        code.extend(_build_check_error(error_addr))

    # Read status
    if turbo_safe:
        code.extend(_build_read_status_tsx(pc(), status_addr, stat_len_addr))
    else:
        code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    if turbo_safe:
        code.extend(_build_wait_idle_tsx(pc()))
    else:
        code.extend(_build_acknowledge())

    # Sentinel + RTS (routine is dispatched via SYS, returns to BASIC)
    code.extend(_build_sentinel(sentinel_addr))
    code.append(_RTS)

    return bytes(code)


# ---------------------------------------------------------------------------
# Internal: execute routine on U64 via DMA trampoline
# ---------------------------------------------------------------------------

class UCIError(Exception):
    """UCI command returned an error."""


def _execute_uci_routine(
    transport: C64Transport,
    code: bytes,
    code_addr: int = _CODE_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    error_addr: int = _ERROR_ADDR,
    timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    """Inject and execute a UCI routine on the U64.

    Dispatches via ``SYS <code_addr>\\r`` injected into the keyboard
    buffer ($0277, length in $00C6). BASIC's command-line processor
    reads the buffer as if the user typed the SYS command, which calls
    the routine as a subroutine via JSR; the routine writes the sentinel
    byte and executes RTS to return to BASIC.

    The prior IMAIN ($0302) vector patch did not work on cold devices
    because BASIC's idle READY loop does not traverse $0302 — only the
    command-line processor (which runs after the user hits RETURN) does.
    On warm machines the patch happened to succeed because prior BASIC
    activity had already exercised that code path; on cold boots the
    routine never fired.

    Sequence:
    1. Clear sentinel and error bytes.
    2. Write CTL_ABORT (0x04) to $DF1C, sleep briefly to drain UCI state.
    3. Write the routine code to *code_addr*.
    4. Inject ``SYS <code_addr>\\r`` at $0277, set keyboard fill count
       at $00C6 to the command length.
    5. Poll sentinel until set or *timeout* expires.
    6. Check error flag and raise UCIError with status if set.

    Routines dispatched this way MUST end with RTS, not JMP or BRK.

    Raises:
        UCIError: If the error flag is set after execution.
        TimeoutError: If sentinel is not set within *timeout* seconds.
    """
    from .transport import TimeoutError

    # Clear sentinel and error
    transport.write_memory(sentinel_addr, bytes([0x00]))
    transport.write_memory(error_addr, bytes([0x00]))

    # Clear any stale UCI state via DMA abort
    transport.write_memory(0xDF1C, bytes([0x04]))  # CTL_ABORT
    time.sleep(0.1)  # Let UCI process the abort

    # Write routine
    transport.write_memory(code_addr, code)

    # Inject "SYS <code_addr>\r" into the keyboard buffer so BASIC's
    # command-line processor JSRs our routine.
    sys_cmd = f"SYS{code_addr}\r".encode("ascii")
    if len(sys_cmd) > 10:
        raise ValueError(
            f"SYS command too long for keyboard buffer: "
            f"{len(sys_cmd)} bytes (max 10). code_addr=${code_addr:04X}"
        )
    transport.write_memory(0x0277, sys_cmd)
    transport.write_memory(0x00C6, bytes([len(sys_cmd)]))

    # Poll sentinel
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = transport.read_memory(sentinel_addr, 1)
        if val[0] == _SENTINEL_DONE:
            break
        time.sleep(_POLL_INTERVAL)
    else:
        raise TimeoutError(
            f"UCI routine did not complete within {timeout}s "
            f"(sentinel at ${sentinel_addr:04X} never set)"
        )

    # Check error flag
    err = transport.read_memory(error_addr, 1)
    if err[0] != 0x00:
        stat_len = transport.read_memory(_STAT_LEN_ADDR, 1)[0]
        status_msg = ""
        if stat_len > 0:
            raw = transport.read_memory(_STATUS_ADDR, stat_len)
            status_msg = raw.decode("ascii", errors="replace")
        raise UCIError(f"UCI command failed: {status_msg}" if status_msg
                       else "UCI command returned error")


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def uci_probe(
    transport: C64Transport,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> int:
    """Read the UCI identification byte.

    Returns the ID value (0xC9 if UCI is present, 0x00 otherwise).

    :param turbo_safe: If ``True``, emit the delay-loop fence required for
        turbo CPU speeds (8/24/48 MHz) on real U64E. See
        :func:`build_uci_command` for background.
    """
    code = build_uci_probe(turbo_safe=turbo_safe)
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_get_ip(
    transport: C64Transport,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> str:
    """Query the U64's IP address via UCI GET_IP_ADDRESS.

    Returns the IP address as a dotted-quad string (e.g. ``"192.168.1.81"``).

    :param turbo_safe: see :func:`build_uci_command`.

    .. note::
        The UCI firmware returns 12 raw bytes: IP(4) + Netmask(4) +
        Gateway(4).  This helper extracts the first 4 bytes and formats
        them.  If the response looks like ASCII text (firmware variation),
        it is returned as-is.
    """
    code = build_get_ip(turbo_safe=turbo_safe)
    _execute_uci_routine(transport, code, timeout=timeout)
    resp_len = transport.read_memory(_RESP_LEN_ADDR, 1)[0]
    if resp_len == 0:
        return ""
    raw = transport.read_memory(_RESP_ADDR, resp_len)
    # Firmware returns 12 raw bytes: IP(4)+Netmask(4)+Gateway(4).
    # If the response is exactly 12 bytes and looks binary, parse it.
    # Otherwise treat as ASCII (for compatibility / firmware variations).
    if resp_len == 12 and not all(0x20 <= b < 0x7F for b in raw):
        return f"{raw[0]}.{raw[1]}.{raw[2]}.{raw[3]}"
    return raw.decode("ascii", errors="replace").rstrip("\x00")


def uci_get_interface_count(
    transport: C64Transport,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> int:
    """Query the number of network interfaces via UCI.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_INTERFACE_COUNT,
        turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)
    resp_len = transport.read_memory(_RESP_LEN_ADDR, 1)[0]
    if resp_len == 0:
        return 0
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_tcp_connect(
    transport: C64Transport,
    host: str,
    port: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> int:
    """Open a TCP connection to *host*:*port*.

    Returns the UCI socket ID (used for read/write/close).

    :param turbo_safe: see :func:`build_uci_command`.
    """
    host_bytes = host.encode("ascii") + b"\x00"
    transport.write_memory(_DATA_ADDR, host_bytes)
    code = build_tcp_connect(_DATA_ADDR, port, turbo_safe=turbo_safe)
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_udp_connect(
    transport: C64Transport,
    host: str,
    port: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> int:
    """Open a UDP socket to *host*:*port*.

    Returns the UCI socket ID.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    host_bytes = host.encode("ascii") + b"\x00"
    transport.write_memory(_DATA_ADDR, host_bytes)
    code = build_udp_connect(_DATA_ADDR, port, turbo_safe=turbo_safe)
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_socket_write(
    transport: C64Transport,
    socket_id: int,
    data: bytes,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> None:
    """Write *data* to a UCI socket.

    *data* must be at most 255 bytes (single-call limit due to Y register
    indexing).  For larger payloads, call this function in a loop.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    if len(data) > 255:
        raise ValueError(f"data must be <= 255 bytes, got {len(data)}")

    socket_id_addr = _DATA_ADDR
    data_area = _DATA_ADDR + 1
    data_len_addr = _DATA_ADDR + 1 + len(data)

    transport.write_memory(socket_id_addr, bytes([socket_id]))
    transport.write_memory(data_area, data)
    transport.write_memory(data_len_addr, bytes([len(data)]))

    code = build_socket_write(
        socket_id_addr, data_area, data_len_addr,
        turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)


def uci_socket_read(
    transport: C64Transport,
    socket_id: int,
    max_len: int = 255,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> bytes:
    """Read up to *max_len* bytes from a UCI socket.

    Returns the received data (may be shorter than *max_len*).

    :param turbo_safe: see :func:`build_uci_command`.

    .. note::
        The UCI firmware response is ``[actual_len_lo] [actual_len_hi]
        [data...]``.  The 6502 routine stores the full response at the
        result address; this helper reads *resp_len* bytes from there.
    """
    if max_len > 255:
        raise ValueError(f"max_len must be <= 255, got {max_len}")

    socket_id_addr = _DATA_ADDR
    transport.write_memory(socket_id_addr, bytes([socket_id]))

    code = build_socket_read(
        socket_id_addr, max_len=max_len, turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)

    actual_len = transport.read_memory(_RESP_LEN_ADDR, 1)[0]
    if actual_len == 0:
        return b""
    return transport.read_memory(_RESP_ADDR, actual_len)


def uci_socket_close(
    transport: C64Transport,
    socket_id: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> None:
    """Close a UCI socket.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    socket_id_addr = _DATA_ADDR
    transport.write_memory(socket_id_addr, bytes([socket_id]))

    code = build_socket_close(socket_id_addr, turbo_safe=turbo_safe)
    _execute_uci_routine(transport, code, timeout=timeout)


def uci_tcp_listen_start(
    transport: C64Transport,
    port: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> None:
    """Start a TCP listener on *port*.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    port_lo = port & 0xFF
    port_hi = (port >> 8) & 0xFF
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_TCP_LISTENER_START,
        params=bytes([port_lo, port_hi]),
        turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)


def uci_tcp_listen_state(
    transport: C64Transport,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> int:
    """Query the TCP listener state.

    Returns 0=idle, 1=listening, 2=connected, 3=bind_error, 4=port_in_use.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_LISTENER_STATE,
        turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)
    resp_len = transport.read_memory(_RESP_LEN_ADDR, 1)[0]
    if resp_len == 0:
        return 0
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_tcp_listen_socket(
    transport: C64Transport,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> int:
    """Get the accepted socket ID from the TCP listener.

    Call this after :func:`uci_tcp_listen_state` returns CONNECTED.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_LISTENER_SOCKET,
        turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_tcp_listen_stop(
    transport: C64Transport,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    turbo_safe: bool = False,
) -> None:
    """Stop the TCP listener.

    :param turbo_safe: see :func:`build_uci_command`.
    """
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_TCP_LISTENER_STOP,
        turbo_safe=turbo_safe,
    )
    _execute_uci_routine(transport, code, timeout=timeout)


# ---------------------------------------------------------------------------
# UCI config helpers (REST API — require Ultimate64Client, not transport)
# ---------------------------------------------------------------------------

_UCI_CATEGORY = "C64 and Cartridge Settings"
_UCI_ITEM = "Command Interface"


def get_uci_enabled(client: "Ultimate64Client") -> bool:
    """Return ``True`` if UCI (Command Interface) is currently enabled.

    Reads the ``C64 and Cartridge Settings`` category from the device
    and checks whether the ``Command Interface`` item is ``"Enabled"``.

    :param client: Connected :class:`Ultimate64Client` instance.
    :returns: ``True`` when UCI is enabled, ``False`` otherwise.
    """
    resp = client.get_config_category(_UCI_CATEGORY)
    inner = resp.get(_UCI_CATEGORY, {})
    return inner.get(_UCI_ITEM) == "Enabled"


def enable_uci(client: "Ultimate64Client") -> None:
    """Enable UCI (Command Interface) on the device.

    Sets ``Command Interface`` to ``"Enabled"`` in the
    ``C64 and Cartridge Settings`` category.  The change is **not**
    saved to flash — a device reboot reverts to the default state.

    A machine reset is typically needed after enabling UCI so that
    the I/O registers at $DF1C-$DF1F become active.

    :param client: Connected :class:`Ultimate64Client` instance.
    """
    client.set_config_items(_UCI_CATEGORY, {_UCI_ITEM: "Enabled"})


def disable_uci(client: "Ultimate64Client") -> None:
    """Disable UCI (Command Interface) on the device.

    Sets ``Command Interface`` to ``"Disabled"`` in the
    ``C64 and Cartridge Settings`` category.  The change is **not**
    saved to flash.

    :param client: Connected :class:`Ultimate64Client` instance.
    """
    client.set_config_items(_UCI_CATEGORY, {_UCI_ITEM: "Disabled"})
