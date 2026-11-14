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

    def __init__(self, ev, live_idcode=XILINX_7A50T, bit_id=XILINX_7A50T, user_id=0xDEAD1541, tdi_ok=True,
                 ident="esp-jtag 0.1 esp32"):
        self.ev, self.live_idcode, self.bit_id, self.user_id = ev, live_idcode, bit_id, user_id
        self.tdi_ok, self.ident = tdi_ok, ident
        self.steps = []                                  # the cold/warm load steps, kept out of ev
        self.loaded = None
        esp = self

        class Ser:
            def close(self_):
                esp.ev.append(("ser_close",))

        class Link:
            ser = Ser()
            info = {"ident": ident}

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

            def write_ir(self_, v, n):
                self_.ir = v

            def change_state(self_, name):
                pass

            def go_idle(self_):
                pass

            def shift_register(self_, bits):
                assert self_.ir == 0x3F
                return [0] + bits[:-1] if esp.tdi_ok else [0] * len(bits)

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
                esp.steps.append(("warm_run",))
                esp.ev.append(("load_fpga_and_upload",))
                esp.loaded = (bit, Path(bit).read_bytes(), binary, Path(binary).read_bytes())
                self_.user_id = esp.user_id

            # the cold path (default): jtag_load drives these steps itself
            def load_fpga(self_, bit):
                esp.steps.append(("load_fpga",))
                self_._bit = bit

            def user_set_outputs(self_, value):
                esp.steps.append(("outputs", value))

            def user_upload(self_, binary, addr):
                esp.steps.append(("upload", addr))
                esp.ev.append(("load_fpga_and_upload",))
                esp.loaded = (self_._bit, Path(self_._bit).read_bytes(), binary, Path(binary).read_bytes())

            def user_write_memory(self_, addr, buf):
                esp.steps.append(("write", addr, bytes(buf)))

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
                     "user_read_id", "pins_released", "ser_close", "release:adapter", "release:device"]
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
        jl.jtag_load("10.0.0.1", bit, app, bit_sha256=bsha, app_sha256=asha, expect_git_hash="abc123def",
                     esp=FakeEsp(ev))
    assert e.value.stage == "binding"


def test_dry_run_touches_nothing(env):
    ev = env[0]
    rec = load(env, FakeEsp(ev), FakeClient(ev, [info()]), dry_run=True)
    assert rec["result"] == "dry-run ok" and ev == []


def test_probe_reads_user_id_without_loading(env):
    ev = env[0]
    esp = FakeEsp(ev)
    out = jl.read_ids("c64u", user_block=True, esp=esp, lock_factory=locks(ev), log=lambda m: None)
    assert out == {"idcode": "0362c093", "xilinx": True, "tdi_ok": True, "user_id": "dead1541"}
    assert esp.loaded is None


def test_idcode_only_skips_user_block(env):
    ev = env[0]
    out = jl.read_ids("c64u", esp=FakeEsp(ev, live_idcode=0), lock_factory=locks(ev), log=lambda m: None)
    assert out == {"idcode": "00000000", "xilinx": False} and ("user_read_id",) not in ev


class FakeBrowser:
    """Task menu: C64 Machine > [Reset C64, Reboot C64, Reboot (Clr Mem), Power OFF, Power Cycle]."""

    def __init__(self, ev, items=("Reset C64", "Reboot C64", "Reboot (Clr Mem)", "Power OFF", "Power Cycle"),
                 highlight=None):
        self.ev, self.items, self.highlight = ev, list(items), highlight
        outer = self

        class Backend:
            def close(self_):
                outer.ev.append(("socket_close",))
        self.backend = Backend()

    screens = None             # list of screen texts returned in turn (last repeats); None = plain browser
    empty_first = False

    def screen(self):
        if not self.screens:
            return "/ SD USB1 Temp"
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def press_popup_button(self, key):
        self.ev.append(("popup", key))
        if self.screens:
            self.screens = ["/ SD USB1 Temp"]

    def open_task_menu(self):
        if self.empty_first:
            self.empty_first = False
            return []
        return ["C64 Machine", "Developer", "Configuration"]

    def rows(self):
        return []

    def choose_overlay_item(self, labels, label):
        self.ev.append(("category", label))

    def wait_for_overlay(self, before):
        return self.items

    def select_framed_entry(self, label):
        return (0, 0, 9, 9), (self.highlight or label) + "      "

    def press(self, key):
        self.ev.append(("press", key))

    def close(self):
        self.ev.append(("telnet_close",))


def test_power_cycle_menu_then_down_then_up(env):
    ev = env[0]
    client = FakeClient(ev, [info("f1a5bed00"), info("f1a5bed00"), None, None, info("f1a5bed00")])
    rec = jl.power_cycle("c64u", expect_git_hash="f1a5bed00", lock_factory=locks(ev), client_factory=client,
                         browser_factory=lambda h: FakeBrowser(ev), sleep=lambda s: None, log=lambda m: None)
    assert rec["result"] == "ok"
    seq = [e for e in ev if e[0] in ("acquire", "category", "press", "socket_close", "telnet_close", "release")]
    assert seq == [("acquire", "device"), ("category", "C64 Machine"), ("press", "ENTER"), ("socket_close",),
                   ("release", "device")]


def test_power_cycle_refuses_other_product(env):
    ev = env[0]
    client = FakeClient(ev, [info(product="Ultimate 64 Elite")])
    with pytest.raises(jl.JtagLoadError):
        jl.power_cycle("c64u", lock_factory=locks(ev), client_factory=client,
                       browser_factory=lambda h: FakeBrowser(ev), log=lambda m: None)
    assert ("press", "ENTER") not in ev


def test_power_cycle_that_never_drops_fails(env):
    ev = env[0]
    t = [0.0]
    client = FakeClient(ev, [info("f1a5bed00")])
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


def test_payload_from_app_round_trip_and_refusals(tmp_path):
    import struct
    body = bytes(range(256)) * 4
    good = tmp_path / "u.app"
    good.write_bytes(struct.pack("<3I", 0x30000, len(body), 0x30000) + body)
    out = tmp_path / "u.bin"
    assert jl.payload_from_app(good, out) == jl.sha256(out) and out.read_bytes() == body
    for hdr in (struct.pack("<3I", 0x0, len(body), 0x234),            # a U64E (Nios) app
                struct.pack("<3I", 0x30000, len(body) - 8, 0x30000)):  # a second record follows
        bad = tmp_path / "bad.app"
        bad.write_bytes(hdr + body)
        with pytest.raises(jl.JtagLoadError):
            jl.payload_from_app(bad, tmp_path / "x.bin")


@pytest.mark.parametrize("browser_kw", [{"highlight": "Power OFF"}, {"items": ("Reset C64", "Power OFF")}])
def test_power_cycle_never_presses_enter_off_target(env, browser_kw):
    ev = env[0]
    client = FakeClient(ev, [info("f1a5bed00")])
    with pytest.raises(jl.JtagLoadError) as e:
        jl.power_cycle("c64u", lock_factory=locks(ev), client_factory=client,
                       browser_factory=lambda h: FakeBrowser(ev, **browser_kw), sleep=lambda s: None,
                       log=lambda m: None)
    assert e.value.stage == "menu" and ("press", "ENTER") not in ev and ("press", "ESC") in ev


def test_loose_tdi_stops_before_fpga(env):
    ev = env[0]
    esp = FakeEsp(ev, tdi_ok=False)
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, esp, FakeClient(ev, [info()]))
    assert e.value.stage == "tdi" and esp.loaded is None


def test_probe_with_loose_tdi_reads_no_user_id(env):
    ev = env[0]
    out = jl.read_ids("c64u", user_block=True, esp=FakeEsp(ev, tdi_ok=False), lock_factory=locks(ev),
                      log=lambda m: None)
    assert out["tdi_ok"] is False and "user_id" not in out and ("user_read_id",) not in ev


def test_port_answering_as_something_else_refuses(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev, ident="sniffer2"), FakeClient(ev, [info()]))
    assert e.value.stage == "adapter"


def test_missing_adapter_port_refuses(env, monkeypatch):
    ev, bit, app, bsha, asha, tmp = env
    monkeypatch.setattr(jl, "ADAPTERS", {UID: {"port": str(tmp / "absent"), "lock": "adapter"}})
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]))
    assert e.value.stage == "adapter" and ev == []


@pytest.mark.parametrize("h", ["", "abc", "zzzzzzzzz", None])
def test_expect_hash_is_required_and_hex(env, h):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]), expect_git_hash=h)
    assert e.value.stage == "hash" and ev == []


def test_lock_shell_holder_is_honoured_and_a_stale_claim_refused(env, monkeypatch):
    import c64_test_harness.backends.device_lock as dl
    ev = env[0]
    monkeypatch.setenv("C64U_LOCK_PID", "4242")
    monkeypatch.setenv("C64U_LOCK_HOST", "c64u")
    monkeypatch.setattr(dl, "device_lock_holder", lambda h: {"pid": 4242})
    _chain(monkeypatch, 4242)                          # the shell is this process's parent
    load(env, FakeEsp(ev), FakeClient(ev, [info()]))
    assert ("acquire", "device") not in ev and ("acquire", "adapter") in ev
    monkeypatch.setattr(dl, "device_lock_holder", lambda h: {"pid": 1})
    with pytest.raises(jl.JtagLoadError) as e:
        load(env, FakeEsp(ev), FakeClient(ev, [info()]))
    assert e.value.stage == "lock"


def test_rehearsal_never_presses_enter_on_the_item(env):
    ev = env[0]
    jl.rehearse_power_cycle("c64u", lock_factory=locks(ev), client_factory=FakeClient(ev, [info()]),
                            browser_factory=lambda h: FakeBrowser(ev))
    assert ("category", "C64 Machine") in ev and ("press", "ENTER") not in ev and ("press", "ESC") in ev


class CycloneTap:
    """One Cyclone V-like device: 10-bit IR, IDCODE after reset, an SLD hub behind USER1/USER0."""
    IDCODE_IR = 0x006

    def __init__(self, idcode=0x02B050DD, hub=0x0008_6E08, nodes=(0x0010_6E00,)):
        self.idcode, self.L = idcode, 10
        self.words = [hub, *nodes]
        self.latched = []          # every IR value that reached Update-IR
        self.reset()

    def reset(self):
        self.ir, self.shifting_ir, self.ptr = self.IDCODE_IR, False, 0
        self.tdi = 0

    def change_state(self, name):
        if name == "shift_ir":
            self.shifting_ir, self.reg = True, 0b01

    def shift_register(self, bits):
        out = []
        for b in bits:
            out.append(self.reg & 1)
            self.reg = (self.reg >> 1) | (b << (self.L - 1))
        return out

    def go_idle(self):
        if self.shifting_ir:
            self.ir = self.reg
            self.latched.append(self.reg)
            self.shifting_ir = False

    def write_ir(self, v, n):
        assert n == self.L
        self.ir = v
        self.latched.append(v)

    def write_dr(self, v, n):
        if self.ir == 0x00E and v == 0:
            self.ptr = 0

    def read_dr(self, n):
        if self.ir == self.IDCODE_IR:
            return self.idcode
        if self.ir == 0x00C:
            w = self.words[self.ptr // 8] if self.ptr // 8 < len(self.words) else 0
            nib = (w >> (4 * (self.ptr % 8))) & 0xF
            self.ptr += 1
            return nib
        return 0


def test_ir_length_latches_only_bypass():
    t = CycloneTap()
    assert jl.ir_length(t) == 10
    assert t.latched == [0x3FF]      # never a zero-ish IR such as PULSE_NCONFIG (0x001)


def test_idcodes_single_device():
    assert jl.idcodes(CycloneTap()) == [0x02B050DD]


def test_sld_hub_info_and_nodes():
    t = CycloneTap()
    info = jl.sld_hub_info(t)
    assert info["manufacturer"] == "06e" and info["nodes"] == 1 and info["vir_width"] == 8
    assert info["node_list"][0]["node_id"] == 2 and info["node_list"][0]["manufacturer"] == "06e"
    assert set(t.latched) <= {0x00E, 0x00C, 0x3FF} and t.latched[-1] == 0x3FF


def test_scan_refuses_the_c64u(env):
    with pytest.raises(jl.JtagLoadError) as e:
        jl.scan_u64e("c64u", esp=FakeEsp(env[0]), lock_factory=locks(env[0]))
    assert e.value.stage == "binding"


SID_POPUP = "+--------------------------------+\n| SID changed. Please review settings |\n|              Ok              |\n+---------+"


def test_power_cycle_answers_the_sid_popup_first(env):
    ev = env[0]
    b = FakeBrowser(ev)
    b.screens = [SID_POPUP]
    client = FakeClient(ev, [info("f1a5bed00"), info("f1a5bed00"), None, None, info("f1a5bed00")])
    jl.power_cycle("c64u", expect_git_hash="f1a5bed00", lock_factory=locks(ev), client_factory=client,
                   browser_factory=lambda h: b, sleep=lambda s: None, log=lambda m: None)
    seq = [e for e in ev if e[0] in ("popup", "category", "press")]
    assert seq[0] == ("popup", "o") and ("press", "ENTER") in seq


def test_empty_task_menu_is_retried_once(env):
    ev = env[0]
    b = FakeBrowser(ev)
    b.empty_first = True
    jl.rehearse_power_cycle("c64u", lock_factory=locks(ev), client_factory=FakeClient(ev, [info()]),
                            browser_factory=lambda h: b)
    assert ("category", "C64 Machine") in ev and ("press", "ENTER") not in ev


def test_question_popups_are_never_answered():
    class B:
        def screen(self):
            return "SID changed? Save changes to Flash?  Yes  No"

        def press_popup_button(self, k):
            raise AssertionError("pressed")
    assert jl.clear_message_popups(B()) == 0


def test_power_cycle_from_a_jtag_image_is_proven_by_the_hash_change(env):
    ev = env[0]
    client = FakeClient(ev, [info("42c032f42"), info("6970255d1")])     # no REST drop observed at all
    rec = jl.power_cycle("c64u", expect_git_hash="6970255d1", lock_factory=locks(ev), client_factory=client,
                         browser_factory=lambda h: FakeBrowser(ev), sleep=lambda s: None, log=lambda m: None)
    assert rec["result"] == "ok" and rec["info"]["git_commit_hash"] == "6970255d1"


def test_cold_load_holds_reset_from_the_fpga_load_until_upload_and_magic(env, monkeypatch):
    """Standing rule 2026-11-08: the CPU must never run the flashed app between the FPGA load and the upload
    (stale neorv32 I-cache). Order: load_fpga, reset (0x80), upload to 0x30000, magic at 0xFFF8, unreset (0x00)."""
    import struct
    monkeypatch.delenv("JTAG_LOAD_WARM", raising=False)
    ev = env[0]
    esp = FakeEsp(ev)
    rec = load(env, esp, FakeClient(ev, [info()]))
    assert rec["cold"] is True
    kinds = [e[0] for e in esp.steps]
    assert kinds == ["load_fpga", "outputs", "upload", "write", "outputs"], esp.steps
    assert esp.steps[1] == ("outputs", 0x80) and esp.steps[2] == ("upload", 0x30000)
    assert esp.steps[3] == ("write", 0xFFF8, struct.pack("<LL", 0x30000, 0x1571BABE))
    assert esp.steps[4] == ("outputs", 0x00)


def test_warm_load_only_when_asked(env, monkeypatch):
    monkeypatch.setenv("JTAG_LOAD_WARM", "1")
    ev = env[0]
    esp = FakeEsp(ev)
    rec = load(env, esp, FakeClient(ev, [info()]))
    assert rec["cold"] is False and esp.steps == [("warm_run",)]


class _Port:
    def __init__(self, device, location, serial=None, vid=None):
        self.device, self.location, self.serial_number, self.vid = device, location, serial, vid


def test_port_is_chosen_by_usb_location_not_by_name(monkeypatch):
    """2026-11-08: two CP2102s (serial 0001 both) swap /dev names on replug; select by USB location."""
    monkeypatch.delenv("JTAG_PORT", raising=False)
    ports = [_Port("/dev/cu.usbserial-0001", "0-1.2"), _Port("/dev/cu.usbserial-4", "0-1.3"),
             _Port("/dev/tty.usbserial-0001", "0-1.2")]
    assert jl.resolve_port("0-1.2", ports) == "/dev/cu.usbserial-0001"
    assert jl.resolve_port("0-1.3", ports) == "/dev/cu.usbserial-4"


def test_port_refused_when_none_or_several_match(monkeypatch):
    monkeypatch.delenv("JTAG_PORT", raising=False)
    with pytest.raises(jl.JtagLoadError, match="0 serial ports"):
        jl.resolve_port("0-1.2", [_Port("/dev/cu.usbserial-4", "0-1.3")])
    with pytest.raises(jl.JtagLoadError, match="2 serial ports"):
        jl.resolve_port("0-1.2", [_Port("/dev/cu.usbserial-4", "0-1.2"), _Port("/dev/cu.usbserial-0001", "0-1.2")])


def test_port_refused_when_jtag_port_names_another_port(monkeypatch):
    monkeypatch.setenv("JTAG_PORT", "/dev/cu.usbserial-4")
    with pytest.raises(jl.JtagLoadError, match="names swap"):
        jl.resolve_port("0-1.2", [_Port("/dev/cu.usbserial-0001", "0-1.2")])
    assert jl.resolve_port("0-1.2", [_Port("/dev/cu.usbserial-4", "0-1.2")]) == "/dev/cu.usbserial-4"


def test_two_cp210x_with_serial_0001_warn_and_location_decides(monkeypatch, capsys):
    monkeypatch.delenv("JTAG_PORT", raising=False)
    ports = [_Port("/dev/cu.usbserial-0001", "0-1.2", "0001", 0x10C4),
             _Port("/dev/cu.usbserial-4", "0-1.4", "0001", 0x10C4)]
    assert jl.resolve_port("0-1.2", ports) == "/dev/cu.usbserial-0001"
    assert "2 CP210x ports report serial 0001" in capsys.readouterr().err


class _LinkError(Exception):
    pass


def _flaky(ev, fail_times, exc=None):
    """FakeEsp whose upload raises LinkError the first `fail_times` attempts (after the FPGA is programmed)."""
    esp = FakeEsp(ev)
    esp.LinkError = _LinkError
    base = esp.Recover
    count = {"n": 0}

    class R(base):
        def user_upload(self_, binary, addr):
            count["n"] += 1
            if count["n"] <= fail_times:
                raise (exc or _LinkError("response sequence 27, expected 26"))
            return base.user_upload(self_, binary, addr)
    esp.Recover = R
    esp.count = count
    return esp


def test_link_error_after_programming_retries_whole_load_once(env, capsys):
    ev = env[0]
    jl.DUP_SERIAL_WARNED = False
    esp = _flaky(ev, 1)
    rp = env[5] / "loads.jsonl"
    rec = load(env, esp, FakeClient(ev, [info()]), record_path=str(rp))
    assert rec["result"] == "ok" and rec["retries"] == 1
    assert len(rec["link_errors"]) == 1 and "sequence 27" in rec["link_errors"][0]["error"]
    assert [k[0] for k in esp.steps].count("load_fpga") == 2          # restarted from JPROGRAM, not resumed
    assert [e for e in ev if e[0] == "acquire"] == [("acquire", "device"), ("acquire", "adapter")]   # locks kept
    assert ev.count(("ser_close",)) == 2
    line = json.loads(rp.read_text().splitlines()[-1])
    assert line["retries"] == 1 and line["dup_cp210x_serial_warning"] is False


def test_link_error_twice_reports_dark_device(env, capsys):
    ev = env[0]
    esp = _flaky(ev, 5)
    rp = env[5] / "loads.jsonl"
    with pytest.raises(_LinkError):
        load(env, esp, FakeClient(ev, [info()]), record_path=str(rp))
    assert esp.count["n"] == 2                                       # capped at one retry
    assert jl.DARK_MESSAGE in capsys.readouterr().err
    line = json.loads(rp.read_text().splitlines()[-1])
    assert line["result"] == "error" and line["dark"] == jl.DARK_MESSAGE and len(line["link_errors"]) == 2


def test_gate_failure_and_non_link_errors_are_not_retried(env):
    ev = env[0]
    esp = _flaky(ev, 5, exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        load(env, esp, FakeClient(ev, [info()]))
    assert esp.count["n"] == 1
    ev2 = Events()
    esp2 = FakeEsp(ev2, live_idcode=0x12345678)
    esp2.LinkError = _LinkError
    with pytest.raises(jl.JtagLoadError, match="idcode"):
        load(env, esp2, FakeClient(ev2, [info()]))
    assert [k[0] for k in esp2.steps] == []


def test_duplicate_serial_warning_is_recorded(env, monkeypatch):
    ev = env[0]
    monkeypatch.setattr(jl, "DUP_SERIAL_WARNED", False)
    orig = jl.adapter_for
    def afor(uid, comports=None):
        jl.DUP_SERIAL_WARNED = True
        return orig(uid, comports)
    monkeypatch.setattr(jl, "adapter_for", afor)
    rec = load(env, FakeEsp(ev), FakeClient(ev, [info()]))
    assert rec["dup_cp210x_serial_warning"] is True


# -- the lock guard on the real client factory (require_device_lock, _u64_host) -------------------------------

GUARD_HOST = "192.0.2.1"        # TEST-NET-1: nothing answers there, and the guard refuses before any request


@pytest.fixture
def built(monkeypatch):
    """Replace the harness client so _client can be called without building one; record each construction."""
    import c64_test_harness.backends.ultimate64_client as uc
    made = []
    monkeypatch.setattr(uc, "Ultimate64Client", lambda host, **kw: made.append(host) or object())
    monkeypatch.delenv("C64U_LOCK_PID", raising=False)
    monkeypatch.delenv("C64U_LOCK_HOST", raising=False)
    return made


def real_locks(tmp_path):
    from c64_test_harness.backends.device_lock import DeviceLock as RealLock
    return lambda name, allow_nested=False: RealLock(name, lock_dir=tmp_path / "locks", allow_nested=allow_nested,
                                                     heartbeat_interval=None)


def test_client_refuses_unlocked_and_try_info_does_not_hide_it(built):
    with pytest.raises(jl.DeviceLockNotHeldError):
        jl._client(GUARD_HOST)
    with pytest.raises(jl.DeviceLockNotHeldError):       # not None, which would read as "the device is off"
        jl.try_info(GUARD_HOST)
    assert built == []


def test_client_is_built_under_the_scripts_own_lock(built, tmp_path):
    with jl._Locked(GUARD_HOST, {}, 5, real_locks(tmp_path)):
        jl._client(GUARD_HOST)
    assert built == [GUARD_HOST]
    with pytest.raises(jl.DeviceLockNotHeldError):       # released again
        jl._client(GUARD_HOST)


def test_client_is_built_under_a_lock_shell_and_a_stale_claim_refused(built, tmp_path, monkeypatch):
    import c64_test_harness.backends.device_lock as dl
    monkeypatch.setenv("C64U_LOCK_PID", "4242")
    monkeypatch.setenv("C64U_LOCK_HOST", GUARD_HOST)
    monkeypatch.setattr(dl, "device_lock_holder", lambda h: {"pid": 4242})
    _chain(monkeypatch, 4242)
    with jl._Locked(GUARD_HOST, {}, 5, real_locks(tmp_path)) as held:
        assert held.dev is None                         # the shell holds it; this process took nothing
        jl._client(GUARD_HOST)
    assert built == [GUARD_HOST]
    monkeypatch.setattr(dl, "device_lock_holder", lambda h: {"pid": 1})
    with pytest.raises(jl.LockShellClaimError):
        jl._client(GUARD_HOST)
    monkeypatch.setenv("C64U_LOCK_HOST", "192.0.2.99")  # the shell holds another device: no cover here
    with pytest.raises(jl.DeviceLockNotHeldError):
        jl._client(GUARD_HOST)
    assert built == [GUARD_HOST]


def _chain(monkeypatch, *ancestors):
    """Make os.getpid()'s parents, nearest first, *ancestors*; then pid 1."""
    import _u64_host
    me = os.getpid()
    links = dict(zip((me,) + ancestors, ancestors + (1,)))
    monkeypatch.setattr(_u64_host, "_parent_pid", lambda pid: links.get(pid))


@pytest.mark.parametrize("ancestors, ok", [
    ((4242,), True),                 # the shell ran this process
    ((100, 4242), True),             # through a wrapper
    ((100, 200), False),             # 4242 holds the lock, but for some other process tree
    ((), False),
])
def test_a_lock_shell_must_be_an_ancestor_of_this_process(built, monkeypatch, ancestors, ok):
    import c64_test_harness.backends.device_lock as dl
    monkeypatch.setenv("C64U_LOCK_PID", "4242")
    monkeypatch.setenv("C64U_LOCK_HOST", GUARD_HOST)
    monkeypatch.setattr(dl, "device_lock_holder", lambda h: {"pid": 4242})
    _chain(monkeypatch, *ancestors)
    if ok:
        jl._client(GUARD_HOST)
        assert built == [GUARD_HOST]
    else:
        with pytest.raises(jl.LockShellClaimError, match="ancestor"):
            jl._client(GUARD_HOST)
        assert built == []


def test_the_parent_chain_is_read_from_the_process_table():
    """The real _parent_pid (ps): this process's parent is an ancestor, a PID outside the chain is not."""
    import _u64_host
    assert _u64_host._parent_pid(os.getpid()) == os.getppid()
    assert _u64_host._is_ancestor(os.getppid())
    assert not _u64_host._is_ancestor(os.getpid())
    assert not _u64_host._is_ancestor(2 ** 22 + 7)
    assert not _u64_host._is_ancestor(1)               # init/launchd is everyone's ancestor: never a lock shell


def test_the_telnet_browser_refuses_unlocked_and_opens_under_the_lock(built, tmp_path, monkeypatch):
    """_telnet_browser opens a UI session on the shared device's telnet menu: same guard as _client."""
    opened = []
    monkeypatch.setattr(jl, "_telnet_imports",       # the upstream checkout is not even imported unlocked
                        lambda: opened.append("import") or ("telnet", lambda mode, host, **kw: opened.append(host) or "B"))
    with pytest.raises(jl.DeviceLockNotHeldError):
        jl._telnet_browser(GUARD_HOST)
    assert opened == []
    with jl._Locked(GUARD_HOST, {}, 5, real_locks(tmp_path)):
        assert jl._telnet_browser(GUARD_HOST) == "B"
    assert opened == ["import", GUARD_HOST]
