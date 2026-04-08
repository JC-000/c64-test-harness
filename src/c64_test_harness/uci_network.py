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
) -> bytes:
    """Build 6502 routine: read UCI ID byte, store at *result_addr*, set sentinel.

    The ID register is at $DF1D (read). A value of 0xC9 confirms UCI
    is present on the hardware.
    """
    code: list[int] = []
    # Read UCI ID
    code.extend([
        _LDA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        _STA_ABS, _lo(result_addr), _hi(result_addr),
    ])
    # Set sentinel + park
    code.extend(_build_sentinel(sentinel_addr))
    park = code_addr + len(code) + 3
    code.extend([_JMP_ABS, _lo(park), _hi(park)])
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
) -> bytes:
    """Build generic UCI command routine.

    Sends *target* + *cmd* + *params*, reads response and status, sets sentinel.
    Error flag is stored at *error_addr* ($00 = ok, $FF = error).
    """
    if isinstance(params, list):
        params = bytes(params)

    code: list[int] = []

    # Clear error flag
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    code.extend(_build_abort_preamble())

    # Wait for idle
    code.extend(_build_wait_idle())

    # Write target byte
    code.extend([
        _LDA_IMM, target,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Write command byte
    code.extend([
        _LDA_IMM, cmd,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Write parameter bytes
    for b in params:
        code.extend([
            _LDA_IMM, b,
            _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        ])

    # Push command and wait
    code.extend(_build_push_and_wait())

    # Check error
    code.extend(_build_check_error(error_addr))

    # Read response data
    code.extend(_build_read_response(resp_addr, resp_len_addr))

    # Read status string
    code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    code.extend(_build_acknowledge())

    # Sentinel + park
    code.extend(_build_sentinel(sentinel_addr))
    park = code_addr + len(code) + 3
    code.extend([_JMP_ABS, _lo(park), _hi(park)])

    return bytes(code)


def build_get_ip(
    result_addr: int = _RESP_ADDR,
    status_addr: int = _STATUS_ADDR,
    resp_len_addr: int = _RESP_LEN_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
) -> bytes:
    """Build routine: GET_IP_ADDRESS, stores IP string at *result_addr*."""
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
) -> bytes:
    """Build routine: TCP_SOCKET_CONNECT.

    The hostname must be pre-loaded at *host_addr* as a null-terminated
    ASCII string.  *port* is encoded little-endian in the command params.
    The socket ID is stored in the first byte of *result_addr*.
    """
    return _build_connect_routine(
        NET_CMD_TCP_CONNECT, host_addr, port,
        result_addr, status_addr, resp_len_addr,
        stat_len_addr, error_addr, sentinel_addr, code_addr,
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
) -> bytes:
    """Build routine: UDP_SOCKET_CONNECT (same structure as TCP)."""
    return _build_connect_routine(
        NET_CMD_UDP_CONNECT, host_addr, port,
        result_addr, status_addr, resp_len_addr,
        stat_len_addr, error_addr, sentinel_addr, code_addr,
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
) -> bytes:
    """Build TCP or UDP connect routine with hostname from C64 memory."""
    port_lo = port & 0xFF
    port_hi = (port >> 8) & 0xFF

    code: list[int] = []

    # Clear error flag
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    code.extend(_build_abort_preamble())

    # Wait for idle
    code.extend(_build_wait_idle())

    # Write target
    code.extend([
        _LDA_IMM, TARGET_NETWORK,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Write command
    code.extend([
        _LDA_IMM, cmd,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Write port (LE)
    code.extend([
        _LDA_IMM, port_lo,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        _LDA_IMM, port_hi,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Write hostname bytes from host_addr until null terminator
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
    code.extend(_build_push_and_wait())

    # Check error
    code.extend(_build_check_error(error_addr))

    # Read response (socket ID in first byte)
    code.extend(_build_read_response(result_addr, resp_len_addr))

    # Read status
    code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    code.extend(_build_acknowledge())

    # Sentinel + park
    code.extend(_build_sentinel(sentinel_addr))
    park = code_addr + len(code) + 3
    code.extend([_JMP_ABS, _lo(park), _hi(park)])

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
) -> bytes:
    """Build routine: SOCKET_WRITE.

    Socket ID is read from *socket_id_addr* (1 byte).
    Data is at *data_addr*, length (1 byte, max 255) at *data_len_addr*.
    """
    code: list[int] = []

    # Clear error
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    code.extend(_build_abort_preamble())

    # Wait idle
    code.extend(_build_wait_idle())

    # Target
    code.extend([
        _LDA_IMM, TARGET_NETWORK,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Command
    code.extend([
        _LDA_IMM, NET_CMD_SOCKET_WRITE,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Socket ID param
    code.extend([
        _LDA_ABS, _lo(socket_id_addr), _hi(socket_id_addr),
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Write data bytes using X as counter, Y as index
    # LDA len(3); TAX(1); LDY #$00(2)
    # loop: TXA(1); BEQ +10(2); LDA data,Y(3); STA $DF1D(3); INY(1); DEX(1); BNE -13(2)
    # done:
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
    code.extend(_build_push_and_wait())

    # Check error
    code.extend(_build_check_error(error_addr))

    # Read status
    code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    code.extend(_build_acknowledge())

    # Sentinel + park
    code.extend(_build_sentinel(sentinel_addr))
    park = code_addr + len(code) + 3
    code.extend([_JMP_ABS, _lo(park), _hi(park)])

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
) -> bytes:
    """Build routine: SOCKET_READ.

    Params: socket_id, length (2 bytes LE).
    Response data goes to *result_addr*, actual length to *actual_len_addr*.
    """
    len_lo = max_len & 0xFF
    len_hi = (max_len >> 8) & 0xFF

    code: list[int] = []

    # Clear error
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    code.extend(_build_abort_preamble())

    # Wait idle
    code.extend(_build_wait_idle())

    # Target + command
    code.extend([
        _LDA_IMM, TARGET_NETWORK,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        _LDA_IMM, NET_CMD_SOCKET_READ,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Socket ID
    code.extend([
        _LDA_ABS, _lo(socket_id_addr), _hi(socket_id_addr),
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Length (2 bytes LE)
    code.extend([
        _LDA_IMM, len_lo,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        _LDA_IMM, len_hi,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Push and wait
    code.extend(_build_push_and_wait())

    # Check error
    code.extend(_build_check_error(error_addr))

    # Read response
    code.extend(_build_read_response(result_addr, actual_len_addr))

    # Read status
    code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    code.extend(_build_acknowledge())

    # Sentinel + park
    code.extend(_build_sentinel(sentinel_addr))
    park = code_addr + len(code) + 3
    code.extend([_JMP_ABS, _lo(park), _hi(park)])

    return bytes(code)


def build_socket_close(
    socket_id_addr: int = _SOCKET_ID_ADDR,
    status_addr: int = _STATUS_ADDR,
    stat_len_addr: int = _STAT_LEN_ADDR,
    error_addr: int = _ERROR_ADDR,
    sentinel_addr: int = _SENTINEL_ADDR,
    code_addr: int = _CODE_ADDR,
) -> bytes:
    """Build routine: SOCKET_CLOSE.

    Socket ID is read from *socket_id_addr* (1 byte).
    """
    code: list[int] = []

    # Clear error
    code.extend([
        _LDA_IMM, 0x00,
        _STA_ABS, _lo(error_addr), _hi(error_addr),
    ])

    # Abort any pending state
    code.extend(_build_abort_preamble())

    # Wait idle
    code.extend(_build_wait_idle())

    # Target + command
    code.extend([
        _LDA_IMM, TARGET_NETWORK,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
        _LDA_IMM, NET_CMD_SOCKET_CLOSE,
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Socket ID
    code.extend([
        _LDA_ABS, _lo(socket_id_addr), _hi(socket_id_addr),
        _STA_ABS, _lo(UCI_CMD_DATA_REG), _hi(UCI_CMD_DATA_REG),
    ])

    # Push and wait
    code.extend(_build_push_and_wait())

    # Check error
    code.extend(_build_check_error(error_addr))

    # Read status
    code.extend(_build_read_status(status_addr, stat_len_addr))

    # Acknowledge
    code.extend(_build_acknowledge())

    # Sentinel + park
    code.extend(_build_sentinel(sentinel_addr))
    park = code_addr + len(code) + 3
    code.extend([_JMP_ABS, _lo(park), _hi(park)])

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

    Uses the DMA trampoline pattern:
    1. Clear sentinel
    2. Write routine code
    3. Hijack IGONE vector ($0302) with pointer to code_addr
    4. Poll sentinel for completion
    5. Restore IGONE vector
    6. Check error flag

    The BASIC idle loop calls through the IGONE vector ($0302/$0303)
    every statement.  We patch it to jump into our routine so the CPU
    executes the UCI code on the next iteration.

    Raises:
        UCIError: If the error flag is set after execution.
        TimeoutError: If sentinel is not set within *timeout* seconds.
    """
    from .transport import TimeoutError

    igone_addr = 0x0302
    orig_igone = transport.read_memory(igone_addr, 2)

    # Clear sentinel and error
    transport.write_memory(sentinel_addr, bytes([0x00]))
    transport.write_memory(error_addr, bytes([0x00]))

    # Clear any stale UCI state via DMA abort
    transport.write_memory(0xDF1C, bytes([0x04]))  # CTL_ABORT
    time.sleep(0.1)  # Let UCI process the abort

    # Write routine
    transport.write_memory(code_addr, code)

    # Patch IGONE to jump to our code
    transport.write_memory(igone_addr, bytes([_lo(code_addr), _hi(code_addr)]))

    try:
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
    finally:
        # Restore IGONE vector
        transport.write_memory(igone_addr, orig_igone)

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

def uci_probe(transport: C64Transport, *, timeout: float = _DEFAULT_TIMEOUT) -> int:
    """Read the UCI identification byte.

    Returns the ID value (0xC9 if UCI is present, 0x00 otherwise).
    """
    code = build_uci_probe()
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_get_ip(transport: C64Transport, *, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Query the U64's IP address via UCI GET_IP_ADDRESS.

    Returns the IP address as a dotted-quad string (e.g. ``"192.168.1.81"``).

    .. note::
        The UCI firmware returns 12 raw bytes: IP(4) + Netmask(4) +
        Gateway(4).  This helper extracts the first 4 bytes and formats
        them.  If the response looks like ASCII text (firmware variation),
        it is returned as-is.
    """
    code = build_get_ip()
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
    transport: C64Transport, *, timeout: float = _DEFAULT_TIMEOUT,
) -> int:
    """Query the number of network interfaces via UCI."""
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_INTERFACE_COUNT,
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
) -> int:
    """Open a TCP connection to *host*:*port*.

    Returns the UCI socket ID (used for read/write/close).
    """
    host_bytes = host.encode("ascii") + b"\x00"
    transport.write_memory(_DATA_ADDR, host_bytes)
    code = build_tcp_connect(_DATA_ADDR, port)
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_udp_connect(
    transport: C64Transport,
    host: str,
    port: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> int:
    """Open a UDP socket to *host*:*port*.

    Returns the UCI socket ID.
    """
    host_bytes = host.encode("ascii") + b"\x00"
    transport.write_memory(_DATA_ADDR, host_bytes)
    code = build_udp_connect(_DATA_ADDR, port)
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_socket_write(
    transport: C64Transport,
    socket_id: int,
    data: bytes,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    """Write *data* to a UCI socket.

    *data* must be at most 255 bytes (single-call limit due to Y register
    indexing).  For larger payloads, call this function in a loop.
    """
    if len(data) > 255:
        raise ValueError(f"data must be <= 255 bytes, got {len(data)}")

    socket_id_addr = _DATA_ADDR
    data_area = _DATA_ADDR + 1
    data_len_addr = _DATA_ADDR + 1 + len(data)

    transport.write_memory(socket_id_addr, bytes([socket_id]))
    transport.write_memory(data_area, data)
    transport.write_memory(data_len_addr, bytes([len(data)]))

    code = build_socket_write(socket_id_addr, data_area, data_len_addr)
    _execute_uci_routine(transport, code, timeout=timeout)


def uci_socket_read(
    transport: C64Transport,
    socket_id: int,
    max_len: int = 255,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bytes:
    """Read up to *max_len* bytes from a UCI socket.

    Returns the received data (may be shorter than *max_len*).

    .. note::
        The UCI firmware response is ``[actual_len_lo] [actual_len_hi]
        [data...]``.  The 6502 routine stores the full response at the
        result address; this helper reads *resp_len* bytes from there.
    """
    if max_len > 255:
        raise ValueError(f"max_len must be <= 255, got {max_len}")

    socket_id_addr = _DATA_ADDR
    transport.write_memory(socket_id_addr, bytes([socket_id]))

    code = build_socket_read(socket_id_addr, max_len=max_len)
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
) -> None:
    """Close a UCI socket."""
    socket_id_addr = _DATA_ADDR
    transport.write_memory(socket_id_addr, bytes([socket_id]))

    code = build_socket_close(socket_id_addr)
    _execute_uci_routine(transport, code, timeout=timeout)


def uci_tcp_listen_start(
    transport: C64Transport,
    port: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    """Start a TCP listener on *port*."""
    port_lo = port & 0xFF
    port_hi = (port >> 8) & 0xFF
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_TCP_LISTENER_START,
        params=bytes([port_lo, port_hi]),
    )
    _execute_uci_routine(transport, code, timeout=timeout)


def uci_tcp_listen_state(
    transport: C64Transport, *, timeout: float = _DEFAULT_TIMEOUT,
) -> int:
    """Query the TCP listener state.

    Returns 0=idle, 1=listening, 2=connected, 3=bind_error, 4=port_in_use.
    """
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_LISTENER_STATE,
    )
    _execute_uci_routine(transport, code, timeout=timeout)
    resp_len = transport.read_memory(_RESP_LEN_ADDR, 1)[0]
    if resp_len == 0:
        return 0
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_tcp_listen_socket(
    transport: C64Transport, *, timeout: float = _DEFAULT_TIMEOUT,
) -> int:
    """Get the accepted socket ID from the TCP listener.

    Call this after :func:`uci_tcp_listen_state` returns CONNECTED.
    """
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_GET_LISTENER_SOCKET,
    )
    _execute_uci_routine(transport, code, timeout=timeout)
    return transport.read_memory(_RESP_ADDR, 1)[0]


def uci_tcp_listen_stop(
    transport: C64Transport, *, timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    """Stop the TCP listener."""
    code = build_uci_command(
        target=TARGET_NETWORK,
        cmd=NET_CMD_TCP_LISTENER_STOP,
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
