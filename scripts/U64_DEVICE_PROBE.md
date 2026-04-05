# Ultimate 64 Elite - Live Device Probe

Read-only reconnaissance of the real Ultimate 64 Elite on the LAN via its
HTTP REST API (plain HTTP on port 80, no TLS). Captured on 2026-04-05.

Companion script: `scripts/probe_u64.py` (`python3 -m urllib.request`, zero deps).

---

## 1. Device Identity

| Field              | Value                        |
|--------------------|------------------------------|
| product            | `Ultimate 64 Elite`          |
| firmware_version   | `3.14`                       |
| fpga_version       | `121`                        |
| core_version       | `1.45`                       |
| hostname           | `Ultimate-64-Elite-688004`   |
| unique_id          | `601A96`                     |
| REST API version   | `0.1`                        |

Endpoints hit: `GET /v1/version`, `GET /v1/info`, `GET /v1/configs`,
`GET /v1/drives`, `GET /v1/configs/{category}`,
`GET /v1/configs/{category}/{item}`.

---

## 2. CPU Speed (Turbo) - Authoritative Enum

Resolves the Elite II "20 vs 64 MHz" question for THIS device
(U64 Elite, firmware 3.14 / core 1.45):

```
GET /v1/configs/U64%20Specific%20Settings/CPU%20Speed

{
  "U64 Specific Settings" : {
    "CPU Speed" : {
      "current" : " 1",
      "values"  : [ " 1", " 2", " 3", " 4", " 5", " 6",
                    " 8", "10", "12", "14", "16",
                    "20", "24", "32", "40", "48" ],
      "default" : " 1"
    }
  },
  "errors" : [ ]
}
```

- 16 discrete speeds, in approximate MHz.
- Values are **strings, right-padded to width 2** (single-digit values
  prefixed with a space). When comparing, always match the exact string.
- `default` is `" 1"` (1 MHz, stock C64).
- Max is **48 MHz** on this U64 Elite unit. There is no 64 MHz option.
- The companion control `Turbo Control` (`"Off"`) in U64 Specific
  Settings gates whether this speed is actually applied.

---

## 3. REU Size Enum + Cartridge

```
GET /v1/configs/C64%20and%20Cartridge%20Settings/REU%20Size

{
  "REU Size" : {
    "current" : "512 KB",
    "values"  : [ "128 KB", "256 KB", "512 KB", "1 MB",
                  "2 MB",   "4 MB",   "8 MB",   "16 MB" ],
    "default" : "2 MB"
  }
}
```

**Enabling the REU**: there are two controls in
`C64 and Cartridge Settings`:

- `RAM Expansion Unit` = `"Enabled"` / `"Disabled"` - master on/off
- `REU Size` = one of the 8 values above - capacity selector

The `Cartridge` item itself is a preset list, not an enum:

```
GET /v1/configs/C64%20and%20Cartridge%20Settings/Cartridge

{ "Cartridge" : { "current" : "", "presets" : [ "" ], "default" : "" } }
```

So cartridge choice is controlled via **`presets`** rather than
**`values`** - a schema difference worth detecting in code. Other
Cartridge-related current values: `Cartridge Preference: "Auto"`,
`Bus Operation Mode: "Quiet"`, `Fast Reset: "Disabled"`.

---

## 4. SID Configuration

Split across **three** categories. On this device: two physical 8580
SIDs detected in sockets plus two UltiSID emulated cores.

### SID Sockets Configuration (physical sockets)
```
SID Socket 1                 = "Enabled"        # "Enabled" | "Disabled"
SID Socket 2                 = "Enabled"
SID Detected Socket 1        = "8580"           # read-only auto-detect
SID Detected Socket 2        = "8580"
SID Socket 1 1K Ohm Resistor = "Off"
SID Socket 2 1K Ohm Resistor = "Off"
SID Socket 1 Capacitors      = "22 nF"
SID Socket 2 Capacitors      = "22 nF"
```

### UltiSID Configuration (emulated cores, 2 slots)
```
UltiSID {1,2} Filter Curve        = "8580 Lo"
UltiSID {1,2} Filter Resonance    = "Low"
UltiSID {1,2} Combined Waveforms  = "6581"
UltiSID {1,2} Digis Level         = "Medium"
```

### SID Addressing (49-entry address enum)
```
GET /v1/configs/SID%20Addressing/SID%20Socket%201%20Address

values: [ "Unmapped",
          "$D400","$D420","$D440","$D460","$D480","$D4A0","$D4C0","$D4E0",
          "$D500","$D520","$D540","$D560","$D580","$D5A0","$D5C0","$D5E0",
          "$D600","$D620","$D640","$D660","$D680","$D6A0","$D6C0","$D6E0",
          "$D700","$D720","$D740","$D760","$D780","$D7A0","$D7C0","$D7E0",
          "$DE00","$DE20","$DE40","$DE60","$DE80","$DEA0","$DEC0","$DEE0",
          "$DF00","$DF20","$DF40","$DF60","$DF80","$DFA0","$DFC0","$DFE0" ]
```

Current live mapping: Socket1=`$D400`, Socket2=`$D420`,
UltiSID1=`$D400`, UltiSID2=`$D400` (collision is intentional -
UltiSID Range Split / Auto Address Mirroring arbitrate). Stereo is
achieved via a different address for each SID. `Paddle Override` is
Enabled, `Ext DualSID Range Split` and `UltiSID Range Split` are Off.

---

## 5. Config Categories (all 19)

| Category | Items | Purpose |
|---|---|---|
| Audio Mixer | 20 | Per-channel Vol / Pan for UltiSIDs, sockets, sampler, drives, tape |
| SID Sockets Configuration | 8 | Physical SID socket enable + electrical tuning |
| UltiSID Configuration | 8 | Emulated SID filter, waveforms, digi level |
| SID Addressing | 8 | Map each SID/UltiSID to a `$D400`..`$DFE0` slot |
| U64 Specific Settings | 20 | System mode, video, CPU speed, LEDs, joystick swap |
| C64 and Cartridge Settings | 19 | ROMs, REU, cartridge preset, bus sharing, command iface |
| Clock Settings | 7 | RTC date/time + drift correction |
| SoftIEC Drive Settings | 3 | Legacy SoftIEC path-emulated drive on bus 11 |
| Printer Settings | 11 | IEC printer emulation (MPS/Epson/IBM), output file |
| Network Settings | 14 | Hostname, services (FTP/Telnet/Web), SNTP, timezone |
| Ethernet Settings | 5 | Wired NIC DHCP/static addressing |
| WiFi settings | 6 | WiFi on/off + DHCP/static addressing |
| Tape Settings | 1 | Datasette playback clock |
| LED Strip Settings | 8 | APA102 addressable LED strip (length, color, SID-reactive) |
| Drive A Settings | 13 | Primary emulated floppy (1541/1571/1581) |
| Drive B Settings | 13 | Secondary emulated floppy |
| Data Streams | 4 | Multicast VIC/audio/debug streaming targets |
| Modem Settings | 15 | ACIA / SwiftLink modem emulation |
| User Interface Settings | 8 | On-screen menu theme + navigation |

---

## 6. Drive Enumeration

### `GET /v1/drives`
Returns a list of 4 entries (named slots): `a`, `b`, `IEC Drive`,
`Printer Emulation`. Each entry is `{ "<slot>" : { ... } }`.

```
slot a:   enabled=True,  bus_id=8,  type="1581", rom="1581.rom"
slot b:   enabled=False, bus_id=9,  type="1581", rom="1581.rom"
slot IEC Drive:        bus_id=11, enabled=False, type="DOS emulation",
                       partitions=[{ id: 0, path: "/USB0/" }],
                       last_error: "73,U64IEC ULTIMATE DOS V1.1,00,00"
slot Printer Emulation: bus_id=4,  enabled=False
```

**Gotcha**: `GET /v1/drives/a:` and `GET /v1/drives/b:` (with or
without URL-encoded colon) both return the **entire drives list** -
they do not filter to a single drive on this firmware.

### Drive A/B Settings (item-level enums)
```
Drive Type:  values = [ "1541", "1571", "1581" ], default = "1541"
Drive Bus ID: integer, min=8, max=11, format="%d", default=8
Drive (enable): "Enabled" | "Disabled"
ROM for {1541,1571,1581} mode:  ROM file preset (string)
Extra RAM, Resets when C64 resets, Freezes in menu,
  GCR Save Align Tracks, Leave Menu on Mount:  Yes/No
D64 Geos Copy Protection: "none" | ...
Disk swap delay: integer
```

Drive B is currently `Disabled` on bus 9; drive A is `Enabled` on bus 8
as a 1581. `SoftIEC Drive Settings` (bus 11) and `Printer Settings`
(bus 4) are mirror entries of the `IEC Drive` / `Printer Emulation`
slots.

---

## 7. Handy curl recipes (read-only)

```bash
# Identity and API version
curl -s http://192.168.1.81/v1/version
curl -s http://192.168.1.81/v1/info

# List all config categories
curl -s http://192.168.1.81/v1/configs

# Dump one category (current values)
curl -s "http://192.168.1.81/v1/configs/U64%20Specific%20Settings"

# Fetch a single item with its enum / min-max
curl -s "http://192.168.1.81/v1/configs/U64%20Specific%20Settings/CPU%20Speed"

# Drives
curl -s http://192.168.1.81/v1/drives
```

If the device has a password set, add `-H "X-Password: <pw>"`.

---

## 8. Gotchas / Schema quirks

- **Plain HTTP only.** No HTTPS on the device. Do not use fetchers that
  auto-upgrade to HTTPS (WebFetch). Use raw curl / urllib.
- **Whitespace in JSON keys and values.** Responses use `" : "` with
  an extra space after the colon, and single-digit CPU Speed values
  are space-padded (`" 1"`, not `"1"`). Python's `json` module parses
  this fine, but **downstream string comparisons must preserve the
  leading space** on CPU Speed values.
- **Enum schema varies per item**. Three distinct response shapes seen:
  - String enum:  `{"current": X, "values": [...], "default": X}`
  - Preset list:  `{"current": X, "presets": [...], "default": X}`
    (e.g. `Cartridge`, probably ROM filename pickers)
  - Integer:      `{"current": N, "min": .., "max": .., "format": "%d", "default": N}`
- **Every response carries an `errors: []` array** at the top level.
  Treat non-empty `errors` as a soft failure.
- **Category key echo**: single-category / single-item responses wrap
  the data in `{ "<Category Name>" : { ... }, "errors": [] }`. You must
  unwrap by category name to reach the data.
- **`/v1/drives/a:` does not filter**; it returns the same payload as
  `/v1/drives`. Parse the list yourself if you need one drive.
- **CPU Speed vs Turbo Control**: the `CPU Speed` enum is the turbo
  rate table; actual turbo-on is gated by `Turbo Control` in the same
  category (`Off` on this device). Max on the Elite is **48 MHz**, not
  64 MHz.
- **Auth**: untested on this unit (no password configured). The API
  is expected to accept an `X-Password` header when one is set.
  All GETs here returned 200 without auth.
- **Dates**: `Clock Settings` reported `2015 October 13` - the device
  clock is unset / SNTP hasn't synced; don't rely on the RTC for
  test timestamps.
