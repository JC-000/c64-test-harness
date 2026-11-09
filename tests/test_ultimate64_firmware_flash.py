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
    RemoteMenuNotFoundError,
    RemoteMenuVerifyError,
    RunUpdateResult,
    WifiChangeResult,
    WifiStatus,
)
from c64_test_harness.backends.ultimate64_storage import StorageEntry, StoragePutResult

HOST = "127.0.0.1"
LOAD = 0x03000000
HASH_OLD = "7b15448a"
HASH_NEW = "c0ffee12"
UID = "5d2518"

PROMPTS = (b"Reformat Flash Disk?", b"About to update. Continue?",
           b"Reset Configuration? (Recommended)")
MARKER = b"bench: forcing module flash"


# ---------------------------------------------------------------------------
# Images, laid out as hex2bin -r writes them
# ---------------------------------------------------------------------------

def record(load: int, payload: bytes, start: int, *, pad: bool = True) -> bytes:
    out = struct.pack("<III", load, len(payload), start) + payload
    if pad:
        out += b"\xee" * ((-len(payload)) % 4)
    return out


def body(*strings: bytes, size: int = 997) -> bytes:
    text = b"\x00".join(strings)
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
               git_hash: str = HASH_NEW) -> FirmwareImage:
    if content is None:
        content = record(LOAD, SILENT_BODY if kind == "silent" else PROMPT_BODY, LOAD)
    p = write_image(tmp_path, f"update{ext}", content)
    return FirmwareImage(
        local_path=p, device_path=f"/SD/fw/update{device_ext or ext}",
        sha256=sha or hashlib.sha256(content).hexdigest(),
        expected_git_commit_hash=git_hash, kind=kind, expected_wifi_ident=ident)


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
                 run_update_exc=None):
        self.events = events
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
        if not self.enabled:
            return wifi("disabled", self.ident)
        return self.wifi_seq.pop(0) if len(self.wifi_seq) > 1 else self.wifi_seq[0]

    def set_wifi_enabled(self, enabled, *, dry_run=False, verify_timeout=15.0):
        self.events.append(("set_wifi_enabled", enabled))
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
        self.events.append(("put_rom", kind, name))
        path = f"/Flash/roms/{name}"
        return StoragePutResult(path, len(data), hashlib.sha256(data).hexdigest(), True, False)


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


# -- the SILENT gate -------------------------------------------------------

def test_kind_silent_on_a_prompting_image_is_refused(rig, held, tmp_path):
    content = record(LOAD, PROMPT_BODY, LOAD)
    with pytest.raises(FirmwareImageError, match="is not a SILENT"):
        rig.run(make_image(tmp_path, kind="silent", content=content))
    assert rig.names() == ["get_info"]


def test_kind_dev_on_a_silent_image_is_refused(rig, held, tmp_path):
    content = record(LOAD, SILENT_BODY, LOAD)
    with pytest.raises(FirmwareImageError, match="is a SILENT"):
        rig.run(make_image(tmp_path, kind="dev", content=content))
    assert rig.names() == ["get_info"]


@pytest.mark.parametrize("prompt", PROMPTS)
def test_one_prompt_string_makes_a_marked_image_unrecognised(rig, held, tmp_path, prompt):
    content = record(LOAD, body(MARKER, prompt), LOAD)
    with pytest.raises(FirmwareImageError, match="SILENT bench marker and prompt"):
        rig.run(make_image(tmp_path, kind="silent", content=content))
    assert rig.names() == ["get_info"]


def test_the_u64e_has_no_silent_path(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite")]
    content = record(LOAD, SILENT_BODY, LOAD)
    with pytest.raises(FirmwareImageError, match="no SILENT path"):
        rig.run(make_image(tmp_path, kind="silent", ext=".u64", content=content))
    assert rig.names() == ["get_info"]


def test_official_needs_the_reset_prompt_in_the_image(rig, held, tmp_path):
    content = record(LOAD, body(PROMPTS[0], PROMPTS[1]), LOAD)
    with pytest.raises(FirmwareImageError, match="Reset Configuration"):
        rig.run(make_image(tmp_path, kind="official", content=content))
    assert rig.events == []


def test_official_on_a_u64e_waits_for_the_owner(rig, held, tmp_path):
    rig.infos = [info(product="Ultimate 64 Elite")]
    with pytest.raises(FirmwareFlashRefusedError, match="not decided"):
        rig.run(make_image(tmp_path, kind="official", ext=".u64"))
    assert rig.names() == ["get_info"]


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
    assert rec.hash_verified and rec.git_commit_hash_after == HASH_NEW
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
    assert rec.failed_step == "verify" and not rec.hash_verified
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
    with caplog.at_level(logging.WARNING, logger=ff.__name__):
        rec = rig.run(make_image(tmp_path, kind="official"))
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
