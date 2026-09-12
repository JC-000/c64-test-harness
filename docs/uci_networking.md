# UCI Networking (Ultimate Command Interface)

The `uci_network` module drives the Ultimate firmware's host-visible Command
Interface at `$DF1C-$DF1F` to open TCP/UDP sockets from C64 code. The
firmware's lwIP stack handles TCP/IP internally; C64 code just pushes commands
and reads responses. The register interface is the same on both device
generations on this bench (U64E, C64 Ultimate); everything measured below was
measured on the U64E unless it says otherwise.

- High-level helpers: `uci_probe`, `uci_get_ip`, `uci_get_interface_count`,
  `uci_tcp_connect`, `uci_udp_connect`, `uci_socket_write`, `uci_socket_read`,
  `uci_socket_close`, `uci_tcp_listen_*`.
- Diagnostics: `uci_status_peek`, `uci_wedge_probe` — non-blocking reads of
  `$DF1C` that never enter the unbounded wait-idle spin. See
  [`docs/u64_recovery.md`](u64_recovery.md) § "Tier 3 — UCI STATE bit".
- Low-level 6502 builders: `build_uci_probe`, `build_uci_command`,
  `build_get_ip`, `build_tcp_connect`, `build_udp_connect`,
  `build_socket_read`, `build_socket_write`, `build_socket_close`,
  `build_uci_status_peek`.
- Config helpers (REST): `get_uci_enabled`, `enable_uci`, `disable_uci`.

**Prerequisite:** UCI must be enabled in the device settings:
*C64 and Cartridge Settings → Command Interface → Enabled*.
`enable_uci(client)` flips that item over REST, but the `$DF1C-$DF1F`
registers do not go live until the next machine reset: the live suites
follow it with `client.reset()` and a 3 s settle before the first routine
(`tests/test_uci_udp_send_live.py:242-249`), and without that every routine
times out at the sentinel. The write is memory-only — it is a config PUT, so
it survives `machine:reboot` but not a firmware power-on, and it is never
saved to flash (`uci_network.py:2316-2329`). (`enable_uci`'s own
docstring still says "a device reboot reverts to the default state";
that is wrong on the corrected model — `machine:reboot` is a C64-level
reset and leaves firmware RAM config alone. See
[`docs/u64_recovery.md`](u64_recovery.md) § "Harness-side mitigation: FTP
`/Temp` GC", which carries the correction.)

## How the 6502 routine is dispatched

The host (`_execute_uci_routine` in `uci_network.py`, `:1686`) clears the
sentinel and error bytes, writes `CTL_ABORT` to `$DF1C` and sleeps 0.1 s to
drain stale UCI state, writes the generated 6502 routine at `code_addr`
(default `$C000`), then injects the string `SYS <code_addr>\r` into the
keyboard buffer at `$0277` and sets the keyboard fill count at `$00C6` to the
command length. That injection is why `code_addr` is constrained: the
`SYS<addr>\r` string must fit the 10-byte KERNAL buffer, and a longer one
raises `ValueError` before anything is written (`:1741-1745`). BASIC's
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

## Datagram size limits

`uci_socket_write` accepts up to **892 bytes per call** (the constant `SOCKET_WRITE_MAX_BYTES`). This is the *empirical* firmware ceiling on the U64E: the theoretical `CMD_MAX_COMMAND_LEN - 3` from Gideon's source is 893, but the firmware truncates by exactly one byte at that boundary (a fencepost in how `command->length` counts — verified with a size sweep in `tests/test_uci_udp_send_large_live.py` and the standalone probe captured at session time). The 6502 inner loop in `build_socket_write` uses self-modifying code on the `LDA abs,Y` operand to push payloads across 6502 page boundaries in one call.

For UDP, one `uci_socket_write` call produces exactly one `lwip_send` on the firmware side, which is one UDP datagram on the wire (empirically confirmed by `tests/test_uci_udp_send_live.py`'s per-call-per-datagram probe). No firmware-side coalescing. For payloads larger than 892 bytes, call `uci_socket_write` in a loop; each call emits its own UDP datagram. Receivers must reassemble in application code.

`uci_socket_read` is capped far lower, at **253 bytes** per call
(`SOCKET_READ_MAX_BYTES = 255 - _SOCKET_READ_HEADER_LEN`,
`uci_network.py:261-265`), and a larger `max_len` raises `ValueError` rather
than being truncated. The limit is the harness's, not the firmware's: the
6502 drain loop indexes with Y, so the two-byte `[len_lo][len_hi]` reply
header plus 254 payload bytes would wrap it. (An earlier revision of this
page quoted a "theoretical 894 bytes (`CMD_MAX_REPLY_LEN - 2`)"; whatever the
firmware would allow, no caller can ask for it through this helper.) Lifting
the cap needs a 16-bit drain, which is the same work as draining the
multi-block replies firmware 3.15 can return — tracked separately. Until
then, a reply whose header reports more bytes than arrived in the block logs
a WARNING and returns the first block only (`:1993-2004`).

## Cost on leak-prone firmware: zero, one or two attachments per routine

On a device without the upstream `/Temp` collector — the C64 Ultimate on
1.1.0 today — **most UCI calls from host Python cost one managed `/Temp`
attachment; a large `socket_write` costs two and a probe or peek costs
none — the size of the emitted routine decides**, and enough attachments
crash the device firmware. The mechanism, the budget and the hygiene pass
are in [`docs/u64_recovery.md`](u64_recovery.md); what matters here is the
shape:

- `_execute_uci_routine` writes its routine with a single
  `transport.write_memory(code_addr, code)` (`uci_network.py:1735`). It does
  **not** chunk. Measured host-side by `len()` (no device traffic,
  2026-09-10), every command builder emits more than the C64U's 128-byte PUT
  threshold — `build_uci_command` 133, `build_get_ip` 138,
  `build_socket_read` 149, `build_tcp_connect` / `build_udp_connect` 159,
  `build_socket_write` 170 — so the routine write takes the POST path and
  leaks one attachment. Only `build_uci_probe` / `build_uci_status_peek`
  (12 bytes) and `build_socket_close` (112) fit under it — those three
  calls cost nothing. `turbo_safe=True` changes that unevenly: it pushes
  `build_socket_close` to 341, over the threshold and onto POST, while
  probe and peek reach only 28 and stay comfortably under it. Turbo makes
  a free call cost one; it does not make every call cost three times as
  much.
- `uci_socket_write` costs a **second** attachment only when the payload
  itself exceeds the threshold (`uci_network.py:1936-1943` writes the
  socket-id byte, the payload and the two length bytes separately; only the
  payload can cross). The 800/892-byte large-send tests pay two; a small
  write pays one.
- The three other writes `_execute_uci_routine` makes are all far under the
  threshold and add nothing: the `CTL_ABORT` byte to `$DF1C`, the
  `SYS<addr>\r` string (at most 10 bytes) to `$0277`, and the fill count to
  `$00C6` (`uci_network.py:1735-1747`).
- `enable_uci` / `disable_uci` are `set_config_items` — bodyless config PUTs
  that cost nothing.

**The same protocol driven C64-side costs only the upload that put it
there.** A fetch made of many `socket_read`s is many attachments when the
loop runs in host Python, and one attachment when the loop runs inside an
uploaded PRG. On a leak-prone device, moving the loop onto the 6510 removes
the leak rather than cleaning up after it, and it is the first thing to
reach for.

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
