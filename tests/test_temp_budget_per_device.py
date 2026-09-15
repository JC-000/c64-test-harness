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


def test_a_leak_whose_clients_were_collected_still_drains_on_release(host, tmp_path, caplog):
    """Release callbacks hold nothing strongly, so before the ledger a client
    that leaked and was garbage-collected before the lock release never
    drained (review note on #406).  The ledger outlives its clients and
    sweeps the device itself when no live client can."""
    leaker = _client(host)
    with _FTP() as ftp:
        leaker.run_prg(PRG)
        leaker.run_prg(PRG)
        del leaker
        _pygc.collect()
        _release_lock(host, tmp_path)
    assert ftp.hosts == [host]
    assert _client(host).pending_temp_attachments == 0


def test_an_orphaned_drain_that_fails_keeps_the_count_blocks_and_writes_no_config(host, tmp_path, caplog):
    leaker = _client(host, temp_gc_budget=6)
    with _FTP(default=REFUSED) as ftp, _no_config_writes() as set_item:
        leaker.run_prg(PRG)
        del leaker
        _pygc.collect()
        with caplog.at_level("WARNING"):
            _release_lock(host, tmp_path)
        assert ftp.hosts == [host]
        set_item.assert_not_called()
        later = _client(host)
        assert later.pending_temp_attachments == 1
        with pytest.raises(Ultimate64TempHygieneError):
            later.run_prg(PRG)
    assert any("no live client" in r.getMessage() for r in caplog.records)


def test_an_orphaned_leak_from_a_disarmed_or_post_safe_client_is_not_swept(host, tmp_path):
    fixed = _client(host, FIXED)
    quiet = _client(host + "-q", temp_hygiene=False)
    with _FTP() as ftp:
        fixed.run_prg(PRG)
        quiet.run_prg(PRG)
        del fixed, quiet
        _pygc.collect()
        _release_lock(host, tmp_path)
        _release_lock(host + "-q", tmp_path)
    assert ftp.hosts == []


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


# --------------------------------------------------------------------------- #
# Who may write config (reviewer-6 round 1, #263)                             #
# --------------------------------------------------------------------------- #

def test_a_client_that_leaked_nothing_writes_no_config_on_the_budget_path(host):
    """The budget gate fires on the **device's** count, so an armed client
    whose own share is zero can be the one that runs the pass.  It sweeps,
    and it blocks on failure -- but the FTP-enable write belongs only to a
    client holding an uncollected leak of its own (owner ruling on #263)."""
    leaker = _client(host, temp_gc_budget=3)
    bystander = _client(host, temp_gc_budget=3)
    with _FTP(default=REFUSED) as ftp, _no_config_writes() as set_item:
        for _ in range(3):
            leaker.run_prg(PRG)
        assert bystander._own_pending_temp_attachments() == 0
        with pytest.raises(Ultimate64TempHygieneError):
            bystander.run_prg(PRG)     # crosses the device budget; pass fails
        assert ftp.hosts == [host], "it must still sweep"
        set_item.assert_not_called()


def test_a_disarmed_clients_leak_does_not_make_an_armed_client_write_config(host):
    """Every pending attachment here was counted by a ``temp_hygiene=False``
    client, so no armed client has leaked anything -- and none may write
    ``Network Settings > FTP File Service`` on the strength of it."""
    quiet = _client(host, temp_hygiene=False)
    armed = _client(host, temp_gc_budget=3)
    with _FTP(default=REFUSED) as ftp, _no_config_writes() as set_item:
        for _ in range(3):
            quiet.run_prg(PRG)
        assert armed._own_pending_temp_attachments() == 0
        with pytest.raises(Ultimate64TempHygieneError):
            armed.run_prg(PRG)
        assert ftp.hosts == [host]
        set_item.assert_not_called()


# --------------------------------------------------------------------------- #
# A reservation is in flight until its request returns                        #
# --------------------------------------------------------------------------- #

def test_a_sweep_between_a_reservation_and_its_send_does_not_lose_the_attachment(host):
    """A sweep cannot collect an attachment whose request is still being
    sent.  Counting before the send (so the budget gate is atomic) must
    therefore not let a concurrent sweep zero a count the attachment is
    about to land into: the ledger carries in-flight reservations across."""
    a = _client(host)
    b = _client(host)
    in_send = threading.Event()
    may_send = threading.Event()
    real = Ultimate64Client._request_uncounted

    def _blocking(self, method, path, **kwargs):
        if self is a and method == "POST":
            in_send.set()
            assert may_send.wait(5), "test deadlock"
        return real(self, method, path, **kwargs)

    with _FTP() as ftp, patch.object(Ultimate64Client, "_request_uncounted", _blocking):
        sender = threading.Thread(target=lambda: a.run_prg(PRG))
        sender.start()
        try:
            assert in_send.wait(5), "a never reached its send"
            with _lock_held(True):
                b.close()              # inherited sweep succeeds mid-flight
            assert ftp.hosts == [host]
        finally:
            may_send.set()
            sender.join(5)
    assert _client(host).pending_temp_attachments == 1, \
        "a's POST landed after the sweep, so the device holds one"


def test_a_sweep_during_a_probe_refunds_what_the_probe_never_sent(host):
    """The carry is per reservation, not a licence to over-count: what the
    probe never sent is still refunded, even though a sweep bumped the
    generation underneath it."""
    c = _client(host)
    other = _client(host)

    def _probe_run(*args, **kwargs):
        with _lock_held(True):
            other.close()              # a sweep lands mid-reservation
        return MagicMock(healthy=True, failure=None)

    with _FTP() as ftp, patch(
        "c64_test_harness.backends.ultimate64_probe.liveness_probe",
        side_effect=_probe_run,
    ):
        c.liveness_probe()
    assert ftp.hosts == [host]
    assert _client(host).pending_temp_attachments == 0, \
        "the probe sent nothing and the sweep collected the rest"


# --------------------------------------------------------------------------- #
# Ports are part of the device's identity (#434 follow-up)                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("one,other", [
    ("gw.example:8080", "gw.example:8081"),
    ("[::1]:8080", "[::1]:8081"),
], ids=["name-ports", "ipv6-ports"])
def test_a_different_port_is_a_different_device(one, other):
    """``DeviceLock`` keys ports apart, so the ledger must too: two devices
    behind one name would otherwise share a budget, and a failed pass
    against one would refuse attachment-creating requests to the other."""
    from c64_test_harness.backends.device_lock import _sanitize_device_id

    assert gc_mod.temp_ledger_key(one) != gc_mod.temp_ledger_key(other)
    assert _sanitize_device_id(one) != _sanitize_device_id(other)


@pytest.mark.parametrize("spelling,bare", [
    ("gw.example:80", "gw.example"),
    ("10.0.0.7:80", "10.0.0.7"),
    ("[::1]:80", "::1"),
], ids=["name", "ipv4", "ipv6"])
def test_the_default_rest_port_still_folds(spelling, bare):
    """``host:80`` and ``host`` name one device -- 80 is the client's own
    ``port`` default -- so that one port still folds."""
    assert gc_mod.temp_ledger_key(spelling) == gc_mod.temp_ledger_key(bare)


def test_two_ports_on_one_name_do_not_share_a_budget():
    """The behavioural half: ``Ultimate64Client("name:8080")`` is a working
    spelling (``_base`` keeps the port when ``port`` is the default 80), and
    such a client must not spend another port's budget."""
    with _FTP() as ftp:
        for _ in range(3):
            _client("gw.example:8080", temp_gc_budget=3).run_prg(PRG)
        _client("gw.example:8081", temp_gc_budget=3).run_prg(PRG)
    assert ftp.hosts == [], "a different port is a different device"


# --------------------------------------------------------------------------- #
# States no other test reaches (reviewer-6 round 1, surviving mutants)         #
# --------------------------------------------------------------------------- #

def test_an_orphan_drain_does_not_sweep_when_a_refund_left_nothing_pending(host, tmp_path):
    """A fully-refunded reservation leaves ``armed_pending`` set with
    ``pending`` at 0.  The orphan drain must require **both**: with nothing
    pending there is nothing of ours to collect, and a failed sweep would
    block a device on behalf of attachments that were never created."""
    def _fake_probe(*args, **kwargs):
        # A plain function, not a Mock: a Mock records the call, and the
        # recorded arguments include the ``request=`` closure over the
        # client -- which would keep it alive past ``del`` and leave a live
        # client to drain, defeating the orphan path this exercises.
        return MagicMock(healthy=True, failure=None)

    with _FTP() as ftp, patch(
        "c64_test_harness.backends.ultimate64_probe.liveness_probe", _fake_probe
    ):
        c = _client(host)
        c.liveness_probe()             # reserves 2, sends 0, refunds 2
        ledger = c._temp_ledger
        assert ledger.pending == 0 and ledger.armed_pending, \
            "the state this guards is meant to be reachable"
        del c
        _pygc.collect()
        _release_lock(host, tmp_path)
    assert ftp.hosts == []


def test_the_release_drain_prefers_the_client_that_leaked(host, tmp_path, caplog):
    """The ledger ranks an armed client holding its own leak ahead of an
    armed idle one, whatever order they were built in.  The two take
    different paths, so which one ran is observable: the leaker's pass makes
    the FTP-enable attempt and blocks the device, an inherited sweep does
    neither."""
    idle = _client(host)               # built first, so insertion order differs
    leaker = _client(host)
    with _FTP(default=REFUSED) as ftp, _no_config_writes() as set_item:
        leaker.run_prg(PRG)
        with caplog.at_level("WARNING"):
            _release_lock(host, tmp_path)
        # The leaking path sweeps, enables FTP File Service, then retries:
        # two sessions.  An inherited sweep is one session and no config
        # write, so the count discriminates as well as the mock does.
        assert ftp.hosts == [host, host]
        set_item.assert_called_once()
        assert leaker._temp_hygiene_blocked is not None
        assert not any("inherited sweep" in r.getMessage() for r in caplog.records)
    assert idle.pending_temp_attachments == 1
