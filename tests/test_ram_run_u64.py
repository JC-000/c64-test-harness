"""Device-free tests of scripts/ram_run_u64.py: the gates around a U64E RAM-run ("Run Update" on a RAM image).

The telnet menu is tests/remote_menu_fake.py on 127.0.0.1, driven by the harness's Remote Menu driver; REST, FTP
and the lock are stand-ins that record calls. Nothing here talks to a device.
"""
import hashlib
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import jtag_load as jl  # noqa: E402
import ram_run_u64 as rr  # noqa: E402
from c64_test_harness.backends.device_lock import DeviceLock  # noqa: E402
from c64_test_harness.backends.ultimate64_remote_menu import (  # noqa: E402
    RemoteMenuTiming, Ultimate64RemoteMenu)
from remote_menu_fake import Entry, FakeUltimate  # noqa: E402

FAST = RemoteMenuTiming(connect_timeout=2.0, first_byte_timeout=0.5, idle_gap=0.03, max_drain=5.0,
                        poll_interval=0.05, first_probe_delay=0.05)
MENU_KEYS = {"UP", "DOWN", "RIGHT", "LEFT", "PAGEUP", "PAGEDOWN", "ENTER", "o"}


class Lock:
    def __init__(self, ev, name, allow_nested=False):
        self.ev, self.name = ev, name

    def acquire(self, timeout=None):
        self.ev.append(("acquire",))
        return True

    def release(self):
        self.ev.append(("release",))


class Client:
    def __init__(self, ev, script):
        self.ev, self.script = ev, list(script)

    def __call__(self, host, timeout=5.0):
        return self

    def get_info(self):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if item is None:
            raise ConnectionError
        return item


class Storage:
    """FTP put/delete, recorded; with a fake menu, the upload appears in its listing (as *kind*, unless not listed)."""

    def __init__(self, ev, fake=None, listed=True, kind=None):
        self.ev, self.fake, self.listed, self.kind = ev, fake, listed, kind

    def storage_put_file(self, host, path, data, overwrite=False):
        self.ev.append(("put", path, hashlib.sha256(data).hexdigest()))
        if self.fake is not None and self.listed:
            d, name = path.rsplit("/", 1)
            kind = self.kind or name.rsplit(".", 1)[1].upper()
            self.fake.tree.setdefault(d + "/", []).append(Entry(name, kind, "1272K"))

    def storage_delete_file(self, host, path, missing_ok=False):
        self.ev.append(("delete", path))
        if self.fake is not None:
            d, name = path.rsplit("/", 1)
            self.fake.tree[d + "/"] = [e for e in self.fake.tree.get(d + "/", []) if e.name != name]


def info(h="17995fd9c", uid="601A96", product="Ultimate 64 Elite"):
    return {"unique_id": uid, "product": product, "git_commit_hash": h}


APP, STUB, MAGIC = 0x3001000, 0x3000000, 0x524D4152


def rec(load, payload, start):
    return struct.pack("<III", load, len(payload), start) + payload


def stub(app_len, magic=MAGIC, stage=APP):
    """A stub as tools/make_ramrun.py leaves it: code, then ramrun_params filled in."""
    return b"\x13" * 40 + struct.pack("<IIIII", magic, stage, 0x0, app_len, 0x20)


def ramrun_image(app=b"\xa5" * 64):
    """What make_ramrun.py writes: the app at the staging address with start 0, then the stub, start = stub."""
    return rec(APP, app, 0) + rec(STUB, stub(len(app)), STUB)


# /USB1 as the failing runs' logged screens drew it (2026-11-13, twice, and 2026-11-10): directories first, then
# the files. Upstream select_entry's default bound
# (max_steps=30, 21 visible rows) scans three screens and stops with the cursor at index 60, so the logged screen
# shows indexes 40-60; 40 directories therefore come before "mp33r10". About 100 entries, as the failure notes say.
LOGGED_DIRS = ["mp33r10", "mp33r8", "MP33r9", "N", "O", "P", "p1052", "printer", "printer-eb02e9", "Q", "R",
               "S", "SIDs etc", "T", "tlsbench", "U", "uaf", "ultimate_3.10", "ultimate_3.10a", "ultimate_3.14a",
               "V", "W", "Wheels_64_1999_CHS", "Wheels_64_1999_CHS working", "Wheels_64_1999_Click_Here_Software",
               "Wheels_64_1999_Click_working", "X", "x14", "x23", "x38", "x5"]
USB1_LONG = ([Entry(f"dir{i:02d}", "DIR") for i in range(40)] + [Entry(n, "DIR") for n in LOGGED_DIRS]
             + [Entry(f"y{i:02d}", "DIR") for i in range(20)]
             + [Entry("rr71b913.u64", "U64", "1272K"), Entry("update_m3.u64", "U64", "5609K")])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: "uid-601a96" if h == "u64e" else h)
    img = tmp_path / "r.u64"
    img.write_bytes(ramrun_image())
    return [], img, hashlib.sha256(img.read_bytes()).hexdigest()


@pytest.fixture
def menu(tmp_path):
    """make(tree=None, **knobs) -> (fake, menu_factory). Holds the DeviceLock the driver checks for 127.0.0.1,
    as jl._Locked does for the real host."""
    fakes = []
    lock = DeviceLock("127.0.0.1", lock_dir=tmp_path / "locks", heartbeat_interval=None)
    assert lock.acquire(timeout=5)

    def make(tree=None, **kw):
        fake = FakeUltimate(tree=tree if tree is not None else {"/USB1/": [Entry("Backups", "DIR")]}, **kw)
        host, port = fake.start()
        fakes.append(fake)
        return fake, lambda h: Ultimate64RemoteMenu(host, port=port, timing=FAST, connector=fake.connector)

    yield make
    lock.release()
    for f in fakes:
        for s in f.sessions:
            assert set(s.keys) <= MENU_KEYS, s.keys
        f.stop()


def ticks():
    """A clock that advances one second per reading, so a REST wait never runs in real time."""
    t = [0.0]
    return lambda: t.__setitem__(0, t[0] + 1) or t[0]


def no_menu(host):
    raise AssertionError("the telnet menu was reached")


def run(env, client_script, m=None, storage=None, **kw):
    ev, img, sha = env
    fake, factory = m if m is not None else (None, no_menu)
    args = dict(sha256=sha, expect_git_hash="17995fd9c", lock_factory=lambda n, allow_nested=False: Lock(ev, n),
                client_factory=Client(ev, client_script), menu_factory=factory,
                storage=storage or Storage(ev, fake), sleep=lambda s: None, clock=ticks(), log=lambda m: None)
    args.update(kw)
    return rr.ram_run("u64e", img, **args)


def committing(fake):
    return [s for s in fake.sessions if s.commits]


def test_success_sequence(env, menu):
    ev, img, sha = env
    fake, factory = menu()
    rec = run(env, [info("218e36a52"), info("218e36a52"), None, info("17995fd9c")], (fake, factory))
    assert rec["result"] == "ok" and rec["menu"][0] == "Run Update"
    assert [e[0] for e in ev] == ["acquire", "put", "delete", "release"]
    assert ev[1] == ("put", "/USB1/rr17995f.u64", sha)
    assert fake.all_commits() == [("Run Update", "/USB1/")]
    (s,) = committing(fake)
    # Opening the context menu, then the one Run Update. Raw bytes: the fake logs no keys once the app has stopped.
    assert bytes(s.raw).count(b"\r") == 2


def test_pinned_hash_refuses_before_lock(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info()], sha256="0" * 64)
    assert e.value.stage == "hash" and ev == []


def test_c64u_is_not_enabled(env):
    ev, img, sha = env
    with pytest.raises(jl.JtagLoadError) as e:
        rr.ram_run("192.0.2.26", img, sha256=sha, expect_git_hash="x", storage=Storage(ev))
    assert e.value.stage == "binding"


def test_other_product_answering_refuses_before_upload(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError):
        run(env, [info(product="Ultimate 64-II")])
    assert not any(e[0] == "put" for e in ev)


def test_no_run_update_entry_aborts_and_cleans_up(env, menu):
    ev = env[0]
    fake, factory = menu()
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info()], (fake, factory), storage=Storage(ev, fake, kind="PRG"))
    assert e.value.stage == "menu" and "Run Update" in str(e.value)
    assert ("delete", "/USB1/rr17995f.u64") in ev and fake.all_commits() == []


def test_same_hash_after_restart_is_not_success(env, menu):
    t = [0.0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52"), None, info("218e36a52")], menu(), up_timeout=10,
            clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    assert e.value.stage == "network"


def test_never_runs_another_file(env, menu):
    """The upload is not in the listing (it did not land, or the listing is another folder's) while another lane's
    rr file is: an exact-name search refuses, where a prefix search would have offered the neighbour."""
    ev = env[0]
    fake, factory = menu({"/USB1/": [Entry("rr17995e.u64", "U64", "1272K"), Entry("rr17995fd.u64", "U64", "1K")]})
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52")], (fake, factory), storage=Storage(ev, fake, listed=False))
    assert e.value.stage == "menu" and "not in /USB1/" in str(e.value)
    assert fake.all_commits() == [] and ("delete", "/USB1/rr17995f.u64") in ev


def test_never_runs_another_menu_entry(env, menu):
    """A highlight that does not move where the key says (UP and DOWN swapped) stops before any ENTER on it."""
    ev = env[0]
    fake, factory = menu(menu_initial=2, menu_keys_swapped=True)
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52")], (fake, factory))
    assert e.value.stage == "menu" and fake.all_commits() == [] and ("delete", "/USB1/rr17995f.u64") in ev


def test_hash_change_proves_restart_without_seeing_the_drop(env, menu):
    rec = run(env, [info("218e36a52"), info("17995fd9c")], menu())
    assert rec["result"] == "ok" and rec["rest_drop_seen"] is False


def test_same_hash_needs_the_drop(env, menu):
    ev = env[0]
    t = [0.0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("17995fd9c")], menu(), down_timeout=5, clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    assert e.value.stage == "run"
    ev.clear()
    t[0] = 0.0
    with pytest.raises(jl.JtagLoadError):      # one missed poll is not a restart
        run(env, [info("17995fd9c"), info("17995fd9c"), None, info("17995fd9c")], menu(), down_timeout=20,
            clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    rec = run(env, [info("17995fd9c"), info("17995fd9c"), None, None, None, info("17995fd9c")], menu())
    assert rec["result"] == "ok" and rec["rest_drop_seen"] is True


def lane_call(env, fake, factory, volume, client_script, **kw):
    """ram_run exactly as its callers call it: image path, sha256=,
    expect_git_hash=, volume= (and stem=); only the device stand-ins are injected."""
    ev, img, sha = env
    return rr.ram_run("u64e", img, sha256=sha, expect_git_hash="17995fd9c", volume=volume, **kw,
                      lock_factory=lambda n, allow_nested=False: Lock(ev, n), client_factory=Client(ev, client_script),
                      menu_factory=factory, storage=Storage(ev, fake), sleep=lambda s: None, clock=ticks(),
                      log=lambda m: None)


def test_long_usb1_listing_from_the_failing_runs(env, menu):
    fake, factory = menu({"/USB1/": list(USB1_LONG)})
    rec = lane_call(env, fake, factory, "USB1", [info("218e36a52"), info("17995fd9c")])
    assert rec["result"] == "ok"
    assert fake.all_commits() == [("Run Update", "/USB1/")]
    (s,) = committing(fake)
    assert bytes(s.raw).count(b"\r") == 2 and "PAGEDOWN" in s.keys
    assert ("delete", "/USB1/rr17995f.u64") in env[0]


def test_one_file_folder(env, menu):
    fake, factory = menu({"/USB1/": [Entry("rrone", "DIR")], "/USB1/rrone/": []})
    rec = lane_call(env, fake, factory, "USB1/rrone", [info("218e36a52"), info("17995fd9c")], stem="st1")
    assert rec["result"] == "ok"
    assert fake.all_commits() == [("Run Update", "/USB1/rrone/")]


def test_the_menu_path_itself_refuses_a_plain_image(menu):
    """run_update_ramrun skips the flash path's power-off wait, so it checks the image again before connecting."""
    fake, factory = menu({"/USB1/": [Entry("plain.u64", "U64", "1K")]})
    with pytest.raises(jl.JtagLoadError) as e:
        rr.run_update_ramrun(factory("127.0.0.1"), "/USB1/plain.u64", rec(STUB, b"\x5a" * 256, STUB),
                             log=lambda m: None)
    assert e.value.stage == "image" and fake.connect_attempts == 0


def test_a_missing_file_in_a_long_listing_stops_at_the_end_of_it(env, menu):
    """The upload did not land in the /USB1 of the failing runs: the search pages down once past the last screen,
    sees nothing move, and stops. Bounded in keys sent to a menu that other lanes share, not in wall time."""
    ev = env[0]
    fake, factory = menu({"/USB1/": list(USB1_LONG)})
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52")], (fake, factory), storage=Storage(ev, fake, listed=False))
    assert e.value.stage == "menu" and "RemoteMenuNotFoundError" in str(e.value) and "not in /USB1/" in str(e.value)
    (s,) = fake.sessions
    pages = -(-len(USB1_LONG) // (21 // 2))           # PAGEDOWN moves half the 21-row window (tree_browser.cc)
    # 2 DOWN + RIGHT to reach /USB1, one PAGEUP that finds the top, the pages, and one PAGEDOWN that moves nothing.
    assert s.keys.count("PAGEDOWN") <= pages + 1 and len(s.keys) <= 2 + 1 + 1 + pages + 1, s.keys
    assert fake.all_commits() == [] and ("delete", "/USB1/rr17995f.u64") in ev


def test_a_key_that_moves_nothing_stops_the_search(env, menu):
    """The cursor is read back after every key: a DOWN that drew nothing is not resent, and nothing is run."""
    ev = env[0]
    root = [("USB1", "Ultra Fit", "Ready"), ("Temp", "RAM Disk", "Ready"), ("Flash", "Flash Disk", "Ready")]
    fake, factory = menu({"/USB1/": [Entry("Backups", "DIR")]}, root=root, dead_keys=("DOWN",))
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52")], (fake, factory))
    assert e.value.stage == "menu" and "no visible effect" in str(e.value)
    (s,) = fake.sessions
    assert s.keys.count("DOWN") == 1 and fake.all_commits() == [] and ("delete", "/USB1/rr17995f.u64") in ev


def _with_image(env, data):
    ev, img, _ = env
    img.write_bytes(data)
    return ev, img, hashlib.sha256(data).hexdigest()


def test_plain_update_image_is_refused_before_lock_and_upload(env):
    """A plain update.u64 (one record, start = its load address) would FLASH the device on Run Update."""
    plain = rec(0x3000000, b"\x5a" * 256, 0x3000000)
    env = _with_image(env, plain)
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52"), info("17995fd9c")])
    assert e.value.stage == "image"
    assert env[0] == []         # no lock, no client, no upload, no key


def _odd_stub(app_len):
    return stub(app_len) + b"\x01\x02\x03"          # a stub whose length is not a multiple of 4


@pytest.mark.parametrize("data", [
    ramrun_image(),
    rec(APP, b"\xa5" * 64, 0) + rec(STUB, _odd_stub(64), STUB),
    rec(APP, b"\xa5" * 64, 0) + rec(STUB, _odd_stub(64), STUB) + b"\x00",   # hex2bin's padding to 4
    rec(APP, b"\xa5" * 64, 0) + rec(STUB, b"RAMR" + stub(64), STUB),        # make_ramrun.py takes the LAST magic
], ids=["make_ramrun", "odd-stub", "odd-stub-padded", "magic-twice"])
def test_ramrun_images_pass(data):
    rr.check_ramrun_image(data)


A, S = b"\xa5" * 64, stub(64)


@pytest.mark.parametrize("data", [
    rec(APP, A, 0) + rec(STUB, S, STUB) + rec(STUB + 0x100, b"\x00" * 4, STUB),          # three records
    rec(STUB, S, STUB) + rec(APP, A, 0),                                                 # wrong order
    rec(APP, A, APP) + rec(STUB, S, STUB),                                               # app carries a start
    rec(APP + 0x1000, A, 0) + rec(STUB, stub(64, stage=APP + 0x1000), STUB),             # app not at staging addr
    rec(APP, A, 0) + rec(STUB + 0x10, S, STUB),                                          # stub not where it starts
    rec(APP, A, 0) + rec(STUB, S, STUB + 4),                                             # stub start elsewhere
    rec(APP, A, 0) + rec(STUB, stub(64, magic=0x52414D52), STUB),                        # no RAMR magic
    rec(APP, A, 0) + rec(STUB, b"\0" * 3 + struct.pack("<IIII", APP, 0, 64, 0x20), STUB),  # params, no magic
    rec(APP, A, 0) + rec(STUB, b"\x13" * 40 + struct.pack("<III", MAGIC, APP, 0), STUB),  # block cut short
    rec(APP, A, 0) + rec(STUB, stub(64, stage=STUB), STUB),                              # params: other source
    rec(APP, A, 0) + rec(STUB, stub(60), STUB),                                          # params: other length
    rec(APP, A, 0) + rec(STUB, S[:-4] + b"\x00" * 4, STUB),                              # params: entry 0
    rec(APP, A, 0) + rec(STUB, S, STUB)[:-1],                                            # truncated record
    rec(APP, A, 0) + rec(STUB, S, STUB) + b"\x00" * 5,                                   # partial header after
    rec(APP, A, 0) + rec(STUB, _odd_stub(64), STUB) + b"\x00" * 2,                       # more than its padding
    rec(APP, A + b"\x01", 0) + rec(STUB, stub(65), STUB),                                # unaligned app, loader misreads
    rec(STUB, A, STUB),                                                                  # plain image, one record
    b"",
], ids=["three", "order", "app-start", "app-load", "stub-load", "stub-start", "magic", "no-magic-params", "short-block", "param-source",
        "param-length", "param-entry", "truncated", "partial-header", "too-much-padding", "unaligned-app",
        "plain", "empty"])
def test_anything_else_is_refused(data):
    with pytest.raises(jl.JtagLoadError) as e:
        rr.check_ramrun_image(data)
    assert e.value.stage == "image"


@pytest.mark.parametrize("shell_host", [None, "u64e"])
def test_ram_run_takes_its_own_lock_even_under_a_lock_shell(env, menu, monkeypatch, shell_host):
    """The storage helpers and the menu check this process's own lock (lock_held_for), so a lock shell's hold does
    not carry them: ram_run refuses under one, before taking anything, instead of failing half-way."""
    import c64_test_harness.backends.device_lock as dl
    ev = env[0]
    monkeypatch.setenv("C64U_LOCK_PID", "4242")
    if shell_host:
        monkeypatch.setenv("C64U_LOCK_HOST", shell_host)
    monkeypatch.setattr(dl, "device_lock_holder", lambda h: {"pid": 4242})
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52"), info("17995fd9c")], menu())
    assert e.value.stage == "lock" and ev == []


def test_a_lock_shell_for_another_device_does_not_stop_ram_run(env, menu, monkeypatch):
    monkeypatch.setenv("C64U_LOCK_PID", "4242")
    monkeypatch.setenv("C64U_LOCK_HOST", "192.0.2.99")
    rec = run(env, [info("218e36a52"), info("17995fd9c")], menu())
    assert rec["result"] == "ok" and env[0][0] == ("acquire",)
