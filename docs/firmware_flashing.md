# Firmware flashing and the Telnet Remote Menu

Three harness APIs can change what an Ultimate device keeps across a
power cycle. All three are exported at the package root, need the caller
to hold the device's `DeviceLock` for the whole call, and cost zero
`/Temp` attachments on any firmware.

| API | Module | What it writes |
|---|---|---|
| `Ultimate64RemoteMenu` (#543) | `backends/ultimate64_remote_menu.py` | Its reads write nothing. Its WiFi Disable/Enable writes the WiFi config store to flash on the U64E. `run_update` starts the firmware updater. |
| `flash_firmware` (#561) | `backends/ultimate64_firmware_flash.py` | The firmware, from an update image you supply, and optionally ROMs in `/Flash/roms`. |
| `save_config_item_to_flash` (#559) | `backends/ultimate64_flash.py` | One config key, and only when it is the only RAM-vs-flash difference. Contract in the c64-test skill's `REFERENCE.md`. |

The API contract (every parameter and error) is in
`.claude/skills/c64-test/REFERENCE.md`. The short worked patterns are in
`.claude/skills/c64-test/PATTERNS.md` § "Firmware flash and the Remote
Menu". This page explains how the pieces fit and what has been measured.

## The Telnet Remote Menu (`Ultimate64RemoteMenu`)

The firmware serves its file browser over telnet on port 23 (the "Telnet
Remote Menu" service in Network Settings; on by default on 3.x and on the
bench images, off by default on the official C64U 1.1.0). The harness has
its own small telnet client and VT100 screen model: Python 3.13 removed
`telnetlib`, and the harness has no runtime dependencies.

```python
from c64_test_harness import DeviceLock, Ultimate64RemoteMenu

lock = DeviceLock(host)
lock.acquire_or_raise(timeout=120)
try:
    menu = Ultimate64RemoteMenu(host)
    rows = menu.read_screen()        # the root screen as 24 text rows
    wifi = menu.wifi_status()        # WifiStatus(state, ident, ip, mac, detail, row)
    labels = menu.context_actions("/SD/updates/update.ue2")
finally:
    lock.release()
```

Each call opens its own connections and closes them before it returns.

| Call | Sends | Notes |
|---|---|---|
| `read_screen()` | No key (only `o` for a dismissable popup) | The root screen of a fresh session. |
| `wifi_status()` | Same | `state` is one of `link-up`, `link-down`, `disabled`, `scanning`, `busy`, `firmware?`, `no-module`, `unknown`. `ident` (the module's IDENT) is shown only while WiFi is Disabled. |
| `context_actions(path)` | Cursor keys, and the ENTER that opens the menu | Opens the entry's context menu and chooses nothing. |
| `set_wifi_enabled(enabled, *, dry_run=False, verify_timeout=15.0)` | One state-changing ENTER (none on a dry run) | Returns `WifiChangeResult`. |
| `run_update(path, *, dry_run=False, confirm_device_powers_off=False, verify_timeout=60.0)` | One state-changing ENTER (none on a dry run) | Returns `RunUpdateResult`. |

### What the driver will and will not send

- **The lock comes first, on every connection**, reads and verification
  probes included: a telnet session is a UI session on a shared device.
  Without the lock it raises `RemoteMenuLockError` and sends nothing.
- **It never answers telnet negotiation.** The server's `IAC` options are
  parsed and discarded. On the C64U's riscv32 target `char` is unsigned,
  so the firmware's `IAC` skip in `SocketStream::get_char` never matches
  (source read), and a reply such as `IAC DO ECHO` would reach the menu as
  three keys; the last, `$01`, is Ctrl-A, "select all". On 2026-11-09 the
  C64U drew its root screen with zero bytes sent by the client (n=3).
- **Only allow-listed keys**: the cursor keys, PageUp/PageDown
  (`ESC[5~`/`ESC[6~`), ENTER as a bare CR, and `o` for an `Ok` popup.
  Never LF (Ctrl-J swaps the joysticks), never `ESC[2~` (Insert deletes the
  selection), never an F-key (their meaning differs by build), and never a
  letter in the browser (the official C64U defaults to WASD navigation).
  A menu is closed by closing the session, never with a key.
- **The highlight is read, not assumed.** The driver learns the selection
  colour on each session's root screen, moves one key at a time and reads
  the highlight back after every key. A key with no visible effect raises
  `RemoteMenuNavigationError`; nothing is resent.
- **A state-changing ENTER is sent exactly once.** It is never resent, and
  its effect is read on a fresh connection.
- **Modal popups stop the call**, with one exception: the `Ok`-only popup
  "SID changed. Please review settings" (`DISMISSABLE_MESSAGES`), which a
  fresh session can open on. The driver answers it with one `o`, logs it at
  WARNING and records it in `dismissed_messages`. ENTER is never used on a
  popup, because it picks the active button, which on a Yes/No popup is
  Yes.
- **A new session can take queued device messages**, even a read-only
  call: the firmware queues messages device-wide (up to four), and the next
  session to poll shows one per poll. When each is dismissable the session
  answers it and the next can follow, up to four; a verification probe
  never answers one, so it shows at most one. So a call can consume
  messages meant for someone at the device. Each result's `messages` lists
  what that call's sessions showed.
- **No REST or FTP request**, so zero `/Temp` cost.

A **dry run** still connects, navigates and opens the context menu, then
stops with the action highlighted and closes the session without the
ENTER. That proves the path exists and the action is offered. Those keys
move only the connection's own UI state, which the firmware discards on
close; the one device-wide effect a dry run can have is consuming queued
messages.

### WiFi Disable and Enable

`set_wifi_enabled` reads the WiFi row first and does nothing if it is
already where you asked (for Enable, any state but `disabled`,
`no-module` and `unknown`). Otherwise it refuses, without a key, unless
the row's menu offers the action: Disable from `link-up`, `link-down` or
`no-module`, Enable from `disabled`. It also refuses Disable when the
session is connected through the WiFi address itself. Verification polls
fresh connections until the row moves or `verify_timeout` passes (`RemoteMenuVerifyError`, `enter_sent=True`).

What the action changes depends on the product (source read):

- **U64E** (and the rest of the Ultimate line): it sets `WiFi Enabled` and,
  when the value changes, **writes the WiFi config store to flash**. The
  choice survives a power cycle.
- **C64U**: it switches the radio off or on, with no config write.

Enable verifies only that the row left `disabled`, not that WiFi rejoined.
On 2026-11-09 both devices read `link-down` right after Enable (3 cycles
each) and reached `link-up` later, so wait for Link Up if you need the
WiFi address back. `flash_firmware`'s WiFi step does that.

### Run Update

`run_update` refuses a real run unless `confirm_device_powers_off=True`,
because every updater ends by switching the machine off
(`Ultimate64UnsafeOperationError` otherwise; a dry run needs no
confirmation). After the one ENTER it watches the committing session for a
popup (`UpdateImageRejectedError`, nothing flashed) and polls fresh
connections. Two in a row with no answer mean the application has stopped:
`started=True`. That is **not** proof that the flash worked. The first
probe waits `RemoteMenuTiming.first_probe_delay` (5 s), and polling goes on
until `verify_timeout`, then `RemoteMenuVerifyError`.

**The updater's prompts are not visible over telnet.** The application
jumps to an updater that runs with no network stack and draws on the C64
screen. Someone at the keyboard answers the prompts (`prompts_visible` is
always `False`).

**The firmware does not check a `.u64` or `.ue2` image.** It loads the
records and jumps to them. Only a `.cfw` (from 3a1ff9ff on; 1.1.0 has no
`.cfw` loader) is checked. So check the image on the host before
uploading it; `flash_firmware` does.

### Errors

All are `Ultimate64RemoteMenuError` (an `Ultimate64Error`), which carries
`enter_sent` and the last `screen`. If `enter_sent` is `True` the device may
have acted, so do not just repeat the call.

| Error | Meaning |
|---|---|
| `RemoteMenuLockError` | The lock is not held. Nothing was sent. |
| `RemoteMenuConnectError` | No usable session: unreachable, refused, a password prompt, the session cap, or nothing drawn. |
| `RemoteMenuUnexpectedScreenError` | An unknown popup, a state with no action, or a layout the driver cannot read. |
| `RemoteMenuNotFoundError` | The path, entry or label is not there, or cannot be matched exactly on a 60-column screen. |
| `RemoteMenuNavigationError` | A key had no visible effect, or the wrong one. |
| `RemoteMenuVerifyError` | The ENTER was sent and fresh connections did not show its effect in time. |
| `UpdateImageRejectedError` | A popup appeared after Run Update's ENTER (`popup` is its text). Nothing was flashed. |

### What has been measured

2026-11-09, both devices over ethernet on the bench build 3.15a/7b15448a7
(neither was on official 1.1.0):

- `read_screen` / `wifi_status`: n=3 per device, 60×24 layout, about
  0.24 s per read.
- Dry runs of `run_update` and `set_wifi_enabled` on both devices:
  `enter_sent=False`, WiFi still Link Up afterwards.
- `context_actions` paged through a 134-entry directory with
  PageUp/PageDown, and resolved files in subdirectories.
- Real WiFi Disable then Enable, 3 cycles per device: each ENTER sent
  once took effect, 12 of 12.

Not measured: the official C64U 1.1.0 framed layout (read from source
only), and a "SID changed" popup during these runs. One was dismissed on
the first telnet read after the flash below.

## Flashing firmware (`flash_firmware`)

`flash_firmware(host, image, *, confirm_device_powers_off=False, roms=(),
dry_run=False, wait_up_s=900.0, poll_interval_s=5.0,
wifi_link_timeout_s=90.0) -> FlashRecord` takes an update image from the
host to a verified running firmware. It returns a `FlashRecord`, and
writes nothing to the host's disk.

It never answers an updater prompt, never presses a button, never cuts
power, and never retries Run Update.

### Rules

- **The caller holds the `DeviceLock` for the whole call.** It refuses
  before any traffic without it, and checks again before the Run Update
  ENTER and before every step after the wait. The storage helpers and the
  Remote Menu check on every call too.
- **Zero `/Temp`.** The image goes up over FTP (`STOR` writes the target
  directly), Run Update goes through telnet, and every REST call is
  bodyless: `GET /v1/info`, `PUT /v1/machine:reboot` and, on an official
  C64U, the per-category `reset_to_default` PUTs. Never
  `set_config_items_batch`.
- **The image is validated on the host**, because the firmware does not
  validate it: the SHA-256 must match the caller's record; the extension
  must be the product's (`.u64` for a U64E, `.ue2` for a C64U); and the
  records must parse as the loader reads them (`inspect_update_image`).

### Image kinds

`FirmwareImage.kind` is one of `"silent"`, `"dev"` or `"official"`, and the
harness checks it against the image itself:

| Class read from the image | Kinds it can be | Verified afterwards on |
|---|---|---|
| All three prompt strings (`PROMPT_STRINGS`) | `dev`, `official` | `dev`: `git_commit_hash`. `official`: `product` and `firmware_version`; on a C64U the hash must be absent, and on a U64E it is checked too when given. |
| The SILENT marker and none of the prompts | `silent` (C64U only) | `git_commit_hash` |
| Anything else | refused | — |

"Anything else" covers an image with no prompts and no marker (a RAM
application image, say) or only some of the prompts. Run Update would jump
into it and nothing would be flashed. The marker alone proves nothing,
because prompting bench builds print it too.

**The `git_commit_hash` gate.** A `dev` or `silent` image is verified on
`git_commit_hash`, so it must contain that string: a firmware built from an
image without it could never report one. This is also what stops the
official C64U 1.1.0 image (which reports no hash) being flashed as `dev`.
A hash matches as a prefix either way, at least 7 hex digits, because the
firmware reports `git rev-parse --short`, whose length varies.

`dev` and `silent` need `expected_git_commit_hash`. `official` needs
`expected_firmware_version`, and on a C64U `expected_git_commit_hash` must
be `None`.

If the device already reports the target identity before the flash, the
check afterwards cannot tell a flash from none: `flash_distinguishable`
and `identity_verified` stay `False` and a warning is recorded. The flash
is not refused.

### The prompts, and the button

`operator_instruction(family, kind)` (import it from
`c64_test_harness.backends.ultimate64_firmware_flash`) builds the text the
person at the device is given. The harness logs it at WARNING before the
ENTER and again before the wait, and stores it in
`FlashRecord.operator_instruction`.

| Prompt (on the C64 screen) | Answer |
|---|---|
| "Reformat Flash Disk?" (asked only when the flash disk has files) | YES |
| "About to update. Continue?" | YES |
| "Reset Configuration? (Recommended)" | YES, except **NO on an official C64U image** |
| An ESP32 "Flashing ESP32 ..." popup waiting for OK | OK |
| Anything else | Do not answer; report it |

Official C64U 1.1.0 defaults every network service to Disabled, so a reset
there would leave no remote way back. After the NO, the harness resets the
non-network categories itself (`apply_factory_baseline`). On the U64E, 3.x
defaults keep the network services on, so YES is safe.

A prompting updater ends with "Turning OFF machine in 5 seconds" and
switches the machine off. **Press the power button once** to switch it
back on. The U64E has no SILENT path, so every U64E flash needs that
press. A U64E flash on 2026-11-09, not driven by this function, took
684 s including the press; `wait_up_s` defaults to 900 s.

A SILENT image asks nothing and resets the configuration itself. By
source, its updater then has the module switch the machine off and on
again, with no button press.

### Steps

`FlashRecord.steps` lists the steps that finished; on an error,
`record.failed_step` names the one that raised.

1. `validate`: lock, confirmation, kind, hashes, extension, image class,
   and each `RomImage`'s SHA-256, size and signature. No traffic.
2. `preflight`: `GET /v1/info` gives the product (and so the family and
   extension), `unique_id` and the running hash. An unknown product, a
   SILENT image for a U64E, or ROMs for a U64E is refused. On a C64U a
   JTAG-recovery warning is always recorded (it cannot be checked offline).
3. `upload`: reuses a file already at `device_path` with the same SHA-256;
   otherwise `storage_put_file(verify=True)`. A file of that name with other
   content is never overwritten: the step fails.
4. `run_update`: `Ultimate64RemoteMenu.run_update` must report `started`.
   If it fails, the error says whether the ENTER was sent, was not, or is
   unknown, and unknown is to be treated as sent. Do not retry.
5. `wait`: polls `GET /v1/info` until REST answers, up to `wait_up_s`
   (`FirmwareFlashTimeoutError` after it).
6. `verify`: the same `unique_id`, the same product family, and the
   identity the kind calls for.
7. `reset_categories`: official C64U only.
8. `wifi_ident`: only with `expected_wifi_ident`. Disable, read the module
   IDENT, Enable, wait for Link Up. On the U64E the Disable and the Enable
   each write the WiFi config store to flash. A row already Disabled is read
   and left as it was.
9. `roms`: C64U only, when `roms` are given. `storage_put_rom` for each,
   then `reboot()` so the C64 loads its ROMs again, then `/v1/info` is read
   and the identity checked again. That proves the same firmware still
   answers; it proves nothing about the ROMs.

### Dry run

`dry_run=True` needs no `confirm_device_powers_off`. It validates the image
and reads `/v1/info`, then lists the target's directory (and reads the file
back if one of the same size is there) to see whether the image is already
on the device. It uploads nothing and changes no config, WiFi or ROMs. If
the image is on the device, it navigates to it with
`run_update(dry_run=True)`, which opens the context menu and stops without
the ENTER; if not, it records a warning and stops. The operator instruction
is logged either way. A file of the same name with other content fails
the dry run's `upload` step, as it would fail a real run.

### Errors

`FirmwareFlashError` (an `Ultimate64Error`) carries the partial
`FlashRecord` as `record`; `.step` is `record.failed_step`.

| Error | Meaning |
|---|---|
| `FirmwareImageError` | Host-side validation failed. Raised before the image is uploaded or selected. |
| `FirmwareFlashRefusedError` | A precondition failed: the lock, the confirmation, the product, ROMs on a U64E. |
| `FirmwareFlashTimeoutError` | REST, or the WiFi link, did not come back in time. |
| `FirmwareFlashVerifyError` | The device came back with the wrong identity, hash or IDENT, or the upload or Run Update did not verify. |
| `FirmwareFlashStepError` | A building block raised during a step; the original is `__cause__`. |

### What has been measured

**One live flash, on 2026-11-09 (n=1, one device):** the C64U over
ethernet, a SILENT bench image, unattended with the owner's authorisation.

| | before | after |
|---|---|---|
| `git_commit_hash` | 7b15448a7 | 584250385 |
| `unique_id` | 5D2518 | 5D2518 |
| `firmware_version` / `product` | 3.15a / Ultimate 64-II | 3.15a / Ultimate 64-II |

- Steps `validate` → `preflight` → `upload` → `run_update` → `wait` →
  `verify`; `identity_verified=True`, `flash_distinguishable=True`.
- REST answered about 125 s after the Run Update ENTER, with **no button
  press**. `wait_seconds` reads 105: it counts from when `run_update`
  returned, which was when the updater was seen to start, and REST was
  polled every 5 s. The whole call took 176 s.
- `run_update`'s evidence: fresh sessions still answered at the root at
  about 5 s and 7 s after the ENTER, then the committing session received
  1460 bytes, then two fresh connections got no answer (`started=True`).
- Afterwards REST, FTP and telnet answered and WiFi was Link Up. The SILENT
  configuration reset had run (the syslog target was cleared).
  `/Flash/roms` was still present.
- The first telnet read after the flash showed "SID changed. Please review
  settings"; the driver answered `Ok` and logged it.

Not yet run live: the U64E, prompting (`dev`) and `official` images, the
`wifi_ident` step and the `roms` step.

## Saving one config key to flash

`save_config_item_to_flash(client, category, item, value, *, dry_run=False)`
is the only sanctioned config save in the harness.
(`Ultimate64Client.save_config_to_flash` is public, but it saves every
dirty store, and nothing in the harness calls it except this function.)
Its job is to
put back a key that a run persisted. It refuses unless that key is the only
RAM-vs-flash difference among the stores it can compare. Reading flash means
loading it into RAM, so the comparison has side effects even when it
refuses. The full contract is in the skill's `REFERENCE.md` § "Guarded
single-key save to flash (#559)".
