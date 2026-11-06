"""Device-free tests of scripts/jtag_load.py: the safety gates of a volatile JTAG load and the power cycle.

The esp-jtag link, the DeviceLock and the REST client are fakes; the gates under test are the wrapper's own.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import jtag_load as jl  # noqa: E402

UID = "5D2518"
XILINX_7A50T = 0x0362C093


class Events(list):
    pass


class FakeLock:
    def __init__(self, ev, name, ok=True):
        self.ev, self.name, self.ok = ev, name, ok

    def acquire(self, timeout=None):
        self.ev.append(("acquire", self.name))
        return self.ok

    def release(self):
        self.ev.append(("release", self.name))


class FakeEsp:
    """Stands in for esp_recover: records what would have been driven."""

    def __init__(self, ev, live_idcode=XILINX_7A50T, bit_id=XILINX_7A50T, user_id=0xDEAD1541):
        self.ev, self.live_idcode, self.bit_id, self.user_id = ev, live_idcode, bit_id, user_id
        self.loaded = None
        esp = self

        class Ser:
            def close(self_):
                esp.ev.append(("ser_close",))

        class Link:
            ser = Ser()

            def drain(self_):
                pass

        class Jtag:
            link = Link()
            clock_count = 123

            def reset(self_):
                pass

            def sync(self_):
                pass

            def close(self_):
                esp.ev.append(("pins_released",))

        class Recover:
            def __init__(self_, jtag, log=print):
                self_.user_id = None

            def read_id(self_):
                return esp.live_idcode

            def user_read_id(self_):
                esp.ev.append(("user_read_id",))
                self_.user_id = esp.user_id
                return esp.user_id

            def run(self_, bit, binary, sleep=None, idcode_ok=lambda i: None):
                idcode_ok(esp.live_idcode)
                esp.ev.append(("load_fpga_and_upload",))
                esp.loaded = (bit, Path(bit).read_bytes(), binary, Path(binary).read_bytes())
                self_.user_id = esp.user_id

        self.Link, self.Jtag, self.Recover = Link, Jtag, Recover

    def open_link(self, ns):
        self.ev.append(("open", ns.port))
        return self.Link()

    def open_jtag(self, link, ns):
        return self.Jtag()

    def bit_idcode(self, path):
        return self.bit_id

    @staticmethod
    def idcode_matches(a, b):
        return (a & 0x0FFFFFFF) == (b & 0x0FFFFFFF)


class FakeClient:
    """get_info answers from a script: a list of dicts / None (unreachable), last one repeats."""

    def __init__(self, ev, script):
        self.ev, self.script = ev, list(script)

    def __call__(self, host, timeout=5.0):
        return self

    def get_info(self):
        self.ev.append(("get_info",))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if item is None:
            raise ConnectionError("down")
        return item


def info(hash_="abc123def", uid=UID, product="Ultimate 64-II"):
    return {"unique_id": uid, "product": product, "git_commit_hash": hash_}


@pytest.fixture
def env(tmp_path, monkeypatch):
    ev = Events()
    port = tmp_path / "cu.fake"
    port.write_text("")
    monkeypatch.setattr(jl, "ADAPTERS", {UID: {"port": str(port), "lock": "adapter", "baud": "921600"}})
    monkeypatch.setattr(jl, "normalize_device_host",
                        lambda h: "uid-5d2518" if h == "c64u" else h)
    monkeypatch.setattr(jl.time, "sleep", lambda s: None)
    bit = tmp_path / "x.bit"
    app = tmp_path / "x.bin"
    bit.write_bytes(b"BIT" * 100)
    app.write_bytes(b"APP" * 100)
    return ev, bit, app, jl.sha256(bit), jl.sha256(app), tmp_path


def locks(ev, dev_ok=True, ad_ok=True):
    def factory(name, allow_nested=False):
        return FakeLock(ev, "device" if name == "c64u" else name, dev_ok if name == "c64u" else ad_ok)
    return factory


def load(env_, esp, client, **kw):
    ev, bit, app, bsha, asha, tmp = env_
    args = dict(bit_sha256=bsha, app_sha256=asha, expect_git_hash="abc123def", net_timeout=5, esp=esp,
                lock_factory=locks(ev), client_factory=client, log=lambda m: None)
    args.update(kw)
    return jl.jtag_load("c64u", bit, app, **args)


def test_success_order_and_record(env):
    ev, bit, app, bsha, asha, tmp = env
    esp = FakeEsp(ev)
    rec_path = tmp / "loads.jsonl"
    rec = load(env, esp, FakeClient(ev, [info("old"), None, info("abc123def")]), record_path=str(rec_path))
    assert rec["result"] == "ok" and rec["user_id"] == "dead1541"
    names = [e[0] + (":" + e[1] if len(e) > 1 else "") for e in ev if e[0] != "get_info"]
    assert names == ["acquire:device", "acquire:adapter", "open:" + str(tmp / "cu.fake"), "load_fpga_and_upload",
                     "pins_released", "ser_close", "release:adapter", "release:device"]
    line = json.loads(rec_path.read_text().splitlines()[-1])
    assert line["result"] == "ok" and line["bin_sha256"] == asha


def test_loads_the_pinned_copy_not_the_source(env):
    ev, bit, app, bsha, asha, tmp = env
    esp = FakeEsp(ev)
    load(env, esp, FakeClient(ev, [info()]))
    lbit, bitdata, lapp, appdata = esp.loaded
    assert Path(lbit).parent != bit.parent and bitdata == bit.read_bytes() and appdata == app.read_bytes()


def test_hash_mismatch_refuses_before_any_lock(env):
    ev, bit, app, bsha, asha, tmp = env
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]), app_sha256="0" * 64)
    assert e.value.stage == "hash" and ev == []


def test_missing_hash_refuses(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]), bit_sha256="")
    assert e.value.stage == "hash"


def test_bit_for_another_fpga_refuses(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev, bit_id=0x02D120DD), FakeClient(ev, [info()]))
    assert e.value.stage == "bit" and ev == []


def test_unwired_idcode_stops_before_fpga(env):
    ev = env[0]
    esp = FakeEsp(ev, live_idcode=0)
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, esp, FakeClient(ev, [info()]))
    assert e.value.stage == "idcode" and esp.loaded is None
    assert ("pins_released",) in ev and ev[-1] == ("release", "device")


def test_wrong_device_identity_refuses_before_open(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info(uid="601A96", product="Ultimate 64 Elite")]))
    assert e.value.stage == "identity" and not any(x[0] == "open" for x in ev)


def test_bad_user_id_fails(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev, user_id=0xFFFFFFFF), FakeClient(ev, [info()]))
    assert e.value.stage == "userid"


def test_wrong_hash_after_load_times_out(env, monkeypatch):
    ev = env[0]
    t = [0.0]
    monkeypatch.setattr(jl.time, "monotonic", lambda: t.__setitem__(0, t[0] + 1) or t[0])
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info("someother")]))
    assert e.value.stage == "network"


def test_busy_device_lock_never_opens_adapter(env):
    ev, bit, app, bsha, asha, tmp = env
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]), lock_factory=locks(ev, dev_ok=False))
    assert e.value.stage == "lock" and not any(x[0] == "open" for x in ev)


def test_busy_adapter_releases_device(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]), lock_factory=locks(ev, ad_ok=False))
    assert e.value.stage == "lock" and ev[-1] == ("release", "device")


def test_unbound_host_refuses(env):
    ev, bit, app, bsha, asha, tmp = env
    with pytest.raises(jl.JtagLoadError) as e:
        jl.jtag_load("10.0.0.1", bit, app, bit_sha256=bsha, app_sha256=asha, esp=FakeEsp(ev))
    assert e.value.stage == "binding"


def test_dry_run_touches_nothing(env):
    ev = env[0]
    rec = load(env, FakeEsp(ev), FakeClient(ev, [info()]), dry_run=True)
    assert rec["result"] == "dry-run ok" and ev == []


def test_probe_reads_user_id_without_loading(env):
    ev = env[0]
    esp = FakeEsp(ev)
    out = jl.read_ids("c64u", user_block=True, esp=esp, lock_factory=locks(ev), log=lambda m: None)
    assert out == {"idcode": "0362c093", "xilinx": True, "user_id": "dead1541"} and esp.loaded is None


def test_idcode_only_skips_user_block(env):
    ev = env[0]
    out = jl.read_ids("c64u", esp=FakeEsp(ev, live_idcode=0), lock_factory=locks(ev), log=lambda m: None)
    assert out == {"idcode": "00000000", "xilinx": False} and ("user_read_id",) not in ev


class FakeBrowser:
    def __init__(self, ev):
        self.ev = ev

    def invoke_task_action(self, cat, item):
        self.ev.append(("menu", cat, item))

    def close(self):
        self.ev.append(("telnet_close",))


def test_power_cycle_menu_then_down_then_up(env):
    ev = env[0]
    client = FakeClient(ev, [info("flashed"), info("flashed"), None, None, info("flashed")])
    rec = jl.power_cycle("c64u", expect_git_hash="flashed", lock_factory=locks(ev), client_factory=client,
                         browser_factory=lambda h: FakeBrowser(ev), sleep=lambda s: None, log=lambda m: None)
    assert rec["result"] == "ok"
    seq = [e for e in ev if e[0] in ("acquire", "menu", "telnet_close", "release")]
    assert seq == [("acquire", "device"), ("menu", "C64 Machine", "Power Cycle"), ("telnet_close",),
                   ("release", "device")]


def test_power_cycle_refuses_other_product(env):
    ev = env[0]
    client = FakeClient(ev, [info(product="Ultimate 64 Elite")])
    with pytest.raises(jl.JtagLoadError):
        jl.power_cycle("c64u", lock_factory=locks(ev), client_factory=client,
                       browser_factory=lambda h: FakeBrowser(ev), log=lambda m: None)
    assert not any(e[0] == "menu" for e in ev)


def test_power_cycle_that_never_drops_fails(env):
    ev = env[0]
    t = [0.0]
    client = FakeClient(ev, [info("flashed")])
    with pytest.raises(jl.JtagLoadError) as e:
        jl.power_cycle("c64u", lock_factory=locks(ev), client_factory=client, down_timeout=5,
                       browser_factory=lambda h: FakeBrowser(ev), sleep=lambda s: None,
                       clock=lambda: t.__setitem__(0, t[0] + 1) or t[0], log=lambda m: None)
    assert e.value.stage == "power"


def test_other_device_answering_after_load_is_an_identity_failure(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [None, info("abc123def", uid="601A96")]))
    assert e.value.stage == "identity"
