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


def info(h, uid="5D2518", product="Ultimate 64-II"):
    return {"unique_id": uid, "product": product, "git_commit_hash": h}


BIND = {"outlet": "http://plug", "outlet_kind": "shelly-gen2"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: {"c64u": "uid-5d2518", "u64e": "uid-601a96"}.get(h, h))
    return []


def cycle(ev, plug, client, host="c64u", **kw):
    sleeps = []
    args = dict(binding=BIND, outlet_factory=lambda b: plug, lock_factory=lambda n, allow_nested=False: Lock(ev, n),
                client_factory=client, sleep=lambda s: sleeps.append(s), log=lambda m: None)
    args.update(kw)
    rec = po.outlet_cycle(host, **args)
    return rec, sleeps


def test_sequence_graceful_off_wait_on_back(env):
    ev = env
    plug = Plug(ev)
    rec, sleeps = cycle(ev, plug, Client(ev, plug, info("f1a5bed00"), info("f1a5bed00")), expect_git_hash="f1a5bed00")
    assert rec["result"] == "ok"
    assert [e for e in ev if e[0] != "get_info"] == [("acquire",), ("poweroff",), ("outlet", "OFF"), ("outlet", "ON"),
                                                     ("release",)]
    assert 10.0 in sleeps and plug.on


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

    class Dead(Client):
        def get_info(self):
            self.cycled = self.plug.was_off
            if not self.plug.on:
                raise ConnectionError
            if not self.cycled:
                raise ConnectionError           # wedged before the cycle
            return self.after
    rec, _ = cycle(ev, plug, Dead(ev, plug, None, info("f1a5bed00")))
    assert ("poweroff",) not in ev and ("outlet", "OFF") in ev and rec["result"] == "ok"


def test_c64u_not_back_names_the_power_on_mode(env):
    ev = env
    plug = Plug(ev)
    t = [0.0]

    class StaysOff(Client):
        def get_info(self):
            if any(e == ("poweroff",) for e in self.ev):
                raise ConnectionError
            return self.before
    with pytest.raises(po.OutletError) as e:
        cycle(ev, plug, StaysOff(ev, plug, info("f1a5bed00"), None), up_timeout=5,
              clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    assert "power-on mode is ON" in str(e.value) and plug.on


def test_wrong_device_behind_the_plug_refuses_before_cutting(env):
    ev = env
    plug = Plug(ev)
    with pytest.raises(po.OutletError):
        cycle(ev, plug, Client(ev, plug, info("x", uid="601A96"), None))
    assert ("outlet", "OFF") not in ev


def test_dry_run_reads_the_plug_only(env):
    ev = env
    rec, _ = cycle(ev, Plug(ev), None, dry_run=True)
    assert rec["result"] == "dry-run ok" and rec["outlet_on"] is True and ev == []


def test_binding_from_devices_toml(tmp_path):
    p = tmp_path / "devices.toml"
    p.write_text('[devices.5d2518]\nhosts = ["192.0.2.26"]\noutlet = "http://192.0.2.200"\noutlet_kind = "tasmota"\n')
    assert po.outlet_binding("5D2518", p) == {"outlet": "http://192.0.2.200", "outlet_kind": "tasmota"}
    with pytest.raises(po.OutletError):
        po.outlet_binding("601A96", p)


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
