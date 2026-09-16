"""Unit tests for ultimate64_temp_gc (no network — FTP is faked)."""
from __future__ import annotations

from ftplib import error_perm

import pytest

from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends.ultimate64_temp_gc import (
    DEFAULT_FTP_PASSWORD,
    DEFAULT_FTP_USER,
    DEFAULT_KEEP,
    TempGCResult,
    auto_gc_enabled,
    gc_temp_folder,
)


class _FakeFTP:
    """Stand-in for ftplib.FTP used as `with FTP() as ftp: ...`."""

    #: populated per-test before gc_temp_folder() is called
    files: list[str] = []
    #: (host, port) captured from connect()
    connected: tuple[str, int] | None = None
    logins: list[tuple[str, str]] = None  # type: ignore[assignment]
    deleted: list[str] = None  # type: ignore[assignment]
    cwd_path: str | None = None
    connect_error: Exception | None = None
    login_error: Exception | None = None
    delete_error_names: set = None  # type: ignore[assignment]

    def __init__(self) -> None:
        pass

    def __enter__(self) -> "_FakeFTP":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def connect(self, host: str, port: int, timeout: float = 10.0) -> None:
        if _FakeFTP.connect_error is not None:
            raise _FakeFTP.connect_error
        _FakeFTP.connected = (host, port)

    def login(self, user: str, password: str) -> None:
        if _FakeFTP.login_error is not None:
            raise _FakeFTP.login_error
        _FakeFTP.logins.append((user, password))

    def cwd(self, path: str) -> None:
        _FakeFTP.cwd_path = path

    def nlst(self) -> list[str]:
        return list(_FakeFTP.files)

    def delete(self, name: str) -> None:
        if _FakeFTP.delete_error_names and name in _FakeFTP.delete_error_names:
            raise error_perm(f"550 cannot delete {name}")
        _FakeFTP.deleted.append(name)


@pytest.fixture(autouse=True)
def _reset_fake_ftp(monkeypatch: pytest.MonkeyPatch):
    _FakeFTP.files = []
    _FakeFTP.connected = None
    _FakeFTP.logins = []
    _FakeFTP.deleted = []
    _FakeFTP.cwd_path = None
    _FakeFTP.connect_error = None
    _FakeFTP.login_error = None
    _FakeFTP.delete_error_names = set()
    monkeypatch.setattr(gc_mod, "FTP", _FakeFTP)
    # Belt-and-suspenders: clear any GC env vars a prior test left set.
    for var in (gc_mod.AUTO_GC_ENV, gc_mod.KEEP_ENV, gc_mod.FTP_USER_ENV, gc_mod.FTP_PASSWORD_ENV):
        monkeypatch.delenv(var, raising=False)
    yield


def test_deletes_oldest_first_keeps_default_youngest():
    _FakeFTP.files = [f"temp{i:04d}" for i in range(6)]
    result = gc_temp_folder("10.0.0.1")
    assert result.ok
    assert result.deleted == ["temp0000", "temp0001", "temp0002", "temp0003"]
    assert result.kept == ["temp0004", "temp0005"]
    assert _FakeFTP.deleted == result.deleted
    assert _FakeFTP.cwd_path == "/Temp"
    assert _FakeFTP.connected == ("10.0.0.1", 21)
    assert _FakeFTP.logins == [(DEFAULT_FTP_USER, DEFAULT_FTP_PASSWORD)]


def test_ignores_non_managed_files():
    _FakeFTP.files = ["temp0000", "temp0001", "temp0002", "somefile.d64", "temp", "temp12g"]
    result = gc_temp_folder("10.0.0.1", keep=1)
    assert result.deleted == ["temp0000", "temp0001"]
    assert result.kept == ["temp0002"]
    assert "somefile.d64" not in _FakeFTP.deleted
    assert "temp" not in _FakeFTP.deleted
    assert "temp12g" not in _FakeFTP.deleted


def test_hex_suffix_ordering_across_letter_boundary():
    # Firmware's attachment counter is hex: temp0009 is followed by
    # temp000A, not treated as "after" a hypothetical temp0010. A
    # decimal-only pattern skips lettered names entirely (issue #153).
    _FakeFTP.files = ["temp000A", "temp0009", "temp000B"]
    result = gc_temp_folder("10.0.0.1", keep=1)
    assert result.deleted == ["temp0009", "temp000A"]
    assert result.kept == ["temp000B"]


def test_hex_suffix_lowercase_also_matches():
    _FakeFTP.files = ["temp000a", "temp000b"]
    result = gc_temp_folder("10.0.0.1", keep=1)
    assert result.deleted == ["temp000a"]
    assert result.kept == ["temp000b"]


def test_keep_le_len_keeps_everything():
    _FakeFTP.files = ["temp0000", "temp0001"]
    result = gc_temp_folder("10.0.0.1", keep=5)
    assert result.deleted == []
    assert result.kept == ["temp0000", "temp0001"]


def test_keep_zero_deletes_everything():
    _FakeFTP.files = ["temp0000", "temp0001"]
    result = gc_temp_folder("10.0.0.1", keep=0)
    assert result.deleted == ["temp0000", "temp0001"]
    assert result.kept == []


def test_keep_override_via_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.KEEP_ENV, "1")
    _FakeFTP.files = [f"temp{i:04d}" for i in range(3)]
    result = gc_temp_folder("10.0.0.1")
    assert result.kept == ["temp0002"]


def test_keep_kwarg_overrides_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.KEEP_ENV, "1")
    _FakeFTP.files = [f"temp{i:04d}" for i in range(3)]
    result = gc_temp_folder("10.0.0.1", keep=0)
    assert result.kept == []


def test_default_keep_constant_used_when_unset():
    assert DEFAULT_KEEP == 2


def test_credentials_via_kwargs():
    _FakeFTP.files = ["temp0000"]
    gc_temp_folder("10.0.0.1", username="bench", password="hunter2")
    assert _FakeFTP.logins == [("bench", "hunter2")]


def test_credentials_via_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(gc_mod.FTP_USER_ENV, "bench")
    monkeypatch.setenv(gc_mod.FTP_PASSWORD_ENV, "hunter2")
    _FakeFTP.files = ["temp0000"]
    gc_temp_folder("10.0.0.1")
    assert _FakeFTP.logins == [("bench", "hunter2")]


def test_never_raises_on_connect_failure():
    _FakeFTP.connect_error = OSError("connection refused")
    result = gc_temp_folder("10.0.0.1")
    assert not result.ok
    assert "connection refused" in result.error
    assert result.deleted == []


def test_connection_refused_names_ftp_file_service_setting():
    # Seen on C64U fw 1.1.0, which ships FTP File Service disabled by
    # default (issue #153 correction) -- the error should say what to
    # check instead of just surfacing the bare connect exception.
    _FakeFTP.connect_error = ConnectionRefusedError("[Errno 61] Connection refused")
    result = gc_temp_folder("10.0.0.1")
    assert not result.ok
    assert "FTP File Service" in result.error
    assert "Network Settings" in result.error
    assert result.deleted == []


def test_never_raises_on_login_failure():
    _FakeFTP.login_error = error_perm("530 Login incorrect")
    result = gc_temp_folder("10.0.0.1")
    assert not result.ok
    assert result.deleted == []


def test_partial_delete_failure_does_not_raise_and_continues():
    _FakeFTP.files = ["temp0000", "temp0001", "temp0002"]
    _FakeFTP.delete_error_names = {"temp0000"}
    result = gc_temp_folder("10.0.0.1", keep=0)
    assert result.ok
    # temp0000's delete raised but was swallowed; the others still ran.
    assert result.deleted == ["temp0001", "temp0002"]


def test_result_is_dataclass_with_host():
    result = TempGCResult(host="10.0.0.1")
    assert result.ok
    assert result.deleted == []
    assert result.kept == []


def test_auto_gc_disabled_by_default():
    assert auto_gc_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_auto_gc_enabled_truthy_values(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, val)
    assert auto_gc_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_auto_gc_disabled_falsy_values(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(gc_mod.AUTO_GC_ENV, val)
    assert auto_gc_enabled() is False


# --------------------------------------------------------------------------- #
# #261 (dup #256): the RAM disk is 16 MiB, cited by its computation            #
# --------------------------------------------------------------------------- #

import inspect as _inspect
import re as _re

#: The stale figures. "31%" is matched with or without a space or tilde.
_STALE_RAMDISK = _re.compile(r"~?\s*3\s*MB\b|\b31\s*%|945\s*KB\b")


def test_stale_ramdisk_scan_detects_what_it_is_looking_for():
    """Positive control: the scan must fire on each stale phrasing it
    exists to catch, or a clean result below proves nothing."""
    for bad in (
        "a ~3 MB RAM disk",
        "that is ~31% of it",
        "at 31% full with 15 entries",
        "so 945 KB is",
    ):
        assert _STALE_RAMDISK.search(bad), bad
    for correct in ("16 MiB", "a 16 MiB RAM disk"):
        assert not _STALE_RAMDISK.search(correct), correct


def test_temp_gc_source_no_longer_states_the_3_mb_ramdisk():
    from c64_test_harness.backends import ultimate64_temp_gc as mod

    src = _inspect.getsource(mod)
    # This file states the RAM-disk size in both the module docstring and
    # the budget comment.  The guessed wedge count it used to carry ("63 KB
    # PRG", "967,680") is retired (owner, 2026-09-15; #256) and must not return.
    assert "63 KB PRG" not in src
    assert "DEFAULT_LEAK_BUDGET = " in src
    hits = [ln.strip() for ln in src.splitlines() if _STALE_RAMDISK.search(ln)]
    assert hits == []
    # The durable fix cites the computation, not just a new number -- at
    # BOTH sites that state the size (module docstring and the budget
    # comment), since a reader copies from whichever one they found.
    assert src.count("__ram_disk_start") >= 2
    assert src.count("__ram_disk_limit") >= 2
    assert src.count("16 MiB") >= 2
    assert "967,680" not in src
    # Review round 1, finding 3: the reproduction was a U64E on 3.14d and the
    # C64U runs 1.1.0 -- each figure cites the tree it came from.
    # Bound to the path, not merely present somewhere in the file: "v3.14d"
    # and "1.1.0" also occur in unrelated prose (mutation R10 survived a
    # membership check).
    flat = _re.sub(r"[\s`]+", " ", src)
    # #316: the shipped U64E image is the nios2 build, whose linker script
    # is the BSP's -- cite that tree, and the build chain that selects it.
    assert _re.search(r"software/nios_appl_bsp/linker\.x at v3\.14d", flat)
    assert _re.search(r"target/u64/nios2/ultimate/Makefile", flat)
    assert _re.search(r"target/u64ii/riscv/ultimate/linker\.x at 1\.1\.0", flat)
    assert "target/u64/riscv/ultimate/linker.x at v3.14d" not in flat
    assert "is not established. So" not in flat
    assert "carry no RAM-disk symbols" not in flat


def test_budget_comment_prices_uci_writes_by_grade():
    """#294 chunks ``Ultimate64Transport.write_memory`` into PUT-sized pieces
    unless the cached grade is ``writemem_post_safe is True``, and UCI
    routines and payloads go through the transport. So a UCI socket write
    costs no attachment on a leak-prone or unknown grade; only a post-safe
    grade (whose firmware collects) or a direct ``client.write_mem`` caller
    pays. The budget comment used to state the pre-#294 cost unqualified.

    Named limit (#318 review round 2): these are phrase pins. They catch a
    rewrite of the pinned claims and the specific wrong wordings forbidden
    below, but not a *contradicting sentence appended* after them (V1: "In
    practice a UCI socket write still leaks one attachment on the C64U."
    survives). Proving the paragraph's meaning is beyond a text test; the
    reviewer accepted this limit.
    """
    from c64_test_harness.backends import ultimate64_temp_gc as mod

    src = _inspect.getsource(mod)
    start = src.index("#: Note the unit:")
    end = src.index("DEFAULT_LEAK_BUDGET = ")
    # Strip only the "#:" comment prefixes, so "#294" survives the flattening.
    note = _re.sub(r"[\s`]+", " ", src[start:end].replace("\n#:", " "))
    # Vacuity guard: this is the paragraph about UCI costs.
    assert "build_socket_write" in note and "enable_uci" in note
    assert "write spends one of the budget for its routine code" not in note
    assert "writemem_post_safe is True" in note
    assert "transport.write_memory" in note
    assert "client.write_mem" in note
    assert "#294" in note
    # Review round 1 (#318): keywords let two wrong rewrites through (U1
    # "costs one attachment ... on every grade", U2 "... which that firmware
    # never collects"). Pin each grade-bound claim as one contiguous phrase.
    claim = note.lower()
    assert ("on a leak-prone or unknown grade (the c64u) a uci socket write "
            "costs no attachment") in claim
    assert ("on a post-safe grade the routine, and a payload over the "
            "ceiling, are one post each, which that firmware collects") in claim
    assert "on every grade" not in claim
    assert "never collects" not in claim
    # The same qualifier applies to the "raw write_memory" parenthetical
    # above the note: the transport now chunks those on a leak-prone grade.
    assert "those are lane bugs to fix by chunking" not in src


# ------------------------------------------------- mounted images (#418)
def _drives(*image_files: str) -> dict:
    """A GET /v1/drives document mounting *image_files* on a, b, ..."""
    return {
        "drives": [
            {chr(ord("a") + i): {"enabled": True, "image_file": f}}
            for i, f in enumerate(image_files)
        ],
        "errors": [],
    }


def test_mounted_managed_image_is_not_deleted():
    """A temp%04x name a drive has mounted survives the sweep (#418).

    Raw-body uploads (and multipart parts with no filename=) keep the
    firmware's managed name and are mounted from that file, so the
    oldest-first sweep would otherwise delete a mounted image's backing
    store.
    """
    _FakeFTP.files = ["temp0001", "temp0002", "temp0003", "temp0004"]
    result = gc_temp_folder(
        "dev", mounted_probe=lambda: _drives("/Temp/temp0001", "/Temp/image.d64")
    )
    assert "temp0001" not in _FakeFTP.deleted
    assert result.deleted == ["temp0002"]
    assert result.mounted_excluded == ["temp0001"]
    assert result.ok


def test_mounted_image_in_a_subdirectory_is_excluded_by_basename():
    """The U64E reports /Temp/cache/upload/temp0082; the name still matches.

    Measured on the U64E (fw 3.15 bce4535e, 2026-09-15, n=1). Comparing
    basenames rather than full paths is what makes the exclusion hold
    across the two generations' differing layouts.
    """
    _FakeFTP.files = ["temp0082", "temp0083", "temp0084"]
    result = gc_temp_folder(
        "dev", keep=1, mounted_probe=lambda: _drives("/Temp/cache/upload/temp0082")
    )
    assert _FakeFTP.deleted == ["temp0083"]
    assert result.mounted_excluded == ["temp0082"]


def test_excluding_a_mounted_image_never_widens_the_delete_set():
    """Excluding the *youngest* name must not promote an older one into deletion.

    Mounted names are dropped before the keep-count is applied, so the
    survivors are re-chosen from what is left: the delete set can only
    shrink. Applying the keep-count first and subtracting afterwards
    would still delete temp0001 AND temp0002 here.
    """
    _FakeFTP.files = ["temp0001", "temp0002", "temp0003", "temp0004"]
    result = gc_temp_folder("dev", mounted_probe=lambda: _drives("/Temp/temp0004"))
    assert result.deleted == ["temp0001"]
    assert result.kept == ["temp0002", "temp0003"]
    assert result.mounted_excluded == ["temp0004"]


def test_unmanaged_mounted_names_leave_the_sweep_untouched():
    """A normally-named mounted image excludes nothing (harness uploads, #311)."""
    _FakeFTP.files = ["temp0001", "temp0002", "temp0003"]
    result = gc_temp_folder("dev", mounted_probe=lambda: _drives("/Temp/image.d64"))
    assert result.deleted == ["temp0001"]
    assert result.mounted_excluded == []


def test_probe_failure_still_sweeps_and_is_not_a_failed_pass():
    """An unreadable drives listing must not stop hygiene, and must say so.

    Skipping the sweep would trade a recoverable data hazard for the
    unrecoverable one this module prevents, and setting .error would
    block later attachment-creating requests to a healthy device.
    """
    _FakeFTP.files = ["temp0001", "temp0002", "temp0003"]

    def boom():
        raise OSError("connection reset")

    result = gc_temp_folder("dev", mounted_probe=boom)
    assert result.deleted == ["temp0001"]
    assert result.ok and result.error is None
    assert "connection reset" in (result.mounted_probe_error or "")


def test_probe_is_not_consulted_when_nothing_would_be_deleted():
    """No drives request on a device the keep-count already spares."""
    _FakeFTP.files = ["temp0001", "temp0002"]
    calls = []

    def probe():
        calls.append(1)
        return _drives()

    result = gc_temp_folder("dev", mounted_probe=probe)
    assert calls == []
    assert result.deleted == []


def test_default_probe_is_a_bodyless_drives_get():
    """The built-in probe costs no /Temp attachment: GET, no body.

    A body-carrying request here would make every hygiene pass leak the
    very thing it is sweeping for.
    """
    from unittest.mock import patch

    captured = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self):
            return b'{"drives":[{"a":{"image_file":"/Temp/temp0001"}}]}'

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _Resp()

    _FakeFTP.files = ["temp0001", "temp0002", "temp0003"]
    with patch("urllib.request.urlopen", fake_urlopen):
        result = gc_temp_folder("10.0.0.5")

    assert len(captured) == 1
    assert captured[0].get_full_url() == "http://10.0.0.5/v1/drives"
    assert captured[0].get_method() == "GET"
    assert captured[0].data is None
    assert result.mounted_excluded == ["temp0001"]
