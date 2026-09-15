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
`enable_uci(client)` flips that item over REST. The live suites follow it
with `client.reset()` and a 3 s settle before the first routine
(`tests/test_uci_udp_send_live.py:274-280`), recorded as: without that, every routine
times out at the sentinel. **That requirement was not reproduced when it
was measured on the U64E** (#270: fw 3.15, `git_commit_hash` bce4535e,
2026-09-15, `DeviceLock` held, `Cartridge Preference` `Auto`, each trial
starting at `READY.` from `disable_uci` + `client.reset()` + 3 s; arms
interleaved ABCCBA twice, n=4 per arm): with the enable applied and no
reset, `$DF1D` read `$C9` on the first read after the PUT and `uci_probe`
returned `$C9` in 4/4; with `client.reset()` + 3 s, 4/4; with `reboot()` +
5 s, 4/4. So on that build the enable is live without `reset()`, as the
source trace below predicts. The reset in the live suites is harmless and
stays. The C64U was not measured, and on it this is still source-read.
Why the earlier runs timed out is not established; the untested
candidates are `Cartridge Preference` External (#359, next sentence), the
C64 not sitting at `READY.` when the `SYS` is typed, and a different
firmware build. On an Ultimate transport every routine except `uci_probe` first reads the UCI identifier at `$DF1D` (one bodyless GET, zero `/Temp` cost) and raises `UCIInterfaceAbsentError` if it is not `$C9`, because with `Cartridge Preference` = External the slot stays off the bus after the reset while `Command Interface` still reads Enabled (#359: U64E, paired ABBAAB, identifier present and routine completing 3/3 with Auto, 0/3 with External). It is a recorded
observation, and **its cause is not explained by firmware source**. From
source, read at tag `1.1.0` (the C64U) and `7f6fcb51` (the U64E's
v3.15-85), not measured:

- The item drives the FPGA register `CMD_IF_SLOT_ENABLE`, which
  `C64::set_emulation_flags()` sets to `!!cfg->get_value(CFG_CMD_ENABLE)`
  (`software/io/c64/c64.cc:326-327` at `1.1.0`; `:329-330` at `7f6fcb51`).
- A REST single-item config PUT reaches that function when the PUT
  closes, with no reset in between. The route's `set_item` calls
  `item->setValue(n)` (`software/api/route_configs.cc:63-85` at `1.1.0`).
  `setValue` is `value = v; return setChanged();` (`config.h:121`).
  `setChanged` calls `store->set_need_effectuate()` unless the item has a
  change hook (`config.cc:901-911`; `:902` at `7f6fcb51`). No hook is
  registered for `CFG_CMD_ENABLE` anywhere in the whole `software/` tree
  at either ref (whole-tree search from #309's review, re-checked here).
  The item appears only at its definition (`c64.cc:115`), the read in
  `set_emulation_flags` (`:326`; `:329` at `7f6fcb51`), a menu-group append
  (`:1580`; `:1863`) and its `#define` (`c64.h:222`; `:224`). The only
  direct `setChangeHook` call is inside `ConfigStore::set_change_hook`
  (`config.cc:493`; `:494`), which looks the id up in its own store; the
  id-looping hook calls in `software/u64/u64_config.cc` register mixer
  items in the Audio Mixer and Speaker Mixer stores. The route then calls
  `st->at_close_config()`
  (`route_configs.cc:244` at `1.1.0`, `:313` at `7f6fcb51`), which
  effectuates only `if (need_effectuate())` (`config.h:165-168`; `:201`
  at `7f6fcb51`). Because `setChanged` sets that flag on every set, even
  one that leaves the value unchanged, the gate is "the item was set",
  not "the item changed". The chain continues `effectuate()` →
  `C64::effectuate_settings()` → `set_emulation_flags()`
  (`c64.cc:267-277`). So by source the enable takes effect without a
  reset.
- #270 proposed a cause that is half the path: `machine:reboot` →
  `MENU_C64_REBOOT` → `C64::start_cartridge(NULL)` (`route_machine.cc:40`,
  `c64_subsys.cc:231-236`) zeroes `CMD_IF_SLOT_ENABLE` (`c64.cc:913`). But
  when no external cartridge holds the bus it then calls
  `set_cartridge(NULL)` (`c64.cc:923-924`), which calls
  `set_emulation_flags()` again (`c64.cc:992`) and restores the enable
  from config. Two cases leave it at 0 all the same:
  - **An external cartridge holds the bus.** `ConfigureU64SystemBus()`
    reports one (Cartridge Preference *Automatic* with a cart present, or
    *External*), so `set_cartridge` is skipped (`c64.cc:921-924`).
  - **The configured cartridge image prohibits UCI.** `set_cartridge(NULL)`
    loads the `.crt` named by `CFG_C64_CART_CRT` (`c64.cc:962-964`;
    `:1241-1243` at `7f6fcb51`) and restores the enable in
    `set_emulation_flags()`. It then zeroes the enable again if that
    definition's `prohibit` mask includes `CART_UCI`, `CART_UCI_DFFC` or
    `CART_UCI_DE1C` (`c64.cc:1062-1068`; `:1341-1346` at `7f6fcb51`).
    `CART_PROHIBIT_DFXX` includes `CART_UCI` (`c64.h:254`; `:256` at
    `7f6fcb51`). GeoRAM's `CART_PROHIBIT_ALL_BUT_REU` does not
    (`c64.h:256`; `:258`). Which `.crt` types carry such a mask is not
    traced here.

  The REU enable follows the same two cases; its prohibit check is
  `c64.cc:1056-1061` (`:1335-1339` at `7f6fcb51`).
  `Ultimate64Client.reboot`'s docstring stated the unconditional version
  until #299 corrected it to this account
  (`tests/test_reboot_docstring.py` pins the two together).

#270 has since run the U64E without the reset, with the result given at
the top of this section. The write is memory-only — it is a config PUT, so
it survives `machine:reboot` but not a firmware power-on, and it is never
saved to flash (`uci_network.py:2316-2329`). (`enable_uci`'s docstring
used to say "a device reboot reverts to the default state"; that was
wrong on the corrected model — `machine:reboot` is a C64-level reset and
leaves firmware RAM config alone — and #270 corrected it. See
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

**If the sentinel never arrives, the host resets the 6510 before it
raises** (issue #313, PR #332). The routine's wait fragments
(`_build_wait_idle`, `_build_push_and_wait`, the turbo `JMP busy_loop`
forms) are unbounded, so a timed-out routine may still be executing at
`code_addr`, and the next call's chunked upload would land on live code.
On a timeout the host:

1. calls `transport.reset(scope="cpu")`. On a U64 that is the bodyless
   `PUT /v1/machine:reset`, so it costs no `/Temp` attachment;
2. sleeps `_TIMEOUT_RESET_SETTLE` (3 s), because the KERNAL reset clears
   `$0200-$03FF` and a `SYS` typed before `READY.` would be lost. The 3 s
   is borrowed from the live suites' post-`reset()` settle and is
   unmeasured for this path;
3. raises `TimeoutError`. If the reset itself raised, the `TimeoutError`
   is still what the caller gets, chained from the reset error, with no
   settle taken.

It never uses `scope="machine"`, which on a U64 is `machine:reboot`, a
different operation (see #299). **By source, the UCI enable survives the
reset:**

- `machine:reset` runs `MENU_C64_RESET` → `C64::reset()`.
- `C64::reset()` only pulses `C64_MODE_RESET`; it calls neither
  `set_emulation_flags()` nor `start_cartridge()`.
- Read at tag `1.1.0` (`c64.cc:593-601`, `c64_subsys.cc:183-190`,
  `route_machine.cc:30-36`) and at `7f6fcb51` (`c64.cc:612-620`,
  `c64_subsys.cc:217-224`, `route_machine.cc:73-85`).
- Two side effects, neither of which affects UCI:
  - on both refs, `MENU_C64_RESET` calls `release_host()` before
    resetting (`c64_subsys.cc:184-187` at `1.1.0`,
    `c64_subsys.cc:218-221` at `7f6fcb51`), which closes an open menu, on
    the C64U as well;
  - at `7f6fcb51` only, on success the route also releases REST-held
    keyboard keys and the joystick (`route_machine.cc:77-81`:
    `restReleaseAll`, `releaseAllRest`, under `#if U64`; the `1.1.0`
    route has no such block).

  So a timeout closes an open menu, and on `7f6fcb51` also releases held
  keys and joystick.
- This has not been measured on a device.

The reset does not clear a UCI STATE-bit wedge (#112); that still needs a
physical power-cycle. Whatever program was running on the C64 is gone
after a timeout.

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

- **Since [#252](https://github.com/JC-000/c64-test-harness/issues/252)
  the costs in this list apply only where `transport.write_memory` does not
  chunk.** That means a post-safe device, where every POST is collected, or
  a transport other than `Ultimate64Transport`. On a leak-prone or unknown
  grade `Ultimate64Transport.write_memory` chunks every write below at the
  client threshold, so none of them costs an attachment. The costs are
  kept as the record of the pre-#252 behaviour and of the blob sizes.
- `_execute_uci_routine` writes its routine with a single
  `transport.write_memory(code_addr, code)` (`uci_network.py:1780`). Before
  #252 that did **not** chunk. Measured host-side by `len()` (no device
  traffic, 2026-09-10), every command builder emits more than the C64U's
  128-byte PUT threshold — `build_uci_command` 133, `build_get_ip` 138,
  `build_socket_read` 149, `build_tcp_connect` / `build_udp_connect` 159,
  `build_socket_write` 170 — so the routine write took the POST path and
  leaked one attachment. Only `build_uci_probe` / `build_uci_status_peek`
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
