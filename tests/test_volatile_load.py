"""Device-free tests of scripts/volatile_load.py: one volatile firmware load above ram_run_u64.py and jtag_load.py.

The real ram_run and jtag_load run underneath, against stand-ins: a U64 whose storage, telnet menu and /v1/info
are one fake (the menu's ENTER starts whatever file was staged, and FLASHES when that file is not a registered
RAM-run image), and a C64 Ultimate whose /v1/info follows what the fake esp-jtag adapter uploaded. Nothing here
talks to a device or an adapter.
"""
import functools
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
from lock_shell_fake import FORGED_IDS, FORGED_KW, fake_lock_shell, forge  # noqa: E402
from test_blaster_load_u64 import Remote as BlasterRemote, elf as nios_elf  # noqa: E402
from test_jtag_load import FakeEsp  # noqa: E402

APP, STUB, MAGIC = 0x3001000, 0x3000000, 0x524D4152
LOCATION = "9-9.9"
ADAPTER_LOCK = "jtag-adapter-usb-" + LOCATION
PROMPTS = b"Reformat Flash Disk?\0About to update. Continue?\0Reset Configuration? (Recommended)\0"
PROMPTS += b"\0" * (-len(PROMPTS) % 4)          # record lengths stay word-aligned, as the loader requires




HASH_A, HASH_B, HASH_BASE = "aaaaaaa11", "bbbbbbb22", "ba5e110e0"


def build(fill: bytes, git_hash, n=64) -> bytes:
    """Application bytes as a firmware build leaves them: code, and the System Info line that carries the build's
    abbreviated commit hash (system_info.cc: "Git hash:   " APP_VERSION_HASH "\\n"). Word-aligned."""
    out = fill * n + (b"Git hash:   " + git_hash.encode() + b"\n\0" if git_hash else b"")
    return out + b"\0" * (-len(out) % 4)


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


def update_image(payload=build(b"\x13", HASH_A) + PROMPTS):
    """A plain update image: one record in the updater's load window, started there. "Run Update" on it flashes."""
    return rec(STUB, payload, STUB)


# ---- the U64 stand-in ---------------------------------------------------------------------------------------------

class FakeU64:
    """/v1/info, storage and the telnet menu of one U64. ``known``: SHA-256 of a staged file -> the git hash that
    image reports once started. Starting any other file is a flash update (``flashed``)."""

    def __init__(self, ev, known, running="f1a54ed00", uid="ABC123", product="Ultimate 64 Elite"):
        self.ev, self.known, self.running, self.uid, self.product = ev, known, running, uid, product
        self.files, self.flashed, self.dark, self.dies_on_run, self.boots_as = {}, False, False, False, None
        self.misses = 0                 # /v1/info reads that go unanswered before it answers again
        self.drops_file_on_run = False  # the staged file is gone from the volume once the image has started

    # REST
    def client(self, host, timeout=5.0):
        return self

    def get_info(self):
        self.ev.append(("get_info",))
        if self.misses:
            self.misses -= 1
            raise ConnectionError("busy")
        if self.dark:
            raise ConnectionError("down")
        out = {"unique_id": self.uid, "product": self.product, "git_commit_hash": self.running}
        if self.running is None:        # a firmware that does not report the key
            del out["git_commit_hash"]
        return out

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
        if self.drops_file_on_run:
            del self.files[path]


class Storage:
    """The two storage calls ram_run makes. Any other storage call is recorded and fails the test."""

    def __init__(self, dev):
        self.dev = dev

    def storage_put_file(self, host, path, data, overwrite=False):
        if path in self.dev.files and not overwrite:        # as the harness helper: refused before any write
            raise FileExistsError(path)
        self.dev.ev.append(("put", path))
        self.dev.files[path] = bytes(data)

    def storage_delete_file(self, host, path, missing_ok=False):
        self.dev.ev.append(("delete", path))
        if path not in self.dev.files and not missing_ok:       # as the harness helper
            raise FileNotFoundError(path)
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


class Bench:
    """Image files, a bench.toml that binds fake devices to them, and the stand-ins."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.ev = tmp_path, []
        self.known = {}
        # U64 line: RAM-run images
        self.u_a = self.u64_image("a.u64", build(b"\xa5", HASH_A), HASH_A)
        self.u_b = self.u64_image("b.u64", build(b"\x5a", HASH_B), HASH_B)
        self.u_base = self.u64_image("base.u64", build(b"\x11", HASH_BASE), HASH_BASE)
        # C64U class: one-record apps plus a bitstream
        self.bit = tmp_path / "x.bit"
        self.bit.write_bytes(b"BIT" * 100)
        self.c_a_payload = build(b"\xc1", HASH_A, 96)
        self.c_a = self.c64u_image("a.app", self.c_a_payload, HASH_A)
        self.c_base = self.c64u_image("base.app", build(b"\xc2", HASH_BASE, 96), HASH_BASE)
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
            + 'jtag_blaster_ssh = "blaster-vm"\njtag_blaster_cable = "USB-Blaster"\n'
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
    assert bench.esp.loaded[3] == bench.c_a_payload          # the extracted DRAM image, not the .app with its header
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
    "update-image-without-prompts": update_image(build(b"\x13", HASH_A)),
    "ramrun-around-an-updater": ramrun_image(build(b"\x13", HASH_A) + PROMPTS),
    "c64u-app-that-is-an-updater": c64u_app(build(b"\x13", HASH_A) + PROMPTS),
    "silent-updater": ramrun_image(build(b"\x13", HASH_A) + b"bench: forcing module flash\0"),
    "cfw": struct.pack("<I", 0x4115ADDE) + build(b"\x00", HASH_A),
    "garbage": bytes(range(256)) * 3 + build(b"", HASH_A),
    "empty": b"",
    "one-header": struct.pack("<III", 0x30000, 0, 0x30000),
    "raw-bin": build(b"\x13\x00\x00\x00", HASH_A),
    "c64u-app-at-another-address": rec(0x10000, build(b"\xc1", HASH_A, 96), 0x10000),
    "c64u-app-truncated": c64u_app(build(b"\xc1", HASH_A, 96))[:-4],
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


# What the image itself says it is: the build's abbreviated hash, as the firmware prints it in System Info.

def _image(bench, name, data, git_hash=HASH_A, c64u=False):
    p = bench.tmp / name
    p.write_bytes(data)
    extra = dict(bit=bench.bit, bit_sha256=sha(bench.bit.read_bytes())) if c64u else {}
    return vl.VolatileImage(p, sha(data), git_hash, **extra)


@pytest.mark.parametrize("c64u", [False, True], ids=["u64", "c64u"])
@pytest.mark.parametrize("embedded,named,stage", [
    (HASH_B, HASH_A, "hash"),                       # the file is another build than the caller names
    (HASH_A, HASH_A[:7], "hash"),                   # fewer digits than the image carries: not the whole comparison
    (HASH_A[:8] + "f", HASH_A, "hash"),             # equal in the first eight digits only
    (None, HASH_A, "image"),                        # no hash in the file: the binding cannot be established
    ("two", HASH_A, "image"),                       # two different hashes in one file
], ids=["another-build", "caller-names-fewer-digits", "differs-in-the-last-digit", "no-hash", "two-hashes"])
def test_an_image_that_is_not_the_build_the_caller_names_is_refused_before_any_lock(bench, c64u, embedded, named,
                                                                                    stage):
    app = build(b"\x77", HASH_A) + build(b"\x78", HASH_B) if embedded == "two" else build(b"\x77", embedded)
    image = _image(bench, "x.img", c64u_app(app) if c64u else ramrun_image(app), named, c64u)
    dev, host = (bench.c64u, "c64u") if c64u else (bench.u64, "u64e")
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load(host, image, **bench.opts(dev))
    assert e.value.stage == stage and bench.ev == []


def test_a_longer_hash_from_the_caller_is_compared_over_every_digit_the_image_carries(bench):
    full = HASH_A + "0123456789abcdef0123456789abcde"          # 40 digits; the image carries the first nine
    r = vl.volatile_load("u64e", vl.dataclasses.replace(bench.u_a, git_hash=full), **bench.opts(bench.u64))
    assert (r.ok, r.image_git_hash, r.git_hash) == (True, HASH_A, HASH_A)
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", vl.dataclasses.replace(bench.u_b, git_hash=HASH_B[:8] + "f" + "0" * 31),
                         **bench.opts(bench.u64))
    assert e.value.stage == "hash"


def test_the_running_hash_must_be_the_images_own_not_a_prefix_of_it(bench):
    """The firmware reports the very string its image carries, so a report that only shares leading digits with it
    is another build."""
    def runner(host, image, **kw):
        return {"result": "ok", "info": {"unique_id": "ABC123", "product": "Ultimate 64 Elite",
                                         "git_commit_hash": HASH_A[:8]}}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, ram_run=runner))
    assert e.value.stage == "verify" and e.value.result.image_git_hash == HASH_A


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
    del bench.known[sha(bench.c_a_payload)]
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert e.value.stage == "network" and e.value.result.loaded is False


@pytest.mark.parametrize("before", [None, "", "unknown"], ids=["no-key", "empty", "not-hex"])
def test_a_u64_that_reports_no_hash_before_the_load_is_refused_unstaged(bench, before):
    """Without a hash before the load, the hash afterwards shows no change, and a RAM-run has no other evidence of a
    restart (#570): as for a same-hash load, nothing is staged."""
    bench.u64.running = before
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    r = e.value.result
    assert e.value.stage == "run" and (r.loaded, r.restart_proof, r.hash_changed) == (False, "none", None)
    assert not bench.names("put", "run")


def test_no_hash_change_is_claimed_when_the_hash_before_is_unknown(bench):
    """A backend that loads anyway: the hash before was not reported, so "hash-change" is not what proves it."""
    bench.u64.running = None

    def runner(host, image, **kw):
        return {"result": "ok", "info": {"unique_id": "ABC123", "product": "Ultimate 64 Elite",
                                         "git_commit_hash": HASH_A}}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, ram_run=runner))
    assert (e.value.result.restart_proof, e.value.result.hash_changed) == ("none", None)
    bench.c64u.running = None           # the cold JTAG load proves itself; the hash change stays unknown
    r = vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u))
    assert (r.ok, r.restart_proof, r.hash_changed) == (True, "jtag-reconfigured", None)


def test_a_jtag_record_without_the_user_id_read_back_is_not_a_proven_reload(bench):
    def runner(host, bit, app, **kw):
        return {"cold": True, "user_id": None, "info": {"unique_id": "DEF456", "product": "C64 Ultimate",
                                                        "git_commit_hash": HASH_A}}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u, jtag_load=runner))
    assert e.value.stage == "verify" and e.value.result.loaded is False


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


def test_restore_loads_when_the_running_hash_only_shares_leading_digits_with_the_baseline(bench):
    bench.u64.running = HASH_BASE[:7] + "ff"
    r = vl.volatile_restore("u64e", **bench.opts(bench.u64))
    assert (r.ok, r.loaded, r.git_hash, r.restart_proof) == (True, True, HASH_BASE, "hash-change")


def test_a_baseline_of_the_other_family_is_refused_before_the_lock(bench):
    bench.toml.write_text(bench.toml.read_text().replace(
        "[devices.0a0b0c]\nram_run = true\n", "[devices.0a0b0c]\nram_run = true\n" + bench.baseline(bench.c_base)))
    dev = FakeU64(bench.ev, bench.known, uid="0A0B0C")
    with pytest.raises(jl.JtagLoadError) as e:
        with vl.volatile_session("both", bench.u_a, **bench.opts(dev)):
            pytest.fail("the body ran")
    assert e.value.stage == "baseline" and bench.ev == []


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_in_the_body_starts_no_restore(bench, interrupt):
    """Ctrl-C means stop: a minutes-long load is not started behind it (a second Ctrl-C would cut that load)."""
    with pytest.raises(interrupt):
        with vl.volatile_session("c64u", bench.c_a, **bench.opts(bench.c64u)) as s:
            raise interrupt()
    assert s.restore is None and s.restore_skipped and len(bench.names("load_fpga_and_upload")) == 1
    assert bench.c64u.get_info()["git_commit_hash"] == HASH_A          # the image is still what runs


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
    del bench.known[sha(bench.c_a_payload)]          # the image comes up reporting another hash
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


@pytest.mark.parametrize("value", [300, 300.0, "300", 3600])
def test_a_jtag_timeout_of_300_s_or_more_is_accepted(bench, value):
    assert vl.volatile_load("c64u", bench.c_a, **bench.opts(bench.c64u, jtag_timeout=value)).ok


@pytest.mark.parametrize("misses", [1, 2])
def test_a_box_that_misses_fewer_than_three_polls_is_not_dark(bench, misses):
    """Dark means three unanswered reads in a row. One or two misses of a busy box choose nothing: the product is
    read, the RAM-run is taken, and no "dark-then-answering" proof is on offer."""
    bench.u64.misses = misses
    reloads = []
    r = vl.volatile_load("u64e", bench.u_a, **bench.opts(
        bench.u64, u64_jtag_reload=lambda *a, **k: reloads.append(a)))
    assert (r.ok, r.dark_before, r.backend, r.restart_proof) == (True, False, "ram_run", "hash-change")
    assert reloads == []


def test_a_dark_u64_without_a_jtag_reload_is_refused_and_nothing_is_staged(bench):
    bench.u64.dark = True
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "backend" and e.value.result.dark_before is True
    assert not bench.names("put", "run", "open")


def test_dark_is_three_unanswered_reads_with_a_pause_between_them(bench):
    """A refused connection fails at once, so only the pauses give "dark" a duration: three reads at least 2 s
    apart, not three refusals inside one blip."""
    bench.u64.dark = True
    with pytest.raises(vl.VolatileLoadError):
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, sleep=lambda s: bench.ev.append(("sleep", s))))
    assert bench.ev == [("acquire", "device"), ("get_info",), ("sleep", 2.0), ("get_info",), ("sleep", 2.0),
                        ("get_info",), ("release", "device")]


def test_a_staged_file_that_is_already_gone_at_cleanup_is_not_an_error(bench):
    """ram_run deletes with missing_ok; the storage it is handed must pass that on."""
    bench.u64.drops_file_on_run = True
    r = vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert r.ok and "cleanup_error" not in r.record and bench.names("delete") == [("delete", "/USB1/rraaaaaa.u64")]


def test_a_dark_u64_goes_through_the_jtag_reload_seam_with_at_least_300_s(bench):
    bench.u64.dark = True
    elf = bench.tmp / "a.elf"
    elf.write_bytes(b"ELF")
    image = vl.dataclasses.replace(bench.u_a, jtag_image=elf, jtag_sha256=sha(b"ELF"))
    calls = []

    def reload(host, image_path, **kw):
        calls.append((host, image_path, kw))
        bench.u64.dark, bench.u64.running = False, HASH_A
        return {"result": "ok", "restart_proof": "no-answer-before", "info": bench.u64.get_info()}
    r = vl.volatile_load("u64e", image, **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert (r.ok, r.backend, r.dark_before, r.restart_proof) == (True, "u64_jtag_reload", True, "no-answer-before")
    (host, path, kw), = calls
    assert (host, path, kw["sha256"], kw["expect_git_hash"]) == ("u64e", elf, sha(b"ELF"), HASH_A)
    assert kw["timeout"] >= 300 and not bench.names("put", "run")
    with pytest.raises(vl.VolatileLoadError) as e:          # no JTAG image named: refused, the seam is not called
        bench.u64.dark = True
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert e.value.stage == "backend" and len(calls) == 1


def blaster(bench, dev, comes_up_as=HASH_A):
    """The merged U64 JTAG reload (blaster_load_u64.blaster_load behind volatile_load's seam) over that script's
    own stand-in for the far side's tools; the download brings the stand-in U64 up as *comes_up_as*."""
    remote = BlasterRemote(bench.ev)

    def runner(argv, timeout):
        reply = remote(argv, timeout)
        if argv[0] == "ssh" and "nios2-download -g" in argv[-1]:
            dev.dark, dev.running = False, comes_up_as
        return reply
    return remote, functools.partial(vl.blaster_reload, runner=runner, which=lambda tool: "/usr/bin/" + tool,
                                     drop_window=6)


def elf_image(bench, image, git_hash=HASH_A):
    path = bench.tmp / "app.elf"
    path.write_bytes(nios_elf(hash_=git_hash))
    return vl.dataclasses.replace(image, jtag_image=path, jtag_sha256=sha(path.read_bytes()))


def test_the_seam_is_wired_to_the_blaster_reload_and_a_dark_u64_comes_back_through_it(bench):
    assert vl.U64_JTAG_RELOAD is vl.blaster_reload
    bench.u64.dark = True
    remote, reload = blaster(bench, bench.u64)
    r = vl.volatile_load("u64e", elf_image(bench, bench.u_a), **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert (r.ok, r.loaded, r.backend, r.dark_before) == (True, True, "u64_jtag_reload", True)
    assert (r.restart_proof, r.git_hash, r.record["dark"]) == ("no-answer-before", HASH_A, True)
    assert remote.timeouts["download"] == 300 and not bench.names("put", "run")       # the default is the floor
    remote, reload = blaster(bench, bench.u64)
    bench.u64.dark, bench.u64.running = True, "f1a54ed00"
    vl.volatile_load("u64e", elf_image(bench, bench.u_a), **bench.opts(bench.u64, u64_jtag_reload=reload,
                                                                        jtag_timeout=900))
    assert remote.timeouts["download"] == 900


def test_a_reload_whose_restart_is_not_proven_is_not_reported_ok(bench):
    """The box misses this wrapper's three reads, then answers the reload's own read with the hash the image has:
    the download returns 0, REST never drops, and blaster_load says "ok-unproven". That is a failure here, with the
    stage "unproven" and what was read in the result, not a load with a proof of this wrapper's making."""
    bench.u64.misses, bench.u64.running = 3, HASH_A
    remote, reload = blaster(bench, bench.u64)
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", elf_image(bench, bench.u_a), **bench.opts(bench.u64, u64_jtag_reload=reload))
    r = e.value.result
    assert e.value.stage == "unproven" and r.record["result"] == "ok-unproven"
    assert (r.ok, r.loaded, r.restart_proof, r.git_hash, r.dark_before) == (False, False, "none", HASH_A, True)


@pytest.mark.parametrize("record", [{}, {"restart_proof": None}, {"restart_proof": "trust-me"},
                                    {"restart_proof": "hash-change", "result": "ok-unproven"}],
                         ids=["no-proof", "proof-none", "unknown-proof", "ok-unproven"])
def test_a_reload_record_without_a_known_proof_is_unproven(bench, record):
    bench.u64.dark = True

    def reload(host, image_path, **kw):
        bench.u64.dark, bench.u64.running = False, HASH_A
        return {"result": "ok", **record, "info": bench.u64.get_info()}
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", elf_image(bench, bench.u_a), **bench.opts(bench.u64, u64_jtag_reload=reload))
    assert e.value.stage == "unproven" and e.value.result.ok is False


def test_the_cli_exits_3_for_an_unproven_restart_and_2_for_any_other_failure(bench, monkeypatch, capsys):
    def fail(stage):
        def raiser(host, image, **kw):
            raise vl.VolatileLoadError(stage, f"[{stage}] x", vl.VolatileResult(ok=False, action="load", host=host))
        return raiser
    args = ["load", "u64e", str(bench.u_a.path), "--sha256", bench.u_a.sha256, "--expect-hash", HASH_A]
    monkeypatch.setattr(vl, "volatile_load", fail("unproven"))
    assert vl.main(args) == 3
    monkeypatch.setattr(vl, "volatile_load", fail("verify"))
    assert vl.main(args) == 2
    capsys.readouterr()


def test_without_a_wired_reload_a_dark_u64_is_refused(bench, monkeypatch):
    monkeypatch.setattr(vl, "U64_JTAG_RELOAD", None)
    bench.u64.dark = True
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", elf_image(bench, bench.u_a), **bench.opts(bench.u64))
    assert e.value.stage == "backend" and "power cycle" in str(e.value)
    assert bench.names("acquire", "release", "get_info") == bench.ev


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


@pytest.mark.parametrize("host", ["u64e", "c64u"])
def test_under_a_verified_lock_shell_the_session_takes_no_device_lock_and_both_backends_load(bench, monkeypatch,
                                                                                            host):
    """The shell's hold is the hold (#582): nothing is acquired for the device, and the RAM-run goes through as
    the JTAG load does."""
    fake_lock_shell(monkeypatch, host)
    dev, image = (bench.u64, bench.u_a) if host == "u64e" else (bench.c64u, bench.c_a)
    with vl.volatile_session(host, image, **bench.opts(dev)) as s:
        assert s.lock_in_process is False and s.load.ok and s.load.git_hash == HASH_A
    assert s.restore.ok and s.restore.git_hash == HASH_BASE and ("acquire", "device") not in bench.ev


@pytest.mark.parametrize("kw", FORGED_KW, ids=FORGED_IDS)
@pytest.mark.parametrize("host", ["u64e", "c64u"])
def test_a_lock_shell_claim_that_does_not_hold_is_refused_before_any_traffic(bench, monkeypatch, host, kw):
    forge(monkeypatch, host, kw)
    dev, image = (bench.u64, bench.u_a) if host == "u64e" else (bench.c64u, bench.c_a)
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load(host, image, **bench.opts(dev))
    assert e.value.stage == "lock" and bench.ev == []
    with pytest.raises(jl.JtagLoadError) as e:
        with vl.volatile_session(host, image, **bench.opts(dev)):
            pytest.fail("the body ran")
    assert e.value.stage == "lock" and bench.ev == []
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_restore(host, **bench.opts(dev))
    assert e.value.stage == "lock" and bench.ev == []


def test_a_lock_shell_for_another_device_covers_nothing_here(bench, monkeypatch):
    fake_lock_shell(monkeypatch, "c64u")
    assert vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64)).ok
    assert bench.ev[0] == ("acquire", "device") and bench.ev[-1] == ("release", "device")


def test_a_hold_this_process_already_has_is_joined_even_with_its_own_pid_exported(bench, monkeypatch, tmp_path):
    """A driver that holds the lock and is a lock shell for its children (its own PID in C64U_LOCK_PID): the same
    claim is refused when the process does not hold the lock (the forged case "names itself")."""
    def real(name, allow_nested=False):
        return DeviceLock(name, lock_dir=tmp_path / "locks", heartbeat_interval=None, allow_nested=allow_nested)
    outer = real("u64e", allow_nested=True)
    assert outer.acquire(timeout=5)
    try:
        monkeypatch.setenv("C64U_LOCK_PID", str(os.getpid()))
        monkeypatch.setenv("C64U_LOCK_HOST", "u64e")
        with vl.volatile_session("u64e", bench.u_a, **bench.opts(bench.u64, lock_factory=real)) as s:
            assert s.lock_in_process and s.load.ok
        assert lock_held_for("u64e")                # the caller's hold is still the caller's
    finally:
        outer.release()
    assert not lock_held_for("u64e")


def test_the_bindings_are_read_from_the_file_the_environment_names(bench, monkeypatch, tmp_path):
    """A live run points C64_BENCH_BINDINGS_FILE at a private file; the default bench.toml is not consulted."""
    shared = tmp_path / "xdg" / "c64-test-harness"
    shared.mkdir(parents=True)
    (shared / "bench.toml").write_text("[devices.abc123]\nram_run = true\n")       # no baseline there
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert vl.volatile_restore("u64e", dry_run=True, **bench.opts(bench.u64))["expect_git_hash"] == HASH_BASE
    monkeypatch.delenv("C64_BENCH_BINDINGS_FILE")
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_restore("u64e", dry_run=True, **bench.opts(bench.u64))
    assert e.value.stage == "baseline"


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


def test_a_file_already_at_the_staging_name_is_neither_overwritten_nor_deleted(bench):
    bench.u64.files["/USB1/rraaaaaa.u64"] = b"SOMEONE ELSE'S FILE"
    with pytest.raises(vl.VolatileLoadError) as e:
        vl.volatile_load("u64e", bench.u_a, **bench.opts(bench.u64))
    assert e.value.stage == "storage" and bench.u64.files == {"/USB1/rraaaaaa.u64": b"SOMEONE ELSE'S FILE"}
    assert not bench.names("put", "delete", "run")


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
