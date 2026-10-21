"""Config write with read-back verify (#545) and ``enable_ftp_service`` (#546).

Every test drives the real ``Ultimate64Client`` request path against a faked
device: ``urllib.request.urlopen`` is replaced by :class:`_FakeConfigDevice`,
which keeps a config store and answers the two routes these features use --
``GET /v1/configs/<cat>/<item>`` and the bodyless
``PUT /v1/configs/<cat>/<item>?value=``. It matches enum values the way
1.1.0's ``route_configs.cc`` ``set_item`` does (``strcasecmp`` against the
item's choices, spaces significant; ``strtol`` for range items). No device
traffic: there is no ``U64_HOST`` here and the host is ``h``.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from unittest.mock import patch

import pytest

import c64_test_harness as harness
from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64ConfigVerifyError,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64_helpers import enable_ftp_service

NET = "Network Settings"
FTP = "FTP File Service"
U64 = "U64 Specific Settings"
CPU = "CPU Speed"


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeConfigDevice:
    """A config store behind the two REST routes the features use.

    *ignore_writes* makes every PUT answer 200 without changing anything: the
    accepted-but-not-applied write the read-back exists to catch.
    """

    def __init__(self, items: dict, *, ignore_writes: bool = False,
                 echo: "dict | None" = None) -> None:
        self.items = {k: dict(v) for k, v in items.items()}
        self.ignore_writes = ignore_writes
        #: (cat, item) -> callable(stored) giving the value the GET echoes,
        #: for firmware formatting that differs from what was written.
        self.echo = echo or {}
        self.requests: list[tuple[str, str, dict, object]] = []

    def __call__(self, req, timeout=None):
        split = urllib.parse.urlsplit(req.get_full_url())
        path = urllib.parse.unquote(split.path)
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        method = req.get_method()
        self.requests.append((method, path, query, req.data))
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "v1", "configs"]:
            return _Resp(b"{}")
        key = (parts[3], parts[4])
        item = self.items[key]
        if method == "GET":
            shown = dict(item)
            if key in self.echo:
                shown["current"] = self.echo[key](item["current"])
            return _Resp(json.dumps(
                {key[0]: {key[1]: shown}, "errors": []}
            ).encode())
        assert method == "PUT" and req.data is None, "config writes are bodyless"
        value = query["value"][0]
        if not self.ignore_writes:
            if "values" in item:
                match = [v for v in item["values"] if v.lower() == value.lower()]
                assert match, f"firmware would reject {value!r}"
                item["current"] = match[0]
            elif "min" in item:
                # C strtol(…, 0): a leading 0 is octal (Python's int(…, 0)
                # refuses that spelling instead).
                digits = value.lstrip("+-")
                octal = len(digits) > 1 and digits[0] == "0" and digits[1] in "01234567"
                item["current"] = int(value, 8) if octal else int(value, 0)
            else:
                item["current"] = value
        return _Resp(b"{}")

    def methods(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _q, _d in self.requests]


def _ftp_item(current: str) -> dict:
    return {
        (NET, FTP): {
            "current": current,
            "values": ["Disabled", "Enabled"],
            "default": "Disabled",
        }
    }


ITEM_PATH = f"/v1/configs/{NET}/{FTP}"


@pytest.fixture
def locked(monkeypatch: pytest.MonkeyPatch):
    """This process holds the device's DeviceLock (the hygiene check)."""
    seen: list[str] = []

    def held(host: str) -> bool:
        seen.append(host)
        return True

    monkeypatch.setattr(gc_mod, "lock_held_for", held)
    return seen


@pytest.fixture
def unlocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gc_mod, "lock_held_for", lambda host: False)


# ------------------------------------------------------- #545 set_config_item
def test_verify_off_by_default_sends_only_the_put():
    """Existing behaviour unchanged: no read-back unless asked for."""
    dev = _FakeConfigDevice(_ftp_item("Disabled"), ignore_writes=True)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        c.set_config_item(NET, FTP, "Enabled")
    assert dev.methods() == [("PUT", ITEM_PATH)]


def test_verify_raises_when_the_device_ignores_the_write():
    dev = _FakeConfigDevice(_ftp_item("Disabled"), ignore_writes=True)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64ConfigVerifyError) as info:
        c.set_config_item(NET, FTP, "Enabled", verify=True)
    err = info.value
    assert isinstance(err, Ultimate64Error)
    assert (err.category, err.item) == (NET, FTP)
    assert err.requested == "Enabled"
    assert err.readback == "Disabled"
    # Bodyless PUT, then one GET: zero /Temp cost.
    assert dev.methods() == [("PUT", ITEM_PATH), ("GET", ITEM_PATH)]
    assert all(d is None for _m, _p, _q, d in dev.requests)


def test_verify_passes_when_the_device_applies_the_write():
    dev = _FakeConfigDevice(_ftp_item("Disabled"))
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        c.set_config_item(NET, FTP, "Enabled", verify=True)
    assert dev.items[(NET, FTP)]["current"] == "Enabled"
    assert dev.methods() == [("PUT", ITEM_PATH), ("GET", ITEM_PATH)]


def test_verify_enum_compare_is_case_insensitive_like_the_firmware():
    """The firmware matches enum values with strcasecmp and echoes its own
    spelling, so 'enabled' written reads back 'Enabled' and is a match."""
    dev = _FakeConfigDevice(_ftp_item("Disabled"))
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        c.set_config_item(NET, FTP, "enabled", verify=True)
    assert dev.items[(NET, FTP)]["current"] == "Enabled"


def test_verify_enum_ignores_firmware_padding_cpu_speed():
    """CPU Speed choices carry a leading space (' 5'). The value written and
    the value echoed are compared stripped, so padding on either side is not
    a mismatch."""
    items = {(U64, CPU): {"current": " 1", "values": [" 1", " 2", " 5"],
                          "default": " 1"}}
    dev = _FakeConfigDevice(items)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        c.set_config_item(U64, CPU, " 5", verify=True)
    assert dev.items[(U64, CPU)]["current"] == " 5"
    # The comparison itself: written padded, compared against an echo that
    # dropped the padding, and the other way round.
    dev2 = _FakeConfigDevice(items, echo={(U64, CPU): str.strip})
    with patch("urllib.request.urlopen", dev2):
        c.set_config_item(U64, CPU, " 2", verify=True)
    assert dev2.items[(U64, CPU)]["current"] == " 2"


def test_verify_enum_mismatch_still_raises_after_normalising():
    items = {(U64, CPU): {"current": " 1", "values": [" 1", " 2", " 5"],
                          "default": " 1"}}
    dev = _FakeConfigDevice(items, ignore_writes=True)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64ConfigVerifyError) as info:
        c.set_config_item(U64, CPU, " 5", verify=True)
    assert info.value.readback == " 1"


def test_verify_range_item_compares_numerically():
    """A range item is parsed with strtol(…, 0): '0x10' sets 16, and the
    firmware may echo it as an int or as a formatted, padded string."""
    items = {("Drive A Settings", "Drive Bus ID"): {
        "current": 8, "min": 8, "max": 11, "format": "%d", "default": 8}}
    dev = _FakeConfigDevice(items)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        c.set_config_item("Drive A Settings", "Drive Bus ID", 9, verify=True)
        c.set_config_item("Drive A Settings", "Drive Bus ID", "0xA", verify=True)
    assert dev.items[("Drive A Settings", "Drive Bus ID")]["current"] == 10
    # strtol base 0 reads a leading 0 as octal: "011" sets 9, not 11.
    with patch("urllib.request.urlopen", dev):
        c.set_config_item("Drive A Settings", "Drive Bus ID", "011", verify=True)
    assert dev.items[("Drive A Settings", "Drive Bus ID")]["current"] == 9
    dev2 = _FakeConfigDevice(items, echo={
        ("Drive A Settings", "Drive Bus ID"): lambda v: f"{v:>3}"})
    with patch("urllib.request.urlopen", dev2):
        c.set_config_item("Drive A Settings", "Drive Bus ID", 11, verify=True)
    ignored = _FakeConfigDevice(items, ignore_writes=True)
    with patch("urllib.request.urlopen", ignored), \
            pytest.raises(Ultimate64ConfigVerifyError):
        c.set_config_item("Drive A Settings", "Drive Bus ID", 9, verify=True)


def test_verify_range_item_handles_a_sign():
    items = {(U64, "Pan"): {"current": 0, "min": -5, "max": 5, "default": 0}}
    dev = _FakeConfigDevice(items)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        c.set_config_item(U64, "Pan", "-3", verify=True)
    assert dev.items[(U64, "Pan")]["current"] == -3
    dev.items[(U64, "Pan")]["current"] = 3
    dev.ignore_writes = True
    with patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64ConfigVerifyError):
        c.set_config_item(U64, "Pan", "-3", verify=True)


def test_verify_refuses_an_item_map_without_current():
    from c64_test_harness.backends.ultimate64_client import (
        Ultimate64ProtocolError,
    )

    items = {(NET, "Odd"): {"default": "x"}}
    dev = _FakeConfigDevice(items, ignore_writes=True)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64ProtocolError, match="current"):
        c.set_config_item(NET, "Odd", "x", verify=True)


def test_verify_free_string_item_is_case_sensitive():
    """A free-string item is stored as written, so a case-only difference is
    a dropped write, not a formatting difference."""
    items = {(NET, "Host Name"): {"current": "Bench", "default": "C64U"}}
    dev = _FakeConfigDevice(items, ignore_writes=True)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64ConfigVerifyError):
        c.set_config_item(NET, "Host Name", "bench", verify=True)
    ok = _FakeConfigDevice(items)
    with patch("urllib.request.urlopen", ok):
        c.set_config_item(NET, "Host Name", "bench", verify=True)
    assert ok.items[(NET, "Host Name")]["current"] == "bench"


# ------------------------------------------------------ #546 enable_ftp_service
def test_enable_ftp_refuses_without_the_device_lock(unlocked):
    dev = _FakeConfigDevice(_ftp_item("Disabled"))
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64Error, match="DeviceLock"):
        enable_ftp_service(c)
    assert dev.requests == []  # refused before any request


def test_enable_ftp_checks_the_clients_device_key(locked):
    dev = _FakeConfigDevice(_ftp_item("Enabled"))
    c = Ultimate64Client("h", port=8080)
    with patch("urllib.request.urlopen", dev):
        enable_ftp_service(c)
    assert locked == [c._device_key]


def test_enable_ftp_writes_verifies_and_warns(locked, caplog):
    dev = _FakeConfigDevice(_ftp_item("Disabled"))
    c = Ultimate64Client("h")
    with caplog.at_level(logging.WARNING), \
            patch("urllib.request.urlopen", dev):
        wrote = enable_ftp_service(c)
    assert wrote is True
    assert dev.items[(NET, FTP)]["current"] == "Enabled"
    # Read first, bodyless write, read back -- and nothing else: in
    # particular no PUT /v1/configs:save_to_flash.
    assert dev.methods() == [
        ("GET", ITEM_PATH), ("PUT", ITEM_PATH), ("GET", ITEM_PATH),
    ]
    assert dev.requests[1][2] == {"value": ["Enabled"]}
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "FTP" in r.getMessage()]
    assert len(warnings) == 1
    assert "power" in warnings[0].getMessage()


def test_enable_ftp_is_idempotent_no_write_when_enabled(locked, caplog):
    dev = _FakeConfigDevice(_ftp_item("Enabled"))
    c = Ultimate64Client("h")
    with caplog.at_level(logging.WARNING), \
            patch("urllib.request.urlopen", dev):
        wrote = enable_ftp_service(c)
    assert wrote is False
    assert dev.methods() == [("GET", ITEM_PATH)]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                and "FTP" in r.getMessage()]


def test_enable_ftp_raises_when_the_write_does_not_take(locked, caplog):
    dev = _FakeConfigDevice(_ftp_item("Disabled"), ignore_writes=True)
    c = Ultimate64Client("h")
    with caplog.at_level(logging.WARNING), \
            patch("urllib.request.urlopen", dev), \
            pytest.raises(Ultimate64ConfigVerifyError):
        enable_ftp_service(c)
    assert dev.methods() == [
        ("GET", ITEM_PATH), ("PUT", ITEM_PATH), ("GET", ITEM_PATH),
    ]
    # Nothing opened, so no "now open on the LAN" warning.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                and "open" in r.getMessage()]


def test_enable_ftp_service_is_exported_at_the_package_root():
    assert harness.enable_ftp_service is enable_ftp_service
    assert harness.Ultimate64ConfigVerifyError is Ultimate64ConfigVerifyError
