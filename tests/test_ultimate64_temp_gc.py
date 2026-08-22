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
