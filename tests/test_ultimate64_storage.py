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
        #: MLST of a missing path: 501 at 1.1.0, 550 at 3a1ff9ff (ftpd.cc)
        self.mlst_missing = "501 Syntax error in parameters or arguments."


_MOUNTABLE = (".d64", ".d71", ".d81", ".dnp", ".t64", ".iso", ".fat")


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
        # ftpd.cc vfs_chdir opens a mountable image (find_mount_point), so
        # CWD into a .d64 succeeds on the firmware (U64E 3a1ff9ff, 2026-10-08).
        if path in self.server.dirs or (
            path in self.server.files and path.lower().endswith(_MOUNTABLE)
        ):
            return "250 OK"
        raise ftplib.error_perm("550 Requested action not taken.")

    def sendcmd(self, cmd):
        self.server.log.append(cmd)
        verb, _, arg = cmd.partition(" ")
        if verb == "MLST":
            # ftpd.cc cmd_mlst: vfs_stat without mounting; 501 when missing.
            # vfs_stat enters a mountable image on the way to a deeper path
            # (find_pathentry -> find_mount_point): record that it happened.
            for f in self.server.files:
                if arg.startswith(f + "/"):
                    self.server.log.append(f"MOUNTED {f}")
            if arg in self.server.dirs:
                kind = "dir"
            elif arg in self.server.files:
                kind = "file"
            else:
                raise ftplib.error_perm(self.server.mlst_missing)
            name = arg.rsplit("/", 1)[-1]
            return f"250- Listing {name}\r\ntype={kind};modify=19800101000000; {name}\r\n250 End"
        raise AssertionError(f"unexpected sendcmd {cmd!r}")

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
            # ftpd.cc cmd_mkd answers 553 (1.1.0 and 3a1ff9ff)
            raise ftplib.error_perm("553 Requested action not taken.")
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

    def mlsd(self, path="", facts=()):
        self.server.log.append(f"MLSD {path}")
        if path not in self.server.dirs:
            raise ftplib.error_temp("451 Requested action aborted")
        prefix = path.rstrip("/") + "/"
        # FileManager::get_directory skips names starting with '.' (and
        # AM_HID entries) at 1.1.0 and 3a1ff9ff, so MLSD never shows them.
        for d in sorted(self.server.dirs):
            name = d[len(prefix):]
            if d.startswith(prefix) and "/" not in name and d != path and not name.startswith("."):
                yield name, {"type": "dir", "size": "0"}
        for f, data in sorted(self.server.files.items()):
            name = f[len(prefix):]
            if f.startswith(prefix) and "/" not in name and not name.startswith("."):
                yield name, {"type": "file", "size": str(len(data))}

    def rmd(self, path):
        self.server.log.append(f"RMD {path}")
        prefix = path + "/"
        if path not in self.server.dirs or any(
            x.startswith(prefix) for x in list(self.server.dirs) + list(self.server.files)
        ):
            raise ftplib.error_perm("550 Requested action not taken.")
        self.server.dirs.discard(path)
        return "250 OK"

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
        # the firmware's Path::cd treats '\\' as a separator and resolves '..'
        "/USB1/..\\Temp\\x",
        "/USB1/a\\..\\..\\Flash\\x",
        "/USB1\\..\\Temp/x",
        "/SD/a\\b",
        # 1.1.0's cmd_mlst sprintfs the name into the format argument of
        # send_msg (fixed upstream in #713, in 3a1ff9ff, not in 1.1.0)
        "/SD/100%s.d64",
        "/SD/a%n",
        "/SD/%",
        # stay clear of 1.1.0's 200-byte MLST buffer (63-char LFN + size)
        "/SD/" + "x" * 61,
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
    with pytest.raises(NotADirectoryError):
        st.storage_mkdir("dev", "/SD/a/b")
    with pytest.raises(NotADirectoryError):
        st.storage_put_file("dev", "/SD/a/b.bin", b"x")
    with pytest.raises(FileExistsError):
        st.storage_mkdir("dev", "/SD/a")
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


# ---------------------------------------------------------------- review round 1 (#526)
def test_backslash_is_refused_for_reads_too(server):
    with pytest.raises(ValueError, match="backslash"):
        st.storage_get_file("dev", "/USB1/..\\Flash\\config.cfg")
    assert server.connects == 0


def test_a_mountable_image_is_a_file_not_a_directory(server):
    """CWD into a .d64 succeeds on the firmware; MLST says type=file."""
    image = b"\x00" * 174848
    st.storage_put_file("dev", "/SD/disks/game.d64", image)
    assert st.storage_get_file("dev", "/SD/disks/game.d64") == image
    again = st.storage_put_file("dev", "/SD/disks/game.d64", b"\x01" * 174848, overwrite=True)
    assert again.replaced
    assert st.storage_delete_file("dev", "/SD/disks/game.d64") is True
    assert "/SD/disks/game.d64" not in server.files


def test_a_mountable_image_is_never_used_as_a_parent(server):
    server.dirs.add("/SD/disks")
    server.files["/SD/disks/game.d64"] = b"\x00" * 174848
    for call in (
        lambda: st.storage_put_file("dev", "/SD/disks/game.d64/inner.prg", b"x"),
        lambda: st.storage_mkdir("dev", "/SD/disks/game.d64/sub"),
        lambda: st.storage_get_file("dev", "/SD/disks/game.d64/inner.prg"),
        lambda: st.storage_delete_file("dev", "/SD/disks/game.d64/inner.prg", missing_ok=True),
    ):
        with pytest.raises(NotADirectoryError):
            call()
    assert not any(e.startswith(("STOR", "MKD", "RETR", "DELE")) for e in server.log)
    # classification is top-down: nothing is ever stat'ed through the image,
    # which would mount it (and on 1.1.0 leave a stale mount after a DELE)
    assert not any(e.startswith("MOUNTED") for e in server.log), server.log


def test_a_failed_new_upload_is_removed_so_a_retry_succeeds(server):
    real = _FakeFTP.storbinary

    def partial(self, cmd, fp, blocksize=8192):
        path = cmd.split(" ", 1)[1]
        self.server.log.append(f"STOR {path}")
        self.server.files[path] = fp.read()[:1024]
        raise TimeoutError("timed out")

    with patch.object(_FakeFTP, "storbinary", partial):
        with pytest.raises(st.Ultimate64StorageError, match="removed"):
            st.storage_put_file("dev", "/SD/wb.adf", DATA)
    assert "/SD/wb.adf" not in server.files
    with patch.object(_FakeFTP, "storbinary", real):
        assert st.storage_put_file("dev", "/SD/wb.adf", DATA).verified


def test_a_failed_new_upload_with_a_bad_read_back_is_removed(server):
    server.corrupt_reads = True
    with pytest.raises(st.Ultimate64StorageError, match="removed"):
        st.storage_put_file("dev", "/SD/x.bin", DATA)
    assert "/SD/x.bin" not in server.files


def test_a_failed_overwrite_says_the_old_file_is_gone(server):
    server.files["/SD/x.bin"] = b"old"

    def partial(self, cmd, fp, blocksize=8192):
        path = cmd.split(" ", 1)[1]
        self.server.files[path] = fp.read()[:10]
        raise TimeoutError("timed out")

    with patch.object(_FakeFTP, "storbinary", partial):
        with pytest.raises(st.Ultimate64StorageError, match="overwrite=True"):
            st.storage_put_file("dev", "/SD/x.bin", DATA, overwrite=True)


def test_an_empty_payload_is_refused_before_connecting(server):
    """3a1ff9ff creates nothing for a zero-byte STOR (measured on the U64E)."""
    with pytest.raises(ValueError, match="empty"):
        st.storage_put_file("dev", "/SD/x.bin", b"")
    assert server.connects == 0


def test_a_non_550_size_error_is_not_read_as_missing(server):
    def size(self, path):
        raise ftplib.error_perm("530 Not logged in.")

    with patch.object(_FakeFTP, "size", size):
        with pytest.raises(st.Ultimate64StorageError, match="530"):
            st.storage_put_file("dev", "/SD/x.bin", b"x")


def test_a_non_501_mlst_error_is_not_read_as_missing(server):
    real = _FakeFTP.sendcmd

    def sendcmd(self, cmd):
        if cmd.startswith("MLST"):
            raise ftplib.error_perm("530 Not logged in.")
        return real(self, cmd)

    server.files["/SD/x.bin"] = b"x"
    with patch.object(_FakeFTP, "sendcmd", sendcmd):
        for call in (
            lambda: st.storage_put_file("dev", "/SD/y.bin", b"x"),
            lambda: st.storage_delete_file("dev", "/SD/x.bin", missing_ok=True),
            lambda: st.storage_get_file("dev", "/SD/x.bin"),
            lambda: st.storage_mkdir("dev", "/SD/d"),
        ):
            with pytest.raises(st.Ultimate64StorageError, match="530"):
                call()
    assert not any(e.startswith(("STOR", "DELE", "MKD", "RETR")) for e in server.log)


def test_session_is_closed_and_uses_timeout_and_credentials(server, monkeypatch):
    seen = {}
    monkeypatch.setenv("U64_TEMP_GC_FTP_USER", "alice")
    monkeypatch.setenv("U64_TEMP_GC_FTP_PASSWORD", "secret")

    def connect(self, host, port, timeout=None):
        seen["connect"] = (host, port, timeout)

    def login(self, user, password):
        seen["login"] = (user, password)

    def quit(self):
        seen["quit"] = True

    with patch.object(_FakeFTP, "connect", connect), patch.object(
        _FakeFTP, "login", login
    ), patch.object(_FakeFTP, "quit", quit):
        st.storage_volumes("dev", port=2121, timeout=7.5)
    assert seen == {"connect": ("dev", 2121, 7.5), "login": ("alice", "secret"), "quit": True}


# ---------------------------------------------------------------- review round 2 (#526)
@pytest.mark.parametrize(
    "missing",
    ["501 Syntax error in parameters or arguments.", "550 File not found."],
    ids=["1.1.0-501", "3a1ff9ff-550"],
)
def test_mlst_missing_codes_of_both_firmwares(server, missing):
    server.mlst_missing = missing
    with pytest.raises(FileNotFoundError):
        st.storage_get_file("dev", "/SD/nope.bin")
    assert st.storage_delete_file("dev", "/SD/nope.bin", missing_ok=True) is False
    assert st.storage_put_file("dev", "/SD/new/x.bin", b"x").verified
    assert st.storage_mkdir("dev", "/SD/other/d") is True


def test_type_fact_is_read_from_the_fact_line_not_the_name(server):
    """A file named like a fact must not be read as a directory."""
    server.files["/SD/atype=dir.bin"] = b"payload"
    assert st.storage_get_file("dev", "/SD/atype=dir.bin") == b"payload"
    assert st.storage_delete_file("dev", "/SD/atype=dir.bin") is True


# ---------------------------------------------------------------- review round 3 (#526)
@pytest.mark.parametrize("path", ["/SD/100%s.d64", "/SD/a%n", "/SD/" + "x" * 61])
def test_reads_refuse_percent_and_long_names_before_connecting(server, path):
    """get MLSTs its path, so it reaches 1.1.0's format-string sink too."""
    with pytest.raises(ValueError):
        st.storage_get_file("dev", path)
    assert server.connects == 0


def test_an_image_directly_under_the_volume_is_never_stated_through(server):
    server.files["/SD/game.d64"] = b"\x00" * 174848
    for call in (
        lambda: st.storage_put_file("dev", "/SD/game.d64/inner.prg", b"x"),
        lambda: st.storage_mkdir("dev", "/SD/game.d64/sub"),
        lambda: st.storage_get_file("dev", "/SD/game.d64/inner.prg"),
        lambda: st.storage_delete_file("dev", "/SD/game.d64/inner.prg", missing_ok=True),
        lambda: st.storage_list_dir("dev", "/SD/game.d64/sub"),
        lambda: st.storage_rmdir("dev", "/SD/game.d64/sub", missing_ok=True),
    ):
        with pytest.raises(NotADirectoryError):
            call()
    assert not any(e.startswith("MOUNTED") for e in server.log), server.log


# ---------------------------------------------------------------- list_dir / rmdir / writable volumes
def test_writable_volumes_drop_temp_and_flash_whatever_their_case(server):
    """storage_volumes spells them 'Flash'/'Temp'; WRITE_REFUSED_VOLUMES is casefolded."""
    assert st.storage_writable_volumes("dev") == ["SD", "USB1"]
    server.dirs.discard("/SD")
    server.empty_slots.add("SD")
    assert st.storage_writable_volumes("dev") == ["USB1"]


def test_list_dir_returns_entries_with_kind_and_size(server):
    server.dirs.update({"/SD/t", "/SD/t/sub"})
    server.files["/SD/t/a.bin"] = b"abc"
    server.files["/SD/t/game.d64"] = b"\x00" * 10
    server.files["/SD/t/sub/deep.bin"] = b"x"
    entries = st.storage_list_dir("dev", "/SD/t")
    assert entries == [
        st.StorageEntry("a.bin", "file", 3),
        st.StorageEntry("game.d64", "file", 10),
        st.StorageEntry("sub", "dir", 0),
    ]
    assert not any(e.startswith("MOUNTED") for e in server.log)


def test_list_dir_of_a_volume_root(server):
    server.files["/USB1/x.bin"] = b"x"
    assert [e.name for e in st.storage_list_dir("dev", "/usb1")] == ["x.bin"]


def test_list_dir_refuses_files_missing_paths_and_bad_paths(server):
    server.files["/SD/x.d64"] = b"\x00"
    with pytest.raises(NotADirectoryError):
        st.storage_list_dir("dev", "/SD/x.d64")
    assert not any(e.startswith("MLSD") for e in server.log)
    with pytest.raises(FileNotFoundError):
        st.storage_list_dir("dev", "/SD/nope")
    for bad in ("/SD/..\\Temp", "/SD/a%s", "SD", "/", ""):
        with pytest.raises(ValueError):
            st.storage_list_dir("dev", bad)


def test_list_dir_may_read_any_volume_but_needs_the_lock(server):
    server.dirs.add("/Flash/roms")
    assert st.storage_list_dir("dev", "/Flash") == [st.StorageEntry("roms", "dir", 0)]
    with patch.object(st, "lock_held_for", lambda host: False):
        with pytest.raises(st.Ultimate64StorageError, match="DeviceLock"):
            st.storage_list_dir("dev", "/SD")


def test_rmdir_removes_an_empty_directory(server):
    server.dirs.update({"/SD/c64https-test"})
    assert st.storage_rmdir("dev", "/SD/c64https-test") is True
    assert "/SD/c64https-test" not in server.dirs
    with pytest.raises(FileNotFoundError):
        st.storage_rmdir("dev", "/SD/c64https-test")
    assert st.storage_rmdir("dev", "/SD/c64https-test", missing_ok=True) is False


def test_rmdir_refuses_a_non_empty_directory_before_sending_rmd(server):
    server.dirs.add("/SD/d")
    server.files["/SD/d/zz.bin"] = b"x"
    server.files["/SD/d/keep.bin"] = b"x"
    with pytest.raises(st.Ultimate64StorageNotEmptyError) as ei:
        st.storage_rmdir("dev", "/sd/d")
    assert ei.value.entries == ["keep.bin", "zz.bin"]
    assert "/SD/d" in server.dirs
    assert not any(e.startswith("RMD") for e in server.log)


def test_rmdir_refuses_files_volume_roots_and_refused_volumes(server):
    server.files["/SD/x.bin"] = b"x"
    with pytest.raises(NotADirectoryError):
        st.storage_rmdir("dev", "/SD/x.bin")
    for bad in ("/SD", "/Temp/cache", "/Flash/roms", "/SD/a\\..\\..\\Temp", "/SD/a%n"):
        with pytest.raises(ValueError):
            st.storage_rmdir("dev", bad)
    assert not any(e.startswith("RMD") for e in server.log)


def test_not_empty_error_is_a_storage_error():
    assert issubclass(st.Ultimate64StorageNotEmptyError, st.Ultimate64StorageError)


# ---------------------------------------------------------------- #527 review round 1
def test_rmdir_of_a_dir_holding_only_hidden_entries_is_a_typed_not_empty(server):
    """MLSD omits '.'-names, so the pre-check passes; RMD's 550 must still be typed."""
    server.dirs.add("/SD/d")
    server.files["/SD/d/.DS_Store"] = b"x"
    assert st.storage_list_dir("dev", "/SD/d") == []
    with pytest.raises(st.Ultimate64StorageNotEmptyError, match="hidden") as ei:
        st.storage_rmdir("dev", "/SD/d")
    assert ei.value.entries == []
    assert "/SD/d" in server.dirs


def test_rmdir_lowercase_volume_uses_device_spelling(server):
    server.dirs.add("/SD/e")
    assert st.storage_rmdir("dev", "/sd/e") is True
    assert "RMD /SD/e" in server.log


@pytest.mark.parametrize("path", ["/SD/.keep", "/SD/.hidden/x.bin", "/SD/a/._x"])
def test_writes_refuse_dot_names_the_listing_cannot_show(server, path):
    with pytest.raises(ValueError, match="'.'"):
        st.storage_put_file("dev", path, b"x")
    with pytest.raises(ValueError):
        st.storage_mkdir("dev", path)
    assert server.connects == 0


def test_session_speaks_the_firmware_code_page(server):
    """FF_CODE_PAGE 437, FF_LFN_UNICODE 0: names are CP437 bytes on the wire."""
    seen = {}
    real = _FakeFTP.mlsd

    def mlsd(self, path="", facts=()):
        seen["encoding"] = getattr(self, "encoding", None)
        return real(self, path, facts)

    with patch.object(_FakeFTP, "mlsd", mlsd):
        st.storage_list_dir("dev", "/SD")
    assert seen["encoding"] == "cp437"


def test_a_decode_error_inside_a_session_is_a_storage_error(server):
    def mlsd(self, path="", facts=()):
        raise UnicodeDecodeError("utf-8", b"\x9a", 0, 1, "invalid start byte")

    with patch.object(_FakeFTP, "mlsd", mlsd):
        with pytest.raises(st.Ultimate64StorageError, match="UnicodeDecodeError"):
            st.storage_list_dir("dev", "/SD")
