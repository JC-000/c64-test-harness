"""Device-free tests of scripts/ram_run_u64.py: the gates around a U64E RAM-run ("Run Update" on a RAM image)."""
import hashlib
import os
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jl, "normalize_device_host", lambda h: "uid-601a96" if h == "u64e" else h)
    img = tmp_path / "r.u64"
    img.write_bytes(b"\x00" * 64)
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
        rr.ram_run("10.43.23.26", img, sha256=sha, expect_git_hash="x", storage=Storage(ev))
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
    rec = run(env, [info("17995fd9c"), info("17995fd9c"), None, info("17995fd9c")])
    assert rec["result"] == "ok" and rec["rest_drop_seen"] is True
