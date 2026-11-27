# Firmware flashing and the Telnet Remote Menu

Four harness APIs can change what an Ultimate device keeps across a
power cycle. All four are reached from the package root, need the caller
to hold the device's `DeviceLock` for the whole call, and cost zero
`/Temp` attachments on any firmware.

| API | Module | What it writes |
|---|---|---|
| `Ultimate64RemoteMenu` (#543) | `backends/ultimate64_remote_menu.py` | Its reads write nothing. Its WiFi Disable/Enable writes the WiFi config store to flash on the U64E. `run_update` starts the firmware updater. `run_task`'s printer Turn On/Off changes the IEC printer setting in RAM and marks the store for a flash save (#566). |
| `Ultimate64RemoteMenu.flash_esp` (#575) | `backends/ultimate64_remote_menu.py`, CLI `tools/flash_esp.py` | The ESP32 WiFi module's own flash, from a `.esp` image you supply. U64E only. |
| `flash_firmware` (#561) | `backends/ultimate64_firmware_flash.py` | The firmware, from an update image you supply, and optionally ROMs in `/Flash/roms`. |
| `save_config_item_to_flash` (#559) | `backends/ultimate64_flash.py` | One config key, and only when it is the only RAM-vs-flash difference. Contract in the c64-test skill's `REFERENCE.md`. |

The API contract (every parameter and error) is in
`.claude/skills/c64-test/REFERENCE.md`. The short worked patterns are in
`.claude/skills/c64-test/PATTERNS.md` § "Firmware flash and the Remote
Menu". This page explains how the pieces fit and what has been measured.
The bench scripts that load firmware into RAM or over JTAG, or power-cycle
through a switched outlet, are at the end
(§ "Bench scripts: RAM-run, JTAG load, outlet power cycle").

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
| `task_categories()`, `task_items(category)` | F5 (F1 on the framed layout), cursor keys | Read the task menu and a category's submenu; choose nothing. |
| `run_task(category, item, *, dry_run=False, settle_timeout=10.0, verify_timeout=15.0)` | F5/F1, cursor keys, one state-changing ENTER (none on a dry run) | Returns `TaskRunResult`. Only categories in `TASK_CATEGORIES` (Printer) are opened. |

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
  (`ESC[5~`/`ESC[6~`), ENTER as a bare CR, `o` for an `Ok` popup, and one
  F-key, only to open the task menu from the browser. F-keys mean different
  things per build: the task menu is F5 on the Ultimate line and F1 on the
  framed COMMODORE layout of official 1.1.0, where F5 is PageDown (source
  read), so the driver picks the key from the layout. Never LF (Ctrl-J swaps
  the joysticks), never `ESC[2~` (Insert deletes the selection), and never a
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
  Yes. A popup that a task action draws after its ENTER does not raise:
  `run_task` returns it in `TaskRunResult.popup` (lines joined with `\n`)
  and `popup_buttons`, with `closed=False`, and leaves it unanswered.
  Closing the session ends the firmware's popup loop
  (`UserInterface::popup` runs while `host->exists()`, 700c7f2fa). On
  builds before 1f3212c8b, the C64U's 1.1.0 (7b628eb16) among them, it ends
  through the closed socket instead: `SocketStream::get_char` returns -2,
  `Keyboard_VT100::getch` passes it through and the popup's `poll` returns
  -1, which ends its `do ... while (!ret)` loop. An
  example is Flush/Eject when the page is still rendering after the
  printer's 20 s wait: "The page is still printing.\nIt is saved when it
  is done.", with `Ok` (`iec_printer.cc:345`, 700c7f2fa; seen on the U64E,
  3.15a, 2026-11-13). The page is saved all the same. To get that popup
  rather than the progress box, pass `settle_timeout` above 20 s.
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

### Reading the WiFi module IDENT (`read_wifi_ident`)

`read_wifi_ident(host, *, link_timeout_s=90.0, poll_interval_s=5.0) ->
WifiIdentReading(ident, before, after, toggled)` (package root) reads the
module IDENT (e.g. `ESP32 WiFi Bridge V1.494`) outside a flash, on the U64E
and the C64U alike. The firmware shows the IDENT only while WiFi is
Disabled, so the function always leaves WiFi as it found it:

- A row already `disabled` is read and left alone (`toggled=False`).
- A `link-up` or `link-down` row is Disabled, read, Enabled again, and then
  polled until it is back where it was (`toggled=True`): a row found
  `link-up` must read Link Up again; a row found `link-down` counts as
  restored at Link Down or Link Up, so a module with no access point to
  join is not a failure. The Enable is also sent when the Disable or the
  read fails, unless the failure says the Disable's ENTER was never sent.
- Any other state (`scanning`, `busy`, `firmware?`, `no-module`, `unknown`)
  raises `WifiIdentUnreadableError` with nothing sent.
- Not back within `link_timeout_s`: `WifiLinkTimeoutError`; its `reading`
  holds the IDENT and the last row. WiFi has been enabled again.
- A `KeyboardInterrupt` during the re-Enable itself can leave WiFi
  Disabled. That cannot be avoided; check WiFi after an interrupted call.

It needs the `DeviceLock` (`RemoteMenuLockError` otherwise, before any
connection). It costs zero `/Temp`. **On the U64E each Disable and Enable writes
the WiFi config store to flash**, as above. `flash_firmware`'s `wifi_ident`
step runs the same code, but always waits for Link Up.

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

2026-11-10, U64E on the 1541ultimate printer bench build 178517dec:
`task_categories`, `task_items("Printer")`, dry runs, then a real Turn On,
Flush/Eject, Reset and Turn Off. One ENTER each, every one took effect; Turn
On/Off read back on fresh connections; 2.4-7.1 s per call. The Printer
submenu draws inside the task window, as `TaskSubMenu` places it.

Not measured: the official C64U 1.1.0 framed layout (read from source
only), and a "SID changed" popup during these runs. One was dismissed on
the first telnet read after the flash below.

## Flashing firmware (`flash_firmware`)

`flash_firmware(host, image, *, reformat_flash_disk,
confirm_device_powers_off=False, roms=(), dry_run=False, wait_up_s=900.0,
poll_interval_s=5.0, wifi_link_timeout_s=90.0) -> FlashRecord` takes an
update image from the host to a verified running firmware. It returns a
`FlashRecord`, and writes nothing to the host's disk.

`reformat_flash_disk` is required and has no default (new after 0.14.0, so
0.14.0 callers must add it): it is the answer the operator is told to give to
"Reformat Flash Disk?", and it is stored in
`FlashRecord.reformat_flash_disk`. Omitting it is a `TypeError`, and a
value that is not a `bool` is refused at `validate`. Both happen before
any traffic.

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

`operator_instruction(family, kind, *, reformat_flash_disk)` (import it from
`c64_test_harness.backends.ultimate64_firmware_flash`) builds the text the
person at the device is given. The harness logs it at WARNING before the
ENTER and again before the wait, and stores it in
`FlashRecord.operator_instruction`.

| Prompt (on the C64 screen) | Answer |
|---|---|
| "Reformat Flash Disk?" (asked only when the flash disk has files) | The caller's `reformat_flash_disk`: NO on this bench, YES only when a test needs it (owner, 2026-11-14) |
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

1. `validate`: lock, confirmation, `reformat_flash_disk`, kind, hashes,
   extension, image class, and each `RomImage`'s SHA-256, size and
   signature. No traffic.
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
   IDENT, Enable, wait for Link Up (`read_wifi_ident`'s cycle). On the U64E
   the Disable and the Enable each write the WiFi config store to flash. A
   row already Disabled is read and left as it was.
9. `roms`: C64U only, when `roms` are given. A ROM that `/Flash/roms`
   already holds with the same SHA-256 (matched by name as
   `storage_put_rom` matches it, then size, then the file read back over
   FTP) is not written; its path goes in `FlashRecord.roms_unchanged`. Every
   other ROM goes through `storage_put_rom`, so a stock file with other
   content still needs `overwrite=True, replace_stock=True`. If any ROM was
   written, or `reset_categories` ran (it resets the ROM selection in RAM,
   and a config change does not reload ROMs), `reboot()` follows so the C64
   loads its ROMs again, then `/v1/info` is read and the identity checked
   again. That proves the same firmware still answers; it proves nothing
   about the ROMs. Otherwise there is no reboot (`rebooted=False`).

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
| `FirmwareFlashRefusedError` | A precondition failed: the lock, the confirmation, a `reformat_flash_disk` that is not a `bool`, the product, ROMs on a U64E. |
| `FirmwareFlashTimeoutError` | REST, or the WiFi link, did not come back in time. |
| `FirmwareFlashVerifyError` | The device came back with the wrong identity, hash or IDENT, or the upload or Run Update did not verify. |
| `FirmwareFlashStepError` | A building block raised during a step; the original is `__cause__`. |

### What has been measured

**One live flash, on 2026-11-09 (n=1, one device):** the C64U over
ethernet, a SILENT bench image, unattended with the owner's authorisation.

| | before | after |
|---|---|---|
| `git_commit_hash` | 7b15448a7 | 584250385 |
| `unique_id` | the bench C64U's | unchanged |
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

## Flashing the ESP32 WiFi module (`flash_esp`)

`Ultimate64RemoteMenu.flash_esp(path, *, expect_ident=None,
confirm_module_flash=False, ...)` chooses the file browser's
"Flash into ESP32" action on a `.esp` image that is already on the device
(`FileTypeESP::execute`). The firmware stops the application, WiFi
included, flashes the module, shows a result popup, and only then restarts
the module. The driver answers that popup with exactly one `o`, and only
when its text is one of the firmware's exact strings: `Flashing ESP32
Success!`, `Flashing Error -4`, `Flashing Error -5`, `Could not read
header.`, `Incorrect signature in file.`, `Couldn't alloc mem to load
segment.` or `Could not switch to download mode.`, with an `Ok`-only button
row. Anything else raises without a key. By source (not tested live), a
session that dies does not leave the module in download mode: the popup
loop ends with the socket and the module restarts. Only "Could not switch to download mode." skips the
restart.

**The firmware checks nothing.** It does not check the ident or the chip,
and it does not check that the file was read whole: a truncated upload
still ends in "Success!". An image identical to the one running looks
exactly like nothing written. So:
- check the image before uploading it (the CLI runs an external
  `esp_image.py check`; the harness carries no copy of that tool);
- verify the upload's size and hash on the device before the ENTER (the
  CLI reads it back and compares bytes);
- read the ident before and after the flash. `EspFlashResult.proven` is
  true only when both were read and the ident changed. A verified flash
  whose ident did not change is reported `UNPROVEN`.

**Refused before any connection:** without `confirm_module_flash=True`;
without the `DeviceLock`; a path that is not a `.esp`, or is on `/Temp` or
`/Flash`; an `expect_ident` longer than 31 characters (the module reports
at most 31).

**Refused before the ENTER, after reads:** a driver host that is, or
resolves to, the module's WiFi address, or that is not the address the
`Net0` row shows (the flash stops WiFi, so a WiFi session would die
mid-flash), checked after two read sessions; an `expect_ident` equal to the
ident before the flash (`allow_same_ident=True` overrides), checked after
the ident read, which through the CLI is `read_wifi_ident` and so already
cost two WiFi config flash writes on the U64E; and a file whose menu has no
"Flash into ESP32" (C64U builds lack it), checked after the navigation keys.

**After the popup** it verifies on fresh sessions: the WiFi row back at Link
Up, a ping of the module's WiFi address (`ping=False` skips it, and the
result is then never proven), and the ident against `expect_ident`.
Errors carry `.result`: `EspFlashFailedError` (a failure popup, answered
once, never retried), `EspFlashNoPopupError` (no popup in time or the
device closed the session: nothing pressed, outcome unknown, check the
module first) and `EspFlashVerifyError` (success popup but WiFi, ping or
ident failed). An error after the ENTER carries `enter_sent=True`,
including a `DeviceLock` lost after the flash.

**The CLI** does the whole sequence on a U64E:

```
python -m c64_test_harness.tools.flash_esp --host <ethernet address> \
    --esp raw_u64.esp --platform u64e --esp-image-tool /path/to/esp_image.py \
    --dest /USB1/espflash/raw_u64.esp --expect-ident "<ident>" \
    --wifi-ip <module WiFi address> --confirm-module-flash
```

It checks the image with `esp_image.py`, takes the `DeviceLock`
(`--lock-timeout`, else `U64_DEVICE_LOCK_TIMEOUT`, else 60 s), uploads to
`--dest` (refusing an existing file), reads the upload back, reads the ident
with `read_wifi_ident` before and after (never inside the flash window),
runs `flash_esp`, and prints one result line. It deletes the upload, and its
directory if empty, after a normal return, a failure popup, a verification
failure or a refusal before the ENTER. After anything else (an interrupt,
no popup, an unexpected popup, a session or lock lost after the flash) the
file is kept and its path printed, because the firmware may still be
reading it.
Exit codes: **0** verified and proven (or, with `--allow-same-ident`,
verified with a ping reply); **3** verified but unproven (no ident change,
or `--no-ping`); **1** failed or unverified after the ENTER; **2** refused
before the ENTER, including a `DeviceLock` timeout or an unreadable lock
budget (`--lock-timeout nan`, a malformed `U64_DEVICE_LOCK_TIMEOUT`).

Costs and limits:
- Each ident read disables and re-enables WiFi, and on the U64E each toggle
  writes the WiFi config store to flash, so a default CLI run costs four
  such writes. The owner kept both reads (2026-11-21): the change of ident
  is the proof.
- The firmware's action has no mutex: nothing stops someone starting the
  same flash from the device's own menu at the same time. Do not touch the
  keyboard or the menu during the flash window.
- C64U: out of scope. Its builds have no "Flash into ESP32" action, and
  the CLI accepts `--platform u64e` only.

**Measured** (U64E over ethernet, 2026-11-21): four tagged-then-restore
pairs during PR #575's review, every flash `PROVEN`, 61.8-69.4 s each. Run
4 (on `6535087`) recorded the device at aab1343c2, firmware 3.15a, core
1.50, and exited 0; run 3 was the same device and day. The ident went
`ESP32 WiFi Bridge V1.507` → `Taab1343 WiFi Bridge V1.507` → back, WiFi
came back at Link Up each time, and each upload was read back byte-exact
before the ENTER. Upstream, GideonZ/1541ultimate#1121 (issue) and its fix
#1122 (PR), both open, make the action check the image and stop reporting
"Success!" for an empty or truncated file.

## Bench scripts: RAM-run, JTAG load, outlet power cycle

Three scripts under `scripts/` (#571) load firmware without writing flash,
or power-cycle a device. They are bench tools, not test steps. Each acts
only on a device bound in the local `~/.config/c64-test-harness/bench.toml`
(`[devices.<unique_id>]` tables; `C64_BENCH_BINDINGS_FILE` overrides the
path) and refuses any other: `ram_run = true` for `ram_run_u64.py`;
`jtag_adapter_location` and `jtag_adapter_baud` for `jtag_load.py`, plus
`jtag_scan_only = true` for a device that may only be scanned; `outlet` and
`outlet_kind` for `power_outlet.py`. The host still reaches its
`unique_id` through `devices.toml`, so a `bench.toml` table alone binds
nothing; `docs/development.md` has the details. Each takes the device's `DeviceLock`
(`ram_run_u64.py` takes its own and refuses to run inside a lock shell;
`jtag_load.py` and `power_outlet.py` also accept a hold by a verified
ancestor lock shell). All three are gated by
`tests/test_u64_runner_script_gates.py`.

- **`ram_run_u64.py run HOST IMAGE --sha256 H --expect-hash G`** runs a
  U64E application from RAM through Run Update, with no flash write. The
  flashed application comes back only with a power cycle.
  - **The image must be a RAM-run build** (`check_ramrun_image`: the app
    record at `0x3001000`, the stub at `0x3000000`, a `RAMR` parameter
    block). This is checked before any lock, client or upload. Run Update
    on a plain `update.u64` *flashes* the device; a plain image passed by
    mistake on 2026-11-13 did.
  - The image's SHA-256 is pinned. The upload goes to a USB volume, the
    Remote Menu browses to the exact file, and `/v1/info` must then report
    the expected git hash. The upload is deleted afterwards.
  - The hash change is the only proof that the RAM image started, so a
    device that already runs the expected hash is refused before the
    upload. A stronger restart proof is #570.
- **`jtag_load.py`** loads a `.bit` and an application image into a C64
  Ultimate over its P5 JTAG header (an ESP32 running esp-jtag), again with
  no flash write. It checks file SHA-256s, the bitstream's and the live
  FPGA's IDCODE, the user ID after the load, and the device's identity and
  git hash afterwards. `$ESP_JTAG_DIR` and `$C64U_TESTS_DIR` must be set;
  neither has a default.
- **`power_outlet.py cycle HOST`** cold-cycles a device through a switched
  outlet (Shelly gen2 or Tasmota), bound in `bench.toml`. Whether it powers
  the device off first is decided under the lock from the `/v1/info`
  `product` (#577):
  - "Ultimate 64" and "Ultimate 64 Elite": a graceful
    `poweroff(confirm_irrecoverable=True)`, then the cut (`--no-graceful`
    skips the poweroff). The source shows only that both switch off
    through the same power register; that they power up again after the
    cut is not measured (#576).
  - "C64 Ultimate" and "Ultimate 64-II": the cut only, unless
    `--c64u-poweroff` is given. A poweroff stores last-state OFF in the
    module, so the machine then comes back only with power-on mode ON.
  - Any other product, none, or REST down: the cut only. An explicit
    `graceful=True` or `--c64u-poweroff` is refused there, before the
    outlet is switched.
  - The outlet is never left off: any error after switching it off
    switches it back on before raising.

### One volatile load for both products: `volatile_load.py` (#585)

`scripts/volatile_load.py` sits above `ram_run_u64.py` and `jtag_load.py`
and gives both products one call, one result shape and one way back. It
adds no device I/O of its own beyond a bodyless `/v1/info` read; each
backend keeps its gates.

```python
from volatile_load import VolatileImage, volatile_load, volatile_restore, volatile_session

image = VolatileImage(path, sha256, expected_hash)              # U64 line: a RAM-run .u64
image = VolatileImage(app, sha256, expected_hash, bit=bit, bit_sha256=bit_sha)   # C64U class

with volatile_session(host, image, baseline_first=True) as s:   # one DeviceLock across all of it
    ...                                                         # the rows; s.load_image(other) for a second image
```

- **Backend.** Chosen under the lock from the `/v1/info` product:
  "Ultimate 64" and "Ultimate 64 Elite" get `ram_run`; "C64 Ultimate" and
  "Ultimate 64-II" get `jtag_cold`. Any other product is refused. A box
  that does not answer three polls is dark: the backend then follows the
  image's family, which the device's `bench.toml` table must enable, and
  the identity is checked on `/v1/info` after the load. A dark U64-line
  box needs a JTAG reload through a second adapter (#583). That backend is
  an injected callable (`u64_jtag_reload`, default
  `volatile_load.U64_JTAG_RELOAD`); while none is wired it is a refusal,
  and the way back is a power cycle.
- **Images.** Checked before any lock, upload or JTAG pin. Exactly two
  shapes pass: a RAM-run file (`check_ramrun_image`) for the U64 line, and
  a one-record `ultimate.app` at `0x30000` plus the FPGA `.bit` for the
  C64U class (the DRAM image is extracted with `payload_from_app`; a raw
  `ultimate.bin` carries nothing to check and is not accepted). Update
  images, `.cfw` files and garbage are refused, and so is any file holding
  an updater prompt string or the SILENT marker, whatever its shape. The
  image's family must be the device's.
- **The image must be the build the caller names.** Both formats carry
  the application's System Info line, `Git hash:   <hash>`
  (`system_info.cc`), where the hash is the build's
  `git rev-parse --short HEAD` (`target/common/rules.mk`), the same macro
  `/v1/info` reports as `git_commit_hash`. Pre-flight reads it from the
  file. An image with no such line, or with two different ones, is
  refused. So is an expected hash that does not begin with every digit
  the image carries, which includes one with fewer digits than the image
  has. After the load `/v1/info` must report exactly the image's hash.
  Read offline from builds on this bench (2026-11-26, no device): three
  RAM-run `.u64` files and one u64ii `ultimate.app` each carry exactly
  one such line with a 9-digit hash and none of the updater strings, and
  pass; an `update.u64`, an `update.ue2`, a raw u64ii `ultimate.bin` and a
  U64 `ultimate.app` (one record at `0x0`) are refused. The image a U64
  JTAG reload loads (`jtag_image`) is not read here: that check is its
  backend's.
- **Result.** `VolatileResult`: `ok`, `backend`, `family`, `product`,
  `git_hash` (reported by the running firmware), `expect_git_hash`,
  `image_git_hash` (read from the file),
  `loaded`, `dark_before`, `hash_changed`, `restart_proof`, `timings`
  (seconds per step), `notes`, and the backend's own `record`. A failure
  raises `VolatileLoadError` (a `JtagLoadError`, so it has a `stage`) whose
  `result` has `ok` False.
- **What a result proves.** After a load the running firmware must report
  the bound `unique_id`, a product of the image's family and the expected
  hash. `restart_proof` then says why the load is believed to have
  started: `hash-change`; `jtag-reconfigured` (the FPGA was reloaded cold
  and the user ID read back, which also covers a same-hash load);
  `dark-then-answering`; or `none`. A same-hash RAM-run cannot be proven
  (#570) and `ram_run` refuses it. So `volatile_restore` on a U64-line
  device that already reports the baseline hash loads nothing and returns
  `loaded` False with `restart_proof` `none`: the hashes agree, and that
  the running image is the baseline file is not proven. For the same
  reason `baseline_first` fails on a U64-line device that already reports
  the baseline hash, and the image is then never loaded.
- **Baseline and restore.** `volatile_restore(host)` loads the baseline
  from the device's `bench.toml` table: `volatile_baseline_image`,
  `volatile_baseline_sha256`, `volatile_baseline_git_hash`; for the C64U
  class also `volatile_baseline_bit` and `volatile_baseline_bit_sha256`;
  optionally, for the U64 line, `volatile_baseline_jtag_image` and
  `volatile_baseline_jtag_sha256` (what a JTAG reload loads into a dark
  box). The session restores on the way out, also when the load or the
  body raised. The body's exception propagates; a restore failure is then
  kept in `session.restore_error` and logged, and is raised only after a
  clean body.
- **Lock.** The session takes the `DeviceLock` in this process
  (`allow_nested`), so the backends and the harness helpers called in the
  body join it. Under a verified lock shell it takes none
  (`session.lock_in_process` is False): the C64U path works, the RAM-run
  path is refused by `ram_run_u64.py`, and helpers that check for an
  in-process hold refuse (#582).
- **Adapter.** Only the adapter bound to the device in `bench.toml` is
  used. A `jtag_scan_only` device and a missing binding are refused before
  the lock, and so is a `JTAG_USB_LOCATION` that names another location
  (`jtag_load.py` alone lets that variable override the table).
  `jtag_load.py`'s live IDCODE gate still runs before the FPGA is touched.
- **Cold only.** The C64 Ultimate load is always cold. There is no option
  for a warm load, and `JTAG_LOAD_WARM` is not honoured here.
- **JTAG timeout.** `jtag_timeout` under 300 s is refused. The cold
  esp-jtag load is never interrupted by this code; the value is the budget
  handed to the U64 JTAG reload.
- **Storage.** On the RAM-run path the only storage writes are
  `ram_run_u64.py`'s: one staged file, deleted again.

```
volatile_load.py load    HOST IMAGE --sha256 H --expect-hash G [--bit F --bit-sha256 H]
                         [--jtag-image F --jtag-sha256 H] [--baseline-first] [--dry-run] [--record F]
volatile_load.py restore HOST [--dry-run] [--record F]
```

`--dry-run` prints the plan (the backend's own dry run included) and
takes no lock and opens no client, storage, menu or adapter. Nothing here
has run against a device: every test uses stand-ins.
