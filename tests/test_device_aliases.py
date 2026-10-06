"""#519: one device on two interfaces queues on one lock and spends one budget.

A U64E/C64U answers on ethernet and WiFi at different addresses.  Before
#519 each address was its own ``DeviceLock`` and its own ``/Temp`` ledger, so
two lanes could drive one device at once.  Pinned here:

* the offline alias map folds every listed spelling into ``uid-<unique_id>``
  for the lock, the ledger and the lock-holder query, with no DNS;
* a broken map is refused before any lock or request;
* after acquire, a device reached under an *unconfigured* second key that
  another process holds right now is refused on a leak-prone grade
  (``U64_TEMP_GC_REQUIRED=0`` does not lift it) and warned on a post-safe
  one, and an address that answers as a different device than configured is
  refused.

No device: HTTP and FTP are faked, lock dirs are per-test.
"""
from __future__ import annotations

import json
import logging
import socket
import multiprocessing
import os
import time
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import device_aliases as da
from c64_test_harness.backends import device_lock as dl
from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends.device_aliases import DeviceAliasConfigError
from c64_test_harness.backends.device_lock import (
    DeviceLock,
    device_key,
    device_lock_holder,
    normalize_device_host,
)
from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64DeviceAliasError,
)
from c64_test_harness.backends.ultimate64_temp_gc import TempGCResult

ETH, WIFI = "192.0.2.81", "192.0.2.83"
UID = "601a96"
PRG = b"\x01\x08x"


@pytest.fixture
def aliases(tmp_path, monkeypatch):
    """Write an alias file and point the harness (and spawned children) at it."""
    path = tmp_path / "devices.toml"
    monkeypatch.setenv("C64_DEVICE_ALIASES_FILE", str(path))

    def write(text: str) -> Path:
        path.write_text(text)
        da._reset_alias_cache()   # the map is frozen per process
        return path

    return write


@pytest.fixture
def lock_root(tmp_path, monkeypatch):
    """Redirect the *default* lock dir: the client's lock checks and the
    identity records both use it."""
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root / "c64-test-harness"


BENCH = f'[devices.{UID}]\nhosts = ["{ETH}", "{WIFI}"]\n'


def _hold(host, started, stop, lock_dir, record=None, reacquire=False, record_again=False):
    lock = DeviceLock(host, lock_dir=Path(lock_dir) if lock_dir else None)
    if not lock.acquire(timeout=5.0):
        return
    try:
        if record is not None:
            # A lane the device has answered: what its client records.
            da.check_device_identity(normalize_device_host(host), host, record)
            if reacquire:
                # ...that then re-took the lock and has not been answered since.
                lock.release()
                time.sleep(0.05)
                assert lock.acquire(timeout=5.0)
                if record_again:
                    # ...and has been answered again (its next upload).
                    da.check_device_identity(normalize_device_host(host), host, record)
        started.set()
        stop.wait(timeout=20.0)
    finally:
        lock.release()


class _Holder:
    """Another process holding *host*'s lock for the duration of the block."""

    def __init__(
        self, host: str, lock_dir: Path | None = None, *,
        record: str | None = None, reacquire: bool = False,
        record_again: bool = False,
    ) -> None:
        ctx = multiprocessing.get_context("spawn")
        self._started, self._stop = ctx.Event(), ctx.Event()
        self._proc = ctx.Process(
            target=_hold,
            args=(host, self._started, self._stop,
                  str(lock_dir) if lock_dir else None, record, reacquire,
                  record_again),
        )

    def __enter__(self):
        self._proc.start()
        assert self._started.wait(timeout=10.0), "holder never acquired"
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._proc.join(timeout=10.0)


# --------------------------------------------------------------------------- #
# Part A: the alias map                                                        #
# --------------------------------------------------------------------------- #

def test_two_interfaces_of_one_device_share_one_lock(aliases, tmp_path):
    aliases(BENCH)
    with _Holder(ETH, tmp_path):
        contender = DeviceLock(WIFI, lock_dir=tmp_path)
        assert not contender.acquire(timeout=0.3, progress_window=None), (
            "the WiFi address took its own lock while ethernet was held"
        )


def test_an_unlisted_address_keeps_its_own_lock(aliases, tmp_path):
    aliases(BENCH)
    with _Holder(ETH, tmp_path):
        other = DeviceLock("192.0.2.99", lock_dir=tmp_path)
        assert other.acquire(timeout=0.3, progress_window=None)
        other.release()


def test_keys_fold_to_the_unique_id_without_dns(aliases):
    aliases(BENCH + '\n[devices.abcdef]\nhosts = ["c64u.lan"]\n')
    with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")):
        assert normalize_device_host(ETH) == normalize_device_host(f"http://{WIFI}:80/") == f"uid-{UID}"
        assert gc_mod.temp_ledger_key(WIFI) == f"uid-{UID}"
        assert normalize_device_host("C64U.lan.") == "uid-abcdef"
        assert normalize_device_host(f"uid-{UID}") == f"uid-{UID}"


def test_a_non_default_port_folds_only_when_listed(aliases):
    aliases(BENCH)
    assert device_key(ETH, 8080) != device_key(WIFI, 8080)
    assert device_key(ETH, 8080) == f"{ETH}:8080"
    aliases(f'[devices.{UID}]\nhosts = ["{ETH}:8080", "{WIFI}:8080"]\n')
    assert device_key(ETH, 8080) == device_key(WIFI, 8080) == f"uid-{UID}"


def test_the_environment_form_folds_and_wins(aliases, monkeypatch):
    monkeypatch.setenv("C64_DEVICE_ALIASES", f"bbbbbb={WIFI}")
    aliases(BENCH)
    assert normalize_device_host(WIFI) == "uid-bbbbbb"
    assert normalize_device_host(ETH) == f"uid-{UID}"


class _Drain:
    def __init__(self):
        self.calls = 0

    def drain(self, reason=""):
        self.calls += 1


@pytest.mark.parametrize("edit", [BENCH, "[devices.601a96\n"], ids=["new-alias", "broken"])
def test_editing_the_map_during_a_hold_changes_nothing_in_this_process(
    aliases, tmp_path, edit
):
    """The map is frozen per process: a hold keyed before an edit must still
    find its release callbacks and its held-registry entry afterwards, and a
    broken edit must not make release() raise (#519 review, finding 1)."""
    path = aliases(f'[devices.{UID}]\nhosts = ["192.0.2.50"]\n')
    lock = DeviceLock(ETH, lock_dir=tmp_path)
    assert lock.acquire(timeout=5.0)
    drain = _Drain()
    dl.register_release_callback(ETH, drain, "drain")
    path.write_text(edit)
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert DeviceLock.held_by_this_process(ETH, lock_dir=tmp_path)
    lock.release()
    assert drain.calls == 1, "the release /Temp drain was skipped"
    assert not lock.held
    other = DeviceLock(ETH, lock_dir=tmp_path)
    assert other.acquire(timeout=0.5, progress_window=None)
    other.release()
    dl.unregister_release_callback(ETH, drain, "drain")


def test_the_lock_holder_query_reports_the_spelling_and_the_device(aliases, lock_root):
    aliases(BENCH)
    with _Holder(ETH):
        holder = device_lock_holder(WIFI)
    assert holder is not None
    assert holder["device_host"] == ETH
    assert holder["device_key"] == f"uid-{UID}"


@pytest.mark.parametrize("text", [
    f'[devices.{UID}]\nhosts = ["{ETH}"]\n[devices.abcdef]\nhosts = ["{ETH}"]\n',
    f'[devices.{UID}]\nhosts = []\n',
    f'[devices.{UID}]\nhost = ["{ETH}"]\n',
    f'[devices."60 1a"]\nhosts = ["{ETH}"]\n',
    f'[devices.{UID}]\nhosts = ["uid-abcdef"]\n',
    "[devices.601a96\n",
    f'[device.{UID}]\nhosts = ["{ETH}"]\n',
], ids=["two-devices", "empty", "typo-key", "bad-id", "canonical-as-host", "bad-toml",
        "typo-table"])
def test_a_broken_map_is_refused_before_any_lock(aliases, tmp_path, text):
    aliases(text)
    with patch.object(dl.fcntl, "flock", side_effect=AssertionError("flock reached")):
        with pytest.raises(DeviceAliasConfigError):
            DeviceLock(ETH, lock_dir=tmp_path)


@pytest.mark.parametrize("env", [
    UID, f"{UID}=", f"{UID}= , ", f"60 1a={ETH}", f"{UID}={ETH};abcdef={ETH}",
], ids=["no-equals", "no-hosts", "blank-hosts", "bad-id", "two-devices"])
def test_a_broken_environment_map_is_refused_before_any_lock(
    aliases, tmp_path, monkeypatch, env
):
    monkeypatch.setenv("C64_DEVICE_ALIASES", env)
    aliases("")
    with patch.object(dl.fcntl, "flock", side_effect=AssertionError("flock reached")):
        with pytest.raises(DeviceAliasConfigError):
            DeviceLock(ETH, lock_dir=tmp_path)


def test_a_broken_map_is_refused_before_any_request(aliases):
    aliases("[devices.601a96\n")
    with patch("urllib.request.urlopen", side_effect=AssertionError("request sent")):
        with pytest.raises(DeviceAliasConfigError):
            Ultimate64Client(ETH, warn_unlocked=False)


# --------------------------------------------------------------------------- #
# Part B: catching an alias nobody configured                                  #
# --------------------------------------------------------------------------- #

class _Device:
    """A fake device answering ``/v1/info`` with *info*; records the wire."""

    def __init__(self, info: dict) -> None:
        self.info = info
        self.wire: list[tuple[str, str]] = []

    def __call__(self, req, timeout=None):
        parsed = urllib.parse.urlsplit(req.full_url)
        self.wire.append((req.get_method(), parsed.path))
        body = json.dumps(self.info).encode() if parsed.path == "/v1/info" else b""
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = body
        resp.status = 200
        return resp

    def posts(self) -> list[str]:
        return [p for m, p in self.wire if m == "POST"]


LEAKY_INFO = {"product": "C64 Ultimate", "firmware_version": "1.1.0", "unique_id": UID}
FIXED_INFO = {"product": "Ultimate 64", "firmware_version": "3.15", "unique_id": UID}


@pytest.fixture
def clean_env(monkeypatch):
    for var in (gc_mod.AUTO_GC_ENV, gc_mod.BUDGET_ENV, gc_mod.REQUIRED_ENV,
                gc_mod.KEEP_ENV, dl.REQUIRE_DEVICE_LOCK_ENV):
        monkeypatch.delenv(var, raising=False)


def _sweeps_ok():
    return patch.object(
        gc_mod, "gc_temp_folder", side_effect=lambda host, **kw: TempGCResult(host=host)
    )


def _locked_client(host: str, device: _Device) -> tuple[Ultimate64Client, DeviceLock]:
    lock = DeviceLock(host)
    assert lock.acquire(timeout=5.0)
    return Ultimate64Client(host, warn_unlocked=False, temp_gc_budget=6), lock


def _reach_under(host: str, device: _Device) -> None:
    """Record *host* as a key for the device, as an earlier lane would."""
    with patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
        c, lock = _locked_client(host, device)
        try:
            c.run_prg(PRG)
        finally:
            c.close()
            lock.release()


def test_an_unconfigured_alias_held_elsewhere_is_refused_on_leak_prone_firmware(
    lock_root, clean_env, monkeypatch
):
    device = _Device(LEAKY_INFO)
    monkeypatch.setenv(gc_mod.REQUIRED_ENV, "0")  # must not lift this refusal
    with _Holder(ETH, record=UID), patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
        c, lock = _locked_client(WIFI, device)
        device.wire.clear()
        try:
            with pytest.raises(Ultimate64DeviceAliasError, match=r"192\.0\.2\.81"):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts() == [], "an upload reached the device before the refusal"


def test_an_unconfigured_alias_held_elsewhere_warns_on_post_safe_firmware(
    lock_root, clean_env, caplog
):
    device = _Device(FIXED_INFO)
    with _Holder(ETH, record=UID), patch("urllib.request.urlopen", side_effect=device):
        c, lock = _locked_client(WIFI, device)
        try:
            with caplog.at_level(logging.WARNING):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert any("locked right now under another key" in r.getMessage() for r in caplog.records)
    assert device.posts(), "a post-safe device must not be refused"


def test_require_device_lock_turns_the_post_safe_warning_into_a_refusal(
    lock_root, clean_env, monkeypatch
):
    device = _Device(FIXED_INFO)
    monkeypatch.setenv(dl.REQUIRE_DEVICE_LOCK_ENV, "1")
    with _Holder(ETH, record=UID), patch("urllib.request.urlopen", side_effect=device):
        c, lock = _locked_client(WIFI, device)
        device.wire.clear()
        try:
            with pytest.raises(Ultimate64DeviceAliasError):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts() == []


def test_a_configured_alias_is_not_a_finding(lock_root, clean_env, aliases, caplog):
    device = _Device(LEAKY_INFO)
    _reach_under(ETH, device)         # recorded under the raw key, pre-alias
    aliases(BENCH)
    with patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
        c, lock = _locked_client(WIFI, device)
        try:
            with caplog.at_level(logging.WARNING):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert not [r for r in caplog.records if "several keys" in r.getMessage()]
    assert device.posts()


def test_an_address_that_answers_as_another_device_is_refused(
    lock_root, clean_env, aliases
):
    aliases(BENCH)
    device = _Device({**LEAKY_INFO, "unique_id": "abcdef"})
    with patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
        c, lock = _locked_client(WIFI, device)
        device.wire.clear()
        try:
            with pytest.raises(Ultimate64DeviceAliasError, match="reports unique_id abcdef"):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts() == []


def test_aliases_share_one_temp_budget(aliases, clean_env, lock_root):
    """Clients on both interfaces spend one device budget: the sweep comes
    when the *device* has spent it, not when either address has."""
    aliases(BENCH)
    swept: list[str] = []

    def _gc(host, **kw):
        swept.append(host)
        return TempGCResult(host=host)

    device = _Device(LEAKY_INFO)
    with patch("urllib.request.urlopen", side_effect=device), \
            patch.object(gc_mod, "gc_temp_folder", side_effect=_gc), \
            patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")), \
            patch.object(gc_mod, "lock_held_for", return_value=True):
        eth = Ultimate64Client(ETH, warn_unlocked=False, temp_gc_budget=3,
                               write_mem_query_threshold=128)
        wifi = Ultimate64Client(WIFI, warn_unlocked=False, temp_gc_budget=3,
                                write_mem_query_threshold=128)
        eth._capabilities = wifi._capabilities = DeviceCapabilities.from_info(LEAKY_INFO)
        eth.run_prg(PRG)           # handover sweep, then 1 pending
        swept.clear()
        wifi.run_prg(PRG)
        eth.run_prg(PRG)
        assert swept == [], "swept before the device's budget was spent"
        assert eth.pending_temp_attachments == wifi.pending_temp_attachments == 3
        wifi.run_prg(PRG)
    assert len(swept) == 1


def test_a_device_seen_under_another_key_warns_once(lock_root, clean_env, caplog):
    device = _Device(LEAKY_INFO)
    _reach_under(ETH, device)
    with caplog.at_level(logging.WARNING):
        _reach_under(WIFI, device)
        _reach_under(WIFI, device)
    seen = [r for r in caplog.records if "several keys" in r.getMessage()]
    assert len(seen) == 1
    assert f"[devices.{UID}]" in seen[0].getMessage()


def test_an_address_handed_to_another_device_leaves_the_old_record(
    lock_root, clean_env
):
    """DHCP gave WIFI to device B: B's lane holding it must not read as a
    collision for device A's lane on ETH."""
    a = _Device(LEAKY_INFO)
    b = _Device({**LEAKY_INFO, "unique_id": "bbbbbb"})
    _reach_under(ETH, a)
    _reach_under(WIFI, a)          # WIFI was A's, once
    _reach_under(WIFI, b)          # ...and is B's now
    record = json.loads((lock_root / f"identity-uid-{UID}.json").read_text())
    assert WIFI not in record["keys"], "A's record still lists the moved address"
    with _Holder(WIFI), patch("urllib.request.urlopen", side_effect=a), _sweeps_ok():
        c, lock = _locked_client(ETH, a)
        try:
            c.run_prg(PRG)         # must not raise Ultimate64DeviceAliasError
        finally:
            lock.release()
    assert a.posts()


def test_a_stale_record_never_blocks_a_device(lock_root, clean_env):
    """WIFI was A's, then went to B, whose lane holds it but has not been
    answered yet: A's lane on ETH must not be refused (review finding 2)."""
    a = _Device(LEAKY_INFO)
    _reach_under(ETH, a)
    _reach_under(WIFI, a)
    with _Holder(WIFI), patch("urllib.request.urlopen", side_effect=a), _sweeps_ok():
        c, lock = _locked_client(ETH, a)
        try:
            c.run_prg(PRG)
        finally:
            lock.release()
    assert a.posts()


def test_an_unlocked_client_writes_no_identity_record(lock_root, clean_env):
    device = _Device(FIXED_INFO)
    with patch("urllib.request.urlopen", side_effect=device):
        Ultimate64Client(ETH, warn_unlocked=False).run_prg(PRG)
    assert not list(lock_root.glob("identity-*.json")) if lock_root.exists() else True
    assert device.posts()


def _hold_raw(path, started, stop):
    """An older harness: flock the un-aliased lockfile directly."""
    import fcntl as _f
    import json as _j
    import os as _o

    fd = _o.open(path, _o.O_RDWR | _o.O_CREAT, 0o644)
    _f.flock(fd, _f.LOCK_EX)
    _o.write(fd, _j.dumps({"pid": _o.getpid(), "ts": 0.0, "device_host": "old"}).encode())
    started.set()
    stop.wait(timeout=20.0)
    _o.close(fd)


def test_a_lane_on_an_older_harness_holding_the_raw_address_is_refused(
    lock_root, clean_env, aliases
):
    aliases(BENCH)
    lock_root.mkdir(parents=True, exist_ok=True)
    ctx = multiprocessing.get_context("spawn")
    started, stop = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_raw, args=(str(lock_root / f"device-{WIFI}.lock"), started, stop))
    proc.start()
    device = _Device({**LEAKY_INFO, "unique_id": ""})   # needs no unique_id
    try:
        assert started.wait(timeout=10.0)
        with patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
            c, lock = _locked_client(ETH, device)
            device.wire.clear()
            try:
                with pytest.raises(Ultimate64DeviceAliasError, match="raw address"):
                    c.run_prg(PRG)
            finally:
                lock.release()
    finally:
        stop.set()
        proc.join(timeout=10.0)
    assert device.posts() == []


def _a_lane_on_eth_is_not_refused(device: _Device) -> None:
    with patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
        c, lock = _locked_client(ETH, device)
        try:
            c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts()


def test_a_holder_that_did_not_record_the_key_is_not_a_collision(lock_root, clean_env):
    """WIFI's record is newer than the WIFI holder's acquire, but another
    process wrote it: the holder itself was never answered by this device."""
    a = _Device(LEAKY_INFO)
    with _Holder(WIFI):
        time.sleep(0.05)
        da.check_device_identity(WIFI, WIFI, UID)   # recorded by *this* pid
        _a_lane_on_eth_is_not_refused(a)


def test_a_recorder_that_re_took_the_lock_since_is_not_a_collision(lock_root, clean_env):
    """Same pid, but its current hold began after it was last answered."""
    a = _Device(LEAKY_INFO)
    with _Holder(WIFI, record=UID, reacquire=True):
        _a_lane_on_eth_is_not_refused(a)


def test_a_lane_that_re_took_the_lock_and_was_answered_again_is_a_collision(
    lock_root, clean_env
):
    """Per-test locking: every pytest lane re-acquires between tests.  Its
    record must follow its current hold, or detection decays to "seen" from
    its second test on (round-2 review, finding 1)."""
    device = _Device(LEAKY_INFO)
    with _Holder(ETH, record=UID, reacquire=True, record_again=True), \
            patch("urllib.request.urlopen", side_effect=device), _sweeps_ok():
        c, lock = _locked_client(WIFI, device)
        device.wire.clear()
        try:
            with pytest.raises(Ultimate64DeviceAliasError):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts() == []


class _SlowFirstInfo(_Device):
    """The construct-time ``/v1/info`` times out; later requests answer."""

    def __init__(self, info: dict) -> None:
        super().__init__(info)
        self._slow = True

    def __call__(self, req, timeout=None):
        if self._slow and urllib.parse.urlsplit(req.full_url).path == "/v1/info":
            self._slow = False
            raise socket.timeout("slow first probe")
        return super().__call__(req, timeout)


def test_the_first_upload_after_a_slow_construct_probe_is_still_checked(
    lock_root, clean_env
):
    """The re-probe fills the unique_id; the check must run after it
    (round-2 review, finding 2)."""
    device = _SlowFirstInfo(LEAKY_INFO)
    with _Holder(ETH, record=UID), patch("urllib.request.urlopen", side_effect=device), \
            _sweeps_ok():
        c, lock = _locked_client(WIFI, device)
        try:
            assert c._device_unique_id is None, "the construct-time probe should have failed"
            c.get_version()                 # the device has now answered a request
            device.wire.clear()
            with pytest.raises(Ultimate64DeviceAliasError):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert device.posts() == []


def test_the_free_liveness_probe_is_checked_before_it_reserves(lock_root, clean_env):
    """Round-2 review, finding 3: the root-exported probe reserves two POSTs."""
    from c64_test_harness.backends.ultimate64_probe import liveness_probe

    posts: list[str] = []

    def _send(method, host, port, path, password, timeout, **kwargs):
        if path == "/v1/info":
            return 200, json.dumps(LEAKY_INFO).encode()
        if method == "POST":
            posts.append(path)
        return 200, bytes(128)

    with _Holder(ETH, record=UID), _sweeps_ok(), patch(
        "c64_test_harness.backends.ultimate64_probe.probe_u64",
        return_value=MagicMock(reachable=True, error=None),
    ):
        lock = DeviceLock(WIFI)
        assert lock.acquire(timeout=5.0)
        try:
            with pytest.raises(Ultimate64DeviceAliasError):
                liveness_probe(WIFI, request=_send)
        finally:
            lock.release()
    assert posts == []


@pytest.mark.parametrize("uid", ["ab cd", "../x", "x" * 17])
def test_an_unusable_reported_unique_id_writes_no_record(lock_root, clean_env, uid):
    lock_root.mkdir(parents=True, exist_ok=True)
    assert da.check_device_identity(ETH, ETH, uid) is None
    assert not list(lock_root.parent.rglob("identity-*"))


def test_a_deliberately_disarmed_leak_prone_client_only_warns(lock_root, clean_env, caplog):
    """temp_hygiene=False is the explicit disarm: a collision is a WARNING
    there, not a refusal (the docs table's "otherwise" column)."""
    device = _Device(LEAKY_INFO)
    with _Holder(ETH, record=UID), patch("urllib.request.urlopen", side_effect=device):
        lock = DeviceLock(WIFI)
        assert lock.acquire(timeout=5.0)
        c = Ultimate64Client(WIFI, warn_unlocked=False, temp_hygiene=False)
        try:
            with caplog.at_level(logging.WARNING):
                c.run_prg(PRG)
        finally:
            lock.release()
    assert any("locked right now under another key" in r.getMessage() for r in caplog.records)
    assert device.posts()


def _probe_sender(info: dict, posts: list):
    def _send(method, host, port, path, password, timeout, **kwargs):
        if path == "/v1/info":
            return 200, json.dumps(info).encode()
        if method == "POST":
            posts.append(path)
        return 200, bytes(128)

    return _send


def _reachable():
    return patch(
        "c64_test_harness.backends.ultimate64_probe.probe_u64",
        return_value=MagicMock(reachable=True, error=None),
    )


def test_the_free_liveness_probe_only_warns_on_a_post_safe_grade(
    lock_root, clean_env, caplog
):
    from c64_test_harness.backends.ultimate64_probe import liveness_probe

    posts: list[str] = []
    with _Holder(ETH, record=UID), _reachable():
        lock = DeviceLock(WIFI)
        assert lock.acquire(timeout=5.0)
        try:
            with caplog.at_level(logging.WARNING):
                liveness_probe(WIFI, request=_probe_sender(FIXED_INFO, posts))
        finally:
            lock.release()
    assert any("locked right now under another key" in r.getMessage() for r in caplog.records)
    assert posts, "a post-safe device must not be refused"


def test_an_unlocked_free_liveness_probe_writes_no_identity_record(lock_root, clean_env):
    from c64_test_harness.backends.ultimate64_probe import liveness_probe

    posts: list[str] = []
    with _reachable():
        liveness_probe(ETH, request=_probe_sender(FIXED_INFO, posts))
    assert posts
    assert not list(lock_root.parent.rglob("identity-*"))
