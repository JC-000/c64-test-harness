"""#295: the ``/Temp`` leak budget follows the device, not the client.

Before #295 ``_pending_temp_attachments`` and the refusal state were instance
state on ``Ultimate64Client``, while the wedge accumulates per **device**.  So
a fresh client per upload got a fresh budget, two clients on one device spent
one budget each, and N clients swept N times on one lock release.

Owner decision (2026-09-15): a per-device budget, with the FTP cleanup
reaching every harness path transparently on unpatched devices.  What is
pinned here:

* one ledger per normalised host, shared by every client in the process
  (case, trailing dot, scheme, ``:port`` and IP spelling fold together; a
  name and an IP do **not** -- no DNS in the accounting path);
* sweeps stay bounded: budget crossed, ``close()``, lock release -- and a
  lock release sweeps **once per host** however many clients registered;
* a failed pass by a client that leaked blocks attachment-creating calls on
  **every** armed client for that device; bodyless calls, disarmed clients
  and ``U64_TEMP_GC_REQUIRED=0`` are unaffected;
* a failed inherited sweep only warns; no client that leaked nothing writes
  config; the FTP-enable attempt is once per device per process;
* the shared count is zeroed only by a successful sweep;
* post-safe devices never sweep and never block;
* counting is thread-safe.

In-process only: two processes still keep two ledgers (the lock-release drain
is what hands a device on clean).  No device: HTTP and FTP are faked.
"""
from __future__ import annotations

import gc as _pygc
import json
import threading
import urllib.parse
import uuid
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64TempHygieneError,
)
from c64_test_harness.backends.ultimate64_temp_gc import TempGCResult

LEAKY = DeviceCapabilities.from_info({"firmware_version": "1.1.0", "product": "C64 Ultimate"})
FIXED = DeviceCapabilities.from_info({"firmware_version": "3.15", "product": "Ultimate 64"})
PRG = b"\x01\x08x"
REFUSED = "ConnectionRefusedError: [Errno 61] refused"


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


def _device_urlopen():
    """A fake device: records ``(method, host, path)``; writemem POST stores,
    readmem returns what is there, ``/v1/info`` reports leak-prone firmware."""
    ram = bytearray(0x10000)
    wire: list[tuple[str, str, str]] = []
    guard = threading.Lock()

    def _fake(req, timeout=None):
        method = req.get_method()
        parsed = urllib.parse.urlsplit(req.full_url)
        with guard:
            wire.append((method, parsed.hostname or "", parsed.path))
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/v1/info":
            return _Resp(json.dumps({"firmware_version": "1.1.0"}).encode())
        if parsed.path == "/v1/machine:readmem":
            addr, length = int(q["address"], 16), int(q["length"])
            return _Resp(bytes(ram[addr:addr + length]))
        if parsed.path == "/v1/machine:writemem" and method == "POST":
            addr = int(q["address"], 16)
            ram[addr:addr + len(req.data)] = req.data
        return _Resp()

    return MagicMock(side_effect=_fake), wire


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for var in (gc_mod.AUTO_GC_ENV, gc_mod.BUDGET_ENV, gc_mod.REQUIRED_ENV, gc_mod.KEEP_ENV):
        monkeypatch.delenv(var, raising=False)
    reset = getattr(gc_mod, "_reset_temp_ledgers", None)
    if reset is not None:
        reset()
    wire_mock, wire = _device_urlopen()
    with patch("urllib.request.urlopen", wire_mock):
        yield wire
    if reset is not None:
        reset()


@pytest.fixture
def host() -> str:
    return f"dev-{uuid.uuid4().hex[:10]}"


def _client(host: str, caps: DeviceCapabilities = LEAKY, **kwargs) -> Ultimate64Client:
    kwargs.setdefault("write_mem_query_threshold", 128)
    kwargs.setdefault("warn_unlocked", False)
    c = Ultimate64Client(host, **kwargs)
    c._capabilities = caps
    return c


class _FTP:
    """Patches the module-level GC every client reaches; records the host."""

    def __init__(self, *errors: str | None, default: str | None = None) -> None:
        self._errors = list(errors)
        self._default = default
        self.hosts: list[str] = []

    def __call__(self, host, **kwargs):
        self.hosts.append(host)
        error = self._errors.pop(0) if self._errors else self._default
        return TempGCResult(host=host, error=error)

    def __enter__(self):
        self._patch = patch.object(gc_mod, "gc_temp_folder", side_effect=self)
        self._patch.__enter__()
        return self

    def __exit__(self, *exc):
        return self._patch.__exit__(*exc)


def _no_config_writes():
    return patch.object(Ultimate64Client, "set_config_item")


def _lock_held(value: bool):
    return patch(
        "c64_test_harness.backends.device_lock.DeviceLock.held_by_this_process",
        return_value=value,
    )


def _release_lock(host: str, tmp_path) -> None:
    from c64_test_harness.backends.device_lock import DeviceLock

    lock = DeviceLock(host, lock_dir=tmp_path)
    assert lock.acquire(timeout=5.0)
    lock.release()


# --------------------------------------------------------------------------- #
# The three shapes from the issue                                             #
# --------------------------------------------------------------------------- #

def test_a_fresh_client_per_upload_does_not_reset_the_budget(host):
    with _FTP() as ftp:
        for _ in range(3):
            _client(host, temp_gc_budget=3).run_prg(PRG)
            _pygc.collect()
        assert ftp.hosts == [], "must not sweep before the device budget is spent"
        _client(host, temp_gc_budget=3).run_prg(PRG)
    assert ftp.hosts == [host]
    assert _client(host).pending_temp_attachments == 1


def test_two_clients_on_one_host_spend_one_budget(host):
    a = _client(host, temp_gc_budget=3)
    b = _client(host, temp_gc_budget=3)
    with _FTP() as ftp:
        a.run_prg(PRG)
        b.run_prg(PRG)
        a.run_prg(PRG)
        assert ftp.hosts == []
        assert a.pending_temp_attachments == b.pending_temp_attachments == 3
        b.run_prg(PRG)
    assert ftp.hosts == [host]
    assert a.pending_temp_attachments == b.pending_temp_attachments == 1


def test_different_hosts_keep_separate_budgets(host):
    other = host + "-other"
    a1 = _client(host, temp_gc_budget=3)
    b = _client(other, temp_gc_budget=3)
    with _FTP() as ftp:
        for _ in range(3):
            a1.run_prg(PRG)
            b.run_prg(PRG)
        assert ftp.hosts == []
        _client(host, temp_gc_budget=3).run_prg(PRG)
    assert ftp.hosts == [host]
    assert b.pending_temp_attachments == 3
    assert a1.pending_temp_attachments == 1


@pytest.mark.parametrize("spellings", [
    ["10.0.0.7", "10.0.0.7:80", " 10.0.0.7 "],
    ["U64.Lan", "u64.lan.", "http://u64.lan/"],
    ["::1", "[::1]", "0:0:0:0:0:0:0:1"],
], ids=["ipv4-port-space", "name-case-dot-scheme", "ipv6"])
def test_spellings_of_one_host_share_a_budget(spellings):
    with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS in accounting")), \
            _FTP() as ftp:
        for spelling in spellings:
            _client(spelling, temp_gc_budget=3).run_prg(PRG)
        assert ftp.hosts == []
        _client(spellings[0], temp_gc_budget=3).run_prg(PRG)
    assert len(ftp.hosts) == 1


def test_a_name_and_an_ip_are_not_merged_documented_limit():
    """No DNS in the accounting path: a name and the address it resolves to
    keep separate ledgers.  Pinned so the limit is a decision, not an accident."""
    with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")), _FTP() as ftp:
        for _ in range(3):
            _client("localhost", temp_gc_budget=3).run_prg(PRG)
        _client("127.0.0.1", temp_gc_budget=3).run_prg(PRG)
    assert ftp.hosts == []


# --------------------------------------------------------------------------- #
# Bounded, deduplicated sweeps                                                #
# --------------------------------------------------------------------------- #

def test_a_lock_release_sweeps_once_per_host_however_many_clients(host, tmp_path):
    clients = [_client(host) for _ in range(3)]
    bystander = _client(host)          # leaked nothing
    with _FTP() as ftp:
        for c in clients:
            c.run_prg(PRG)
        _release_lock(host, tmp_path)
    assert ftp.hosts == [host]
    assert all(c.pending_temp_attachments == 0 for c in [*clients, bystander])


def test_a_lock_release_with_only_idle_clients_sweeps_once(host, tmp_path):
    """#264's inherited sweep, deduplicated: three armed clients that leaked
    nothing still make one FTP session, not three."""
    idle = [_client(host) for _ in range(3)]
    with _FTP() as ftp:
        _release_lock(host, tmp_path)
    assert ftp.hosts == [host]
    assert len(idle) == 3


def test_close_ordering_one_successful_sweep_covers_both_clients(host):
    a = _client(host)
    b = _client(host)
    with _lock_held(False), _FTP() as ftp:
        a.run_prg(PRG)
        b.run_prg(PRG)
        a.close()
        assert ftp.hosts == [host]
        b.close()   # b's attachment was collected by a's sweep
    assert ftp.hosts == [host]
    assert b.pending_temp_attachments == 0


def test_close_by_a_client_whose_leak_was_not_collected_still_sweeps(host):
    """Positive control for the test above: dedupe must not swallow a real
    leak.  b leaks after a's sweep, so b's close sweeps again."""
    a = _client(host)
    b = _client(host)
    with _lock_held(False), _FTP() as ftp:
        a.run_prg(PRG)
        a.close()
        b.run_prg(PRG)
        b.close()
    assert ftp.hosts == [host, host]


def test_a_successful_sweep_by_any_client_zeroes_the_device(host):
    a = _client(host)
    b = _client(host)                      # leaked nothing
    with _FTP() as ftp:
        a.run_prg(PRG)
        a.run_prg(PRG)
        with _lock_held(True):
            b.close()
    assert ftp.hosts == [host]
    assert a.pending_temp_attachments == 0


def test_the_liveness_probe_reserves_against_the_device_budget(host):
    a = _client(host, temp_gc_budget=3)
    b = _client(host, temp_gc_budget=3)
    order: list[str] = []
    with patch("c64_test_harness.backends.ultimate64_probe.probe_u64",
               return_value=MagicMock(reachable=True, error=None)), _FTP() as ftp:
        a.run_prg(PRG)
        a.run_prg(PRG)
        original = gc_mod.gc_temp_folder.side_effect

        def _gc(h, **kw):
            order.append("gc")
            return original(h, **kw)

        gc_mod.gc_temp_folder.side_effect = _gc
        result = b.liveness_probe()
    assert result.healthy, result
    assert ftp.hosts == [host], "2 pending + 2 reserved > 3: sweep before probing"
    assert b.pending_temp_attachments == 2


# --------------------------------------------------------------------------- #
# Failure semantics                                                           #
# --------------------------------------------------------------------------- #

def test_a_failed_pass_blocks_attachment_calls_on_every_armed_client_of_that_device(host):
    a = _client(host, temp_gc_budget=1)
    b = _client(host, temp_gc_budget=6)
    other = _client(host + "-other", temp_gc_budget=1)
    disarmed = _client(host, temp_hygiene=False)
    with _FTP(default=REFUSED), _no_config_writes():
        a.run_prg(PRG)
        with pytest.raises(Ultimate64TempHygieneError):
            a.run_prg(PRG)                 # a's pass failed: device blocked
        with pytest.raises(Ultimate64TempHygieneError):
            b.run_prg(PRG)                 # b never leaked, same device
        with pytest.raises(Ultimate64TempHygieneError):
            _client(host).run_prg(PRG)     # nor does a fresh client escape it
        b.reset()                          # bodyless calls still go out
        b.read_mem(0x0400, 1)
        disarmed.run_prg(PRG)              # temp_hygiene=False opts out
        other.run_prg(PRG)                 # another device is not blocked


def test_the_required_opt_out_applies_to_the_shared_block(host, monkeypatch):
    a = _client(host, temp_gc_budget=1)
    with _FTP(default=REFUSED), _no_config_writes():
        a.run_prg(PRG)
        with pytest.raises(Ultimate64TempHygieneError):
            a.run_prg(PRG)
        monkeypatch.setenv(gc_mod.REQUIRED_ENV, "0")
        _client(host).run_prg(PRG)


def test_a_successful_sweep_lifts_the_shared_block(host):
    a = _client(host, temp_gc_budget=1)
    b = _client(host)
    with _FTP(REFUSED, REFUSED, None), _no_config_writes():
        a.run_prg(PRG)
        with pytest.raises(Ultimate64TempHygieneError):
            a.run_prg(PRG)
        with _lock_held(True):
            b.close()                      # inherited sweep succeeds
        a.run_prg(PRG)                     # no longer refused


def test_a_failed_sweep_does_not_zero_the_shared_count(host, tmp_path):
    a = _client(host)
    with _FTP(default=REFUSED), _no_config_writes():
        a.run_prg(PRG)
        a.run_prg(PRG)
        _release_lock(host, tmp_path)      # a's drain fails
        assert a.pending_temp_attachments == 2
        idle = _client(host)
        with _lock_held(True):
            idle.close()                   # inherited sweep fails too
    assert _client(host).pending_temp_attachments == 2


def test_the_ftp_enable_is_attempted_once_per_device(host):
    a = _client(host, temp_gc_budget=1)
    with _FTP(default=REFUSED), _no_config_writes() as set_item:
        a.run_prg(PRG)
        with pytest.raises(Ultimate64TempHygieneError):
            a.run_prg(PRG)
        assert set_item.call_count == 1
        b = _client(host, temp_gc_budget=1)
        with pytest.raises(Ultimate64TempHygieneError):
            b.run_prg(PRG)
        b.close()
    assert set_item.call_count == 1


def test_a_second_leaking_client_does_not_repeat_the_ftp_enable(host):
    """The block does not hide a per-client attempt: once a successful sweep
    lifts it, a different client's failing pass still must not write config
    again (one attempt per device per process)."""
    a = _client(host, temp_gc_budget=1)
    b = _client(host, temp_gc_budget=1)
    idle = _client(host)
    with _FTP(REFUSED, REFUSED, None, default=REFUSED), \
            _no_config_writes() as set_item:
        a.run_prg(PRG)
        with pytest.raises(Ultimate64TempHygieneError):
            a.run_prg(PRG)                 # fail, enable, fail: blocked
        assert set_item.call_count == 1
        with _lock_held(True):
            idle.close()                   # a sweep succeeds: block lifted
        b.run_prg(PRG)
        with pytest.raises(Ultimate64TempHygieneError):
            b.run_prg(PRG)                 # b's own pass fails
    assert set_item.call_count == 1


def test_a_client_that_leaked_nothing_writes_no_config_even_with_device_pending(host, tmp_path, caplog):
    leaker = _client(host)
    with _FTP(default=REFUSED), _no_config_writes() as set_item:
        leaker.run_prg(PRG)
        del leaker
        _pygc.collect()
        idle = _client(host)
        with caplog.at_level("WARNING"):
            _release_lock(host, tmp_path)
        set_item.assert_not_called()
        idle.run_prg(PRG)                  # a failed inherited sweep blocks nothing
    assert any("inherited sweep" in r.getMessage() for r in caplog.records)
    assert idle.pending_temp_attachments == 2


def test_post_safe_devices_never_sweep_or_block_across_clients(host, tmp_path):
    with _FTP(default=REFUSED) as ftp, _no_config_writes() as set_item:
        for _ in range(5):
            _client(host, FIXED, temp_gc_budget=1).run_prg(PRG)
        c = _client(host, FIXED, temp_gc_budget=1)
        c.run_prg(PRG)
        c.close()
        _release_lock(host, tmp_path)
    assert ftp.hosts == []
    set_item.assert_not_called()


def test_a_disarmed_clients_attachments_still_count_for_the_device(host):
    """``temp_hygiene=False`` disarms that client's sweeping and refusal; the
    attachments it leaves are still on the device, so an armed client of the
    same device collects them when the budget says so."""
    quiet = _client(host, temp_hygiene=False)
    with _FTP() as ftp:
        for _ in range(3):
            quiet.run_prg(PRG)
        assert ftp.hosts == []
        _client(host, temp_gc_budget=3).run_prg(PRG)
    assert ftp.hosts == [host]


# --------------------------------------------------------------------------- #
# Thread safety and the test hook                                             #
# --------------------------------------------------------------------------- #

def test_concurrent_clients_count_every_attachment(host):
    threads, per_thread = 8, 40
    start = threading.Barrier(threads)

    def _work():
        c = _client(host, FIXED)
        start.wait()
        for _ in range(per_thread):
            c.run_prg(PRG)

    workers = [threading.Thread(target=_work) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert _client(host, FIXED).pending_temp_attachments == threads * per_thread


def test_concurrent_budget_crossings_sweep_without_losing_the_bound(host):
    """Every attachment is either collected by a sweep or still pending, and
    no reservation leaves more than the budget pending."""
    threads, per_thread, budget = 6, 20, 4
    seen: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(threads)

    def _work():
        c = _client(host, temp_gc_budget=budget)
        start.wait()
        for _ in range(per_thread):
            c.run_prg(PRG)
            with guard:
                seen.append(c.pending_temp_attachments)

    with _FTP() as ftp:
        workers = [threading.Thread(target=_work) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
    assert max(seen) <= budget, max(seen)
    assert len(ftp.hosts) >= (threads * per_thread) // budget - 1


def test_the_reset_hook_clears_the_registry(host):
    _client(host).run_prg(PRG)
    gc_mod._reset_temp_ledgers()
    assert _client(host).pending_temp_attachments == 0
