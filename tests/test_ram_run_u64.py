"""Device-free tests of scripts/ram_run_u64.py: the gates around a U64E RAM-run ("Run Update" on a RAM image)."""
import hashlib
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import jtag_load as jl  # noqa: E402
import ram_run_u64 as rr  # noqa: E402


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
    def __init__(self, ev):
        self.ev = ev

    def storage_put_file(self, host, path, data, overwrite=False):
        self.ev.append(("put", path, hashlib.sha256(data).hexdigest()))

    def storage_delete_file(self, host, path, missing_ok=False):
        self.ev.append(("delete", path))


class Browser:
    def __init__(self, ev, labels=("Run Update", "Rename", "Delete"), picks=None, highlight=None):
        self.ev, self.labels, self.picks, self.highlight = ev, list(labels), picks, highlight
        self.sel = ""
        outer = self

        class Backend:
            def close(self_):
                outer.ev.append(("socket_close",))
        self.backend = Backend()

    def go_to_directory(self, d):
        self.ev.append(("cd", d))

    def select_entry(self, prefix):
        self.sel = (self.picks or prefix).upper() + ".U64"

    def selected_text(self):
        return self.sel

    def open_context_menu(self):
        return self.labels

    def select_framed_entry(self, label):
        return (0, 0, 9, 9), (self.highlight or label) + "    "

    def press(self, k):
        self.ev.append(("press", k))

    def close(self):
        self.ev.append(("telnet_close",))


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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: "uid-601a96" if h == "u64e" else h)
    img = tmp_path / "r.u64"
    img.write_bytes(ramrun_image())
    return [], img, hashlib.sha256(img.read_bytes()).hexdigest()


def run(env, client_script, browser=None, **kw):
    ev, img, sha = env
    args = dict(sha256=sha, expect_git_hash="17995fd9c", lock_factory=lambda n, allow_nested=False: Lock(ev, n),
                client_factory=Client(ev, client_script), browser_factory=lambda h: browser or Browser(ev),
                storage=Storage(ev), sleep=lambda s: None, log=lambda m: None)
    args.update(kw)
    return rr.ram_run("u64e", img, **args)


def test_success_sequence(env):
    ev, img, sha = env
    rec = run(env, [info("218e36a52"), info("218e36a52"), None, info("17995fd9c")])
    assert rec["result"] == "ok"
    kinds = [e[0] for e in ev]
    assert kinds == ["acquire", "put", "cd", "press", "socket_close", "delete", "release"]
    assert ev[1] == ("put", "/USB1/rr17995f.u64", sha) and ev[3] == ("press", "ENTER")


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


def test_no_run_update_entry_aborts_and_cleans_up(env):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info()], browser=Browser(ev, labels=("Rename", "Delete")))
    assert e.value.stage == "menu"
    assert ("delete", "/USB1/rr17995f.u64") in ev and ("press", "ENTER") not in ev


def test_same_hash_after_restart_is_not_success(env, monkeypatch):
    ev = env[0]
    t = [0.0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52"), None, info("218e36a52")], up_timeout=10,
            clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    assert e.value.stage == "network"


@pytest.mark.parametrize("bkw", [{"picks": "rr218e36"}, {"highlight": "Rename"}])
def test_never_runs_another_file_or_entry(env, bkw):
    ev = env[0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("218e36a52")], browser=Browser(ev, **bkw))
    assert e.value.stage == "menu" and ("press", "ENTER") not in ev and ("delete", "/USB1/rr17995f.u64") in ev


def test_hash_change_proves_restart_without_seeing_the_drop(env):
    ev = env[0]
    rec = run(env, [info("218e36a52"), info("17995fd9c")])
    assert rec["result"] == "ok" and rec["rest_drop_seen"] is False


def test_same_hash_needs_the_drop(env):
    ev = env[0]
    t = [0.0]
    with pytest.raises(jl.JtagLoadError) as e:
        run(env, [info("17995fd9c")], down_timeout=5, clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    assert e.value.stage == "run"
    ev.clear()
    t[0] = 0.0
    with pytest.raises(jl.JtagLoadError):      # one missed poll is not a restart
        run(env, [info("17995fd9c"), info("17995fd9c"), None, info("17995fd9c")], down_timeout=20,
            clock=lambda: t.__setitem__(0, t[0] + 1) or t[0])
    rec = run(env, [info("17995fd9c"), info("17995fd9c"), None, None, None, info("17995fd9c")])
    assert rec["result"] == "ok" and rec["rest_drop_seen"] is True


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
