# UCI Networking (Ultimate Command Interface)

The `uci_network` module drives the Ultimate 64 Elite's host-visible Command
Interface at `$DF1C-$DF1F` to open TCP/UDP sockets from C64 code. The
firmware's lwIP stack handles TCP/IP internally; C64 code just pushes commands
and reads responses.

- High-level helpers: `uci_probe`, `uci_get_ip`, `uci_tcp_connect`,
  `uci_udp_connect`, `uci_socket_write`, `uci_socket_read`,
  `uci_socket_close`, `uci_tcp_listen_*`.
- Low-level 6502 builders: `build_uci_probe`, `build_uci_command`,
  `build_get_ip`, `build_tcp_connect`, `build_socket_read`,
  `build_socket_write`, `build_socket_close`.
- Config helpers (REST): `get_uci_enabled`, `enable_uci`, `disable_uci`.

**Prerequisite:** UCI must be enabled in the device settings:
*C64 and Cartridge Settings → Command Interface → Enabled*.

## How the 6502 routine is dispatched

The host (`_execute_uci_routine` in `uci_network.py`) writes the
generated 6502 routine at `code_addr` (default `$C000`), then injects
the string `SYS <code_addr>\r` into the keyboard buffer at `$0277` and
sets the keyboard fill count at `$00C6` to the command length. BASIC's
command-line processor reads the buffer on its next cycle as if the user
typed the `SYS` command and RETURN, which JSRs into the routine. The
routine does its work, writes the sentinel byte, and executes `RTS` to
return to BASIC, which resumes its READY prompt loop.

```asm
; Tail of every UCI routine:
    LDA #$01            ; sentinel done value
    STA sentinel_addr   ; host polls this byte
    RTS                 ; return to BASIC (SYS dispatch)
```

An earlier version patched the IMAIN vector at `$0302/$0303` to point at
`code_addr` and waited for BASIC's idle loop to jump through it. That
worked on warm devices (where prior BASIC activity had already traversed
`$0302`) but silently failed on cold boots because BASIC's READY loop
does not cycle through IMAIN — only the command-line processor does.

Custom builders MUST end with `RTS` (0x60), not `JMP` or `BRK`.

## Turbo speed support (`turbo_safe=True`)

The UCI FPGA inside the Ultimate 64 Elite needs **~38 µs** of wall-clock time
between consecutive register accesses regardless of CPU clock speed. At stock
1 MHz the 6502 bus cycle naturally provides ample settling time. At U64 turbo
speeds (4/8/16/24/48 MHz) the CPU outruns the FPGA, which causes:

- double-latched writes (the FPGA sees only the first of two back-to-back
  writes),
- stale/glitched reads (the first `LDA $DF1C` returns the previous value,
  not the current one),
- corrupted command/response sequencing.

### The fix: a delay-loop fence

Every builder and helper accepts an opt-in `turbo_safe: bool = False`
keyword. When set, the generated 6502 routine:

1. Inserts a nested delay-loop fence after every read/write of a UCI register
   (`$DF1C-$DF1F`). The fence burns ~2525 cycles — ~52 µs at 48 MHz, ~2.5 ms
   at 1 MHz. Loop parameters: `UCI_FENCE_OUTER = 5`, `UCI_FENCE_INNER = 100`.
2. Adds a 255-iteration settle delay after every `PUSH_CMD` write, before
   the first `CMD_BUSY` poll. At turbo speeds the FPGA may not have asserted
   `CMD_BUSY` yet when the CPU reaches the poll loop; the settle loop closes
   that gap.
3. Converts loop-back short branches (`BNE`/`BEQ`) to `JMP` trampolines
   wherever the fence expansion blows the 8-bit branch range.

The fence preserves A and X via the stack, so callers that staged a status
byte in A (for a subsequent `AND #mask`) still get the correct value.

### When to enable it

| Scenario | `turbo_safe` |
|----------|--------------|
| Stock U64 / U64E at 1 MHz (default) | `False` |
| U64E with turbo on (4/8/16/24/48 MHz) | `True` |
| VICE emulator | either — unfenced is faster; fenced still correct |
| 1541 Ultimate cartridge | `False` |

### Example

```python
from c64_test_harness import (
    uci_probe, uci_tcp_connect, uci_socket_write,
    uci_socket_read, uci_socket_close,
)
from c64_test_harness.backends.ultimate64_helpers import set_turbo_mhz

# Switch the U64 into 48 MHz turbo
set_turbo_mhz(client, 48)

# Every UCI call must now use turbo_safe=True
ident = uci_probe(transport, turbo_safe=True)           # 0xC9
sock  = uci_tcp_connect(transport, "example.com", 80, turbo_safe=True)
uci_socket_write(transport, sock, b"GET / HTTP/1.0\r\n\r\n",
                 turbo_safe=True)
data  = uci_socket_read(transport, sock, turbo_safe=True)
uci_socket_close(transport, sock, turbo_safe=True)

# Back to stock speed
set_turbo_mhz(client, None)
```

### Fence tuning (advanced)

The macro parameters are exposed at module top level:

```python
from c64_test_harness.uci_network import (
    UCI_FENCE_OUTER,       # default 5
    UCI_FENCE_INNER,       # default 100
    UCI_PUSH_SETTLE_ITERS, # default 0xFF
)
```

They are compiled into the emitted 6502 code, so changing the module-level
constant **before** calling a builder is the only way to retune (there is no
runtime override). Changing the defaults has not been necessary in any tested
configuration; the values were chosen via binary search on real U64E
hardware in the c64-https reference implementation (which is the authoritative
source for the timing analysis).

Minimum measured value: OUTER=3 INNER=122 (~1845 cycles / ~38.4 µs at 48 MHz).
Default: OUTER=5 INNER=100 (~2525 cycles / ~52 µs at 48 MHz, 35% margin).

## Ported from

The fence design is a direct port of c64-https PR #20
(`fix/uci-nop-fencing`, commits `6d6a717` → `87092bd` → `1b6ccf3`). The
c64-https implementation is a pure-6502 assembler macro (`uci_fence` in
`uci_regs.inc`); this harness port compiles the same macro into the
dynamically-generated 6502 routines that the Python builders emit.
