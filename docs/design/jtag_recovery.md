# Design note: does JTAG recovery belong in the harness?

**Status:** assessment for the owner's decision. Nothing implemented. Requested 2026-10-20 by the 1541ultimate lane.

**Sources:**
- `~/Documents/1541u-tools/esp-jtag/`: `README.md`, `WIRING.md`, `host/esp_recover.py`
- `~/Documents/1541ultimate/recovery/u64ii/`: `README.md`, `recover.py`, `u64_mk2_artix.bit`, `ultimate.bin`
- the 1541ultimate lane's report of 2026-10-20

No claim below was measured by this repo.

## What the kit is

A classic ESP32 devkit at `/dev/cu.usbserial-0001` acts as the JTAG adapter, wired to the C64U's P5 header (TCK/TDI/TDO/TMS/GND, no 3.3 V). `esp_recover.py` replays `recover.py`'s exact TCK sequence; the README says equivalence is checked over 25,433,093 TCK cycles against a fake FTDI. It then does three things:

1. It reads the IDCODE, and stops if it differs from the `.bit`'s. The XC7A50T reads `0362c093`.
2. It loads the FPGA with `u64_mk2_artix.bit` and uploads `ultimate.bin` into DRAM.
3. It checks the user ID `0xDEAD1541`, which proves the image is alive.

**Nothing is written to flash, and a power cycle reverts it.** The input files are pinned by SHA-256 (`FILE_SHA256`); `--force-files` overrides that. On 2026-10-20 it passed `--idcode-only` (`0362c093`) and a full dry run (user ID `dead1541`, 76.5 s at 921,600 baud; 3 M/2 M baud fail on this bridge).

**What it covers:** a bad Ultimate application or FPGA image, with the machine powered. The recovery app then runs the official update from the file browser.

**What it doesn't cover:** an ESP module that dies before it drives the regulators. The machine stays dark and the FPGA never powers up.

## Q1: Should the harness's flashing call recovery automatically, as an "unbrick" step?

**Recommendation: no automatic call. A harness-wrapped manual step is acceptable.**

- **The trigger cannot be detected reliably.** A device that does not answer REST after a flash might be bricked. It might also be waiting for the human's power press, still running the updater, or have its services turned off by an official config reset (runbook Settings 1). A wrong "it's bricked" verdict would reconfigure the FPGA under a healthy updater or app. That turns a recoverable situation into an unknown one.
- **It needs the machine on, and only the human knows the power state.** If the module is dead, the board is dark and JTAG reads `00000000`/`ffffffff`, which looks like a wiring fault.
- **It is the last-resort path.** An automated mistake in it has no further remedy short of the bench.

What the harness can usefully do:

- `scripts/jtag_recover.py`: a thin wrapper around `esp_recover.py`. It holds the device's DeviceLock and checks the adapter-to-device binding (see "Identity and locks"). It runs `--idcode-only` first and requires the operator to confirm the full load. Afterwards it waits for `/v1/info` and checks `unique_id`.
- The flasher's `preflight()` checks that the adapter is present and answers `--link-test` before a C64U flash. Per the runbook, JTAG is the recovery for "Flashing ESP32 Failed / You shouldn't see this" with the machine on. Whether a missing kit blocks or only warns is the owner's call (flashing note, Q4).

## Q2: JTAG-load a dev build into DRAM for unattended, app-only test cycles?

**Promising, but unproven on three points. Recommend a bounded experiment, not a harness feature yet.**

The kit already does the core operation: it loads *a* bitstream and *an* application into DRAM and starts it. A load of image X, a test, then a return to the official image, with no flash write and no updater prompts, would remove both human steps from app-only iterations. Those are exactly the cycles the RED/GREEN runs spend most of their time on.

Open points, each of which could sink it:

1. **Which `.bit`/`.bin` pair?** The kit is pinned to the recovery pair. A dev build would need its own matching pair. The build produces those artifacts, but they are not in the `.ue2`, so extraction or a parallel build output is needed. `--force-files` exists, but every non-pinned load is unreviewed.
2. **"No flash" is weaker than it sounds.** A DRAM-booted app still owns the flash filesystem. It may write config, format `/Flash` or touch the ROMs like any app. A dev app with a different config layout could leave state that the official app reads after the power cycle. The kit's "nothing is written to flash" describes the *loader*, not the app it starts.
3. **Getting back is a power cycle, and that is human.** "Load the official image back" would need the official `.bit`/`.bin` pair, which we don't have separately, or a power cycle. A cold power cycle is the step the runbook forbids before the F5 gate, because a dead ESP module leaves the board dark. JTAG-loading an app does not change the module, so the risk should be low. But "should be low" is not measured.

Also unverified:

- whether the ESP module (u64ctrl) and the DRAM-loaded app agree on the RPC protocol version when they come from different builds. A module/app mismatch is a new failure surface;
- whether `/v1/info` reports the loaded build's `git_commit_hash`, which the evidence trail keys on.

**The bounded experiment (owner's call; a person at the bench; C64U on official 1.1.0):**

1. JTAG-load the recovery pair and check `/v1/info`.
2. Power-cycle and check that the device is back on 1.1.0 with the config unchanged: compare `get_config_item` across all categories before and after.
3. Repeat with one dev `.bit`/`.bin` pair.

If the config is unchanged and `/v1/info` reports the dev hash, write the feature design. Otherwise stop.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Recovery fires on a healthy device that is mid-update | high | never automatic; the operator confirms; IDCODE check first |
| The adapter is cabled to the other device (both on one desk) | high | bind the adapter to a `unique_id`; verify `/v1/info` after load |
| A DRAM app writes flash-resident state the official app then misreads | medium, unmeasured | experiment step 2 (config diff) |
| Module/app RPC mismatch when the builds differ | medium, unmeasured | experiment; record IDENT and the app hash together |
| The pins back-feed the FPGA while it is off | low | firmware tri-states the pins at reset, on CLOSE and after a 10 s idle timeout (WIRING.md); don't leave the host tool running |
| A serial-port open resets the ESP32 (DTR/RTS) mid-sequence | low | one process owns the port; take a separate host-side lock on the adapter |
| Loading an unpinned image (`--force-files`) | medium | a harness wrapper refuses unless the caller passes the expected SHA-256 |

## Identity and locks

- **The device lock:** the same `DeviceLock` as REST/FTP, keyed `uid-5d2518` through `devices.toml`. A JTAG load replaces the running application, so it is the most invasive operation on the device and must hold the lock for the whole load *and* the post-load `/v1/info` check.
- **The adapter is a second resource.** Two lanes could each hold a different device's lock and both open `/dev/cu.usbserial-0001`. Give the adapter its own lock file (`DeviceLock("jtag-esp32-0001")`, or an `flock` on the tty), taken after the device lock.
- **Binding:** extend `devices.toml` with `[devices.5d2518] jtag = "/dev/cu.usbserial-0001"`, preferably keyed by the USB serial number rather than the tty name, which can renumber. The wrapper refuses an adapter not bound to the device it locked. IDCODE identifies only the FPGA family, so after the load the wrapper checks `/v1/info` `unique_id` against the binding. The ID is flash-serial-derived, so it survives a DRAM load.
- **Never on the U64E with this kit.** Its wiring and files are for the U64-II/C64U board (XC7A50T). The wrapper refuses any product other than C64 Ultimate / Ultimate 64-II.

## Recommendation, in one line

Keep JTAG out of automatic flashing. Wrap it as a locked, identity-bound, operator-confirmed script. Run the bounded DRAM-load experiment before deciding whether unattended app-only cycles are a harness feature.
