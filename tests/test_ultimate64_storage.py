"""Tests for the Ultimate persistent-storage helpers (FTP put/delete).

Functional: the helpers run against a fake FTP server that behaves like
the firmware's ``ftpd.cc`` (SIZE answers 550 for a missing path, MKD 550
for an existing one). Safety: the ``DeviceLock`` gate and the ``/Temp`` /
``/Flash`` refusals are checked before any connection is made.
"""

from __future__ import annotations

import ftplib
import hashlib
import io
from unittest.mock import patch

import pytest

from c64_test_harness.backends import ultimate64_storage as st
from c64_test_harness.backends.ultimate64_client import Ultimate64Error


class _FakeFTPServer:
    """Filesystem state shared by every fake connection in one test."""

    def __init__(self, dirs=("/", "/SD", "/USB1", "/Temp", "/Flash")):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set(dirs)
        self.connects = 0
        self.connect_error: Exception | None = None
        self.corrupt_reads = False
        self.log: list[str] = []
        #: listed at the root but CWD fails (an empty media slot)
        self.empty_slots: set[str] = set()


class _FakeFTP:
    server: _FakeFTPServer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def connect(self, host, port, timeout=10.0):
        self.server.connects += 1
        if self.server.connect_error is not None:
            raise self.server.connect_error
        self.server.log.append(f"connect {host}:{port}")

    def login(self, user, password):
        self.server.log.append("login")

    def cwd(self, path):
        self.server.log.append(f"CWD {path}")
        if path not in self.server.dirs:
            raise ftplib.error_perm("550 Requested action not taken.")
        return "250 OK"

    def quit(self):
        return "221 Bye"

    def close(self):
        return None

    def nlst(self, path="/"):
        self.server.log.append(f"NLST {path}")
        assert path == "/"
        listed = {d[1:] for d in self.server.dirs if d.count("/") == 1 and d != "/"}
        return sorted(listed | self.server.empty_slots)

    def voidcmd(self, cmd):
        self.server.log.append(cmd)
        return "200 OK"

    def size(self, path):
        self.server.log.append(f"SIZE {path}")
        if path in self.server.files:
            return len(self.server.files[path])
        if path in self.server.dirs:
            return 0
        raise ftplib.error_perm("550 Requested action not taken.")

    def mkd(self, path):
        self.server.log.append(f"MKD {path}")
        parent = path.rsplit("/", 1)[0] or "/"
        if path in self.server.dirs or parent not in self.server.dirs:
            raise ftplib.error_perm("550 Requested action not taken.")
        self.server.dirs.add(path)
        return path

    def storbinary(self, cmd, fp, blocksize=8192):
        path = cmd.split(" ", 1)[1]
        self.server.log.append(f"STOR {path}")
        parent = path.rsplit("/", 1)[0] or "/"
        if parent not in self.server.dirs:
            raise ftplib.error_perm("550 Requested action not taken.")
        self.server.files[path] = fp.read()
        return "226 Transfer complete"

    def retrbinary(self, cmd, callback, blocksize=8192):
        path = cmd.split(" ", 1)[1]
        self.server.log.append(f"RETR {path}")
        if path not in self.server.files:
            raise ftplib.error_perm("550 Requested action not taken.")
        data = self.server.files[path]
        if self.server.corrupt_reads:
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        callback(data)
        return "226 Transfer complete"

    def delete(self, path):
        self.server.log.append(f"DELE {path}")
        if path not in self.server.files:
            raise ftplib.error_perm("550 Requested action not taken.")
        del self.server.files[path]
        return "250 OK"


@pytest.fixture
def server():
    srv = _FakeFTPServer()
    fake = type("_BoundFakeFTP", (_FakeFTP,), {"server": srv})
    with patch.object(st, "FTP", fake), patch.object(st, "lock_held_for", lambda host: True):
        yield srv


DATA = bytes(range(256)) * 3521  # 901,376 B, ADF-sized


def test_put_creates_parents_writes_and_verifies(server):
    result = st.storage_put_file("dev", "/SD/amiga64/wb.adf", DATA)
    assert server.files["/SD/amiga64/wb.adf"] == DATA
    assert "/SD/amiga64" in server.dirs
    assert "TYPE I" in server.log
    assert result.path == "/SD/amiga64/wb.adf"
    assert result.size == len(DATA)
    assert result.sha256 == hashlib.sha256(DATA).hexdigest()
    assert result.verified is True
    assert result.replaced is False
    assert "RETR /SD/amiga64/wb.adf" in server.log


def test_put_accepts_a_local_path(server, tmp_path):
    src = tmp_path / "wb.adf"
    src.write_bytes(DATA)
    st.storage_put_file("dev", "/USB1/amiga64/wb.adf", src)
    assert server.files["/USB1/amiga64/wb.adf"] == DATA


def test_put_refuses_to_overwrite_by_default(server):
    server.dirs.add("/SD/amiga64")
    server.files["/SD/amiga64/wb.adf"] = b"old"
    with pytest.raises(FileExistsError):
        st.storage_put_file("dev", "/SD/amiga64/wb.adf", DATA)
    assert server.files["/SD/amiga64/wb.adf"] == b"old"
    assert not any(e.startswith("STOR") for e in server.log)


def test_put_overwrite_replaces(server):
    server.dirs.add("/SD/amiga64")
    server.files["/SD/amiga64/wb.adf"] = b"old"
    result = st.storage_put_file("dev", "/SD/amiga64/wb.adf", DATA, overwrite=True)
    assert server.files["/SD/amiga64/wb.adf"] == DATA
    assert result.replaced is True


def test_put_refuses_a_directory_target(server):
    server.dirs.add("/SD/amiga64")
    with pytest.raises(IsADirectoryError):
        st.storage_put_file("dev", "/SD/amiga64", DATA, overwrite=True)


def test_put_verify_mismatch_raises(server):
    server.corrupt_reads = True
    with pytest.raises(st.Ultimate64StorageError, match="read back"):
        st.storage_put_file("dev", "/SD/x.bin", DATA)


def test_put_verify_false_skips_the_read_back(server):
    server.corrupt_reads = True
    result = st.storage_put_file("dev", "/SD/x.bin", DATA, verify=False)
    assert result.verified is False
    assert not any(e.startswith("RETR") for e in server.log)
    # the size check still runs
    assert "SIZE /SD/x.bin" in server.log


def test_put_size_mismatch_raises_even_without_verify(server):
    real_stor = _FakeFTP.storbinary

    def short_stor(self, cmd, fp, blocksize=8192):
        r = real_stor(self, cmd, fp, blocksize)
        path = cmd.split(" ", 1)[1]
        self.server.files[path] = self.server.files[path][:-10]
        return r

    with patch.object(_FakeFTP, "storbinary", short_stor):
        with pytest.raises(st.Ultimate64StorageError, match="size"):
            st.storage_put_file("dev", "/SD/x.bin", DATA, verify=False)


def test_delete_removes_the_file(server):
    server.files["/SD/x.bin"] = b"x"
    st.storage_delete_file("dev", "/SD/x.bin")
    assert "/SD/x.bin" not in server.files


def test_delete_missing_raises_unless_missing_ok(server):
    with pytest.raises(FileNotFoundError):
        st.storage_delete_file("dev", "/SD/nope.bin")
    assert st.storage_delete_file("dev", "/SD/nope.bin", missing_ok=True) is False


def test_delete_refuses_a_directory(server):
    server.dirs.add("/SD/amiga64")
    with pytest.raises(IsADirectoryError):
        st.storage_delete_file("dev", "/SD/amiga64")
    assert "/SD/amiga64" in server.dirs


# ---------------------------------------------------------------- safety gates
@pytest.mark.parametrize(
    "path",
    [
        "/Temp/x.bin",
        "/temp/x.bin",
        "/TEMP",
        "/Flash/x.bin",
        "/flash/roms/x.rom",
        "/SD/../Temp/x.bin",
        "/SD/./x.bin",
        "SD/x.bin",
        "/SD//x.bin",
        "/SD/x.bin/",
        "/",
        "",
        "/SD/a\nb",
        "/SD",
    ],
)
def test_write_refused_paths_never_connect(server, path):
    with pytest.raises(ValueError):
        st.storage_put_file("dev", path, b"x")
    with pytest.raises(ValueError):
        st.storage_delete_file("dev", path)
    with pytest.raises(ValueError):
        st.storage_mkdir("dev", path)
    assert server.connects == 0


def test_unlocked_process_is_refused_before_connecting():
    srv = _FakeFTPServer()
    fake = type("_BoundFakeFTP", (_FakeFTP,), {"server": srv})
    with patch.object(st, "FTP", fake), patch.object(st, "lock_held_for", lambda host: False):
        with pytest.raises(st.Ultimate64StorageError, match="DeviceLock"):
            st.storage_put_file("dev", "/SD/x.bin", b"x")
        with pytest.raises(st.Ultimate64StorageError, match="DeviceLock"):
            st.storage_delete_file("dev", "/SD/x.bin")
    assert srv.connects == 0


def test_lock_gate_is_keyed_on_the_host():
    seen = []
    srv = _FakeFTPServer()
    fake = type("_BoundFakeFTP", (_FakeFTP,), {"server": srv})

    def held(host):
        seen.append(host)
        return host == "10.0.0.1"

    with patch.object(st, "FTP", fake), patch.object(st, "lock_held_for", held):
        st.storage_put_file("10.0.0.1", "/SD/x.bin", b"x")
        with pytest.raises(st.Ultimate64StorageError):
            st.storage_put_file("10.0.0.2", "/SD/x.bin", b"x")
    assert seen == ["10.0.0.1", "10.0.0.2"]


def test_ftp_refused_is_a_storage_error_naming_the_remedy(server):
    server.connect_error = ConnectionRefusedError(61, "Connection refused")
    with pytest.raises(st.Ultimate64StorageError, match="FTP File Service"):
        st.storage_put_file("dev", "/SD/x.bin", b"x")


def test_storage_error_is_an_ultimate64_error():
    assert issubclass(st.Ultimate64StorageError, Ultimate64Error)


def test_put_rejects_non_bytes_data(server):
    with pytest.raises(TypeError):
        st.storage_put_file("dev", "/SD/x.bin", "text")  # type: ignore[arg-type]


# ---------------------------------------------------------------- volumes, get, mkdir
def test_volumes_lists_the_root(server):
    assert st.storage_volumes("dev") == ["Flash", "SD", "Temp", "USB1"]


def test_missing_volume_is_a_typed_error_listing_what_exists(server):
    server.dirs.discard("/USB1")
    with pytest.raises(st.Ultimate64StorageVolumeError) as ei:
        st.storage_mkdir("dev", "/USB1/trust")
    assert ei.value.available == ["Flash", "SD", "Temp"]
    assert "/SD" in str(ei.value)
    assert not any(e.startswith("MKD") for e in server.log)
    for call in (
        lambda: st.storage_put_file("dev", "/USB1/x.bin", b"x"),
        lambda: st.storage_get_file("dev", "/USB1/x.bin"),
        lambda: st.storage_delete_file("dev", "/USB1/x.bin", missing_ok=True),
    ):
        with pytest.raises(st.Ultimate64StorageVolumeError):
            call()
    assert not any(e.startswith(("STOR", "RETR", "DELE")) for e in server.log)


def test_volume_error_is_a_storage_error():
    assert issubclass(st.Ultimate64StorageVolumeError, st.Ultimate64StorageError)


def test_volume_name_is_matched_case_insensitively_and_device_spelling_used(server):
    result = st.storage_put_file("dev", "/usb1/amiga64/wb.adf", b"abc")
    assert result.path == "/USB1/amiga64/wb.adf"
    assert server.files["/USB1/amiga64/wb.adf"] == b"abc"


def test_get_reads_back_and_may_read_any_volume(server):
    server.files["/Flash/config.cfg"] = b"cfg"
    server.files["/SD/x.bin"] = b"payload"
    assert st.storage_get_file("dev", "/SD/x.bin") == b"payload"
    assert st.storage_get_file("dev", "/Flash/config.cfg") == b"cfg"


def test_get_missing_and_directory(server):
    with pytest.raises(FileNotFoundError):
        st.storage_get_file("dev", "/SD/nope")
    server.dirs.add("/SD/d")
    with pytest.raises(IsADirectoryError):
        st.storage_get_file("dev", "/SD/d")


def test_get_requires_the_lock():
    srv = _FakeFTPServer()
    fake = type("_BoundFakeFTP", (_FakeFTP,), {"server": srv})
    with patch.object(st, "FTP", fake), patch.object(st, "lock_held_for", lambda host: False):
        with pytest.raises(st.Ultimate64StorageError, match="DeviceLock"):
            st.storage_get_file("dev", "/SD/x.bin")
        with pytest.raises(st.Ultimate64StorageError, match="DeviceLock"):
            st.storage_volumes("dev")
        with pytest.raises(st.Ultimate64StorageError, match="DeviceLock"):
            st.storage_mkdir("dev", "/SD/d")
    assert srv.connects == 0


def test_mkdir_creates_parents_and_honours_exist_ok(server):
    assert st.storage_mkdir("dev", "/SD/a/b/c") is True
    assert {"/SD/a", "/SD/a/b", "/SD/a/b/c"} <= server.dirs
    assert st.storage_mkdir("dev", "/SD/a/b/c") is False
    with pytest.raises(FileExistsError):
        st.storage_mkdir("dev", "/SD/a/b/c", exist_ok=False)


def test_mkdir_refuses_when_a_file_is_in_the_way(server):
    server.files["/SD/a"] = b"file"
    with pytest.raises(FileExistsError):
        st.storage_mkdir("dev", "/SD/a/b")
    with pytest.raises(FileExistsError):
        st.storage_put_file("dev", "/SD/a/b.bin", b"x")
    assert "/SD/a/b" not in server.dirs


def test_transfer_error_becomes_a_storage_error(server):
    def boom(self, cmd, fp, blocksize=8192):
        raise ftplib.error_temp("451 Requested action aborted")

    with patch.object(_FakeFTP, "storbinary", boom):
        with pytest.raises(st.Ultimate64StorageError, match="451"):
            st.storage_put_file("dev", "/SD/x.bin", b"x")


def test_an_empty_media_slot_is_not_a_usable_volume(server):
    """C64U fw 1.1.0, 2026-10-08: /SD listed at the root, but CWD /SD 550, MKD 553."""
    server.dirs.discard("/SD")
    server.empty_slots.add("SD")
    assert st.storage_volumes("dev") == ["Flash", "Temp", "USB1"]
    with pytest.raises(st.Ultimate64StorageVolumeError, match="no medium") as ei:
        st.storage_put_file("dev", "/SD/amiga64/wb.adf", b"x")
    assert ei.value.available == ["Flash", "Temp", "USB1"]
    assert not any(e.startswith(("MKD", "STOR")) for e in server.log)
