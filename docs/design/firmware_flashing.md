# Design note: firmware flashing in the harness

**Status:** proposal for the owner's decision. Not implemented. Requested 2026-10-20 by the 1541ultimate lane, on the user's behalf.

**Sources:** `~/Documents/1541u-tools/c64u-idf6/`: `RUNBOOK.md` § "Flash procedure F", `flash_c64u.py`, `wifi_toggle.py`, `install_roms.sh`, `locked.py`, `lockcheck.py`. That kit is the working reference, and it has flashed both products. Facts in this note come from it unless a harness file:line is given.

## What a flash is today

| # | Step | Who | Transport | Automatable |
|---|---|---|---|---|
| 1 | Upload the `.ue2` (C64U) or `.u64` (U64E) to `/SD` or `/USB1` and read it back to check the SHA-256 | lead | FTP | **yes**: `storage_put_file(verify=True)` exists |
| 2 | Telnet Remote menu: browse to the file by a short dot-free name, open its context menu, choose **Run Update** | lead | telnet UI | **yes**, with care (the mirror sends diffs and may not redraw after ENTER) |
| 3 | Updater prompts: Detected FPGA Type, Reformat flash disk?, About to update, WiFi module detected (x.y), Flashing ESP32 Success OK, Reset Configuration? | **human at the C64 keyboard** | the C64's own keyboard and screen | **no** (see below) |
| 4 | "Turning OFF machine in 5 seconds"; the user presses power | **human** | the power button | **no**, unless switched mains (see below) |
| 5 | Wait for REST to go down and come back; check that `/v1/info` `git_commit_hash` starts with the image's id | lead | REST | **yes** |
| 6 | After a config reset, an official 1.1.0 turns the network services off; the user turns them back on at the machine | human, official image only | keyboard | no; dev images leave them on |
| 7 | C64U, dev image: write kernal/basic/chars to `/Flash/roms`, then `client.reboot()` and look for the BASIC banner | lead | FTP + REST | **yes**, once `/Flash/roms` writes are allowed ([#544](https://github.com/JC-000/c64-test-harness/issues/544)). 1541ultimate confirmed on 2026-10-20 that the harness's C64-level `reboot()` loads the ROMs and `machine:reset` does not |
| 8 | Read the module IDENT from the WiFi row on the browser root; it is shown only while WiFi is Disabled | lead | telnet UI | **yes** ([#543](https://github.com/JC-000/c64-test-harness/issues/543)) |
| 9 | Gate: IDENT matches, WiFi joins, keyboard works. No cold power cycle before the gate passes | lead + human | mixed | partly |

**Why step 3 stays human.** The updater runs on the device, prompts on the C64 screen, and reads the C64 keyboard. REST and telnet are down while it runs. No remote input path into those prompts is known. Faking keystrokes would need a hardware keyboard injector: a new device, with new failure modes, during the one step where a mistake costs a board. The prompts are also the last human checkpoint before an irreversible write, and they carry the "WiFi module detected" PREV readout that the runbook's STOP rules depend on. **Recommendation: keep them human.**

## Proposed scope

A new module, `backends/ultimate64_flash.py`, plus a script, `scripts/flash_ultimate.py`. They automate steps 1, 2, 5, 7 and 8 and make the human steps explicit checkpoints. They do **not** try to remove the human.

```python
plan = FlashPlan(
    host,                        # any alias; the lock folds it to uid-<id>
    image=Path("c64u-green-42d8134e.ue2"),
    expect_hash="42d8134e",      # the verify step compares git_commit_hash against this
    expect_ident_after="1.18",   # optional; drives the F5 gate
    install_roms=Path("roms/"),  # C64U dev images only; checked against the three sha1s
)
with DeviceLock(host, allow_nested=True):
    flasher = Flasher(plan, prompt=operator_prompt)  # prompt(msg) -> bool; CLI: ask on the TTY
    flasher.preflight()   # /v1/info, product and unique_id match devices.toml, volume found, a recovery image present
    flasher.upload()      # storage_put_file(verify=True), short dot-free stem, refuses to overwrite a different SHA
    flasher.start()       # telnet: select, check "Run Update" in the context menu, choose it; dry_run=True stops before ENTER
    flasher.await_update()# REST down, then prompt("answer the updater prompts; report 'WiFi module detected'"), REST up
    flasher.verify()      # git_commit_hash prefix; product string; fpga_version recorded
    flasher.post_flash()  # ROMs + reboot + banner (C64U dev); IDENT readout; FlashRecord written
```

- **One lock for the whole flash.** The kit takes one lock per command, so another lane could slip in between the upload and Run Update. `Flasher` refuses to run unless this process holds the lock (`lock_held_for`).
- **Identity before anything is written.** `/v1/info` `unique_id` must match the `devices.toml` table that the host folds to, and the product must match the image type (`.ue2` for the C64U, `.u64` for the U64E). A wrong-device flash is the worst outcome here, and both devices sit on one desk.
- **A recovery image must already be on the stick.** Before Run Update, `preflight()` requires the official image (`cu_off.ue2` on the C64U) on the device volume. This is runbook rule F0, made mechanical.
- **Telnet "Run Update" is sent exactly once** and never retried. The result is checked on a *fresh* connection, because every telnet connection gets a fresh UI (`socket_gui.cc`) and the mirror may not redraw. 1541ultimate's notes say the first ENTER is often lost on a stale mirror. Our answer is to verify, then ask the operator, and never to send blind repeats: a doubled ENTER could land on the next prompt.
- **`/Temp` cost:** zero. FTP and telnet create no REST attachments. The `/v1/info` polling is bodyless.
- **Record.** `FlashRecord` (JSON) holds the image SHA-256, the before and after `/v1/info`, the PREV and IDENT readouts, the operator's answers and timings. It is the evidence the runbook currently writes by hand.
- **U64E differences.** The `.u64` image goes to `/USB1`. The telnet width is 60 (the kit's value). There is no ROM step, and no C64U-style F5 gate: the U64E radio's Disable does not turn RF off, so IDENT is read from the next updater's line.

### What it must not do

- Never power-cycle and never call `poweroff()`. A power cycle with a dead ESP module leaves the C64U dark (runbook F5).
- Never answer the updater, and never retry Run Update.
- Never flash a device whose `unique_id` it has not just read under the lock.
- Never let the official-image restore depend on the flash path that just failed. The fallback is the human at the machine and JTAG ([jtag_recovery.md](jtag_recovery.md)).

## Prerequisite issues (filed 2026-10-20)

- [#543](https://github.com/JC-000/c64-test-harness/issues/543): a telnet Remote-menu driver. Required by steps 2 and 8. The 1541ultimate repo already has one, `tests/e2e/lib/ui_backend.make_browser(MODE_TELNET, …)`. **Decision for the owner:** depend on it, vendor it, or port it. The harness has no runtime dependencies today (`pyproject.toml`), which argues for a port of the parts the flasher needs.
- [#544](https://github.com/JC-000/c64-test-harness/issues/544): `/Flash/roms` writes. Recommended: lift the refusal **only** for `/Flash/roms/{kernal,basic,chars}.bin` at exactly 8192/8192/4096 bytes, with read-back verify, behind a dedicated `storage_install_c64_roms()`. Every other `/Flash` path stays refused. The alternative, a bench-only updater commit that writes the ROMs itself, changes every image's hash. RED/GREEN evidence keys on that hash, and the commit would never go upstream, so this note recommends against it.
- [#545](https://github.com/JC-000/c64-test-harness/issues/545): `set_config_item` with read-back verify. Used for the post-flash settings.
- [#546](https://github.com/JC-000/c64-test-harness/issues/546): a public FTP-enable. Needed after an official-image config reset, if network services ever come back without the human. Low priority.

## Bench integration rule: e2e suites must sweep `/Temp` before releasing the lock

The 1541ultimate repo's e2e suites (`net_target_test.py`, `net_tls_test.py`) make their own REST calls under `locked.py`. `DeviceLock.release` drains `/Temp` only for harness clients registered in that process (`TempLedger.drain_on_lock_release`). Raw `urllib` POSTs from a suite are never counted or swept. So on any leak-prone grade (the C64U on official 1.1.0, or any image without GideonZ/1541ultimate#686), **a suite run must end with `Ultimate64Client(host).gc_temp_folder()` inside the lock, and a result with `.error` must stop further uploads.** Better still, the suites route their body-carrying calls through a harness client, so the per-request budget sweep applies. `locked.py` is the natural place for the end-of-run sweep.

## Optional: switched mains for step 4

A network-switched outlet would let the harness press "power" at step 4, and do the 15 s cold cycle for the F5 re-check. **Not recommended now.** It turns a deliberate human act into a remote one on a device where a cold start with a dead module is the unrecoverable case. If it is ever added, it should be gated like `poweroff(confirm_irrecoverable=True)`, and only after the F5 gate has passed.

## Open questions for the owner

1. Do you approve the scope: the harness automates upload, start, wait, verify, ROMs and IDENT, while the updater prompts and power stay human?
2. For the telnet driver (#543): depend on, vendor or port 1541ultimate's `ui_backend`?
3. For `/Flash/roms` (#544): approve the narrow lift?
4. Should the flasher refuse to start without the JTAG kit present (C64U only), or only warn?
