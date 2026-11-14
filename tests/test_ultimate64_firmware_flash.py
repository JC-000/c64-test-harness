"""Firmware flash orchestration (issue #561), offline.

Functional: ``flash_firmware`` is driven against fakes of its building
blocks (the REST client, the Remote Menu driver, the FTP storage helpers,
the category reset) that answer as the device would, and the image parser
is run on images laid out the way ``hex2bin -r`` writes them.

Safety: nothing is sent without this process holding the device's
``DeviceLock``; an image is refused on the host when its hash, extension,
record structure or kind is wrong, before it is uploaded or selected; a
dry run uploads nothing and sends no ENTER; the client is only ever asked
for bodyless calls (zero ``/Temp``). No test here talks to a device.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from c64_test_harness.backends import ultimate64_firmware_flash as ff
from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import Ultimate64Error, Ultimate64TimeoutError
from c64_test_harness.backends.ultimate64_firmware_flash import (
    FirmwareFlashError,
    FirmwareFlashRefusedError,
    FirmwareFlashStepError,
    FirmwareFlashTimeoutError,
    FirmwareFlashVerifyError,
    FirmwareImage,
    FirmwareImageError,
    RomImage,
    flash_firmware,
    inspect_update_image,
)
from c64_test_harness.backends.ultimate64_remote_menu import (
    RemoteMenuLockError,
    RemoteMenuNotFoundError,
    RemoteMenuVerifyError,
    RunUpdateResult,
    WifiChangeResult,
    WifiStatus,
)
from c64_test_harness.backends.ultimate64_storage import (
    STOCK_ROM_NAMES,
    StorageEntry,
    StoragePutResult,
)

HOST = "127.0.0.1"
LOAD = 0x03000000
HASH_OLD = "7b15448a"
HASH_NEW = "c0ffee12"
UID = "5d2518"

PROMPTS = (b"Reformat Flash Disk?", b"About to update. Continue?",
           b"Reset Configuration? (Recommended)")
MARKER = b"bench: forcing module flash"
HASH_FIELD = b"git_commit_hash"


# ---------------------------------------------------------------------------
# Images, laid out as hex2bin -r writes them
# ---------------------------------------------------------------------------

def record(load: int, payload: bytes, start: int, *, pad: bool = True) -> bytes:
    out = struct.pack("<III", load, len(payload), start) + payload
    if pad:
        out += b"\xee" * ((-len(payload)) % 4)
    return out


def body(*strings: bytes, size: int = 997, with_hash_field: bool = True) -> bytes:
    # Every build that reports git_commit_hash carries the JSON key as a string.
    text = b"\x00".join(strings + ((HASH_FIELD,) if with_hash_field else ()))
    return text + bytes((i * 7) & 0xFF for i in range(size - len(text)))


SILENT_BODY = body(MARKER)
PROMPT_BODY = body(*PROMPTS)


def write_image(tmp_path: Path, name: str, content: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def make_image(tmp_path: Path, *, kind: str = "silent", ext: str = ".ue2",
               content: bytes | None = None, device_ext: str | None = None,
               sha: str | None = None, ident: str | None = None,
               git_hash: str | None = HASH_NEW, fw: str | None = None) -> FirmwareImage:
    if content is None:
        content = record(LOAD, SILENT_BODY if kind == "silent" else PROMPT_BODY, LOAD)
    if kind == "official" and ext == ".ue2" and git_hash == HASH_NEW:
        git_hash, fw = None, fw or "1.1.0"      # official C64U: verified on product + version
    p = write_image(tmp_path, f"update{ext}", content)
    extra = {} if fw is None else {"expected_firmware_version": fw}
    return FirmwareImage(
        local_path=p, device_path=f"/SD/fw/update{device_ext or ext}",
        sha256=sha or hashlib.sha256(content).hexdigest(),
        expected_git_commit_hash=git_hash, kind=kind, expected_wifi_ident=ident, **extra)


#: What official C64U 1.1.0 answers on /v1/info: no git_commit_hash
#: (routes.cc:129-145 at 7b628eb1), product from the COMMODORE build.
OFFICIAL_INFO = {"product": "C64 Ultimate", "firmware_version": "1.1.0", "fpga_version": "122",
                 "core_version": "1.49", "hostname": "C64-Ultimate", "unique_id": UID}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(s, 0.001)


class FakeClient:
    """Only the bodyless calls flash_firmware may make. Anything else fails."""

    _ALLOWED = ("get_info", "reboot", "close")

    def __init__(self, infos, events):
        self._infos = list(infos)
        self.events = events

    def get_info(self):
        self.events.append("get_info")
        item = self._infos.pop(0) if len(self._infos) > 1 else self._infos[0]
        if callable(item):          # a hook: runs, then answers
            item = item()
        if isinstance(item, Exception):
            raise item
        return dict(item)

    def reboot(self):
        self.events.append("reboot")

    def close(self):
        self.events.append("close")

    def __getattr__(self, name):
        raise AssertionError(f"flash_firmware used client.{name}; only {self._ALLOWED} allowed")


def wifi(state: str, ident: str | None = None) -> WifiStatus:
    return WifiStatus(state, ident, "10.0.0.9" if state == "link-up" else None, None, "",
                      f"WiFi {state} {ident or ''}")


class FakeMenu:
    def __init__(self, events, *, started=True, wifi_seq=None, ident="ESP32 WiFi Bridge V1.494",
                 run_update_exc=None, disable_exc=None, status_hook=None, enable_exc=None):
        self.events = events
        self.enable_exc = enable_exc
        self.disable_exc = disable_exc
        self.status_hook = status_hook
        self.started = started
        self.wifi_seq = list(wifi_seq or [wifi("link-up")])
        self.ident = ident
        self.run_update_exc = run_update_exc
        self.enabled = True

    def run_update(self, path, *, dry_run=False, confirm_device_powers_off=False,
                   verify_timeout=60.0):
        self.events.append(("run_update", path, dry_run, confirm_device_powers_off))
        if self.run_update_exc:
            raise self.run_update_exc
        enter = not dry_run
        return RunUpdateResult(path, dry_run, ("Run Update",), enter, enter and self.started, ())

    def wifi_status(self):
        self.events.append("wifi_status")
        if self.status_hook is not None:
            self.status_hook()
        if not self.enabled:
            return wifi("disabled", self.ident)
        return self.wifi_seq.pop(0) if len(self.wifi_seq) > 1 else self.wifi_seq[0]

    def set_wifi_enabled(self, enabled, *, dry_run=False, verify_timeout=15.0):
        self.events.append(("set_wifi_enabled", enabled))
        if enabled is False and self.disable_exc is not None:
            raise self.disable_exc
        if enabled is True and self.enable_exc is not None:
            raise self.enable_exc
        before = wifi("link-up") if enabled is False else wifi("disabled", self.ident)
        self.enabled = enabled
        after = wifi("disabled", self.ident) if not enabled else wifi("link-down")
        return WifiChangeResult("Enable" if enabled else "Disable", False, before, after,
                                True, True, ())


@dataclass
class FakeStorage:
    events: list
    files: dict = field(default_factory=dict)

    def list_dir(self, host, path):
        self.events.append(("list_dir", path))
        out = [StorageEntry(p.rsplit("/", 1)[1], "file", len(d))
               for p, d in self.files.items() if p.rsplit("/", 1)[0] == path]
        if not out and not any(p.startswith(path + "/") for p in self.files):
            raise FileNotFoundError(path)
        return out

    def get_file(self, host, path):
        self.events.append(("get_file", path))
        return self.files[path]

    def put_file(self, host, path, data, *, overwrite=False, verify=True, **kw):
        self.events.append(("put_file", path, verify))
        if path in self.files and not overwrite:
            raise FileExistsError(path)
        self.files[path] = bytes(data)
        return StoragePutResult(path, len(data), hashlib.sha256(data).hexdigest(), verify, False)

    def put_rom(self, host, kind, name, data, *, overwrite=False, replace_stock=False,
                check_signature=True, **kw):
        # Models storage_put_rom's refusals: an existing file (matched
        # ASCII-casefolded, kept in its device spelling) needs overwrite, and
        # a stock name also needs replace_stock.
        self.events.append(("put_rom", kind, name))
        path = f"/Flash/roms/{name}"
        hit = [p for p in self.files if p.lower() == path.lower()]
        if hit:
            path = hit[0]
            if name.lower() in STOCK_ROM_NAMES and not replace_stock:
                raise FileExistsError(f"{path} is a firmware stock ROM file; pass "
                                      "overwrite=True, replace_stock=True")
            if not overwrite:
                raise FileExistsError(path)
        self.files[path] = bytes(data)
        return StoragePutResult(path, len(data), hashlib.sha256(data).hexdigest(), True,
                                bool(hit))


@dataclass
class Rig:
    events: list
    storage: FakeStorage
    clock: Clock
    baseline_calls: list
    client_made: list
    menu: FakeMenu | None = None
    infos: list | None = None

    def run(self, image, **kw):
        infos = self.infos
        menu_kw = kw.pop("menu_kw", {})
        self.menu = FakeMenu(self.events, **menu_kw)

        def client_factory(host):
            self.client_made.append(host)
            return FakeClient(infos, self.events)

        kw.setdefault("confirm_device_powers_off", True)
        kw.setdefault("reformat_flash_disk", False)
        return flash_firmware(HOST, image, client_factory=client_factory,
                              menu_factory=lambda h: self.menu, **kw)

    def names(self):
        return [e if isinstance(e, str) else e[0] for e in self.events]


def info(product="Ultimate 64-II", git_hash=HASH_OLD, uid=UID):
    return {"product": product, "firmware_version": "3.15a", "git_commit_hash": git_hash,
            "unique_id": uid}


@pytest.fixture
def held(tmp_path):
    lock = DeviceLock(HOST, lock_dir=tmp_path / "locks", heartbeat_interval=None)
    assert lock.acquire(timeout=5)
    yield lock
    lock.release()


@pytest.fixture
def rig(monkeypatch):
    events: list = []
    storage = FakeStorage(events)
    clock = Clock()
    baseline_calls: list = []
    monkeypatch.setattr(ff, "storage_list_dir", storage.list_dir)
    monkeypatch.setattr(ff, "storage_get_file", storage.get_file)
    monkeypatch.setattr(ff, "storage_put_file", storage.put_file)
    monkeypatch.setattr(ff, "storage_put_rom", storage.put_rom)
    monkeypatch.setattr(ff, "_sleep", clock.sleep)
    monkeypatch.setattr(ff, "_monotonic", clock.monotonic)

    def fake_baseline(client):
        baseline_calls.append(client)
        events.append("apply_factory_baseline")
        return "baseline-report"

    monkeypatch.setattr(ff, "apply_factory_baseline", fake_baseline)
    r = Rig(events, storage, clock, baseline_calls, [])
    down = Ultimate64TimeoutError("connect timed out")
    r.infos = [info(), down, down, info(git_hash=HASH_NEW)]
    return r


# ---------------------------------------------------------------------------
# The image parser
# ---------------------------------------------------------------------------

def test_a_release_shaped_image_parses():
    img = record(LOAD, SILENT_BODY, LOAD + 0x40)
    got = inspect_update_image(img)
    assert got.records == ((LOAD, len(SILENT_BODY), LOAD + 0x40),)
    assert got.start == LOAD + 0x40
    assert got.padding == 3            # 997 bytes, padded to 1000 by hex2bin
    assert got.has_silent_marker and got.prompts_present == ()
    assert got.sha256 == hashlib.sha256(img).hexdigest()


def test_aligned_records_may_follow_each_other_and_end_in_a_start_record():
    img = record(LOAD, b"\x01" * 16, 0) + record(LOAD + 16, b"\x02" * 8, 0) + record(0, b"", LOAD)
    got = inspect_update_image(img)
    assert len(got.records) == 3 and got.start == LOAD


def test_prompt_strings_are_reported():
    got = inspect_update_image(record(LOAD, PROMPT_BODY, LOAD))
    assert got.prompts_present == tuple(p.decode() for p in PROMPTS)
    assert not got.has_silent_marker


@pytest.mark.parametrize("img, why", [
    (b"\x00" * 11, "shorter than one record"),
    (struct.pack("<III", 0x4115ADDE, 0, 0) + b"\x00" * 32, "cfw fingerprint"),
    (record(LOAD, b"\x00" * 64, LOAD)[:-10], "truncated"),
    (record(0x00010000, b"\x00" * 64, 0x00010000), "load window"),
    (record(0x03FFFFF0, b"\x00" * 64, LOAD), "load window"),
    (record(LOAD, b"\x00" * 64, 0), "no start address"),
    (record(LOAD, b"\x00" * 64, 0x20000000), "outside the load window"),
    (record(LOAD, b"\x00" * 64, LOAD) + b"\x00" * 5, "trailing bytes"),
    (record(LOAD, b"\x00" * 63, LOAD) + b"\x00" * 3, "trailing bytes"),
    (record(LOAD, b"\x00" * 5, LOAD) + record(LOAD + 8, b"\x00" * 8, LOAD), "multiple of 4"),
])
def test_images_the_loader_would_misread_are_refused(img, why):
    with pytest.raises(ValueError, match=why):
        inspect_update_image(img)


# ---------------------------------------------------------------------------
# Refusals before any traffic
# ---------------------------------------------------------------------------

def test_refuses_without_the_lock_before_any_traffic(rig, tmp_path):
    with pytest.raises(FirmwareFlashRefusedError, match="DeviceLock") as ei:
        rig.run(make_image(tmp_path))
    assert rig.events == [] and rig.client_made == []
    assert ei.value.record.failed_step == "validate"


def test_a_lock_on_another_device_does_not_count(rig, tmp_path):
    other = DeviceLock("10.99.99.99", lock_dir=tmp_path / "locks", heartbeat_interval=None)
    assert other.acquire(timeout=5)
    try:
        with pytest.raises(FirmwareFlashRefusedError):
            rig.run(make_image(tmp_path))
    finally:
        other.release()
    assert rig.events == []


def test_a_lock_lost_before_the_enter_stops_the_flash(rig, held, tmp_path, monkeypatch):
    put = rig.storage.put_file

    def put_then_lose_the_lock(*a, **kw):
        result = put(*a, **kw)
        held.release()
        return result

    monkeypatch.setattr(ff, "storage_put_file", put_then_lose_the_lock)
    try:
        with pytest.raises(FirmwareFlashRefusedError, match="no longer held") as ei:
            rig.run(make_image(tmp_path))
    finally:
        assert held.acquire(timeout=5)       # the fixture releases it again
    assert ei.value.record.failed_step == "run_update"
    assert not any(isinstance(e, tuple) and e[0] == "run_update" for e in rig.events)


def test_a_hash_mismatch_is_refused_before_any_traffic(rig, held, tmp_path):
    image = make_image(tmp_path, sha="0" * 64)
    with pytest.raises(FirmwareImageError, match="SHA-256") as ei:
        rig.run(image)
    assert rig.events == [] and rig.client_made == []
    assert ei.value.record.failed_step == "validate"


def test_a_bad_record_structure_is_refused_before_any_traffic(rig, held, tmp_path):
    image = make_image(tmp_path, content=record(LOAD, SILENT_BODY, 0))
    with pytest.raises(FirmwareImageError, match="start address"):
        rig.run(image)
    assert rig.events == []


def test_a_real_run_needs_the_power_off_confirmation(rig, held, tmp_path):
    with pytest.raises(FirmwareFlashRefusedError, match="confirm_device_powers_off"):
        rig.run(make_image(tmp_path), confirm_device_powers_off=False)
    assert rig.events == []


def test_local_and_device_extensions_must_agree(rig, held, tmp_path):
    with pytest.raises(FirmwareImageError, match="different extensions"):
        rig.run(make_image(tmp_path, device_ext=".u64"))
    assert rig.events == []


def test_a_ue2_is_refused_on_a_u64e(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite")]
    with pytest.raises(FirmwareImageError, match=r"takes \.u64") as ei:
        rig.run(make_image(tmp_path, kind="dev"))
    assert rig.names() == ["get_info"]           # preflight only: nothing uploaded or run
    assert ei.value.record.failed_step == "preflight"


def test_a_u64_is_refused_on_a_c64u(rig, held, tmp_path):
    with pytest.raises(FirmwareImageError, match=r"takes \.ue2"):
        rig.run(make_image(tmp_path, kind="dev", ext=".u64"))
    assert rig.names() == ["get_info"]


def test_an_unknown_product_is_refused(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate II+")]
    with pytest.raises(FirmwareFlashRefusedError, match="unknown product"):
        rig.run(make_image(tmp_path))
    assert rig.names() == ["get_info"]


# -- image class: prompting, SILENT, or not an updater ---------------------

def test_kind_silent_on_a_prompting_image_is_refused(rig, held, tmp_path):
    content = record(LOAD, PROMPT_BODY, LOAD)
    with pytest.raises(FirmwareImageError, match="is a prompting updater"):
        rig.run(make_image(tmp_path, kind="silent", content=content))
    assert rig.events == []


def test_kind_dev_on_a_silent_image_is_refused(rig, held, tmp_path):
    content = record(LOAD, SILENT_BODY, LOAD)
    with pytest.raises(FirmwareImageError, match="is a silent updater"):
        rig.run(make_image(tmp_path, kind="dev", content=content))
    assert rig.events == []


@pytest.mark.parametrize("kind", ["dev", "official", "silent"])
def test_an_image_with_no_prompts_and_no_marker_is_not_an_updater(rig, held, tmp_path, kind):
    # A RAM application image looks like this: valid records, no updater strings.
    content = record(LOAD, body(b"plain application code"), LOAD)
    with pytest.raises(FirmwareImageError, match="not an updater image"):
        rig.run(make_image(tmp_path, kind=kind, content=content))
    assert rig.events == [] and rig.client_made == []


@pytest.mark.parametrize("missing", range(3))
@pytest.mark.parametrize("marker", [False, True])
def test_an_image_with_only_some_prompts_is_not_an_updater(rig, held, tmp_path, missing, marker):
    strings = [p for i, p in enumerate(PROMPTS) if i != missing] + ([MARKER] if marker else [])
    content = record(LOAD, body(*strings), LOAD)
    with pytest.raises(FirmwareImageError, match="not an updater image"):
        rig.run(make_image(tmp_path, kind="dev", content=content))
    assert rig.events == []


def test_the_marker_with_all_three_prompts_is_a_prompting_image(rig, held, tmp_path):
    # Non-SILENT bench builds print the marker too (update_u64ii.cc:117 at 7b15448a7).
    content = record(LOAD, body(MARKER, *PROMPTS), LOAD)
    rec = rig.run(make_image(tmp_path, kind="dev", content=content))
    assert rec.image_silent is False and rec.reset_configuration_answer == "YES"
    assert rec.identity_verified


def test_the_u64e_has_no_silent_path(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite")]
    content = record(LOAD, SILENT_BODY, LOAD)
    with pytest.raises(FirmwareImageError, match="no SILENT path"):
        rig.run(make_image(tmp_path, kind="silent", ext=".u64", content=content))
    assert rig.names() == ["get_info"]


# Real update images, when the caller points C64_FLASH_IMAGE_DIR at a directory
# holding them under these names (each test skips if its file is absent):
#   ram-app.u64            an application image loaded into RAM (no updater strings)
#   prompting-marked.ue2   a prompting C64U bench updater that also prints the marker
#   silent.ue2             a SILENT C64U bench updater
#   official-1.1.0.ue2     the official C64U 1.1.0 update image
IMAGE_DIR_ENV = "C64_FLASH_IMAGE_DIR"


def _real(name: str, kind: str, **kw) -> FirmwareImage:
    base = os.environ.get(IMAGE_DIR_ENV)
    if not base:
        pytest.skip(f"{IMAGE_DIR_ENV} is not set")
    path = Path(base) / name
    if not path.is_file():
        pytest.skip(f"{name} is not in ${IMAGE_DIR_ENV}")
    return FirmwareImage(path, f"/SD/fw/{path.name}",
                         hashlib.sha256(path.read_bytes()).hexdigest(),
                         kw.pop("git_hash", HASH_NEW), kind, **kw)


def test_a_real_ram_application_image_is_not_an_updater(rig, held):
    with pytest.raises(FirmwareImageError, match="not an updater image"):
        rig.run(_real("ram-app.u64", "dev"))
    assert rig.events == []


def test_a_real_marked_prompting_bench_image_is_a_dev_image(rig, held):
    rec = rig.run(_real("prompting-marked.ue2", "dev"), dry_run=True,
                  confirm_device_powers_off=False)
    assert rec.image.has_silent_marker and len(rec.image.prompts_present) == 3
    assert rec.image_silent is False and rec.reset_configuration_answer == "YES"


def test_a_real_silent_bench_image_is_silent(rig, held):
    rec = rig.run(_real("silent.ue2", "silent"), dry_run=True,
                  confirm_device_powers_off=False)
    assert rec.image_silent is True and rec.image.prompts_present == ()
    assert rec.image.reports_git_commit_hash


def test_the_real_official_image_is_refused_as_dev(rig, held):
    with pytest.raises(FirmwareImageError, match="never report"):
        rig.run(_real("official-1.1.0.ue2", "dev"))
    assert rig.events == []


def test_the_real_official_image_is_accepted_as_official(rig, held):
    rig.infos = [info(), {"product": "C64 Ultimate", "firmware_version": "1.1.0",
                          "unique_id": UID}]
    rec = rig.run(_real("official-1.1.0.ue2", "official", git_hash=None,
                        expected_firmware_version="1.1.0"))
    assert rec.reset_configuration_answer == "NO" and rec.identity_verified
    assert not rec.image.reports_git_commit_hash


# -- an image that cannot report git_commit_hash is not dev or SILENT -------

@pytest.mark.parametrize("kind, strings", [
    ("dev", PROMPTS),
    ("silent", (MARKER,)),
])
def test_an_image_without_the_hash_field_cannot_be_dev_or_silent(rig, held, tmp_path,
                                                                 kind, strings):
    # Official 1.1.0 has the prompts but no git_commit_hash in its /v1/info
    # (routes.cc:129-145 at 7b628eb1), so the string is absent from the image.
    # As kind="dev" the operator would be told YES, which turns REST, FTP and
    # telnet off on 1.1.0, and the hash could never verify anyway.
    content = record(LOAD, body(*strings, with_hash_field=False), LOAD)
    with pytest.raises(FirmwareImageError, match="never report") as ei:
        rig.run(make_image(tmp_path, kind=kind, content=content))
    assert rig.events == [] and rig.client_made == []
    assert ei.value.record.failed_step == "validate"


def test_an_official_image_without_the_hash_field_is_accepted(rig, held, tmp_path):
    rig.infos = [info(), dict(OFFICIAL_INFO)]
    content = record(LOAD, body(*PROMPTS, with_hash_field=False), LOAD)
    rec = rig.run(make_image(tmp_path, kind="official", content=content))
    assert rec.identity_verified and rec.reset_configuration_answer == "NO"


# -- official images: verified on product + firmware_version ---------------

def test_official_needs_an_expected_firmware_version(rig, held, tmp_path):
    image = make_image(tmp_path, kind="official", ext=".u64", git_hash=None, fw=None)
    with pytest.raises(FirmwareImageError, match="expected_firmware_version is required"):
        rig.run(image)
    assert rig.events == []


def test_dev_needs_an_expected_git_hash(rig, held, tmp_path):
    with pytest.raises(FirmwareImageError, match="expected_git_commit_hash is required"):
        rig.run(make_image(tmp_path, kind="dev", git_hash=None))
    assert rig.events == []


def test_official_c64u_refuses_an_expected_git_hash(rig, held, tmp_path):
    image = make_image(tmp_path, kind="official", git_hash="7b628eb1", fw="1.1.0")
    with pytest.raises(FirmwareImageError, match="reports no git_commit_hash"):
        rig.run(image)
    assert rig.names() == ["get_info"]


def test_official_c64u_that_reports_a_hash_is_not_verified(rig, held, tmp_path):
    rig.infos = [info(), dict(OFFICIAL_INFO, git_commit_hash="7b628eb1")]
    with pytest.raises(FirmwareFlashVerifyError, match="official 1.1.0 reports none"):
        rig.run(make_image(tmp_path, kind="official"))
    assert "apply_factory_baseline" not in rig.events


def test_official_c64u_needs_the_commodore_product_name(rig, held, tmp_path):
    rig.infos = [info(), dict(OFFICIAL_INFO, product="Ultimate 64-II")]
    with pytest.raises(FirmwareFlashVerifyError, match="reports 'C64 Ultimate'"):
        rig.run(make_image(tmp_path, kind="official"))


def test_official_c64u_with_the_wrong_version_is_not_verified(rig, held, tmp_path):
    rig.infos = [info(), dict(OFFICIAL_INFO, firmware_version="1.0.9")]
    with pytest.raises(FirmwareFlashVerifyError, match="firmware_version is '1.0.9'"):
        rig.run(make_image(tmp_path, kind="official"))


def test_official_u64e_answers_yes_and_is_verified_on_version_and_hash(
        rig, held, tmp_path, caplog):
    rig.infos = [info(product="Ultimate 64 Elite", git_hash="3a1ff9ff"),
                 info(product="Ultimate 64 Elite", git_hash="ee005041") | {"firmware_version": "3.15"}]
    image = make_image(tmp_path, kind="official", ext=".u64", git_hash="ee005041", fw="3.15")
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        rec = rig.run(image)
    assert rec.reset_configuration_answer == "YES"
    assert "'Reset Configuration? (Recommended)': YES" in caplog.text
    assert rec.identity_verified and "apply_factory_baseline" not in rig.events


def test_official_u64e_with_the_wrong_hash_is_not_verified(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite", git_hash="3a1ff9ff"),
                 info(product="Ultimate 64 Elite", git_hash="3a1ff9ff") | {"firmware_version": "3.15"}]
    image = make_image(tmp_path, kind="official", ext=".u64", git_hash="ee005041", fw="3.15")
    with pytest.raises(FirmwareFlashVerifyError, match="git_commit_hash"):
        rig.run(image)


def test_official_u64e_without_a_hash_is_verified_on_the_version(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite"),
                 info(product="Ultimate 64 Elite", git_hash="whatever") | {"firmware_version": "3.15"}]
    rec = rig.run(make_image(tmp_path, kind="official", ext=".u64", git_hash=None, fw="3.15"))
    assert rec.identity_verified


# -- same identity before and after -----------------------------------------

def test_a_device_already_on_the_target_is_flashed_but_not_verified(rig, held, tmp_path, caplog):
    rig.infos = [info(git_hash=HASH_NEW), info(git_hash=HASH_NEW)]
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        rec = rig.run(make_image(tmp_path, kind="dev"))
    assert rec.flash_distinguishable is False and rec.identity_verified is False
    assert "cannot tell a flash from none" in caplog.text
    assert ("run_update", "/SD/fw/update.ue2", False, True) in rig.events   # not refused
    assert rec.steps[-1] == "verify"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_a_dry_run_navigates_an_image_already_there_and_writes_nothing(rig, held, tmp_path):
    image = make_image(tmp_path)
    rig.storage.files[image.device_path] = image.local_path.read_bytes()
    rec = rig.run(image, dry_run=True, confirm_device_powers_off=False,
                  roms=[_rom(tmp_path)])
    assert ("run_update", image.device_path, True, False) in rig.events
    assert not any(e[0] in ("put_file", "put_rom") for e in rig.events if isinstance(e, tuple))
    assert "reboot" not in rig.events and "apply_factory_baseline" not in rig.events
    assert rig.names().count("get_info") == 1
    assert rec.dry_run and rec.upload_reused and rec.run_update.enter_sent is False
    assert rec.steps == ["validate", "preflight", "upload", "run_update"]


def test_a_dry_run_skips_navigation_when_the_image_is_not_uploaded(rig, held, tmp_path, caplog):
    rec = rig.run(make_image(tmp_path), dry_run=True, confirm_device_powers_off=False)
    assert not any(isinstance(e, tuple) and e[0] in ("put_file", "run_update")
                   for e in rig.events)
    assert any("navigation" in w for w in rec.warnings)


# ---------------------------------------------------------------------------
# A real run, against fakes
# ---------------------------------------------------------------------------

def test_a_silent_c64u_flash_uploads_runs_waits_and_verifies(rig, held, tmp_path, caplog):
    image = make_image(tmp_path)
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        rec = rig.run(image)
    assert ("put_file", image.device_path, True) in rig.events
    assert ("run_update", image.device_path, False, True) in rig.events
    names = rig.names()
    assert names.index("put_file") < names.index("run_update")
    assert names[names.index("run_update"):].count("get_info") == 3   # down, down, up
    assert rec.identity_verified and rec.flash_distinguishable
    assert rec.git_commit_hash_after == HASH_NEW
    assert rec.steps == ["validate", "preflight", "upload", "run_update", "wait", "verify"]
    assert rec.image_silent is True and rec.unique_id_after == UID
    assert "apply_factory_baseline" not in rig.events
    assert "JTAG recovery kit not checked" in caplog.text
    assert "asks nothing" in caplog.text


def test_an_image_already_on_the_device_is_reused(rig, held, tmp_path):
    image = make_image(tmp_path)
    rig.storage.files["/SD/fw/UPDATE.UE2"] = image.local_path.read_bytes()
    rec = rig.run(image)
    assert not any(isinstance(e, tuple) and e[0] == "put_file" for e in rig.events)
    assert rec.upload_reused and rec.device_path_used == "/SD/fw/UPDATE.UE2"
    assert ("run_update", "/SD/fw/UPDATE.UE2", False, True) in rig.events


def test_a_different_file_at_the_target_is_not_overwritten(rig, held, tmp_path):
    image = make_image(tmp_path)
    other = bytearray(image.local_path.read_bytes())
    other[100] ^= 0xFF
    rig.storage.files[image.device_path] = bytes(other)
    with pytest.raises(FirmwareFlashStepError, match="other content") as ei:
        rig.run(image)
    assert ei.value.record.failed_step == "upload"
    assert isinstance(ei.value.__cause__, FileExistsError)
    assert not any(isinstance(e, tuple) and e[0] in ("put_file", "run_update")
                   for e in rig.events)


def test_the_wrong_hash_after_the_flash_is_a_verify_error(rig, held, tmp_path):
    rig.infos = [info(), info(git_hash=HASH_OLD)]
    with pytest.raises(FirmwareFlashVerifyError, match="git_commit_hash") as ei:
        rig.run(make_image(tmp_path))
    rec = ei.value.record
    assert rec.failed_step == "verify" and not rec.identity_verified
    assert rec.run_update is not None and rec.run_update.started
    assert "verify" not in rec.steps and "wait" in rec.steps


def test_a_different_device_answering_is_a_verify_error(rig, held, tmp_path):
    rig.infos = [info(), info(git_hash=HASH_NEW, uid="601a96")]
    with pytest.raises(FirmwareFlashVerifyError, match="different device"):
        rig.run(make_image(tmp_path))


def test_rest_not_returning_is_a_timeout_with_the_record(rig, held, tmp_path):
    rig.infos = [info(), Ultimate64TimeoutError("down")]
    with pytest.raises(FirmwareFlashTimeoutError, match="did not answer within 30") as ei:
        rig.run(make_image(tmp_path), wait_up_s=30, poll_interval_s=5)
    assert ei.value.record.failed_step == "wait"
    assert isinstance(ei.value, Ultimate64Error)


def test_an_updater_that_did_not_start_stops_the_flash(rig, held, tmp_path):
    with pytest.raises(FirmwareFlashVerifyError, match="without the updater starting"):
        rig.run(make_image(tmp_path), menu_kw={"started": False})
    assert rig.names().count("get_info") == 1


def test_a_building_block_error_carries_the_step_and_the_cause(rig, held, tmp_path):
    exc = RemoteMenuNotFoundError("no such entry")
    with pytest.raises(FirmwareFlashStepError, match="run_update") as ei:
        rig.run(make_image(tmp_path), menu_kw={"run_update_exc": exc})
    assert ei.value.__cause__ is exc
    assert ei.value.record.steps == ["validate", "preflight", "upload"]
    assert "No ENTER was sent" in str(ei.value)


def test_an_unclear_run_update_after_the_enter_says_the_updater_may_be_running(
        rig, held, tmp_path):
    exc = RemoteMenuVerifyError("still answers", enter_sent=True)
    with pytest.raises(FirmwareFlashStepError, match="ENTER WAS SENT") as ei:
        rig.run(make_image(tmp_path), menu_kw={"run_update_exc": exc})
    assert ei.value.record.failed_step == "run_update"
    assert rig.names().count("get_info") == 1          # no wait, no retry
    assert [e for e in rig.events if isinstance(e, tuple) and e[0] == "run_update"] == [
        ("run_update", "/SD/fw/update.ue2", False, True)]


# -- Reset Configuration answers ------------------------------------------

def test_official_c64u_answers_no_and_resets_the_categories_itself(rig, held, tmp_path, caplog):
    rig.infos = [info(), Ultimate64TimeoutError("down"), dict(OFFICIAL_INFO)]
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        rec = rig.run(make_image(tmp_path, kind="official"))
    assert rec.identity_verified and rec.git_commit_hash_after is None
    assert rec.product_after == "C64 Ultimate" and rec.firmware_version_after == "1.1.0"
    assert rec.reset_configuration_answer == "NO"
    assert "'Reset Configuration? (Recommended)': NO" in caplog.text
    assert rig.events.count("apply_factory_baseline") == 1
    assert rec.baseline == "baseline-report" and rec.steps[-1] == "reset_categories"


def test_a_dev_image_answers_yes_and_leaves_the_categories(rig, held, tmp_path, caplog):
    rig.infos[0] = info(product="Ultimate 64 Elite")
    rig.infos[-1] = info(product="Ultimate 64 Elite", git_hash=HASH_NEW)
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        rec = rig.run(make_image(tmp_path, kind="dev", ext=".u64"))
    assert rec.reset_configuration_answer == "YES"
    assert "'Reset Configuration? (Recommended)': YES" in caplog.text
    assert "press the power button once" in caplog.text
    assert "apply_factory_baseline" not in rig.events


# -- WiFi IDENT --------------------------------------------------------------

def test_wifi_ident_is_read_disabled_then_enabled_and_waits_for_link_up(rig, held, tmp_path):
    image = make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494")
    rec = rig.run(image, menu_kw={"wifi_seq": [wifi("link-up"), wifi("link-down"),
                                               wifi("link-down"), wifi("link-up")]})
    toggles = [e for e in rig.events if isinstance(e, tuple) and e[0] == "set_wifi_enabled"]
    assert toggles == [("set_wifi_enabled", False), ("set_wifi_enabled", True)]
    assert rec.wifi_ident_verified and rec.wifi_after.state == "link-up"
    assert rec.steps[-1] == "wifi_ident"


def test_a_wrong_wifi_ident_is_reported_after_wifi_is_back_on(rig, held, tmp_path):
    image = make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494")
    with pytest.raises(FirmwareFlashVerifyError, match="IDENT"):
        rig.run(image, menu_kw={"ident": "ESP32 WiFi Bridge V0.903 bench"})
    assert ("set_wifi_enabled", True) in rig.events


def test_wifi_that_never_reaches_link_up_is_a_timeout(rig, held, tmp_path):
    image = make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494")
    with pytest.raises(FirmwareFlashTimeoutError, match="Link Up"):
        rig.run(image, wifi_link_timeout_s=20,
                menu_kw={"wifi_seq": [wifi("link-up"), wifi("link-down")]})


def test_no_expected_ident_means_no_wifi_toggle(rig, held, tmp_path):
    rig.run(make_image(tmp_path))
    assert not any(isinstance(e, tuple) and e[0] == "set_wifi_enabled" for e in rig.events)
    assert "wifi_status" not in rig.events


# -- ROMs ------------------------------------------------------------------------

KERNAL = bytes.fromhex("8556200f") + bytes(8188)


def _rom(tmp_path: Path, sha: str | None = None) -> RomImage:
    p = write_image(tmp_path, "kernal.bin", KERNAL)
    return RomImage("kernal", "kernal.bin", p, sha or hashlib.sha256(KERNAL).hexdigest(),
                    overwrite=True, replace_stock=True)


def test_c64u_roms_are_installed_then_rebooted_and_verified_again(rig, held, tmp_path):
    rec = rig.run(make_image(tmp_path), roms=[_rom(tmp_path)])
    names = rig.names()
    assert names.index("put_rom") < names.index("reboot")
    assert names[names.index("reboot") + 1] == "get_info"
    assert rec.rebooted and rec.git_commit_hash_after_reboot == HASH_NEW
    assert rec.steps[-1] == "roms"


def test_a_rom_with_the_wrong_hash_is_refused_before_any_traffic(rig, held, tmp_path):
    with pytest.raises(FirmwareImageError, match="SHA-256"):
        rig.run(make_image(tmp_path), roms=[_rom(tmp_path, sha="1" * 64)])
    assert rig.events == []


def test_roms_on_a_u64e_are_refused_before_the_upload(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite")]
    with pytest.raises(FirmwareFlashRefusedError, match="C64U-only"):
        rig.run(make_image(tmp_path, kind="dev", ext=".u64"), roms=[_rom(tmp_path)])
    assert rig.names() == ["get_info"]


def test_errors_are_ultimate64_errors():
    for cls in (FirmwareFlashError, FirmwareImageError, FirmwareFlashRefusedError,
                FirmwareFlashTimeoutError, FirmwareFlashVerifyError, FirmwareFlashStepError):
        assert issubclass(cls, Ultimate64Error)


# ---------------------------------------------------------------------------
# Review round 1 (#561)
# ---------------------------------------------------------------------------

def test_an_os_error_from_run_update_is_treated_as_sent(rig, held, tmp_path):
    # A socket error from sendall/select carries no enter_sent: unknown, not "no".
    exc = ConnectionResetError(54, "Connection reset by peer")
    with pytest.raises(FirmwareFlashStepError, match="UNKNOWN: treat it as sent") as ei:
        rig.run(make_image(tmp_path), menu_kw={"run_update_exc": exc})
    assert "No ENTER was sent" not in str(ei.value)
    assert rig.names().count("get_info") == 1


def test_a_lock_lost_during_the_wait_stops_before_verify(rig, held, tmp_path):
    def lose():
        held.release()
        return info(git_hash=HASH_NEW)

    rig.infos = [info(), Ultimate64TimeoutError("down"), lose]
    try:
        with pytest.raises(FirmwareFlashRefusedError, match="no longer held") as ei:
            rig.run(make_image(tmp_path), roms=[_rom(tmp_path)])
    finally:
        assert held.acquire(timeout=5)
    assert ei.value.record.failed_step == "verify"
    assert "reboot" not in rig.events


def test_a_lock_lost_before_reset_categories_stops_it(rig, held, tmp_path, monkeypatch):
    real = ff._identity_problems

    def problems_then_lose(info_, exp):
        out = real(info_, exp)
        if info_.get("product") == "C64 Ultimate":     # the read after the flash
            held.release()
        return out

    monkeypatch.setattr(ff, "_identity_problems", problems_then_lose)
    rig.infos = [info(), dict(OFFICIAL_INFO)]
    try:
        with pytest.raises(FirmwareFlashRefusedError, match="no longer held") as ei:
            rig.run(make_image(tmp_path, kind="official"))
    finally:
        assert held.acquire(timeout=5)
    assert ei.value.record.failed_step == "reset_categories"
    assert "apply_factory_baseline" not in rig.events


def test_a_lock_lost_before_the_wifi_step_stops_it(rig, held, tmp_path, monkeypatch):
    def baseline_then_lose(client):
        rig.events.append("apply_factory_baseline")
        held.release()
        return "baseline-report"

    monkeypatch.setattr(ff, "apply_factory_baseline", baseline_then_lose)
    rig.infos = [info(), dict(OFFICIAL_INFO)]
    try:
        with pytest.raises(FirmwareFlashRefusedError, match="no longer held") as ei:
            rig.run(make_image(tmp_path, kind="official", ident="ESP32 WiFi Bridge V1.494"))
    finally:
        assert held.acquire(timeout=5)
    assert ei.value.record.failed_step == "wifi_ident"
    assert not any(isinstance(e, tuple) and e[0] == "set_wifi_enabled" for e in rig.events)


def test_a_lock_lost_before_the_rom_step_stops_it(rig, held, tmp_path):
    calls = {"n": 0}

    def lose_at_link_up():
        calls["n"] += 1
        if calls["n"] == 4:          # settle, then link-down, link-down, link-up
            held.release()

    try:
        with pytest.raises(FirmwareFlashRefusedError, match="no longer held") as ei:
            rig.run(make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494"),
                    roms=[_rom(tmp_path)],
                    menu_kw={"status_hook": lose_at_link_up,
                             "wifi_seq": [wifi("link-up"), wifi("link-down"),
                                          wifi("link-down"), wifi("link-up")]})
    finally:
        assert held.acquire(timeout=5)
    assert ei.value.record.failed_step == "roms"
    assert "put_rom" not in rig.names() and "reboot" not in rig.events


def test_a_disable_whose_enter_was_not_sent_is_not_followed_by_enable(rig, held, tmp_path):
    exc = RemoteMenuVerifyError("refused before the ENTER", enter_sent=False)
    with pytest.raises(FirmwareFlashStepError):
        rig.run(make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494"),
                menu_kw={"disable_exc": exc})
    toggles = [e for e in rig.events if isinstance(e, tuple) and e[0] == "set_wifi_enabled"]
    assert toggles == [("set_wifi_enabled", False)]


def test_a_disable_that_failed_unclearly_is_followed_by_enable(rig, held, tmp_path):
    exc = ConnectionResetError(54, "reset")          # no enter_sent: may have been sent
    with pytest.raises(FirmwareFlashStepError):
        rig.run(make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494"),
                menu_kw={"disable_exc": exc})
    toggles = [e for e in rig.events if isinstance(e, tuple) and e[0] == "set_wifi_enabled"]
    assert toggles == [("set_wifi_enabled", False), ("set_wifi_enabled", True)]


def test_a_full_length_expected_hash_matches_the_short_reported_one(rig, held, tmp_path):
    full = HASH_NEW + "0" * 32
    rec = rig.run(make_image(tmp_path, git_hash=full))
    assert rec.identity_verified and rec.git_commit_hash_after == HASH_NEW


def test_a_reported_hash_shorter_than_seven_digits_is_refused(rig, held, tmp_path):
    rig.infos = [info(), info(git_hash=HASH_NEW[:3])]
    with pytest.raises(FirmwareFlashVerifyError, match="git_commit_hash"):
        rig.run(make_image(tmp_path))


def test_the_rest_wait_gives_up_at_its_deadline(rig, held, tmp_path):
    rig.infos = [info(), Ultimate64TimeoutError("down")]
    with pytest.raises(FirmwareFlashTimeoutError):
        rig.run(make_image(tmp_path), wait_up_s=120, poll_interval_s=5)
    elapsed = rig.clock.t - 1000.0      # nothing before the wait sleeps
    assert 120 <= elapsed <= 125, elapsed


def test_c64_ultimate_mk1_is_a_u64e_family_product(rig, held, tmp_path):
    rig.infos = [info(product="C64 Ultimate (MK1)"),
                 info(product="C64 Ultimate (MK1)", git_hash=HASH_NEW)]
    rec = rig.run(make_image(tmp_path, kind="dev", ext=".u64"))
    assert rec.family == "u64e" and rec.identity_verified


def test_an_upload_that_does_not_verify_stops_the_flash(rig, held, tmp_path, monkeypatch):
    def unverified(host, path, data, **kw):
        rig.events.append(("put_file", path, kw.get("verify")))
        return StoragePutResult(path, len(data), hashlib.sha256(data).hexdigest(), False, False)

    monkeypatch.setattr(ff, "storage_put_file", unverified)
    with pytest.raises(FirmwareFlashVerifyError, match="not verified") as ei:
        rig.run(make_image(tmp_path))
    assert ei.value.record.failed_step == "upload"
    assert "run_update" not in rig.names()


def test_another_firmware_after_the_rom_reboot_is_a_verify_error(rig, held, tmp_path):
    rig.infos = [info(), info(git_hash=HASH_NEW), info(git_hash=HASH_OLD)]
    with pytest.raises(FirmwareFlashVerifyError, match="git_commit_hash") as ei:
        rig.run(make_image(tmp_path), roms=[_rom(tmp_path)])
    assert ei.value.record.failed_step == "roms" and ei.value.record.rebooted


def test_a_product_of_the_other_family_after_the_flash_is_a_verify_error(rig, held, tmp_path):
    rig.infos = [info(), info(product="Ultimate 64 Elite", git_hash=HASH_NEW)]
    with pytest.raises(FirmwareFlashVerifyError, match="not a c64u product"):
        rig.run(make_image(tmp_path))


def test_an_enable_that_fails_after_a_clean_read_is_a_step_error(rig, held, tmp_path, caplog):
    # Nothing failed before the Enable, so its own failure is the error; it must
    # not be swallowed and turned into a Link Up timeout.
    exc = ConnectionResetError(54, "reset")
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        with pytest.raises(FirmwareFlashStepError) as ei:
            rig.run(make_image(tmp_path, ident="ESP32 WiFi Bridge V1.494"),
                    menu_kw={"enable_exc": exc})
    assert ei.value.record.failed_step == "wifi_ident"
    assert ei.value.__cause__ is exc
    assert "left Disabled" in caplog.text


# ---------------------------------------------------------------------------
# Peer requests (1541ultimate lane, 2026-11-13)
# ---------------------------------------------------------------------------

# -- read_wifi_ident: public, outside a flash ------------------------------

IDENT = "ESP32 WiFi Bridge V1.494"


def _read_ident(events, **menu_kw):
    menu = FakeMenu(events, **menu_kw)
    return ff.read_wifi_ident(HOST, link_timeout_s=20, poll_interval_s=5,
                              menu_factory=lambda h: menu)


def _toggles(events):
    return [e for e in events if isinstance(e, tuple) and e[0] == "set_wifi_enabled"]


def test_read_wifi_ident_refuses_without_the_lock_before_any_traffic(rig):
    made = []

    def factory(h):
        made.append(h)
        return FakeMenu(rig.events)

    with pytest.raises(RemoteMenuLockError):
        ff.read_wifi_ident(HOST, menu_factory=factory)
    assert rig.events == [] and made == []


def test_read_wifi_ident_cycles_wifi_and_leaves_it_link_up(rig, held):
    got = _read_ident(rig.events, wifi_seq=[wifi("link-up"), wifi("link-down"), wifi("link-up")])
    assert got.ident == IDENT
    assert got.before.state == "link-up" and got.after.state == "link-up"
    assert got.toggled is True
    assert _toggles(rig.events) == [("set_wifi_enabled", False), ("set_wifi_enabled", True)]


def test_read_wifi_ident_on_a_disabled_row_reads_it_and_toggles_nothing(rig, held):
    menu = FakeMenu(rig.events)
    menu.enabled = False
    got = ff.read_wifi_ident(HOST, menu_factory=lambda h: menu)
    assert got.ident == IDENT and got.toggled is False
    assert got.before.state == "disabled" and got.after.state == "disabled"
    assert _toggles(rig.events) == []


def test_read_wifi_ident_puts_wifi_back_when_the_disable_failed_unclearly(rig, held):
    exc = ConnectionResetError(54, "reset")
    with pytest.raises(ConnectionResetError):
        _read_ident(rig.events, disable_exc=exc)
    assert _toggles(rig.events) == [("set_wifi_enabled", False), ("set_wifi_enabled", True)]


def test_read_wifi_ident_does_not_enable_when_the_disable_was_never_sent(rig, held):
    exc = RemoteMenuVerifyError("refused before the ENTER", enter_sent=False)
    with pytest.raises(RemoteMenuVerifyError):
        _read_ident(rig.events, disable_exc=exc)
    assert _toggles(rig.events) == [("set_wifi_enabled", False)]


def test_read_wifi_ident_link_up_timeout_carries_the_reading(rig, held):
    with pytest.raises(ff.WifiLinkTimeoutError) as ei:
        _read_ident(rig.events, wifi_seq=[wifi("link-up"), wifi("link-down")])
    assert ei.value.reading.ident == IDENT
    assert ei.value.reading.after.state == "link-down"
    assert ("set_wifi_enabled", True) in rig.events


def test_read_wifi_ident_refuses_a_row_with_no_ident_to_read(rig, held):
    with pytest.raises(ff.WifiIdentUnreadableError):
        _read_ident(rig.events, wifi_seq=[wifi("busy")])
    assert _toggles(rig.events) == []


# -- ROMs already present with the expected content -------------------------

def _stock_kernal(tmp_path: Path, **kw) -> RomImage:
    p = write_image(tmp_path, "kernal.bin", KERNAL)
    return RomImage("kernal", "kernal.bin", p, hashlib.sha256(KERNAL).hexdigest(), **kw)


def test_a_rom_already_present_unchanged_is_not_written(rig, held, tmp_path):
    rig.storage.files["/Flash/roms/KERNAL.BIN"] = KERNAL
    rec = rig.run(make_image(tmp_path), roms=[_stock_kernal(tmp_path)])
    assert "put_rom" not in rig.names()
    assert rec.roms == [] and rec.roms_unchanged == ["/Flash/roms/KERNAL.BIN"]
    assert "reboot" not in rig.events and rec.rebooted is False
    assert rec.steps[-1] == "roms"


def test_a_stock_rom_with_other_content_still_needs_replace_stock(rig, held, tmp_path):
    other = bytearray(KERNAL)
    other[100] ^= 0xFF
    rig.storage.files["/Flash/roms/kernal.bin"] = bytes(other)
    with pytest.raises(FirmwareFlashStepError, match="stock") as ei:
        rig.run(make_image(tmp_path), roms=[_stock_kernal(tmp_path)])
    assert ei.value.record.failed_step == "roms"
    assert ("get_file", "/Flash/roms/kernal.bin") in rig.events   # it was compared
    assert rig.storage.files["/Flash/roms/kernal.bin"] == bytes(other)
    assert "reboot" not in rig.events


def test_a_changed_rom_is_written_and_the_c64_rebooted(rig, held, tmp_path):
    basic = b"\x00" * 4 + b"CBMBASIC" + bytes(8180)
    rig.storage.files["/Flash/roms/kernal.bin"] = KERNAL
    rig.storage.files["/Flash/roms/basic.bin"] = bytes(8192)
    bp = write_image(tmp_path, "basic.bin", basic)
    roms = [_stock_kernal(tmp_path),
            RomImage("basic", "basic.bin", bp, hashlib.sha256(basic).hexdigest(),
                     overwrite=True, replace_stock=True)]
    rec = rig.run(make_image(tmp_path), roms=roms)
    assert [e for e in rig.events if isinstance(e, tuple) and e[0] == "put_rom"] == [
        ("put_rom", "basic", "basic.bin")]
    assert rec.roms_unchanged == ["/Flash/roms/kernal.bin"]
    assert [r.path for r in rec.roms] == ["/Flash/roms/basic.bin"]
    assert rec.rebooted and rig.storage.files["/Flash/roms/basic.bin"] == basic


def test_a_rom_of_another_size_is_not_read_back_before_the_write(rig, held, tmp_path):
    rig.storage.files["/Flash/roms/kernal.bin"] = bytes(4096)
    with pytest.raises(FirmwareFlashStepError, match="stock"):
        rig.run(make_image(tmp_path), roms=[_stock_kernal(tmp_path)])
    assert not any(isinstance(e, tuple) and e[0] == "get_file" for e in rig.events)


# -- "Reformat Flash Disk?": the caller chooses ----------------------------

def test_omitting_the_reformat_answer_fails_before_any_traffic(rig, held, tmp_path):
    with pytest.raises(TypeError, match="reformat_flash_disk"):
        flash_firmware(HOST, make_image(tmp_path), confirm_device_powers_off=True,
                       client_factory=lambda h: rig.client_made.append(h),
                       menu_factory=lambda h: rig.events.append("menu"))
    assert rig.events == [] and rig.client_made == []


@pytest.mark.parametrize("bad", [None, "YES", 1, 0])
def test_a_reformat_answer_that_is_not_a_bool_is_refused(rig, held, tmp_path, bad):
    with pytest.raises(FirmwareFlashRefusedError, match="reformat_flash_disk") as ei:
        rig.run(make_image(tmp_path, kind="dev"), reformat_flash_disk=bad)
    assert rig.events == [] and rig.client_made == []
    assert ei.value.record.failed_step == "validate"


def _reformat_line(text: str) -> str:
    return next(line for line in text.splitlines() if "Reformat Flash Disk?" in line)


@pytest.mark.parametrize("answer, word", [(False, "NO"), (True, "YES")])
def test_the_reformat_answer_reaches_the_operator_and_the_record(rig, held, tmp_path,
                                                                answer, word):
    rec = rig.run(make_image(tmp_path, kind="dev"), reformat_flash_disk=answer)
    assert rec.reformat_flash_disk is answer
    assert _reformat_line(rec.operator_instruction).rstrip().endswith(word)


def test_operator_instruction_needs_the_reformat_answer():
    with pytest.raises(TypeError):
        ff.operator_instruction("c64u", "dev")
    no = ff.operator_instruction("c64u", "dev", reformat_flash_disk=False)
    yes = ff.operator_instruction("c64u", "dev", reformat_flash_disk=True)
    assert _reformat_line(no).rstrip().endswith("NO")
    assert _reformat_line(yes).rstrip().endswith("YES")


def test_read_wifi_ident_found_at_link_down_is_restored_at_link_down(rig, held):
    # A module with no access point to join: it starts and stays Link Down.
    got = _read_ident(rig.events, wifi_seq=[wifi("link-down")])
    assert got.ident == IDENT and got.toggled is True
    assert got.before.state == "link-down" and got.after.state == "link-down"
    assert _toggles(rig.events) == [("set_wifi_enabled", False), ("set_wifi_enabled", True)]


def test_the_flash_wifi_step_still_needs_link_up_from_link_down(rig, held, tmp_path):
    image = make_image(tmp_path, ident=IDENT)
    with pytest.raises(FirmwareFlashTimeoutError, match="Link Up") as ei:
        rig.run(image, wifi_link_timeout_s=20, menu_kw={"wifi_seq": [wifi("link-down")]})
    assert ei.value.record.failed_step == "wifi_ident"
