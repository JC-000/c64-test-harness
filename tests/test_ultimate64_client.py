"""Unit tests for Ultimate64Client (mocks urllib.request.urlopen)."""
from __future__ import annotations

import io
import json
import socket
from unittest.mock import MagicMock, patch

import pytest
import urllib.error

from c64_test_harness.backends.ultimate64_client import (
    Ultimate64AuthError,
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64ProtocolError,
    Ultimate64TimeoutError,
    _build_multipart,
)


class _FakeResponse:
    """Context-manager mock of a urlopen response."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _capture(response_body: bytes = b"{}", status: int = 200):
    """Return (mock_urlopen, captured_list) — each call appends the Request."""
    captured: list[tuple[object, float | None]] = []

    def _fake(req, timeout=None):
        captured.append((req, timeout))
        return _FakeResponse(response_body, status=status)

    mock = MagicMock(side_effect=_fake)
    return mock, captured


# ---------------------------------------------------------------- constructor
def test_constructor_validates_host():
    with pytest.raises(ValueError):
        Ultimate64Client("")


def test_constructor_validates_port():
    with pytest.raises(ValueError):
        Ultimate64Client("h", port=0)
    with pytest.raises(ValueError):
        Ultimate64Client("h", port=70000)


def test_constructor_validates_timeout():
    with pytest.raises(ValueError):
        Ultimate64Client("h", timeout=0)


def test_base_url_default_port_omits_port():
    c = Ultimate64Client("dev.lan")
    assert c._base == "http://dev.lan"


def test_base_url_custom_port_included():
    c = Ultimate64Client("dev.lan", port=8080)
    assert c._base == "http://dev.lan:8080"


# ---------------------------------------------------------------- headers
def test_password_header_added_when_set():
    mock, captured = _capture(b'{"version":"0.1"}')
    c = Ultimate64Client("h", password="secret")
    with patch("urllib.request.urlopen", mock):
        c.get_version()
    req = captured[0][0]
    assert req.get_header("X-password") == "secret"


def test_password_header_omitted_when_unset():
    mock, captured = _capture(b'{"version":"0.1"}')
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.get_version()
    req = captured[0][0]
    assert req.get_header("X-password") is None


# ---------------------------------------------------------------- URL construction
def test_get_version_url():
    mock, captured = _capture(b'{"version":"0.1"}')
    c = Ultimate64Client("10.0.0.5")
    with patch("urllib.request.urlopen", mock):
        c.get_version()
    assert captured[0][0].get_full_url() == "http://10.0.0.5/v1/version"


def test_get_info_url():
    mock, captured = _capture(b'{"product":"Ultimate 64 Elite"}')
    c = Ultimate64Client("10.0.0.5")
    with patch("urllib.request.urlopen", mock):
        result = c.get_info()
    assert captured[0][0].get_full_url() == "http://10.0.0.5/v1/info"
    assert result == {"product": "Ultimate 64 Elite"}


def test_category_name_url_encoded():
    mock, captured = _capture(b'{"U64 Specific Settings":{}, "errors":[]}')
    c = Ultimate64Client("10.0.0.5")
    with patch("urllib.request.urlopen", mock):
        c.get_config_category("U64 Specific Settings")
    url = captured[0][0].get_full_url()
    assert url == "http://10.0.0.5/v1/configs/U64%20Specific%20Settings"


def test_item_name_url_encoded():
    mock, captured = _capture(b'{"U64 Specific Settings":{"CPU Speed":{}}, "errors":[]}')
    c = Ultimate64Client("10.0.0.5")
    with patch("urllib.request.urlopen", mock):
        c.get_config_item("U64 Specific Settings", "CPU Speed")
    url = captured[0][0].get_full_url()
    assert url == "http://10.0.0.5/v1/configs/U64%20Specific%20Settings/CPU%20Speed"


def test_list_configs_returns_categories():
    body = b'{"categories":["Audio Mixer","U64 Specific Settings"],"errors":[]}'
    mock, captured = _capture(body)
    c = Ultimate64Client("10.0.0.5")
    with patch("urllib.request.urlopen", mock):
        cats = c.list_configs()
    assert cats == ["Audio Mixer", "U64 Specific Settings"]


def test_list_drives_url_and_parse():
    body = b'{"drives":[{"a":{"enabled":true}}],"errors":[]}'
    mock, captured = _capture(body)
    c = Ultimate64Client("10.0.0.5")
    with patch("urllib.request.urlopen", mock):
        result = c.list_drives()
    assert captured[0][0].get_full_url() == "http://10.0.0.5/v1/drives"
    assert result["drives"][0]["a"]["enabled"] is True


# ---------------------------------------------------------------- error mapping
def _http_error(status: int, body: bytes = b"nope") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x/",
        code=status,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def test_http_403_raises_auth_error():
    def _raise(req, timeout=None):
        raise _http_error(403)

    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(Ultimate64AuthError) as ei:
            c.get_info()
    assert ei.value.status == 403


def test_http_401_raises_auth_error():
    def _raise(req, timeout=None):
        raise _http_error(401)

    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(Ultimate64AuthError):
            c.get_info()


def test_http_500_raises_base_error():
    def _raise(req, timeout=None):
        raise _http_error(500, b"server blew up")

    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(Ultimate64Error) as ei:
            c.get_info()
    assert ei.value.status == 500
    assert not isinstance(ei.value, Ultimate64AuthError)


def test_socket_timeout_raises_timeout_error():
    def _raise(req, timeout=None):
        raise socket.timeout("timed out")

    c = Ultimate64Client("h", timeout=0.5)
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(Ultimate64TimeoutError):
            c.get_info()


def test_urlerror_raises_timeout_error():
    def _raise(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(Ultimate64TimeoutError):
            c.get_info()


def test_bad_json_raises_protocol_error():
    mock, _ = _capture(b"{not json}")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64ProtocolError):
            c.get_info()


# ---------------------------------------------------------------- memory
def test_read_mem_returns_raw_bytes():
    mock, captured = _capture(b"\x01\x02\x03\x04")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        data = c.read_mem(0x0400, 4)
    assert data == b"\x01\x02\x03\x04"
    url = captured[0][0].get_full_url()
    assert "/v1/machine:readmem" in url
    assert "address=0x0400" in url
    assert "length=4" in url


def test_read_mem_address_formatted_uppercase_hex():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.read_mem(0xABCD, 16)
    url = captured[0][0].get_full_url()
    assert "address=0xABCD" in url


def test_read_mem_validates_address():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.read_mem(-1, 1)
    with pytest.raises(ValueError):
        c.read_mem(0x10000, 1)
    with pytest.raises(ValueError):
        c.read_mem(0, 0)


def test_write_mem_uses_hex_data_query_param():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0x0400, b"\xde\xad\xbe\xef")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    # Device expects data as hex in a query string; no HTTP body.
    assert req.data is None
    url = req.get_full_url()
    assert "address=0x0400" in url
    assert "data=DEADBEEF" in url


def test_write_mem_empty_is_noop():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0x0400, b"")
    assert captured == []


def test_write_mem_validates_types():
    c = Ultimate64Client("h")
    with pytest.raises(TypeError):
        c.write_mem(0, "not bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        c.write_mem(-1, b"\x00")


# ---------------------------------------------------------------- machine ctrl
def test_reset_sends_put():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.reset()
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.get_full_url() == "http://h/v1/machine:reset"
    assert req.data is None


def test_all_machine_endpoints_mapped():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.reset()
        c.reboot()
        c.pause()
        c.resume()
        c.poweroff()
        c.menu_button()
    urls = [r[0].get_full_url() for r in captured]
    assert urls == [
        "http://h/v1/machine:reset",
        "http://h/v1/machine:reboot",
        "http://h/v1/machine:pause",
        "http://h/v1/machine:resume",
        "http://h/v1/machine:poweroff",
        "http://h/v1/machine:menu_button",
    ]


# ---------------------------------------------------------------- runners
def test_run_prg_sends_binary():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08\x0b\x08")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.get_full_url() == "http://h/v1/runners:run_prg"
    assert req.data == b"\x01\x08\x0b\x08"
    assert req.get_header("Content-type") == "application/octet-stream"


def test_sid_play_includes_songnr():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.sid_play(b"PSID", songnr=3)
    url = captured[0][0].get_full_url()
    assert url == "http://h/v1/runners:sid_play?songnr=3"


# ---------------------------------------------------------------- config write
def test_set_config_item_uses_value_query_param():
    mock, captured = _capture(b"{}")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.set_config_item("U64 Specific Settings", "CPU Speed", " 8")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    # Device expects ?value= query param, not a JSON body.
    assert req.get_full_url() == (
        "http://h/v1/configs/U64%20Specific%20Settings/CPU%20Speed?value=%208"
    )
    assert req.data is None


def test_set_config_items_issues_one_put_per_item():
    mock, captured = _capture(b"{}")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.set_config_items("Drive A Settings", {"Drive Bus ID": 8, "Drive Type": "1581"})
    urls = [r[0].get_full_url() for r in captured]
    assert urls == [
        "http://h/v1/configs/Drive%20A%20Settings/Drive%20Bus%20ID?value=8",
        "http://h/v1/configs/Drive%20A%20Settings/Drive%20Type?value=1581",
    ]
    assert all(r[0].get_method() == "PUT" for r in captured)
    assert all(r[0].data is None for r in captured)


def test_config_flash_endpoints():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.save_config_to_flash()
        c.load_config_from_flash()
        c.reset_config_to_default()
    urls = [r[0].get_full_url() for r in captured]
    assert urls == [
        "http://h/v1/configs:save_to_flash",
        "http://h/v1/configs:load_from_flash",
        "http://h/v1/configs:reset_to_default",
    ]


# ---------------------------------------------------------------- drives mount
def test_mount_disk_multipart_body():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mount_disk("a", b"\x01\x02\x03", "d64", mode="readonly")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    ct = req.get_header("Content-type")
    assert ct.startswith("multipart/form-data; boundary=")
    boundary = ct.split("boundary=", 1)[1]
    body = req.data
    assert boundary.encode() in body
    assert b'name="mode"' in body
    assert b"readonly" in body
    assert b'name="type"' in body
    assert b"d64" in body
    assert b'name="file"' in body
    assert b"image.d64" in body
    assert b"\x01\x02\x03" in body
    # terminated with closing boundary
    assert body.rstrip(b"\r\n").endswith(f"--{boundary}--".encode())


def test_mount_disk_url_includes_colon():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mount_disk("a", b"x", "d64")
    url = captured[0][0].get_full_url()
    # slot "a:" URL-encoded -> a%3A
    assert url == "http://h/v1/drives/a%3A:mount"


def test_mount_disk_validates_mode():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.mount_disk("a", b"x", "d64", mode="bogus")


def test_unmount_disk_url():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.unmount_disk("b")
    url = captured[0][0].get_full_url()
    assert url == "http://h/v1/drives/b%3A:unmount"


# ---------------------------------------------------------------- multipart helper
def test_build_multipart_structure():
    body = _build_multipart(
        "BOUNDARY",
        fields={"mode": "readwrite", "type": "d64"},
        file_field="file",
        file_name="image.d64",
        file_bytes=b"\xaa\xbb",
    )
    text = body.decode("latin-1")
    assert text.count("--BOUNDARY\r\n") == 3  # two fields + one file
    assert text.endswith("--BOUNDARY--\r\n")
    assert 'name="mode"' in text
    assert "readwrite" in text
    assert 'filename="image.d64"' in text
    assert "\xaa\xbb" in text


# ---------------------------------------------------------------- input validation
def test_get_config_category_rejects_empty():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.get_config_category("")


def test_get_config_item_rejects_empty():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.get_config_item("cat", "")
    with pytest.raises(ValueError):
        c.get_config_item("", "item")


def test_timeout_passed_to_urlopen():
    mock, captured = _capture(b"{}")
    c = Ultimate64Client("h", timeout=2.5)
    with patch("urllib.request.urlopen", mock):
        c.get_info()
    assert captured[0][1] == 2.5
