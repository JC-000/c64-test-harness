"""Device-free tests of scripts/volatile_load.py: one volatile firmware load above ram_run_u64.py and jtag_load.py.

The real ram_run and jtag_load run underneath, against stand-ins: a U64 whose storage, telnet menu and /v1/info
are one fake (the menu's ENTER starts whatever file was staged, and FLASHES when that file is not a registered
RAM-run image), and a C64 Ultimate whose /v1/info follows what the fake esp-jtag adapter uploaded. Nothing here
talks to a device or an adapter.
"""
import hashlib
import inspect
import json
import os
import struct
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import jtag_load as jl  # noqa: E402
import volatile_load as vl  # noqa: E402
from _u64_host import DeviceLockNotHeldError, require_device_lock  # noqa: E402
from c64_test_harness.backends.device_lock import DeviceLock  # noqa: E402
from c64_test_harness.backends.ultimate64_temp_gc import lock_held_for  # noqa: E402
from test_jtag_load import FakeEsp  # noqa: E402

APP, STUB, MAGIC = 0x3001000, 0x3000000, 0x524D4152
LOCATION = "9-9.9"
ADAPTER_LOCK = "jtag-adapter-usb-" + LOCATION
PROMPTS = b"Reformat Flash Disk?\0About to update. Continue?\0Reset Configuration? (Recommended)\0"
PROMPTS += b"\0" * (-len(PROMPTS) % 4)          # record lengths stay word-aligned, as the loader requires


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rec(load, payload, start):
    return struct.pack("<III", load, len(payload), start) + payload


def ramrun_image(app):
    """What tools/make_ramrun.py writes: the app at the staging address with start 0, then the stub."""
    stub = b"\x13" * 40 + struct.pack("<IIIII", MAGIC, APP, 0x0, len(app), 0x20)
    return rec(APP, app, 0) + rec(STUB, stub, STUB)


def c64u_app(payload):
    """A one-record ultimate.app of the u64ii target: loaded and started at 0x30000."""
    return rec(0x30000, payload, 0x30000)


def update_image(payload=b"\x13" * 64 + PROMPTS):
    """A plain update image: one record in the updater's load window, started there. "Run Update" on it flashes."""
    return rec(STUB, payload, STUB)


# ---- the U64 stand-in ---------------------------------------------------------------------------------------------

class FakeU64:
    """/v1/info, storage and the telnet menu of one U64. ``known``: SHA-256 of a staged file -> the git hash that
    image reports once started. Starting any other file is a flash update (``flashed``)."""

    def __init__(self, ev, known, running="f1a54ed00", uid="ABC123", product="Ultimate 64 Elite"):
        self.ev, self.known, self.running, self.uid, self.product = ev, known, running, uid, product
        self.files, self.flashed, self.dark, self.dies_on_run, self.boots_as = {}, False, False, False, None

    # REST
    def client(self, host, timeout=5.0):
        return self

    def get_info(self):
        self.ev.append(("get_info",))
        if self.dark:
            raise ConnectionError("down")
        return {"unique_id": self.uid, "product": self.product, "git_commit_hash": self.running}

    # telnet menu: the private driver steps ram_run_u64.run_update_ramrun takes
    def menu(self, host):
        return self

    def _open(self, what):
        dev = self

        class Sess:
            path = None

            def send(self_, key):
                assert key == "ENTER"
                dev.run(self_.path)

            def drain(self_, expect=False):
                pass

            def close(self_):
                pass
        return Sess()

    def _navigate(self, sess, path):
        assert path in self.files, f"{path} is not on the device"
        sess.path = path

    def _open_menu(self, sess, path):
        pass

    def _highlight(self, sess, label, path):
        return types.SimpleNamespace(labels=("Run Update",))

    def run(self, path):
        digest = sha(self.files[path])
        self.ev.append(("run", path))
        if digest not in self.known:
            self.flashed = True
        elif self.dies_on_run:
            self.dark = True
        else:
            self.running = self.boots_as or self.known[digest]


class Storage:
    """The two storage calls ram_run makes. Any other storage call is recorded and fails the test."""

    def __init__(self, dev):
        self.dev = dev

    def storage_put_file(self, host, path, data, overwrite=False):
        self.dev.ev.append(("put", path))
        self.dev.files[path] = bytes(data)

    def storage_delete_file(self, host, path, missing_ok=False):
        self.dev.ev.append(("delete", path))
        self.dev.files.pop(path, None)

    def __getattr__(self, name):
        self.dev.ev.append(("storage-other", name))
        raise AssertionError(f"unexpected storage call {name}")


# ---- the C64 Ultimate stand-in ------------------------------------------------------------------------------------

class FakeC64U:
    """/v1/info of a C64 Ultimate: it reports the hash of whatever DRAM image the fake adapter uploaded last."""

    def __init__(self, ev, esp, known, running="f1a54ed00", uid="DEF456", product="C64 Ultimate", dark=False):
        self.ev, self.esp, self.known, self.running, self.uid, self.product = ev, esp, known, running, uid, product
        self.dark, self.uid_after_load = dark, None

    def client(self, host, timeout=5.0):
        return self

    def get_info(self):
        self.ev.append(("get_info",))
        loaded = self.esp.loaded
        if loaded is None:
            if self.dark:
                raise ConnectionError("down")
            return {"unique_id": self.uid, "product": self.product, "git_commit_hash": self.running}
        return {"unique_id": self.uid_after_load or self.uid, "product": self.product,
                "git_commit_hash": self.known.get(sha(loaded[3]), "0bad0bad0")}


class FakeLock:
    def __init__(self, ev, name):
        self.ev, self.name = ev, name

    def acquire(self, timeout=None):
        self.ev.append(("acquire", self.name))
        return True

    def release(self):
        self.ev.append(("release", self.name))


def ticking():
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]
    return clock


KEYS = {"u64e": "uid-abc123", "c64u": "uid-def456", "both": "uid-0a0b0c", "scanonly": "uid-fed789",
        "nobase": "uid-0d0e0f", "unbound": "uid-123456"}
HASH_A, HASH_B, HASH_BASE = "aaaaaaa11", "bbbbbbb22", "ba5e110e0"


class Bench:
    """Image files, a bench.toml that binds fake devices to them, and the stand-ins."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.ev = tmp_path, []
        self.known = {}
        # U64 line: RAM-run images
        self.u_a = self.u64_image("a.u64", b"\xa5" * 64, HASH_A)
        self.u_b = self.u64_image("b.u64", b"\x5a" * 64, HASH_B)
        self.u_base = self.u64_image("base.u64", b"\x11" * 64, HASH_BASE)
        # C64U class: one-record apps plus a bitstream
        self.bit = tmp_path / "x.bit"
        self.bit.write_bytes(b"BIT" * 100)
        self.c_a = self.c64u_image("a.app", b"\xc1" * 96, HASH_A)
        self.c_base = self.c64u_image("base.app", b"\xc2" * 96, HASH_BASE)
        port = tmp_path / "cu.fake"
        port.write_text("")
        self.toml = tmp_path / "bench.toml"
        self.write_toml()
        monkeypatch.setenv("C64_BENCH_BINDINGS_FILE", str(self.toml))
        for var in ("JTAG_USB_LOCATION", "JTAG_PORT", "JTAG_LOAD_WARM", "C64U_LOCK_PID", "C64U_LOCK_HOST"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(jl, "resolve_port", lambda location, comports=None: str(port))
        monkeypatch.setattr(jl, "normalize_device_host", lambda h: KEYS.get(h, h))
        monkeypatch.setattr(jl.time, "sleep", lambda s: None)
        self.esp = FakeEsp(self.ev)
        self.u64 = FakeU64(self.ev, self.known)
        self.c64u = FakeC64U(self.ev, self.esp, self.known)

    def u64_image(self, name, app, git_hash, **kw):
        p = self.tmp / name
        p.write_bytes(ramrun_image(app))
        self.known[sha(p.read_bytes())] = git_hash
        return vl.VolatileImage(p, sha(p.read_bytes()), git_hash, **kw)

    def c64u_image(self, name, payload, git_hash):
        p = self.tmp / name
        p.write_bytes(c64u_app(payload))
        self.known[sha(payload)] = git_hash          # the adapter uploads the extracted DRAM image
        return vl.VolatileImage(p, sha(p.read_bytes()), git_hash, bit=self.bit, bit_sha256=sha(self.bit.read_bytes()))

    def baseline(self, image):
        out = (f'volatile_baseline_image = "{image.path}"\nvolatile_baseline_sha256 = "{image.sha256}"\n'
               f'volatile_baseline_git_hash = "{image.git_hash}"\n')
        if image.bit is not None:
            out += f'volatile_baseline_bit = "{image.bit}"\nvolatile_baseline_bit_sha256 = "{image.bit_sha256}"\n'
        return out

    def write_toml(self, u64_baseline=None, c64u_baseline=None):
        adapter = f'jtag_adapter_location = "{LOCATION}"\njtag_adapter_baud = 460800\n'
        self.toml.write_text(
            "[devices.abc123]\nram_run = true\n" + adapter + "jtag_scan_only = true\n"
            + self.baseline(u64_baseline or self.u_base)
            + "[devices.def456]\n" + adapter + self.baseline(c64u_baseline or self.c_base)
            + "[devices.0a0b0c]\nram_run = true\n" + adapter
            + "[devices.fed789]\n" + adapter + "jtag_scan_only = true\n"
            + "[devices.0d0e0f]\nram_run = true\n")

    def locks(self, name, allow_nested=False):
        return FakeLock(self.ev, {ADAPTER_LOCK: "adapter"}.get(name, "device"))

    def opts(self, dev, **kw):
        o = dict(lock_factory=self.locks, client_factory=dev.client, sleep=lambda s: None, clock=ticking(),
                 log=lambda m: None, net_timeout=0.05)
        if isinstance(dev, FakeU64):
            o.update(storage=Storage(dev), menu_factory=dev.menu)
        else:
            o.update(esp=self.esp)
        o.update(kw)
        return o

    def names(self, *kinds):
        return [e for e in self.ev if e[0] in kinds]


@pytest.fixture
def bench(tmp_path, monkeypatch):
    return Bench(tmp_path, monkeypatch)


# ---- 1. one call, the backend from the product, one result shape --------------------------------------------------

def test_a_u64_product_is_ram_run_and_the_result_says_what_happened(bench):
    r = vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert (r.ok, r.backend, r.family, r.product) == (True, "ram_run", "u64", "Ultimate 64 Elite")
    assert (r.git_hash, r.expect_git_hash, r.loaded, r.dark_before) == (HASH_A, HASH_A, True, False)
    assert (r.restart_proof, r.hash_changed) == ("hash-change", True)
    assert set(r.timings) == {"preflight_s", "lock_s", "identify_s", "load_s", "verify_s", "total_s"}
    assert all(v > 0 for v in r.timings.values())
    assert r.timings["total_s"] >= r.timings["load_s"] >= 1
    assert bench.u64.running == HASH_A and not bench.u64.flashed and not bench.esp.steps


def test_a_c64u_product_is_a_cold_jtag_load_with_the_same_result_shape(bench):
    r = vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert (r.ok, r.backend, r.family, r.product) == (True, "jtag_cold", "c64u", "C64 Ultimate")
    assert (r.git_hash, r.loaded, r.dark_before, r.restart_proof) == (HASH_A, True, False, "jtag-reconfigured")
    assert set(r.timings) == {"preflight_s", "lock_s", "identify_s", "load_s", "verify_s", "total_s"}
    assert bench.esp.loaded[3] == b"\xc1" * 96          # the extracted DRAM image, not the .app with its header
    assert not bench.names("put", "run")
    assert set(vars(r)) == set(vars(vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))))


def test_a_dark_c64u_is_loaded_from_its_binding_and_confirmed_afterwards(bench):
    bench.c64u.dark = True
    r = vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert (r.ok, r.backend, r.dark_before, r.hash_changed) == (True, "jtag_cold", True, None)
    assert r.git_hash == HASH_A and r.product == "C64 Ultimate"


def test_a_dark_box_that_comes_back_as_another_device_fails(bench):
    bench.c64u.dark, bench.c64u.uid_after_load = True, "ABC123"
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert e.value.stage == "identity" and e.value.result.ok is False and e.value.result.loaded is False


def test_a_product_without_a_backend_is_refused_before_anything_is_staged(bench):
    bench.u64.product = "C64 Ultimate (MK1)"
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "identity" and not bench.names("put", "run", "open")


@pytest.mark.parametrize("dev,image", [("c64u", "u_a"), ("u64", "c_a")])
def test_an_image_of_the_other_product_is_refused_under_the_lock(bench, dev, image):
    """The device "both" is bound for either backend; the answering product decides, and the image must match it."""
    device = getattr(bench, dev)
    device.uid = "0A0B0C"
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("both", getattr(bench, image), **bench.opts(device, esp=bench.esp,
                                                                     storage=Storage(bench.u64),
                                                                     menu_factory=bench.u64.menu))
    assert e.value.stage == "image" and not bench.names("put", "run", "open", "load_fpga_and_upload")


# ---- 2. only a volatile image of that product; never a flash update -----------------------------------------------

BAD_IMAGES = {
    "update-image": update_image(),
    "update-image-without-prompts": update_image(b"\x13" * 64),
    "ramrun-around-an-updater": ramrun_image(b"\x13" * 16 + PROMPTS),
    "c64u-app-that-is-an-updater": c64u_app(b"\x13" * 16 + PROMPTS),
    "silent-updater": ramrun_image(b"\x13" * 5 + b"bench: forcing module flash"),
    "cfw": struct.pack("<I", 0x4115ADDE) + b"\x00" * 60,
    "garbage": bytes(range(256)) * 3,
    "empty": b"",
    "one-header": struct.pack("<III", 0x30000, 0, 0x30000),
    "raw-bin": b"\x13\x00\x00\x00" * 64,
    "c64u-app-at-another-address": rec(0x10000, b"\xc1" * 96, 0x10000),
    "c64u-app-truncated": c64u_app(b"\xc1" * 96)[:-4],
}


@pytest.mark.parametrize("host", ["u64e", "c64u", "both"])
@pytest.mark.parametrize("name", sorted(BAD_IMAGES))
def test_anything_but_a_volatile_image_is_refused_before_any_lock(bench, host, name):
    p = bench.tmp / "bad.img"
    p.write_bytes(BAD_IMAGES[name])
    image = vl.VolatileImage(p, sha(BAD_IMAGES[name]), HASH_A, bit=bench.bit, bit_sha256=sha(bench.bit.read_bytes()))
    for img in (image, vl.dataclasses.replace(image, bit=None, bit_sha256=None)):
        for dev in (bench.u64, bench.c64u):
            with pytest.raises(vl.VolatileLoadError) as e:
                vl.volatile_load(host, img, **bench.opts(dev, esp=bench.esp, storage=Storage(bench.u64),
                                                         menu_factory=bench.u64.menu))
            assert e.value.stage == "image", str(e.value)
    assert bench.ev == [] and not bench.u64.flashed and bench.esp.loaded is None


@pytest.mark.parametrize("host,image", [("u64e", "c_a"), ("c64u", "u_a")])
def test_a_volatile_image_of_the_other_product_is_refused_before_any_lock(bench, host, image):
    dev = bench.u64 if host == "u64e" else bench.c64u
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load(host, getattr(bench, image), **bench.opts(dev, esp=bench.esp, storage=Storage(bench.u64),
                                                                   menu_factory=bench.u64.menu))
    assert e.value.stage == "binding" and bench.ev == []


def test_the_stand_in_would_flash_an_update_image_that_reached_run_update(bench):
    """Positive control for ``flashed``: the fake does flash when a plain update image is started."""
    bench.u64.files["/USB1/u.u64"] = update_image()
    bench.u64.run("/USB1/u.u64")
    assert bench.u64.flashed


def test_a_wrong_sha256_is_refused_before_any_lock(bench):
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", vl.dataclasses.replace(bench.u_a, sha256="0" * 64), **bench.opts(bench.u64))
    assert e.value.stage == "hash" and bench.ev == []


def test_a_c64u_app_without_its_bitstream_pin_is_refused(bench):
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", vl.dataclasses.replace(bench.c_a, bit_sha256=None), **bench.opts(bench.c64u))
    assert e.value.stage == "image" and bench.ev == []


# ---- 3. the running hash must be the image's ----------------------------------------------------------------------

def test_a_backend_that_comes_back_with_another_hash_fails(bench):
    def runner(host, image, **kw):
        return {"result": "ok", "info": {"unique_id": "ABC123", "product": "Ultimate 64 Elite",
                                         "git_commit_hash": HASH_B}}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, ram_run=runner))
    r = e.value.result
    assert e.value.stage == "verify" and (r.ok, r.loaded, r.git_hash, r.backend) == (False, False, HASH_B, "ram_run")


def test_a_u64_that_boots_something_else_fails_and_the_staged_file_is_removed(bench):
    bench.u64.boots_as = "0ddba11aa"
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "network" and e.value.result.ok is False
    assert bench.u64.files == {} and bench.names("put", "delete") == [("put", "/USB1/rraaaaaa.u64"),
                                                                      ("delete", "/USB1/rraaaaaa.u64")]


def test_a_same_hash_ram_run_is_refused_as_unprovable(bench):
    bench.u64.running = HASH_A
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "run" and e.value.result.loaded is False and not bench.names("put", "run")


def test_a_restart_no_backend_can_show_is_not_claimed(bench):
    """A runner (e.g. a later ram_run entry point) that reports the expected hash on a device that already reported
    it gives no evidence of a restart: the call fails rather than say "loaded"."""
    bench.u64.running = HASH_A

    def runner(host, image, **kw):
        return {"result": "ok", "info": bench.u64.get_info()}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, ram_run=runner))
    assert e.value.stage == "verify" and e.value.result.restart_proof == "none"


def test_a_same_hash_c64u_load_is_proven_by_the_jtag_reload_not_by_the_hash(bench):
    bench.c64u.running = HASH_A
    r = vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert (r.ok, r.loaded, r.hash_changed, r.restart_proof) == (True, True, False, "jtag-reconfigured")


def test_a_c64u_that_reports_another_hash_after_the_load_fails(bench):
    del bench.known[sha(b"\xc1" * 96)]
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert e.value.stage == "network" and e.value.result.loaded is False


# ---- 4. restore, the session, baseline first ----------------------------------------------------------------------

def test_restore_loads_the_configured_baseline(bench):
    bench.u64.running = HASH_A
    r = vl.volatile_restore("u64e", **bench.opts(bench.u64))
    assert (r.ok, r.action, r.loaded, r.git_hash, r.restart_proof) == (True, "restore", True, HASH_BASE,
                                                                       "hash-change")
    r = vl.volatile_restore("c64u", **bench.opts(bench.c64u))
    assert (r.ok, r.backend, r.git_hash) == (True, "jtag_cold", HASH_BASE)


def test_restore_on_a_u64_already_at_the_baseline_loads_nothing_and_says_so(bench):
    bench.u64.running = HASH_BASE
    r = vl.volatile_restore("u64e", **bench.opts(bench.u64))
    assert (r.ok, r.loaded, r.restart_proof, r.hash_changed) == (True, False, "none", False)
    assert r.notes and not bench.names("put", "run")


def test_restore_without_a_configured_baseline_is_refused(bench):
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_restore("nobase", **bench.opts(bench.u64))
    assert e.value.stage == "baseline" and bench.ev == []


def test_a_baseline_that_is_an_update_image_is_refused(bench):
    p = bench.tmp / "update.u64"
    p.write_bytes(update_image())
    bench.write_toml(u64_baseline=vl.VolatileImage(p, sha(p.read_bytes()), HASH_BASE))
    bench.u64.running = HASH_A
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_restore("u64e", **bench.opts(bench.u64))
    assert e.value.stage == "image" and bench.ev == [] and not bench.u64.flashed


def test_a_session_loads_runs_the_body_and_restores(bench):
    with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64)) as s:
        assert bench.u64.running == HASH_A and s.load.ok and s.restore is None
        s.load_image(bench.u_b)
        assert bench.u64.running == HASH_B
    assert bench.u64.running == HASH_BASE and s.restore.ok and s.restore.loaded and s.restore_error is None
    assert [e[1] for e in bench.names("run")] == ["/USB1/rraaaaaa.u64", "/USB1/rrbbbbbb.u64", "/USB1/rrba5e11.u64"]


def test_baseline_first_proves_the_way_back_before_the_image(bench):
    with vl.volatile_session("c64u", bench.c_a, baseline_first=True, **bench.opts(bench.c64u)) as s:
        assert s.baseline_first.ok and s.baseline_first.git_hash == HASH_BASE and s.load.git_hash == HASH_A
    assert s.restore.git_hash == HASH_BASE
    assert len(bench.names("load_fpga_and_upload")) == 3


def test_when_baseline_first_fails_the_image_is_never_loaded(bench):
    """A U64 that already reports the baseline hash cannot prove the way back (#570): the session stops there."""
    bench.u64.running = HASH_BASE
    with pytest.raises(vl.VolatileLoadError) as e:
        with vl.volatile_session("u64e", bench.u_a, baseline_first=True, **bench.opts(bench.u64)):
            pytest.fail("the body ran")
    assert e.value.stage == "run" and not bench.names("put", "run")


class BodyFailed(Exception):
    pass


def test_the_restore_is_attempted_when_the_body_raises(bench):
    with pytest.raises(BodyFailed):
        with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64)) as s:
            raise BodyFailed("row 3")
    assert bench.u64.running == HASH_BASE and s.restore.ok


def test_a_restore_failure_does_not_mask_the_bodys_exception(bench):
    with pytest.raises(BodyFailed):
        with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64)) as s:
            bench.u64.dies_on_run = True         # the baseline will not come up
            raise BodyFailed("row 3")
    assert isinstance(s.restore_error, vl.VolatileLoadError) and s.restore is None
    assert ("run", "/USB1/rrba5e11.u64") in bench.ev


def test_a_restore_failure_after_a_clean_body_is_raised(bench):
    with pytest.raises(vl.VolatileLoadError) as e:
        with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64)) as s:
            bench.u64.dies_on_run = True
    assert e.value.result.action == "restore" and s.restore_error is e.value


def test_the_restore_is_attempted_when_the_load_itself_fails(bench):
    bench.c64u.dark = True
    del bench.known[sha(b"\xc1" * 96)]          # the image comes up reporting another hash
    with pytest.raises(vl.VolatileLoadError) as e:
        with vl.volatile_session("c64u", bench.c_a, **bench.opts(bench.c64u)):
            pytest.fail("the body ran")
    assert e.value.result.action == "load"
    assert bench.c64u.get_info()["git_commit_hash"] == HASH_BASE


def test_a_session_checks_the_baseline_before_it_takes_the_lock(bench):
    bench.write_toml(u64_baseline=vl.dataclasses.replace(bench.u_base, sha256="0" * 64))
    with pytest.raises(jl.JtagLoadError) as e:
        with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64)):
            pytest.fail("the body ran")
    assert e.value.stage == "hash" and bench.ev == []


def test_restore_false_leaves_the_image_running(bench):
    with vl.volatile_session("nobase", bench.u_a, restore=False, **bench.opts(
            FakeU64(bench.ev, bench.known, uid="0D0E0F"))) as s:
        pass
    assert s.restore is None and len(bench.names("run")) == 1


# ---- 5. the dark box, and no JTAG timeout under 300 s -------------------------------------------------------------

@pytest.mark.parametrize("value", [299.9, 0, -1, float("nan"), None, "soon", True])
def test_a_jtag_timeout_under_300_s_is_refused(bench, value):
    for call in (lambda: vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u, jtag_timeout=value)),
                 lambda: vl.volatile_restore("c64u", **bench.opts(bench.c64u, jtag_timeout=value)),
                 lambda: vl.volatile_session("c64u", bench.c_a,
                                             **bench.opts(bench.c64u, jtag_timeout=value)).__enter__()):
        with pytest.raises(jl.JtagLoadError) as e:
            call()
        assert e.value.stage == "timeout"
    assert bench.ev == []


def test_a_dark_u64_without_a_jtag_reload_is_refused_and_nothing_is_staged(bench):
    bench.u64.dark = True
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "backend" and e.value.result.dark_before is True
    assert not bench.names("put", "run", "open")


def test_a_dark_u64_goes_through_the_jtag_reload_seam_with_at_least_300_s(bench):
    bench.u64.dark = True
    elf = bench.tmp / "a.elf"
    elf.write_bytes(b"ELF")
    image = vl.dataclasses.replace(bench.u_a, jtag_image=elf, jtag_sha256=sha(b"ELF"))
    calls = []

    def reload(host, image_path, **kw):
        calls.append((host, image_path, kw))
        bench.u64.dark, bench.u64.running = False, HASH_A
        return {"result": "ok", "info": bench.u64.get_info()}
    r = vl.volatile_load("u64e", image, **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert (r.ok, r.backend, r.dark_before, r.restart_proof) == (True, "u64_jtag_reload", True, "dark-then-answering")
    (host, path, kw), = calls
    assert (host, path, kw["sha256"], kw["expect_git_hash"]) == ("u64e", elf, sha(b"ELF"), HASH_A)
    assert kw["timeout"] >= 300 and not bench.names("put", "run")
    with pytest.raises(vl.VolatileLoadError) as e:          # no JTAG image named: refused, the seam is not called
        bench.u64.dark = True
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert e.value.stage == "backend" and len(calls) == 1


def test_a_dark_u64_that_the_reload_brings_back_as_another_device_fails(bench):
    """Identity after a dark-box load is this wrapper's own check, whatever the backend checked."""
    bench.u64.dark = True
    image = vl.dataclasses.replace(bench.u_a, jtag_image=bench.u_a.path, jtag_sha256=bench.u_a.sha256)

    def reload(host, image_path, **kw):
        return {"result": "ok", "info": {"unique_id": "DEF456", "product": "Ultimate 64 Elite",
                                         "git_commit_hash": HASH_A}}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", image, **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert e.value.stage == "identity" and e.value.result.loaded is False


# ---- 6. one DeviceLock across the load, the body and the restore --------------------------------------------------

def test_the_session_holds_the_device_lock_in_this_process_for_the_body(bench, tmp_path):
    def real(name, allow_nested=False):
        return DeviceLock(name, lock_dir=tmp_path / "locks", heartbeat_interval=None, allow_nested=allow_nested)
    assert not lock_held_for("u64e")
    with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64, lock_factory=real)) as s:
        assert s.lock_in_process and lock_held_for("u64e")      # still held after the backend released its join
        require_device_lock("u64e")                             # what the harness helpers ask
        s.load_image(bench.u_b)
        assert lock_held_for("u64e")
    assert not lock_held_for("u64e") and bench.u64.running == HASH_BASE
    with pytest.raises(DeviceLockNotHeldError):
        require_device_lock("u64e")


def test_one_lock_acquire_brackets_everything(bench):
    with vl.volatile_session("c64u", bench.c_a, **bench.opts(bench.c64u)):
        bench.ev.append(("body",))
    kinds = [e for e in bench.ev if e[0] in ("body", "load_fpga_and_upload")
             or e in (("acquire", "device"), ("release", "device"))]
    assert kinds[0] == ("acquire", "device") and kinds[-1] == ("release", "device")
    depth, at_body, at_loads = 0, None, []
    for e in kinds:
        if e == ("acquire", "device"):
            depth += 1
        elif e == ("release", "device"):
            depth -= 1
            assert depth >= 1 or e is kinds[-1]         # the session's hold never drops before the end
        elif e == ("body",):
            at_body = depth
        else:
            at_loads.append(depth)
    assert at_body == 1 and at_loads == [3, 3]          # session + volatile_load + jtag_load, for load and restore


def test_under_a_lock_shell_the_session_takes_no_device_lock_and_a_ram_run_is_refused_unstaged(bench, monkeypatch):
    """A verified lock shell's hold is the hold for the JTAG path. ram_run needs the lock in this process and
    refuses before anything is staged (the seam to #582)."""
    monkeypatch.setenv("C64U_LOCK_PID", "4242")
    monkeypatch.setattr(jl, "lock_shell_holds", lambda host, normalize=None: True)
    with vl.volatile_session("c64u", bench.c_a, **bench.opts(bench.c64u)) as s:
        assert s.lock_in_process is False and s.load.ok
    assert s.restore.ok and ("acquire", "device") not in bench.ev and ("acquire", "adapter") in bench.ev
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "lock" and not bench.names("put", "run")


# ---- 7. adapter safety; the storage-staged path writes one file and removes it ------------------------------------

def test_an_adapter_other_than_the_bound_one_is_refused_before_any_jtag(bench, monkeypatch):
    monkeypatch.setenv("JTAG_USB_LOCATION", "1-2.3")
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert e.value.stage == "adapter" and bench.ev == []
    monkeypatch.setenv("JTAG_USB_LOCATION", LOCATION)       # naming the bound one is not a mismatch
    assert vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u)).ok


@pytest.mark.parametrize("host", ["scanonly", "nobase", "unbound", "not-in-the-alias-map"])
def test_a_device_without_a_loadable_adapter_binding_is_refused_before_any_jtag(bench, host):
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load(host, bench.c_a, **bench.opts(bench.c64u))
    assert e.value.stage == "binding" and bench.ev == []


def test_a_live_idcode_that_is_not_the_bitstreams_stops_before_the_fpga(bench):
    """jtag_load's pre-gate still runs under the wrapper: the wrong chain on the bound adapter loads nothing."""
    bench.esp.live_idcode = 0x02D120DD
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert e.value.stage == "idcode" and bench.esp.loaded is None and not bench.esp.steps


def test_a_whole_u64_session_writes_only_the_staged_files_and_leaves_none(bench):
    with vl.volatile_session("u64e", bench.u_a, baseline_first=False, **bench.opts(bench.u64)) as s:
        s.load_image(bench.u_b)
    staged = ["/USB1/rraaaaaa.u64", "/USB1/rrbbbbbb.u64", "/USB1/rrba5e11.u64"]
    assert bench.names("put", "delete", "storage-other") == [x for p in staged for x in (("put", p), ("delete", p))]
    assert bench.u64.files == {} and not bench.u64.flashed


# ---- 8. no warm load on the C64 Ultimate --------------------------------------------------------------------------

def test_the_c64u_load_is_cold_whatever_the_environment_says(bench, monkeypatch):
    monkeypatch.setenv("JTAG_LOAD_WARM", "1")
    r = vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert r.record["cold"] is True and ("warm_run",) not in bench.esp.steps
    assert [s[0] for s in bench.esp.steps][:3] == ["load_fpga", "outputs", "upload"]
    assert bench.esp.steps[1] == ("outputs", 0x80)       # the CPU is in reset before the upload


@pytest.mark.parametrize("extra", [{"cold": False}, {"cold": True}, {"warm": True}])
def test_no_caller_can_ask_for_a_warm_load(bench, extra):
    for call in (lambda: vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u, **extra)),
                 lambda: vl.volatile_restore("c64u", **bench.opts(bench.c64u, **extra)),
                 lambda: vl.volatile_session("c64u", bench.c_a, **bench.opts(bench.c64u, **extra)).__enter__()):
        with pytest.raises(TypeError):
            call()
    assert bench.ev == []


def test_a_backend_record_that_is_not_cold_fails_the_load(bench):
    def warm(host, bit, app, **kw):
        assert kw["cold"] is True
        return {"cold": False, "user_id": "dead1541", "info": {"unique_id": "DEF456", "product": "C64 Ultimate",
                                                               "git_commit_hash": HASH_A}}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u, jtag_load=warm))
    assert e.value.stage == "verify"


# ---- the dry run and the CLI --------------------------------------------------------------------------------------

def test_a_dry_run_returns_the_plan_and_touches_nothing(bench):
    plan = vl.volatile_load("u64e", bench.u_a, dry_run=True, **bench.opts(bench.u64))
    assert (plan["backend_if_answering"], plan["family"], plan["result"]) == ("ram_run", "u64", "dry-run ok")
    assert plan["backend_if_dark"].startswith("refused") and plan["backend_dry_run"]["remote"] == "/USB1/rraaaaaa.u64"
    plan = vl.volatile_load("c64u", bench.c_a, dry_run=True, **bench.opts(bench.c64u))
    assert (plan["backend_if_answering"], plan["backend_if_dark"]) == ("jtag_cold", "jtag_cold")
    assert plan["backend_dry_run"]["cold"] is True and plan["backend_dry_run"]["result"] == "dry-run ok"
    assert vl.volatile_restore("u64e", dry_run=True, **bench.opts(bench.u64))["expect_git_hash"] == HASH_BASE
    assert bench.ev == []


def _forbid(name):
    def boom(*a, **k):
        raise AssertionError(f"{name} was reached")
    return boom


def test_the_cli_dry_run_prints_the_plan_and_reaches_nothing(bench, monkeypatch, capsys):
    for name in ("_Locked", "try_info", "_client", "DeviceLock"):
        monkeypatch.setattr(jl, name, _forbid(name))
    rc = vl.main(["load", "u64e", str(bench.u_a.path), "--sha256", bench.u_a.sha256, "--expect-hash", HASH_A,
                  "--baseline-first", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and [x["action"] for x in out] == ["restore", "load"]
    assert [x["expect_git_hash"] for x in out] == [HASH_BASE, HASH_A]
    assert vl.main(["restore", "u64e", "--dry-run"]) == 0
    assert bench.ev == []


def test_the_cli_refuses_a_short_jtag_timeout_and_an_update_image(bench, monkeypatch, capsys):
    for name in ("_Locked", "try_info", "_client"):
        monkeypatch.setattr(jl, name, _forbid(name))
    args = ["load", "u64e", str(bench.u_a.path), "--sha256", bench.u_a.sha256, "--expect-hash", HASH_A]
    assert vl.main(args + ["--jtag-timeout", "120"]) == 2
    assert "[timeout]" in capsys.readouterr().err
    p = bench.tmp / "update.u64"
    p.write_bytes(update_image())
    rec_path = bench.tmp / "rec.jsonl"
    assert vl.main(["load", "u64e", str(p), "--sha256", sha(p.read_bytes()), "--expect-hash", HASH_A,
                    "--record", str(rec_path)]) == 2
    line = json.loads(rec_path.read_text())
    assert line["ok"] is False and line["error"].startswith("[image]")


def test_the_public_calls_take_no_load_mode_parameter():
    for fn in (vl.volatile_load, vl.volatile_restore, vl.volatile_session):
        assert not {"cold", "warm"} & set(inspect.signature(fn).parameters)
    assert "cold" not in {f.name for f in vl.dataclasses.fields(vl._Opts)}
