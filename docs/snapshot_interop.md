# Cross-backend snapshot interop

The harness can capture the running state of one backend (VICE emulator or Ultimate 64 hardware) and restore it onto the other. The on-disk wire format is VICE's native `.vsf` snapshot file, optionally wrapped in a sidecar directory bundle that carries the things `.vsf` doesn't (raw disk images, cartridge bytes).

This document covers the architecture and per-layer limitations. The canonical API lives in `src/c64_test_harness/snapshot.py` and is re-exported from the package root.

## The `Snapshot` dataclass

`Snapshot` is a frozen dataclass with optional fields for each captured layer. Default values mean "not captured" — every layer can be skipped independently and the snapshot still round-trips.

| Field | Type | What |
|---|---|---|
| `ram` | `bytes` (65536) | $0000–$FFFF as seen through the CPU view; $D000–$DFFF holds I/O-view bytes, not RAM under I/O (see "The I/O window" below) |
| `cpu_port_data`, `cpu_port_dir` | `int` | $01 / $00 |
| `exrom`, `game` | `int` | Cartridge control lines |
| `drives` | `tuple[DriveState, ...]` | Mounted disk images per CBM device |
| `cia1_regs`, `cia2_regs`, `vic_regs`, `sid_regs` | `bytes` | Memory-mapped I/O register banks |
| `reu_size_bytes`, `reu_contents` | `int`, `bytes` | REU configuration and bank dump |
| `cpu_registers` | `CpuRegisters \| None` | 6510 A/X/Y/SP/PC/P |
| `cartridge` | `CartridgeState \| None` | Active `.crt` image bytes |

Adding optional fields to `Snapshot` is the supported extension pattern. Existing callers that construct `Snapshot(ram=..., cpu_port_data=..., cpu_port_dir=...)` continue to work unchanged across feature additions.

Implementation status: the RAM/CPU-port layer (Phase A) and the **REU layer** (`reu_size_bytes`, `reu_contents` — issue #134) are wired; the drive, register, CPU-register, and cartridge fields are the planned extension surface for later phases.

## `.vsf` wire format

VICE's `.vsf` carries ~30 module sections (MAINCPU, C64MEM, CIA1, CIA2, VIC-II, SID, REU1764, DRIVE0, …). VICE 3.10 refuses snapshots that don't include the full module set, so the harness ships `_vsf_template.vsf` — a 179 KB capture from a fresh `x64sc` at BASIC READY — and patches in the snapshot's domain-specific bytes via prefix overlays per module:

- **C64MEM**: full body replaced (RAM image + CPU port).
- **MAINCPU**: first 7 register bytes patched (A, X, Y, SP, PC, P at body offset 8..14); clock counter and last-opcode info preserved from template.
- **VIC-II**: first 47 bytes patched (the visible register file); internal sequencer state preserved.
- **CIA1 / CIA2**: first 16 bytes patched (the visible register file); internal timer state preserved.
- **SID**: first 32 bytes patched after the 4-byte engine prefix; voice/envelope phase counters preserved.
- **REU1764**: built fresh and injected when REU contents are present (the template has no REU module).

The format details (file header layout, machine name `C64SC`, format major 2 minor 0, `C64MEM` VMINOR=1 with its 15-byte trailer) are all in `snapshot.py`. The schepers `VICE_FRZ.TXT` spec is partly outdated — what's in `snapshot.py` is what VICE 3.10 actually accepts.

## Sidecar bundle format

Some state can't live in the `.vsf`:

- Disk images (`.vsf` references them by host path, not embedded bytes)
- Cartridge `.crt` bytes (same — referenced by path)
- Image bytes that the U64 REST API can't read back

The bundle format is a directory:

```
mysnapshot/
  snapshot.vsf       # full .vsf with all in-band state
  manifest.json      # which sidecar layers are present
  drive8.d64         # raw disk image per CBM device (planned — drive phase)
  drive9.d81
  cartridge.crt      # active cartridge image (planned — cartridge phase)
  reu.bin            # raw REU dump (wired; NOT embedded in the .vsf today)
```

Use `Snapshot.to_bundle(path)` / `Snapshot.from_bundle(path)` to round-trip the directory. The `.vsf` inside is also valid on its own. The wired implementation currently carries `snapshot.vsf`, `manifest.json`, and `reu.bin`; the drive and cartridge files are the design target for their phases. REU bytes travel **only** in the sidecar — `to_vsf()` output is byte-identical with or without the REU layer (no `REU1764` module is emitted yet).

## Extract / restore semantics

```python
from c64_test_harness import extract_snapshot, restore_snapshot

snap = extract_snapshot(
    transport,
    include_reu=True,            # opt-in; staging-window extract is slow
    reu_size_bytes=None,         # None = auto-detect from the U64 config
    reu_settle=0.05,             # per-bank DMA settle delay
)

restore_snapshot(transport, snap)                    # REU restored when present
restore_snapshot(transport, snap, restore_reu=False)  # explicit REU opt-out
```

(Registers, drives, and cartridge layers — `include_registers`, `host_image_paths`, `host_cart_path`, `known_pc` — are later phases and not yet part of the signatures.)

**On VICE**, extract uses the binary monitor `read_memory`; restore uses bulk `write_memory`. The `.vsf` template carries the modules VICE expects. (The planned register phase will add `read_registers`/`set_registers` on this side.)

**On U64**, extract reads memory via DMA and DMA-stages REU contents through C64 RAM (pending the upstream firmware feature request for `/v1/machine:reumem`). Restore writes memory directly and uses SocketDMA `reu_write` for fast REU restore. The planned later phases add a shadow for the write-only SID registers, a sideloaded snoop routine/trampoline for CPU registers, and `client.run_crt` / `client.mount_disk` for cartridges and drives — none of these are implemented yet (see the per-layer matrix below).

### The I/O window ($D000–$DFFF)

Neither backend banks out I/O for host memory access — the VICE binary monitor uses the CPU view and U64 `readmem`/`writemem` is real bus DMA. Two consequences (audited 2026-07):

- **Extract captures I/O-view bytes** for `$D000–$DFFF`: live VIC-II/SID/CIA/REC register *reads* plus color RAM — not the RAM under I/O. The full 64 KB is still read for fidelity of everything else.
- **Restore skips the I/O window**, with one exception: color RAM `$D800–$DBFF`, which through the CPU view *is* the real (only) color RAM and whose writes are side-effect-free. Blind byte-writes into live registers are wrong (register restore is a later phase) and were actively dangerous: the previous full-64-KB ascending write landed `ram[$DF01]` in the REC command register while `$DF02–$DF0A` still held pre-restore values, firing a spurious REU DMA with stale address/length registers that clobbered just-restored RAM.

So the RAM round-trip guarantee is `$0000–$CFFF`, color RAM `$D800–$DBFF`, and `$E000–$FFFF` — **not** the full 64 KB. `restore_snapshot` writes those three slices, then re-asserts the CPU port bytes at `$0000`/`$0001`.

### REU layer status (wired — issue #134)

The REU layer is implemented, not just designed:

- **Capture** — `extract_reu_contents(transport, size_bytes)` (also reachable via `extract_snapshot(..., include_reu=True)`) runs the 32 KB staging-window extract described under "Memory-safety contracts". It needs only the `C64Transport` read/write surface. **It runs unpaused by default and must stay that way on Ultimate hardware**: live-verified on C64U fw 1.1.0 (2026-07-21), `machine:pause` freezes the machine clock including the REC's DMA engine, so a paused extract returns stale RAM instead of REU contents. Consequence: the capture is not atomic — don't extract while the running program is actively mutating REU.
- **Restore** — `restore_snapshot` routes `snap.reu_contents` through `Ultimate64Transport.socket_dma_reu_write(offset, data)`, which reuses the transport's **managed SocketDMA client** (the same lazily-connected, teardown-closed TCP/64 client as the `write_memory` fast path) and respects its connect-failure latch. `SocketDMAClient.reu_write` chunks transparently at 65 532 data bytes per `REUWRITE` command (the 16-bit length field covers the 3-byte 24-bit offset prefix) and finishes with an in-band `IDENTIFY` completion barrier — `REUWRITE` has no per-command ack, and without the barrier a read-back races the firmware's socket drain. The drain rate on C64U fw 1.1.0 is erratic (0.4–19 s live-measured for the same 96 KiB burst; the U64E-era ~3 s/16 MB figure does not hold there), so the barrier's recv timeout scales with payload size at the worst-observed rate. Both barriers ride on a reused TCP/64 connection, and the firmware closes one that has been idle for >1 s (issue #223) — the client reopens it before the next command after a gap of `IDLE_RECONNECT_SECONDS` (0.8 s), so a REU restore that follows a slow RAM restore no longer loses its first `REUWRITE` to a socket the device already dropped. (The transport's `DMAWRITE` bulk-`write_memory` fast path now finishes with the same `IDENTIFY` barrier before its tail read-back sanity check — so the RAM-restore writes that precede the REU layer are confirmed applied, not just in flight.) Related C64U fw 1.1.0 findings: `machine:pause` freezes REC DMA (why the extract runs unpaused), and the "Ultimate DMA Service" setting reverts to Disabled on a physical power-cycle (it does survive `reboot()`), so re-enable it via `Network Settings` after power-cycling.
- **REU enablement during restore** goes through the generation-aware `set_reu` helper (the C64U has no `"REU"` Cartridge preset; writing it raw is an HTTP 400).
- **No fallback, no silent skip** — REU memory has no REST write or read endpoint on either generation. If the SocketDMA service is unavailable (TCP/64 refused, or the latch is set), restore raises `Ultimate64Error` with the fix ("Ultimate DMA Service" in Network Settings). A transport without the SocketDMA path at all (VICE) raises `SnapshotRestoreError`; pass `restore_reu=False` to skip the layer explicitly.
- **Fidelity** — **live-verified byte-exact on C64U fw 1.1.0 (2026-07-21)** via the gated `test_reuwrite_byte_fidelity` in `tests/test_socketdma_live.py` (96 KiB pattern via REUWRITE → staging-window read-back → compare; crosses both the 65 532-byte chunk seam and three 32 KiB staging banks). The U64E direction is still pending — run the same gated test against 10.43.23.81 when that site is reachable.

Restoring drives (planned phase) will use temp files for VICE (`attach_drive` takes paths) and direct byte upload for U64 (`mount_disk` takes bytes).

## Per-layer limitations

Rows marked **(planned)** describe the design target for a later phase, not shipped behaviour — today only the RAM/CPU-port and REU layers are wired.

| Layer | U64→VICE | VICE→U64 | Notes |
|---|---|---|---|
| RAM, CPU port | ✓ | ✓ | Symmetric for `$0000-$CFFF`, color RAM `$D800-$DBFF`, `$E000-$FFFF`; the rest of the I/O window is captured (I/O-view bytes) but skipped on restore — see "The I/O window" above |
| Drives (config + image bytes) **(planned)** | partial | ✓ | U64 REST has no `:get_image` endpoint; caller must supply image bytes via `host_image_paths` |
| Drive slot count **(planned)** | partial | partial | U64 has 2 slots (a/b → devices 8/9); devices 10/11 in a snapshot log a WARNING and are skipped on U64 restore |
| CIA1 / CIA2 / VIC-II registers **(planned)** | ✓ | ✓ | Memory-mapped, DMA-readable; internal latches are degraded both ways but the visible register file round-trips |
| SID registers **(planned)** | via shadow (not implemented) | ✓ | 28 of 32 SID registers are write-only on real hardware; the design is a write shadow for `$D400-$D41F` in `Ultimate64Transport` so extract can read the shadow — **the shadow does not exist yet**, so U64-side SID extraction reads back garbage register values today |
| REU contents | slow | fast | **Wired.** Extract via staging window (~30s/16MB native, ~5-10s turbo); restore via SocketDMA `REUWRITE` (~3s/16MB), chunked at 65 532 bytes/command through the transport's managed client — no REST fallback exists, unavailable DMA service raises. Byte fidelity live-verified on C64U fw 1.1.0 (2026-07-21, `test_reuwrite_byte_fidelity`); U64E direction pending reachability. Extract must run unpaused (`machine:pause` freezes REC DMA); direct extract pending upstream firmware feature |
| CPU registers **(planned)** | active snoop (not implemented) | ✓ | U64 has no `read_registers` REST endpoint; the design is a sideloaded snoop routine at `$0334` (PHP/PHA/STX/STY/TSX → scratch area) read back over DMA — **not implemented yet**. PC of arbitrary running code can't be recovered even then — the design passes `known_pc=` or accepts the snoop entry address |
| Cartridge bytes **(planned)** | not extractable | ✓ | Neither backend reads cart bytes back; caller supplies via `host_cart_path`. VICE runtime attach works for `generic`/`generic-8k`/`generic-16k`/`ultimax`/`easyflash`; `freezer`/`action-replay`/others need `ViceConfig.extra_args=["-cartcrt", path]` at launch |

## Memory-safety contracts

The snapshot work introduces two new harness scratch usages:

- **REU extract staging window**: 32 KB at `$0800–$87FF`. The original 32 KB is stashed via `read_memory`, REU→C64 DMA transfers fill the window per bank, and `read_memory` reads each bank out. The original 32 KB is restored afterwards. The extract runs **unpaused** by default (`machine:pause` freezes REC DMA on hardware — see the REU layer section above). The window is opt-in (gated by `include_reu=True`) and writes carry `override="reu-snapshot-staging"`.
- **CPU register snoop / trampoline** *(planned phase — not implemented)*: the design is 19 bytes at `$0334` (snoop) or 16 bytes at `$0334` (restore trampoline) plus 5 bytes save area at `$0350-$0354`, restored after use. These would overlap the harness-reserved `$0334` scratch range and use `override="snapshot-snoop"` / `override="snapshot-restore"`.

`MemoryPolicy` enforces both via the override mechanism. Callers can engineer a stricter policy (`MemoryPolicy.from_prg(...)`) and the snapshot path still works because the overrides are scoped to the snapshot's own write calls.

## Upstream firmware feature request

The U64 REU extract path is currently slow (DMA-via-staging) because firmware 3.14d has no REST endpoint for REU memory readback. A feature request for `GET /v1/machine:reumem` is filed at `https://github.com/GideonZ/1541ultimate/issues` (2026-05-19). When/if it lands, the staging-window dance in `extract_reu_contents` can be swapped for a direct chunked GET — see `project_reu_readback_feature_request` in agent memory for the swap target. The restore path is already on the fast SocketDMA `REUWRITE` (opcode `0xFF07`) and doesn't change.

## Files

- `src/c64_test_harness/snapshot.py` — the full implementation
- `src/c64_test_harness/_vsf_template.vsf` — bundled 179 KB template
- `tests/test_snapshot.py` — Phase A round-trip + .vsf format guards
- `tests/test_snapshot_reu.py` — REU staging extract, `REUWRITE` chunking, SocketDMA restore routing, sidecar round-trip (mock-only)
- `tests/test_socketdma_live.py` — gated live tests (`SOCKETDMA_LIVE`), including the `REUWRITE` byte-fidelity validation (passed on C64U fw 1.1.0, 2026-07-21)
- `tests/test_snapshot_drives.py` — disk side-channel (planned phase)
- `tests/test_snapshot_registers.py` — CIA/VIC/SID + shadow-SID (planned phase)
- `tests/test_snapshot_cpu_regs.py` — active snoop + trampoline (planned phase)
- `tests/test_snapshot_cartridge.py` — cart sidecar with VICE allowlist (planned phase)
