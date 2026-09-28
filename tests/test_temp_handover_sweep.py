"""#511: sweep ``/Temp`` when the device queue advances, and fail closed.

Owner, 2026-09-28: "Can we have the harness check the directory and clear it
when the device queue advances to the next user? I'm not sure we really need
to allow 6 deep either, I think only the most recent file gets a lock."

What is pinned here, against a fake device that answers REST and FTP and
records every request:

* a process that takes a leak-prone device's ``DeviceLock`` (or has never
  swept it at all) sweeps ``/Temp`` before its first attachment-creating
  request -- and if that sweep fails, the request is refused **before**
  anything is sent, so no process spends a budget on a device it could not
  clean first (the #433 gap: a fresh ledger used to spend six);
* the sweep keeps the youngest managed file and any image a drive has
  mounted, and nothing else -- the 1.1.0 source holds no other attachment
  open once its request has returned;
* once one attachment is pending, the next attachment-creating request
  sweeps first (budget 1), so resident managed files stay at two;
* a handover sweep that FTP refuses makes the process's one FTP-enable
  attempt and retries (owner decision 2026-09-28), and is refused if FTP
  still will not come up;
* the sweep and the enable happen only for a process that holds the
  device's ``DeviceLock``: an unlocked process is refused without touching
  FTP or config (supervisor ruling on the #513 review);
* a sweep never deletes a file whose upload is still in flight in this
  process, and deletes nothing when it cannot read the mounted-image list;
* ``U64_TEMP_GC_REQUIRED=0`` and ``temp_hygiene=False`` still disarm;
* a post-safe device (the U64E) sees no new request at all, even under
  ``U64_AUTO_TEMP_GC=1``.

No device: HTTP and FTP are faked.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import uuid
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends import device_lock as lock_mod
from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64TempHygieneError,
)

PRG = b"\x01\x08" + b"\x00" * 40

LEAK_PRONE = {"product": "C64 Ultimate", "firmware_version": "1.1.0"}
POST_SAFE = {"product": "Ultimate 64", "firmware_version": "3.15"}


class _Resp:
    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self) -> bytes:
        return self._body


class FakeDevice:
    """One Ultimate: REST routes that leave ``/Temp/temp%04x`` like 1.1.0
    (``attachment_writer.h``: a static hex counter per body-carrying POST),
    a drives listing, and an FTP server over the same ``/Temp``.

    ``log`` is every request in order, as ``("REST", method, path)`` or
    ``("FTP", command, argument)``.
    """

    def __init__(self, info: dict, *, ftp_up: bool = True) -> None:
        self.info = info
        self.ftp_up = ftp_up
        #: Whether ``PUT /v1/configs/Network Settings/FTP File Service``
        #: ``?value=Enabled`` brings FTP up. ``False`` models a device whose
        #: FTP will not come up however it is configured.
        self.ftp_enable_works = False
        #: ``GET /v1/drives`` answers HTTP 500 when set.
        self.drives_fail = False
        #: POSTs to these paths hold their attachment "in flight" until
        #: :attr:`release_in_flight` is set, like a body still streaming.
        self.hold_paths: set[str] = set()
        self.release_in_flight = threading.Event()
        self.in_flight: list[str] = []
        self.deleted_in_flight: list[str] = []
        self.temp: list[str] = []
        self.counter = 0
        self.mounted: dict[str, str] = {}
        self.log: list[tuple[str, str, str]] = []
        self.peak = 0

    # -- helpers ---------------------------------------------------------
    def attach(self) -> str:
        name = f"temp{self.counter:04x}"
        self.counter += 1
        self.temp.append(name)
        self.peak = max(self.peak, len(self.temp))
        return name

    def posts(self) -> list[str]:
        return [path for kind, method, path in self.log if kind == "REST" and method == "POST"]

    def ftp_sessions(self) -> int:
        return sum(1 for kind, cmd, _ in self.log if kind == "FTP" and cmd == "connect")

    def ftp_enables(self) -> list[str]:
        return [p for p in self.config_writes() if p.endswith("/FTP File Service")]

    def config_writes(self) -> list[str]:
        return [
            path for kind, method, path in self.log
            if kind == "REST" and method == "PUT" and path.startswith("/v1/configs")
        ]

    # -- REST ------------------------------------------------------------
    def urlopen(self, req, timeout=None):
        method = req.get_method()
        parsed = urllib.parse.urlsplit(req.full_url)
        path = urllib.parse.unquote(parsed.path)
        self.log.append(("REST", method, path))
        if path == "/v1/info":
            return _Resp(json.dumps(self.info).encode())
        if path == "/v1/drives":
            if self.drives_fail:
                return _Resp(b"", status=500)
            drives = [
                {letter: {"image_file": name, "image_path": "/Temp/"}}
                for letter, name in sorted(self.mounted.items())
            ]
            return _Resp(json.dumps({"drives": drives}).encode())
        if method == "PUT" and path == "/v1/configs/Network Settings/FTP File Service":
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if query.get("value") == "Enabled" and self.ftp_enable_works:
                self.ftp_up = True
            return _Resp(b"{}")
        if method == "POST" and req.data is not None:
            name = self.attach()
            if path.startswith("/v1/drives/") and path.endswith(":mount"):
                self.mounted[path.split("/")[3].split(":")[0]] = name
            if path in self.hold_paths:
                self.in_flight.append(name)
                assert self.release_in_flight.wait(5.0), "never released"
                self.in_flight.remove(name)
        return _Resp(b"{}")

    # -- FTP -------------------------------------------------------------
    def ftp_factory(self, *args, **kwargs):
        device = self

        class _FTP:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def connect(self, host, port=21, timeout=None):
                device.log.append(("FTP", "connect", host))
                if not device.ftp_up:
                    raise ConnectionRefusedError(61, "Connection refused")

            def login(self, user, password):
                device.log.append(("FTP", "login", user))

            def cwd(self, path):
                device.log.append(("FTP", "cwd", path))

            def nlst(self):
                device.log.append(("FTP", "nlst", ""))
                return list(device.temp)

            def delete(self, name):
                device.log.append(("FTP", "delete", name))
                if name in device.in_flight:
                    device.deleted_in_flight.append(name)
                device.temp.remove(name)

        return _FTP()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for var in (gc_mod.AUTO_GC_ENV, gc_mod.BUDGET_ENV, gc_mod.REQUIRED_ENV, gc_mod.KEEP_ENV):
        monkeypatch.delenv(var, raising=False)
    gc_mod._reset_temp_ledgers()
    yield
    gc_mod._reset_temp_ledgers()


@pytest.fixture
def host() -> str:
    return f"dev-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch):
    """A leak-prone device with FTP up; tests flip ``info``/``ftp_up``."""
    dev = FakeDevice(dict(LEAK_PRONE))
    monkeypatch.setattr(gc_mod, "FTP", dev.ftp_factory)
    with patch("urllib.request.urlopen", MagicMock(side_effect=dev.urlopen)):
        yield dev


def _client(host: str, **kwargs) -> Ultimate64Client:
    """A client that grades itself from the fake device's ``/v1/info``."""
    kwargs.setdefault("warn_unlocked", False)
    return Ultimate64Client(host, **kwargs)


def _lock(host: str, tmp_path) -> DeviceLock:
    lock = DeviceLock(host, lock_dir=tmp_path, heartbeat_interval=None)
    assert lock.acquire(timeout=1.0)
    return lock


# ------------------------------------------------------------- fail closed
def test_failed_acquire_sweep_refuses_the_first_upload(device, host, tmp_path):
    """FTP down at handover: the first upload is refused and nothing is sent."""
    device.ftp_up = False
    device.temp[:] = ["temp0000", "temp0001", "temp0002"]  # a crashed neighbour's
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        assert c.temp_hygiene_armed
        with pytest.raises(Ultimate64TempHygieneError):
            c.run_prg(PRG)
        assert device.posts() == []
        assert device.ftp_sessions() >= 1, "the sweep must have been tried"
        assert device.temp == ["temp0000", "temp0001", "temp0002"]
        # Owner, 2026-09-28: the handover sweep makes the one FTP-enable
        # attempt; FTP stayed down, so the upload is still refused.
        assert device.ftp_enables() == ["/v1/configs/Network Settings/FTP File Service"]
        # And it stays refused: a second attempt sends nothing either, and
        # the enable is not tried again in this process.
        with pytest.raises(Ultimate64TempHygieneError):
            c.write_mem(0xC000, b"x" * 200)
        assert device.posts() == []
        c2 = _client(host)
        with pytest.raises(Ultimate64TempHygieneError):
            c2.run_prg(PRG)
    finally:
        lock.release()
    assert len(device.ftp_enables()) == 1


def test_ftp_off_at_handover_is_enabled_once_then_swept_then_uploaded(
    device, host, tmp_path
):
    """FTP File Service off (the 1.1.0 default): one enable, the sweep, the upload."""
    device.ftp_up = False
    device.ftp_enable_works = True
    device.temp[:] = ["temp0000", "temp0001"]
    device.counter = 2
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        c.run_prg(PRG)
        c.run_prg(PRG)
        log = list(device.log)
    finally:
        lock.release()
    assert device.ftp_enables() == ["/v1/configs/Network Settings/FTP File Service"]
    enable = log.index(("REST", "PUT", "/v1/configs/Network Settings/FTP File Service"))
    first_post = log.index(("REST", "POST", "/v1/runners:run_prg"))
    # The failed sweep, then the enable, then a sweep that deletes, then the upload.
    assert ("FTP", "connect", host) in log[:enable]
    deletes = [i for i, entry in enumerate(log) if entry[1] == "delete"]
    assert deletes and enable < deletes[0] < first_post
    assert len(device.posts()) == 2


def test_the_handover_enable_is_attempted_once_per_process(device, host, tmp_path):
    """FTP will not come up: one enable, then refused. A later sweep lifts
    the block, the next handover fails again, and no second enable goes out."""
    device.ftp_up = False
    lock = _lock(host, tmp_path)
    c = _client(host)
    with pytest.raises(Ultimate64TempHygieneError):
        c.run_prg(PRG)
    device.ftp_up = True
    lock.release()  # the release drain sweeps, and succeeds: block lifted
    device.ftp_up = False
    lock = _lock(host, tmp_path)
    try:
        with pytest.raises(Ultimate64TempHygieneError):
            _client(host).run_prg(PRG)
    finally:
        lock.release()
    assert len(device.ftp_enables()) == 1
    assert device.posts() == []


def test_required_zero_makes_no_enable_attempt_at_handover(
    device, host, tmp_path, monkeypatch
):
    """``U64_TEMP_GC_REQUIRED=0`` opts out of enforcement: no config write."""
    monkeypatch.setenv(gc_mod.REQUIRED_ENV, "0")
    device.ftp_up = False
    device.ftp_enable_works = True
    lock = _lock(host, tmp_path)
    try:
        _client(host).run_prg(PRG)
        log = list(device.log)
    finally:
        lock.release()
    assert ("REST", "PUT", "/v1/configs/Network Settings/FTP File Service") not in log


def test_a_fresh_process_is_refused_before_spending_anything(device, host, tmp_path):
    """The #433 gap: a new process against a device whose sweep fails.

    Before #511 its fresh ledger spent a whole budget (six uploads) before
    its first sweep could fail. Now nothing goes out.
    """
    device.ftp_up = False
    for _process in range(3):
        gc_mod._reset_temp_ledgers()  # a new pytest invocation
        lock = _lock(host, tmp_path)
        try:
            c = _client(host)
            with pytest.raises(Ultimate64TempHygieneError):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts() == []
    assert device.temp == []
    # One enable attempt per process, each of which failed.
    assert len(device.ftp_enables()) == 3


def test_an_unlocked_process_is_refused_without_touching_ftp_or_config(device, host):
    """Only lock holders are in the queue: an unlocked process gets neither
    the sweep nor the FTP-enable, and its upload is refused."""
    device.ftp_up = False
    device.ftp_enable_works = True
    device.temp[:] = ["temp0000", "temp0001"]
    c = _client(host)
    with pytest.raises(Ultimate64TempHygieneError, match="DeviceLock"):
        c.run_prg(PRG)
    assert device.posts() == []
    assert device.ftp_sessions() == 0
    assert device.config_writes() == []
    assert device.temp == ["temp0000", "temp0001"]


def test_an_unlocked_process_is_refused_even_with_ftp_up(device, host):
    device.temp[:] = ["temp0000", "temp0001"]
    with pytest.raises(Ultimate64TempHygieneError, match="DeviceLock"):
        _client(host).run_prg(PRG)
    assert device.posts() == []
    assert device.ftp_sessions() == 0


def test_holding_another_devices_lock_does_not_count(device, host, tmp_path):
    """The lock that counts is this device's, in any lock directory."""
    other = _lock(host + "-other", tmp_path)
    try:
        with pytest.raises(Ultimate64TempHygieneError, match="DeviceLock"):
            _client(host).run_prg(PRG)
        assert device.ftp_sessions() == 0
        mine = _lock(host, tmp_path)
        try:
            _client(host).run_prg(PRG)
        finally:
            mine.release()
    finally:
        other.release()
    assert device.posts() == ["/v1/runners:run_prg"]


def test_the_free_probe_is_refused_unlocked(device, host):
    from c64_test_harness.backends import ultimate64_probe as probe_mod

    with pytest.raises(Ultimate64TempHygieneError, match="DeviceLock"):
        probe_mod._reserve_probe_attachments(
            host, 2, armed=True, operation="liveness_probe"
        )
    assert device.ftp_sessions() == 0
    assert gc_mod.temp_ledger_for(host).pending == 0


# ----------------------------------------------------- in-flight uploads
def test_a_sweep_never_deletes_an_upload_still_in_flight(device, host, tmp_path):
    """#513 review, finding 1: two of this process's uploads are still
    streaming when a third sweeps. keep=1 deleted the older one mid-write;
    1.1.0 FatFS has no open-file lock (FF_FS_LOCK 0), so that is a delete of
    an open file on the RAM disk."""
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        c.run_prg(PRG)                       # handover sweep, then temp0000
        device.hold_paths.add("/v1/machine:writemem")
        workers = [
            threading.Thread(target=c.write_mem, args=(0xC000, b"x" * 200))
            for _ in range(2)
        ]
        for w in workers:
            w.start()
        deadline = time.monotonic() + 5.0
        while len(device.in_flight) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(device.in_flight) == 2
        c.run_prg(PRG)                       # sweeps: pending > 0
        device.release_in_flight.set()
        for w in workers:
            w.join(5.0)
    finally:
        device.release_in_flight.set()
        lock.release()
    assert device.deleted_in_flight == []


def test_the_free_probe_sweep_keeps_in_flight_uploads(device, host, tmp_path):
    from c64_test_harness.backends import ultimate64_probe as probe_mod

    device.temp[:] = ["temp0000", "temp0001", "temp0002"]
    device.counter = 3
    ledger = gc_mod.temp_ledger_for(host)
    ledger.begin_reservation(2, armed=True)  # two uploads still streaming
    lock = _lock(host, tmp_path)
    try:
        probe_mod._reserve_probe_attachments(
            host, 2, armed=True, operation="liveness_probe"
        )
    finally:
        lock.release()
    deleted = [arg for kind, cmd, arg in device.log if cmd == "delete"]
    assert "temp0001" not in deleted and "temp0002" not in deleted


# ------------------------------------------------- unreadable drives list
def test_an_unreadable_drives_list_deletes_nothing_and_refuses(device, host, tmp_path):
    """#513 review, finding 4: without the mounted list a non-youngest image
    could be deleted while open. Fail closed instead."""
    device.temp[:] = ["temp0000", "temp0001", "temp0002"]
    device.counter = 3
    device.mounted["a"] = "temp0000"
    device.drives_fail = True
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        with pytest.raises(Ultimate64TempHygieneError):
            c.run_prg(PRG)
        log = list(device.log)
    finally:
        lock.release()
    assert [arg for kind, cmd, arg in log if cmd == "delete"] == []
    assert device.posts() == []


def test_the_handover_sweep_runs_before_the_first_post(device, host, tmp_path):
    """FTP up: the sweep collects what a neighbour left, then the upload goes."""
    device.temp[:] = ["temp0000", "temp0001", "temp0002"]
    device.counter = 3
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        c.run_prg(PRG)
        log = list(device.log)  # before the release drain adds its own
    finally:
        lock.release()
    first_post = log.index(("REST", "POST", "/v1/runners:run_prg"))
    deleted = [arg for kind, cmd, arg in log[:first_post] if cmd == "delete"]
    assert deleted == ["temp0000", "temp0001"]
    assert ("FTP", "delete", "temp0002") not in log


def test_a_reacquire_sweeps_what_arrived_between_holds(device, host, tmp_path):
    """The queue advanced and came back: whatever another lane left is swept
    before this process uploads again."""
    lock = _lock(host, tmp_path)
    c = _client(host)
    c.run_prg(PRG)
    lock.release()
    # Another process's lane, between our holds.
    for _ in range(3):
        device.attach()
    neighbour = list(device.temp)
    lock = _lock(host, tmp_path)
    try:
        mark = len(device.log)
        c.run_prg(PRG)
        after = device.log[mark:]
    finally:
        lock.release()
    post = after.index(("REST", "POST", "/v1/runners:run_prg"))
    deleted_before_post = {arg for kind, cmd, arg in after[:post] if cmd == "delete"}
    assert set(neighbour[:-1]) <= deleted_before_post


def test_a_sweep_that_succeeds_later_lifts_the_refusal(device, host, tmp_path):
    device.ftp_up = False
    lock = _lock(host, tmp_path)
    c = _client(host)
    with pytest.raises(Ultimate64TempHygieneError):
        c.run_prg(PRG)
    device.ftp_up = True
    lock.release()  # the release drain sweeps, and now succeeds
    lock = _lock(host, tmp_path)
    try:
        c.run_prg(PRG)
    finally:
        lock.release()
    assert device.posts() == ["/v1/runners:run_prg"]


# ------------------------------------------------------------ budget depth
def test_resident_attachments_stay_at_two(device, host, tmp_path):
    """Budget 1, keep 1: a sweep before every upload once one is pending."""
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        for _ in range(8):
            c.run_prg(PRG)
            assert len(device.temp) <= 2, device.temp
    finally:
        lock.release()
    assert device.peak == 2
    assert len(device.posts()) == 8


def test_one_handover_sweep_per_hold_not_one_per_upload(device, host, tmp_path):
    """The handover sweep is once per hold: with a larger budget
    (``U64_TEMP_GC_BUDGET`` / ``temp_gc_budget=``) three uploads cost one FTP
    session, and the next hold pays one more."""
    c = _client(host, temp_gc_budget=4)
    for hold in (1, 2):
        lock = _lock(host, tmp_path)
        try:
            for _ in range(3):
                c.run_prg(PRG)
            assert device.ftp_sessions() == 2 * hold - 1
        finally:
            lock.release()  # the release drain is the other session per hold
    assert len(device.posts()) == 6


def test_the_sweep_keeps_the_youngest_and_the_mounted_image(device, host, tmp_path):
    device.temp[:] = ["temp0000", "temp0001", "temp0002", "temp0003", "temp0004"]
    device.counter = 5
    device.mounted["a"] = "temp0001"
    lock = _lock(host, tmp_path)
    try:
        result = _client(host).gc_temp_folder()
    finally:
        lock.release()
    assert result.ok
    assert sorted(result.deleted) == ["temp0000", "temp0002", "temp0003"]
    assert result.mounted_excluded == ["temp0001"]
    assert result.kept == ["temp0004"]


def test_a_mounted_image_survives_the_handover_sweep(device, host, tmp_path):
    """Mounted via a raw upload, then another lane takes the device."""
    device.temp[:] = ["temp0000", "temp0001"]
    device.counter = 2
    device.mounted["b"] = "temp0000"
    lock = _lock(host, tmp_path)
    try:
        _client(host).run_prg(PRG)
    finally:
        lock.release()
    assert "temp0000" in device.temp
    assert ("FTP", "delete", "temp0000") not in device.log


def test_liveness_probe_fits_a_budget_of_one(device, host, tmp_path):
    """The probe's two POSTs are reserved whole after a sweep; the next
    upload sweeps them."""
    from c64_test_harness.backends import ultimate64_probe as probe_mod

    def _fake_probe(host_, *, request, **kwargs):
        request("POST", host_, 80, "/v1/machine:writemem", None, 2.0, body=b"x" * 128)
        request("POST", host_, 80, "/v1/machine:writemem", None, 2.0, body=b"x" * 128)
        return "ok"

    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        with patch.object(probe_mod, "liveness_probe", _fake_probe):
            assert c.liveness_probe() == "ok"
        assert len(device.temp) == 2
        c.run_prg(PRG)
        assert len(device.temp) <= 2
    finally:
        lock.release()


def test_the_free_liveness_probe_is_gated_at_handover(device, host, tmp_path):
    """The root-exported spelling reaches the same ledger and the same gate."""
    from c64_test_harness.backends import ultimate64_probe as probe_mod

    device.ftp_up = False
    lock = _lock(host, tmp_path)
    try:
        with pytest.raises(Ultimate64TempHygieneError):
            probe_mod._reserve_probe_attachments(
                host, 2, armed=True, operation="liveness_probe"
            )
    finally:
        lock.release()
    assert gc_mod.temp_ledger_for(host).pending == 0
    assert device.ftp_sessions() == 1


# ----------------------------------------------------------------- disarm
def test_required_zero_proceeds_with_a_warning(device, host, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(gc_mod.REQUIRED_ENV, "0")
    device.ftp_up = False
    lock = _lock(host, tmp_path)
    try:
        _client(host).run_prg(PRG)
    finally:
        lock.release()
    assert device.posts() == ["/v1/runners:run_prg"]
    assert "U64_TEMP_GC_REQUIRED=0" in caplog.text


def test_temp_hygiene_false_touches_no_ftp(device, host, tmp_path):
    device.ftp_up = False
    lock = _lock(host, tmp_path)
    try:
        c = _client(host, temp_hygiene=False)
        c.run_prg(PRG)
        c.run_prg(PRG)
    finally:
        lock.release()
    assert device.posts() == ["/v1/runners:run_prg"] * 2
    assert device.ftp_sessions() == 0
    assert device.config_writes() == []


def test_a_forced_arm_on_a_post_safe_device_gets_no_handover_enable(
    device, host, tmp_path, monkeypatch
):
    """#513 review, finding 2: ``U64_AUTO_TEMP_GC=1`` arms any device, but the
    handover sweep and its FTP-enable are for leak-prone grades only."""
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, "1")
    device.info = dict(POST_SAFE)
    device.ftp_up = False
    device.ftp_enable_works = True
    lock = _lock(host, tmp_path)
    try:
        c = _client(host)
        assert c.temp_hygiene_armed
        c.run_prg(PRG)
    finally:
        lock.release()
    assert device.config_writes() == []
    assert device.posts() == ["/v1/runners:run_prg"]


def test_a_post_safe_device_with_ftp_off_gets_no_enable(device, host, tmp_path):
    """The owner's allowance is for leak-prone devices only: FTP off on a
    post-safe device is left alone, and uploads go straight out."""
    device.info = dict(POST_SAFE)
    device.ftp_up = False
    device.ftp_enable_works = True
    for _ in range(2):
        lock = _lock(host, tmp_path)
        try:
            _client(host).run_prg(PRG)
        finally:
            lock.release()
    assert device.config_writes() == []
    assert device.ftp_sessions() == 0
    assert device.posts() == ["/v1/runners:run_prg"] * 2


# -------------------------------------------------------------- post-safe
def test_a_post_safe_device_sees_no_new_traffic(device, host, tmp_path):
    """The U64E: lock handovers and uploads, and not one extra request."""
    device.info = dict(POST_SAFE)
    device.temp[:] = ["temp0000", "temp0001", "temp0002"]
    lock = _lock(host, tmp_path)
    c = _client(host)
    assert not c.temp_hygiene_armed
    c.run_prg(PRG)
    lock.release()
    for _ in range(3):
        lock = _lock(host, tmp_path)
        c.run_prg(PRG)
        c.write_mem(0xC000, b"x" * 200)
        lock.release()
    assert device.log == [("REST", "GET", "/v1/info")] + [
        ("REST", "POST", "/v1/runners:run_prg")
    ] + [
        ("REST", "POST", "/v1/runners:run_prg"),
        ("REST", "POST", "/v1/machine:writemem"),
    ] * 3


# ------------------------------------------------------------- the epoch
def test_the_acquire_epoch_counts_handovers_not_nested_joins(tmp_path):
    acquire_epoch = lock_mod.acquire_epoch
    h = f"dev-{uuid.uuid4().hex[:10]}"
    before = acquire_epoch(h)
    outer = DeviceLock(h, lock_dir=tmp_path, heartbeat_interval=None, allow_nested=True)
    assert outer.acquire(timeout=1.0)
    assert acquire_epoch(h) == before + 1
    inner = DeviceLock(h, lock_dir=tmp_path, heartbeat_interval=None, allow_nested=True)
    assert inner.acquire(timeout=1.0)
    assert acquire_epoch(h) == before + 1
    inner.release()
    outer.release()
    assert outer.acquire(timeout=1.0)
    assert acquire_epoch(h) == before + 2
    outer.release()
