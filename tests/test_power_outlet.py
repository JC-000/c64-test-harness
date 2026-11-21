"""Device-free tests of scripts/power_outlet.py: switched-outlet cold power cycle (mock plug, mock REST, mock lock)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import jtag_load as jl  # noqa: E402
import power_outlet as po  # noqa: E402


class Plug:
    def __init__(self, ev, stuck=None):
        self.ev, self.on, self.stuck, self.was_off = ev, True, stuck, False

    def set(self, on):
        self.ev.append(("outlet", "ON" if on else "OFF"))
        if self.stuck is None:
            self.on = on
            self.was_off = self.was_off or not on

    def is_on(self):
        return self.on if self.stuck is None else self.stuck


class Lock:
    def __init__(self, ev, name, allow_nested=False):
        self.ev = ev

    def acquire(self, timeout=None):
        self.ev.append(("acquire",))
        return True

    def release(self):
        self.ev.append(("release",))


class Client:
    """REST answers from a script; poweroff() is recorded. The plug state gates REST: off plug => no answer."""

    def __init__(self, ev, plug, before, after):
        self.ev, self.plug, self.before, self.after = ev, plug, before, after
        self.cycled = False

    def __call__(self, host, timeout=5.0):
        return self

    def get_info(self):
        self.cycled = self.plug.was_off
        if not self.plug.on:
            raise ConnectionError
        if any(e == ("poweroff",) for e in self.ev) and not self.cycled:
            raise ConnectionError               # machine off after the graceful poweroff
        return self.after if self.cycled else self.before

    def poweroff(self, confirm_irrecoverable=False):
        assert confirm_irrecoverable
        self.ev.append(("poweroff",))


def info(h, uid="ABC123", product="Ultimate 64-II"):
    return {"unique_id": uid, "product": product, "git_commit_hash": h}


BIND = {"outlet": "http://plug", "outlet_kind": "shelly-gen2"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: {"c64u": "uid-abc123", "u64e": "uid-def456"}.get(h, h))
    return []


def cycle(ev, plug, client, host="c64u", **kw):
    sleeps = []
    args = dict(binding=BIND, outlet_factory=lambda b: plug, lock_factory=lambda n, allow_nested=False: Lock(ev, n),
                client_factory=client, sleep=lambda s: sleeps.append(s), log=lambda m: None)
    args.update(kw)
    rec = po.outlet_cycle(host, **args)
    return rec, sleeps


def u64e(h):
    return info(h, uid="DEF456", product="Ultimate 64 Elite")


def test_u64e_sequence_graceful_off_wait_on_back(env):
    ev = env
    plug = Plug(ev)
    rec, sleeps = cycle(ev, plug, Client(ev, plug, u64e("f1a5bed00"), u64e("f1a5bed00")), host="u64e",
                        expect_git_hash="f1a5bed00")
    assert rec["result"] == "ok"
    assert [e for e in ev if e[0] != "get_info"] == [("acquire",), ("poweroff",), ("outlet", "OFF"), ("outlet", "ON"),
                                                     ("release",)]
    # lane scripts read these keys: "graceful" is the resolved decision, as before; the request is under "requested"
    assert rec["graceful"] is True and rec["c64u_poweroff"] is False
    assert rec["requested"] == {"graceful": None, "c64u_poweroff": False}
    assert 10.0 in sleeps and plug.on


def test_c64u_is_never_powered_off_by_default(env):
    """CLAUDE.md rule 7: poweroff() is unreachable on the C64U. The outlet is just cut and restored."""
    ev = env
    plug = Plug(ev)
    rec, _ = cycle(ev, plug, Client(ev, plug, info("f1a5bed00"), info("f1a5bed00")))
    assert [e for e in ev if e[0] != "get_info"] == [("acquire",), ("outlet", "OFF"), ("outlet", "ON"), ("release",)]
    assert rec["result"] == "ok" and rec["poweroff"] is False
    assert rec["graceful"] is False and rec["c64u_poweroff"] is False


def test_c64u_poweroff_needs_its_own_flag(env):
    ev = env
    plug = Plug(ev)
    with pytest.raises(po.OutletError):                 # graceful=True alone is refused, before switching
        cycle(ev, plug, Client(ev, plug, info("f1a5bed00"), info("f1a5bed00")), graceful=True)
    assert ev == [("acquire",), ("release",)]
    ev.clear()
    rec, _ = cycle(ev, plug, Client(ev, plug, info("f1a5bed00"), info("f1a5bed00")), c64u_poweroff=True)
    assert ("poweroff",) in ev and rec["poweroff"] is True
    assert rec["graceful"] is True and rec["c64u_poweroff"] is True


def test_the_c64u_flag_is_refused_on_another_device(env):
    ev = env
    plug = Plug(ev)
    with pytest.raises(po.OutletError):
        cycle(ev, plug, Client(ev, plug, u64e("x"), u64e("x")), host="u64e", c64u_poweroff=True)
    assert ev == [("acquire",), ("release",)]


# -- the poweroff decision is the /v1/info product, read under the lock, never a device id ------------------------

U64_LINE = ["Ultimate 64", "Ultimate 64 Elite"]
C64U_CLASS = ["C64 Ultimate", "Ultimate 64-II"]
OTHER = ["C64 Ultimate (MK1)", "Ultimate 99", None, ["C64 Ultimate"]]   # None: no product field; a list: malformed
POWEROFF, CUT, REFUSE = "poweroff", "cut", "refuse"
POLICY = (
    [(p, None, False, POWEROFF) for p in U64_LINE]
    + [(p, True, False, POWEROFF) for p in U64_LINE]
    + [(p, False, False, CUT) for p in U64_LINE]
    + [(p, g, True, REFUSE) for p in U64_LINE for g in (None, True)]
    + [(p, None, False, CUT) for p in C64U_CLASS]
    + [(p, False, False, CUT) for p in C64U_CLASS]
    + [(p, True, False, REFUSE) for p in C64U_CLASS]
    + [(p, g, True, POWEROFF) for p in C64U_CLASS for g in (None, True)]
    + [(p, None, False, CUT) for p in OTHER]
    + [(p, False, False, CUT) for p in OTHER]
    + [(p, True, False, REFUSE) for p in OTHER]
    + [(p, g, True, REFUSE) for p in OTHER for g in (None, True)]
)


def _info(h, product):
    i = info(h, product=product)
    if product is None:
        del i["product"]
    return i


@pytest.mark.parametrize("product, graceful, c64u_poweroff, want", POLICY)
def test_the_poweroff_decision_follows_the_product(env, product, graceful, c64u_poweroff, want):
    """The same unique_id under every product: only the product decides, and a refusal comes before any switching."""
    ev = env
    plug = Plug(ev)
    client = Client(ev, plug, _info("f1a5bed00", product), _info("f1a5bed00", product))
    if want == REFUSE:
        with pytest.raises(po.OutletError):
            cycle(ev, plug, client, graceful=graceful, c64u_poweroff=c64u_poweroff)
        assert ev == [("acquire",), ("release",)] and plug.on and not plug.was_off
        return
    head = [("acquire",), ("poweroff",)] if want == POWEROFF else [("acquire",)]
    if not isinstance(product, str):
        # cut, but nothing vouches for the board that answers afterwards: no product (or a malformed one) is not
        # accepted after the cycle just because it was reported before it
        with pytest.raises(po.OutletError):
            cycle(ev, plug, client, graceful=graceful, c64u_poweroff=c64u_poweroff)
        assert ev == head + [("outlet", "OFF"), ("outlet", "ON"), ("release",)] and plug.on
        return
    rec, _ = cycle(ev, plug, client, graceful=graceful, c64u_poweroff=c64u_poweroff)
    assert ev == head + [("outlet", "OFF"), ("outlet", "ON"), ("release",)]
    assert rec["result"] == "ok" and rec["poweroff"] is (want == POWEROFF) and plug.on
    assert rec["graceful"] is (want == POWEROFF)
    assert rec["c64u_poweroff"] is (want == POWEROFF and product in C64U_CLASS)
    assert rec["requested"] == {"graceful": graceful, "c64u_poweroff": c64u_poweroff}


@pytest.mark.parametrize("before", ["C64 Ultimate", "C64 Ultimate (MK1)"])
def test_a_different_product_after_the_cycle_is_an_identity_failure(env, before):
    """The bound unique_id answers after the cycle as a product that is neither known nor the one it reported before."""
    ev = env
    plug = Plug(ev)
    with pytest.raises(po.OutletError):
        cycle(ev, plug, Client(ev, plug, _info("f1a5bed00", before), _info("f1a5bed00", "Ultimate 99")))
    assert ev == [("acquire",), ("outlet", "OFF"), ("outlet", "ON"), ("release",)] and plug.on


class Down(Client):
    """REST does not answer before the cycle (a wedged or dead app); it answers after it."""

    def get_info(self):
        self.cycled = self.plug.was_off
        if not self.plug.on or not self.cycled:
            raise ConnectionError
        return self.after


@pytest.mark.parametrize("graceful, c64u_poweroff, want", [
    (None, False, CUT), (False, False, CUT), (True, False, REFUSE), (None, True, REFUSE), (True, True, REFUSE),
])
def test_rest_down_cuts_by_default_and_refuses_an_explicit_poweroff(env, graceful, c64u_poweroff, want):
    """With REST down nothing can be sent and the product is unknown: an explicit poweroff request cannot be honoured."""
    ev = env
    plug = Plug(ev)
    client = Down(ev, plug, None, info("f1a5bed00"))
    if want == REFUSE:
        with pytest.raises(po.OutletError):
            cycle(ev, plug, client, graceful=graceful, c64u_poweroff=c64u_poweroff)
        assert ev == [("acquire",), ("release",)] and plug.on and not plug.was_off
        return
    rec, _ = cycle(ev, plug, client, graceful=graceful, c64u_poweroff=c64u_poweroff)
    assert ev == [("acquire",), ("outlet", "OFF"), ("outlet", "ON"), ("release",)]
    assert rec["result"] == "ok" and rec["poweroff"] is False
    assert rec["graceful"] is False and rec["c64u_poweroff"] is False


@pytest.mark.parametrize("argv, want", [
    (["cycle", "c64u"], {"graceful": None, "c64u_poweroff": False}),
    (["cycle", "c64u", "--no-graceful"], {"graceful": False, "c64u_poweroff": False}),
    (["cycle", "c64u", "--c64u-poweroff"], {"graceful": None, "c64u_poweroff": True}),
])
def test_cli_passes_the_c64u_flag_only_when_given(monkeypatch, argv, want):
    got = {}
    monkeypatch.setattr(po, "outlet_cycle", lambda host, **kw: got.update(kw) or {"result": "ok"})
    assert po.main(argv) == 0
    assert {k: got[k] for k in want} == want


def test_off_time_minimum(env):
    with pytest.raises(po.OutletError):
        cycle(env, Plug(env), None, off_s=3)
    assert env == []


def test_outlet_stuck_on_is_an_error_and_never_left_off(env):
    ev = env
    plug = Plug(ev, stuck=True)
    with pytest.raises(po.OutletError):
        cycle(ev, plug, Client(ev, plug, info("f1a5bed00"), info("f1a5bed00")))
    assert ev[-2] == ("outlet", "ON") and ev[-1] == ("release",)


def test_dead_app_still_gets_cut(env):
    ev = env
    plug = Plug(ev)
    rec, _ = cycle(ev, plug, Down(ev, plug, None, u64e("f1a5bed00")), host="u64e")   # a U64E would get a poweroff
    assert ("poweroff",) not in ev and ("outlet", "OFF") in ev and rec["result"] == "ok"


def test_wrong_device_behind_the_plug_refuses_before_cutting(env):
    ev = env
    plug = Plug(ev)
    with pytest.raises(po.OutletError):
        cycle(ev, plug, Client(ev, plug, info("x", uid="DEF456"), None))
    assert ("outlet", "OFF") not in ev


def test_dry_run_reads_the_plug_only(env):
    ev = env
    rec, _ = cycle(ev, Plug(ev), None, dry_run=True, graceful=True)
    assert rec["result"] == "dry-run ok" and rec["outlet_on"] is True and ev == []
    # the request is recorded, but not as the decision: that needs /v1/info, which a dry run never reads
    assert rec["requested"] == {"graceful": True, "c64u_poweroff": False}
    assert rec["graceful"] is None and rec["c64u_poweroff"] is None


def test_binding_from_devices_toml(tmp_path):
    p = tmp_path / "devices.toml"
    p.write_text('[devices.abc123]\nhosts = ["192.0.2.26"]\noutlet = "http://192.0.2.200"\noutlet_kind = "tasmota"\n')
    assert po.outlet_binding("ABC123", p) == {"outlet": "http://192.0.2.200", "outlet_kind": "tasmota"}
    with pytest.raises(po.OutletError):
        po.outlet_binding("DEF456", p)


def test_drivers_build_the_documented_urls():
    urls = []
    s = po.ShellyGen2("http://p/", fetch=lambda u: urls.append(u) or {"output": False})
    s.set(False)
    assert s.is_on() is False
    t = po.Tasmota("http://q", fetch=lambda u: urls.append(u) or {"POWER": "ON"})
    t.set(True)
    assert t.is_on() is True
    assert urls == ["http://p/rpc/Switch.Set?id=0&on=false", "http://p/rpc/Switch.GetStatus?id=0",
                    "http://q/cm?cmnd=Power%20On", "http://q/cm?cmnd=Power"]


# -- switching needs the device's lock; reading the plug does not ----------------------------------------------

HOST = "192.0.2.1"      # TEST-NET-1; urlopen is replaced, so nothing is sent anywhere


@pytest.fixture
def wire(monkeypatch):
    """Record every plug URL the real classes would open."""
    import io
    urls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout=None):
        urls.append(url)
        return Resp(b'{"output": true, "POWER": "ON"}')
    monkeypatch.setattr(po.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("C64U_LOCK_PID", raising=False)
    return urls


def test_switching_refuses_without_the_lock(wire):
    for plug in (po.ShellyGen2("http://p", host=HOST), po.Tasmota("http://q", host=HOST), po.ShellyGen2("http://p")):
        with pytest.raises(jl.DeviceLockNotHeldError):
            plug.set(False)
    assert wire == []


def test_switching_under_the_lock(wire, tmp_path):
    from c64_test_harness.backends.device_lock import DeviceLock as RealLock
    lock = RealLock(HOST, lock_dir=tmp_path / "locks", heartbeat_interval=None)
    assert lock.acquire(timeout=5)
    try:
        po.ShellyGen2("http://p", host=HOST).set(False)
        po.Tasmota("http://q", host=HOST).set(True)
    finally:
        lock.release()
    assert wire == ["http://p/rpc/Switch.Set?id=0&on=false", "http://q/cm?cmnd=Power%20On"]


def test_status_and_dry_run_read_the_plug_and_never_reach_the_switch(wire, monkeypatch, capsys):
    def no_switch(*a, **k):
        raise AssertionError("the read path reached _switch")
    monkeypatch.setattr(po, "_switch", no_switch)
    monkeypatch.setattr(po, "outlet_binding", lambda uid, path=None: dict(BIND))
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: "uid-abc123")
    assert po.main(["status", "c64u"]) == 0
    rec = po.outlet_cycle("c64u", binding=dict(BIND), dry_run=True, log=lambda m: None)
    assert rec["outlet_on"] is True and rec["result"] == "dry-run ok"
    assert wire == ["http://plug/rpc/Switch.GetStatus?id=0"] * 2


@pytest.mark.parametrize("url", [
    po.ShellyGen2("http://p").switch_url(True), po.ShellyGen2("http://p").switch_url(False),
    po.Tasmota("http://q").switch_url(True), po.Tasmota("http://q").switch_url(False),
    "http://q/cm?cmnd=Power%20Toggle", "http://q/cm?cmnd=Power&cmnd=Power%20Off", "http://q/cm?cmnd=Backlog%20Power%20Off",
    "http://p/rpc/Switch.Toggle?id=0", "http://p/rpc/Switch.GetStatus?id=0&on=false", "http://p/",
])
def test_the_read_path_refuses_anything_but_a_status_url(wire, url):
    """_read_plug runs without the lock, and a Shelly Gen2 Switch.Set is a plain GET: it must not send one."""
    with pytest.raises(po.OutletError):
        po._read_plug(url, 1.0)
    assert wire == []


@pytest.mark.parametrize("url", ["http://p/rpc/Switch.GetStatus?id=0", "http://q/cm?cmnd=Power"])
def test_the_read_path_reads_a_status_url(wire, url):
    po._read_plug(url, 1.0)
    assert wire == [url]


def test_no_graceful_and_the_c64u_poweroff_flag_contradict_and_are_refused(env, monkeypatch):
    """--no-graceful says "never power off", --c64u-poweroff says "power off": neither wins, the run is refused."""
    ev = env
    plug = Plug(ev)
    with pytest.raises(po.OutletError):
        cycle(ev, plug, Client(ev, plug, info("f1a5bed00"), info("f1a5bed00")), graceful=False, c64u_poweroff=True)
    assert ev == []
    called = []
    monkeypatch.setattr(po, "outlet_cycle", lambda host, **kw: called.append(kw) or {"result": "ok"})
    with pytest.raises(SystemExit) as e:
        po.main(["cycle", "c64u", "--no-graceful", "--c64u-poweroff"])
    assert e.value.code == 2 and called == []
