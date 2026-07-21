# Cross-backend snapshot interop

The harness can capture the running state of one backend (VICE emulator or Ultimate 64 hardware) and restore it onto the other. The on-disk wire format is VICE's native `.vsf` snapshot file, optionally wrapped in a sidecar directory bundle that carries the things `.vsf` doesn't (raw disk images, cartridge bytes).

This document covers the architecture and per-layer limitations. The canonical API lives in `src/c64_test_harness/snapshot.py` and is re-exported from the package root.

## The `Snapshot` dataclass

`Snapshot` is a frozen dataclass with optional fields for each captured layer. Default values mean "not captured" — every layer can be skipped independently and the snapshot still round-trips.

| Field | Type | What |
|---|---|---|
| `ram` | `bytes` (65536) | $0000–$FFFF RAM image |
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

**On VICE**, extract uses the binary monitor where possible (`read_registers`, `read_memory`); restore uses `set_registers` + bulk `write_memory`. The `.vsf` template carries the modules VICE expects.

**On U64**, extract reads memory via DMA, uses the shadow-SID for write-only registers, sideloads a snoop routine for CPU registers, and DMA-stages REU contents through C64 RAM (pending the upstream firmware feature request for `/v1/machine:reumem`). Restore writes memory directly, uses SocketDMA `reu_write` for fast REU restore, sideloads a trampoline for CPU registers, and calls `client.run_crt` / `client.mount_disk` for cartridges and drives.

### REU layer status (wired — issue #134)

The REU layer is implemented, not just designed:

- **Capture** — `extract_reu_contents(transport, size_bytes)` (also reachable via `extract_snapshot(..., include_reu=True)`) runs the 32 KB staging-window extract described under "Memory-safety contracts". It needs only the `C64Transport` read/write surface. **It runs unpaused by default and must stay that way on Ultimate hardware**: live-verified on C64U fw 1.1.0 (2026-07-21), `machine:pause` freezes the machine clock including the REC's DMA engine, so a paused extract returns stale RAM instead of REU contents. Consequence: the capture is not atomic — don't extract while the running program is actively mutating REU.
- **Restore** — `restore_snapshot` routes `snap.reu_contents` through `Ultimate64Transport.socket_dma_reu_write(offset, data)`, which reuses the transport's **managed SocketDMA client** (the same lazily-connected, teardown-closed TCP/64 client as the `write_memory` fast path) and respects its connect-failure latch. `SocketDMAClient.reu_write` chunks transparently at 65 532 data bytes per `REUWRITE` command (the 16-bit length field covers the 3-byte 24-bit offset prefix) and finishes with an in-band `IDENTIFY` completion barrier — `REUWRITE` has no per-command ack, and without the barrier a read-back races the firmware's socket drain (live-observed on C64U fw 1.1.0: ~0.5 s to drain 96 KiB).
- **REU enablement during restore** goes through the generation-aware `set_reu` helper (the C64U has no `"REU"` Cartridge preset; writing it raw is an HTTP 400).
- **No fallback, no silent skip** — REU memory has no REST write or read endpoint on either generation. If the SocketDMA service is unavailable (TCP/64 refused, or the latch is set), restore raises `Ultimate64Error` with the fix ("Ultimate DMA Service" in Network Settings). A transport without the SocketDMA path at all (VICE) raises `SnapshotRestoreError`; pass `restore_reu=False` to skip the layer explicitly.
- **Fidelity** — **live-verified byte-exact on C64U fw 1.1.0 (2026-07-21)** via the gated `test_reuwrite_byte_fidelity` in `tests/test_socketdma_live.py` (96 KiB pattern via REUWRITE → staging-window read-back → compare; crosses both the 65 532-byte chunk seam and three 32 KiB staging banks). The U64E direction is still pending — run the same gated test against 10.43.23.81 when that site is reachable.

Restoring drives uses temp files for VICE (`attach_drive` takes paths) and direct byte upload for U64 (`mount_disk` takes bytes).

## Per-layer limitations

| Layer | U64→VICE | VICE→U64 | Notes |
|---|---|---|---|
| RAM, CPU port | ✓ | ✓ | Fully symmetric |
| Drives (config + image bytes) | partial | ✓ | U64 REST has no `:get_image` endpoint; caller must supply image bytes via `host_image_paths` |
| Drive slot count | partial | partial | U64 has 2 slots (a/b → devices 8/9); devices 10/11 in a snapshot log a WARNING and are skipped on U64 restore |
| CIA1 / CIA2 / VIC-II registers | ✓ | ✓ | Memory-mapped, DMA-readable; internal latches are degraded both ways but the visible register file round-trips |
| SID registers | ✓ via shadow | ✓ | 28 of 32 SID registers are write-only on real hardware; `Ultimate64Transport` shadows writes to `$D400-$D41F` so extract reads the shadow |
| REU contents | slow | fast | **Wired.** Extract via staging window (~30s/16MB native, ~5-10s turbo); restore via SocketDMA `REUWRITE` (~3s/16MB), chunked at 65 532 bytes/command through the transport's managed client — no REST fallback exists, unavailable DMA service raises. Byte fidelity live-verified on C64U fw 1.1.0 (2026-07-21, `test_reuwrite_byte_fidelity`); U64E direction pending reachability. Extract must run unpaused (`machine:pause` freezes REC DMA); direct extract pending upstream firmware feature |
| CPU registers | active snoop | ✓ | U64 has no `read_registers` REST endpoint; harness injects a snoop routine at `$0334` (PHP/PHA/STX/STY/TSX → scratch area) and reads it back. PC of arbitrary running code can't be recovered — pass `known_pc=` or accept the snoop entry address |
| Cartridge bytes | not extractable | ✓ | Neither backend reads cart bytes back; caller supplies via `host_cart_path`. VICE runtime attach works for `generic`/`generic-8k`/`generic-16k`/`ultimax`/`easyflash`; `freezer`/`action-replay`/others need `ViceConfig.extra_args=["-cartcrt", path]` at launch |

## Memory-safety contracts

The snapshot work introduces two new harness scratch usages:

- **REU extract staging window**: 32 KB at `$0800–$87FF`. The CPU is paused, the original 32 KB is stashed via `read_memory`, REU→C64 DMA transfers fill the window per bank, and `read_memory` reads each bank out. The original 32 KB is restored before CPU resume. The window is opt-in (gated by `include_reu=True`) and writes carry `override="reu-snapshot-staging"`.
- **CPU register snoop / trampoline**: 19 bytes at `$0334` (snoop) or 16 bytes at `$0334` (restore trampoline) plus 5 bytes save area at `$0350-$0354`. Restored after use. These overlap the harness-reserved `$0334` scratch range and use `override="snapshot-snoop"` / `override="snapshot-restore"`.

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
