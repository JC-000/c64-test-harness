"""Unit tests for Ultimate64Client (mocks urllib.request.urlopen)."""
from __future__ import annotations

import http.client
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
    Ultimate64UnsafeOperationError,
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


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        http.client.IncompleteRead(b"\x01\x02", expected=6),
    ],
    ids=["connection-reset", "broken-pipe", "remote-disconnected", "incomplete-read"],
)
def test_connection_drop_maps_to_ultimate64_error(exc):
    """urllib only wraps the send phase in URLError; getresponse()/read()
    failures escape raw on fw 3.14d. They must land in the client's own
    exception hierarchy, not leak OSError/HTTPException to callers."""
    def _raise(req, timeout=None):
        raise exc

    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(Ultimate64TimeoutError) as ei:
            c.get_info()
    assert isinstance(ei.value, Ultimate64Error)
    assert "connection dropped" in str(ei.value)
    assert ei.value.__cause__ is exc


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
    mock, captured = _capture(b"\x00" * 16)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.read_mem(0xABCD, 16)
    url = captured[0][0].get_full_url()
    assert "address=0xABCD" in url


def test_read_mem_short_payload_raises_protocol_error():
    """A payload shorter than requested must raise, not silently truncate."""
    mock, _ = _capture(b"\x01\x02")  # device returned 2 of 8 bytes
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64ProtocolError) as ei:
            c.read_mem(0x0400, 8)
    assert "expected 8" in str(ei.value)
    assert isinstance(ei.value, Ultimate64Error)


def test_read_mem_long_payload_raises_protocol_error():
    """A payload longer than requested is equally malformed."""
    mock, _ = _capture(b"\x00" * 5)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64ProtocolError):
            c.read_mem(0x0400, 4)


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


def test_write_mem_small_payload_at_threshold_uses_put_query():
    """Exactly WRITE_MEM_QUERY_THRESHOLD bytes still takes the legacy PUT path."""
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    payload = bytes(range(Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD))
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0x0400, payload)
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.data is None
    url = req.get_full_url()
    assert "address=0x0400" in url
    assert f"data={payload.hex().upper()}" in url


def test_write_mem_large_payload_uses_post_with_body():
    """Payloads over the threshold switch to POST with raw-byte body.

    This is required because the device caps the ``data=`` query param
    at 128 hex chars; the error is
    ``"Maximum length of 128 bytes exceeded. Consider using POST method
    with attachment."``.
    """
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    payload = bytes(range(200))  # well past threshold
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0xC000, payload)
    req = captured[0][0]
    assert req.get_method() == "POST"
    # Raw bytes in HTTP body, not hex-encoded in query.
    assert req.data == payload
    assert req.get_header("Content-type") == "application/octet-stream"
    url = req.get_full_url()
    assert "address=0xC000" in url
    # No data= query string in POST form.
    assert "data=" not in url


def test_write_mem_just_above_threshold_uses_post():
    """One byte past the threshold crosses into POST territory."""
    mock, captured = _capture(b"")
    c = Ultimate64Client("h", write_mem_query_threshold=48)
    payload = bytes(range(c.write_mem_query_threshold + 1))
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0x1000, payload)
    req = captured[0][0]
    assert req.get_method() == "POST"
    assert req.data == payload


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
        c.poweroff(confirm_irrecoverable=True)
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


def test_poweroff_requires_confirmation_kwarg():
    """poweroff() must default-deny -- no kwarg means no HTTP call."""
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64UnsafeOperationError):
            c.poweroff()
    # Crucially: the guard fires before any HTTP request is made.
    assert captured == []


def test_poweroff_with_confirmation_fires_request():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.poweroff(confirm_irrecoverable=True)
    assert len(captured) == 1
    assert captured[0][0].get_full_url() == "http://h/v1/machine:poweroff"


def test_poweroff_rejects_positional_confirmation():
    """confirm_irrecoverable is keyword-only; passing positionally must fail."""
    c = Ultimate64Client("h")
    with pytest.raises(TypeError):
        c.poweroff(True)  # type: ignore[misc]


# ---------------------------------------------------------------- runners
def test_run_prg_sends_binary():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08\x0b\x08")
    req = captured[0][0]
    assert req.get_method() == "POST"
    assert req.get_full_url() == "http://h/v1/runners:run_prg"
    assert req.data == b"\x01\x08\x0b\x08"
    assert req.get_header("Content-type") == "application/octet-stream"


def test_run_prg_does_not_gc_temp_by_default(monkeypatch: pytest.MonkeyPatch):
    """U64_AUTO_TEMP_GC is unset by default -- run_prg must not touch FTP.

    Regression guard for issue #153: the auto-GC hook is opt-in
    specifically so this test (and every other run_prg test in this
    file, none of which mock ftplib) never makes a real network call.
    """
    monkeypatch.delenv("U64_AUTO_TEMP_GC", raising=False)
    c = Ultimate64Client("h")
    with patch.object(c, "gc_temp_folder") as mock_gc:
        mock, _ = _capture(b"")
        with patch("urllib.request.urlopen", mock):
            c.run_prg(b"\x01\x08\x0b\x08")
    mock_gc.assert_not_called()


def test_run_prg_gcs_temp_folder_when_auto_enabled(monkeypatch: pytest.MonkeyPatch):
    """U64_AUTO_TEMP_GC=1 makes run_prg GC /Temp before uploading (issue #153)."""
    monkeypatch.setenv("U64_AUTO_TEMP_GC", "1")
    c = Ultimate64Client("h")
    calls: list[str] = []

    def fake_gc(**kwargs):
        calls.append("gc")
        return None

    mock, captured = _capture(b"")
    with patch.object(c, "gc_temp_folder", side_effect=fake_gc) as mock_gc, \
         patch("urllib.request.urlopen", mock):
        c.run_prg(b"\x01\x08\x0b\x08")
    mock_gc.assert_called_once()
    # GC must run before the upload, not after.
    assert calls == ["gc"]
    assert captured[0][0].get_full_url() == "http://h/v1/runners:run_prg"


def test_client_gc_temp_folder_delegates_with_host():
    """Ultimate64Client.gc_temp_folder() forwards this client's host + kwargs."""
    c = Ultimate64Client("10.0.0.64")
    with patch(
        "c64_test_harness.backends.ultimate64_temp_gc.gc_temp_folder"
    ) as mock_module_gc:
        mock_module_gc.return_value = "sentinel"
        result = c.gc_temp_folder(keep=5, ftp_username="bench", ftp_password="hunter2")
    assert result == "sentinel"
    mock_module_gc.assert_called_once_with(
        "10.0.0.64", port=None, username="bench", password="hunter2", keep=5, timeout=10.0
    )


def test_sid_play_includes_songnr():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.sid_play(b"PSID", songnr=3)
    req = captured[0][0]
    # Firmware 3.14 exposes POST /v1/runners:sidplay (no underscore).
    assert req.get_method() == "POST"
    assert req.get_full_url() == "http://h/v1/runners:sidplay?songnr=3"
    assert req.data == b"PSID"
    assert req.get_header("Content-type") == "application/octet-stream"


def test_mod_play_sends_post():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mod_play(b"MODDATA")
    req = captured[0][0]
    # Firmware 3.14 exposes POST /v1/runners:modplay (no underscore).
    assert req.get_method() == "POST"
    assert req.get_full_url() == "http://h/v1/runners:modplay"
    assert req.data == b"MODDATA"
    assert req.get_header("Content-type") == "application/octet-stream"


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


def test_load_config_from_flash_single_category():
    """Per-category reload hits ``/v1/configs/<category>:load_from_flash``.

    The category is percent-encoded as one path segment; the ``:load_from_flash``
    suffix stays literal. An empty category is a local ValueError, not a wire call.
    """
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.load_config_from_flash("C64 and Cartridge Settings")
        with pytest.raises(ValueError):
            c.load_config_from_flash("")
    assert [r[0].get_full_url() for r in captured] == [
        "http://h/v1/configs/C64%20and%20Cartridge%20Settings:load_from_flash",
    ]
    assert captured[0][0].get_method() == "PUT"
    assert captured[0][0].data is None


# ---------------------------------------------------------------- drives mount
def test_mount_disk_multipart_body():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mount_disk("a", b"\x01\x02\x03", "d64", mode="readonly")
    req = captured[0][0]
    # POST is the upload-and-mount route; PUT is mount-by-device-path and
    # has no body handler at all. See test_mount_disk_with_a_body_uses_post.
    assert req.get_method() == "POST"
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


def test_mount_disk_slot_is_a_plain_letter():
    """The slot must not carry an encoded colon.

    ``/v1/drives/a%3A:mount`` draws 400 "Invalid Drive 'a:'" -- the
    trailing colon is the firmware's verb separator, so the drive
    identifier cannot contain one. This test used to assert the
    over-encoded form, which is why the bug survived a live audit that
    fixed the same construction in ``unmount_disk``.
    """
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mount_disk("a", b"x", "d64")
    url = captured[0][0].get_full_url()
    assert "%3A" not in url, url
    assert url == "http://h/v1/drives/a:mount"


def test_mount_disk_with_a_body_uses_post():
    """Upload-and-mount is the POST form; PUT is mount-by-device-path.

    S: 1541u-315preview software/api/route_drives.cc:109 registers
    ``API_CALL(PUT, drives, mount, NULL, ...)`` with ``image`` P_REQUIRED
    and a NULL body handler, while :141 registers
    ``API_CALL(POST, drives, mount, &attachment_writer, ...)`` which
    takes multipart or application/octet-stream. Sending a body by PUT
    hits the route that wants an ``image`` query argument and has no
    body handler, so it 400s whatever the body looks like.
    """
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mount_disk("a", b"x", "d64")
    assert captured[0][0].get_method() == "POST"


def test_mount_disk_image_accepts_a_device_path():
    """The PUT form: mount an image already on the device."""
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.mount_disk_path("a", "/Usb0/games/disk.d64", mode="readonly")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    url = req.get_full_url()
    assert url.startswith("http://h/v1/drives/a:mount?")
    # urllib.parse.quote defaults to safe="/", which every query in this
    # client relies on, and an unencoded "/" is legal in a query
    # component. The device path therefore appears verbatim.
    assert "image=/Usb0/games/disk.d64" in url
    assert "mode=readonly" in url
    assert req.data is None


def test_mount_disk_path_validates_drive_and_mode():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.mount_disk_path("c", "/Usb0/x.d64")
    with pytest.raises(ValueError):
        c.mount_disk_path("a", "/Usb0/x.d64", mode="bogus")
    with pytest.raises(ValueError):
        c.mount_disk_path("a", "")


def test_drive_slot_path_normalises_the_caller_s_spelling():
    """One construction point, so a third hand-rolled site cannot appear.

    Callers have long been told the trailing colon is added for them, so
    "a:" must keep working -- it just must not reach the wire.
    """
    for spelling in ("a", "A", "a:", "A:"):
        assert (
            Ultimate64Client._drive_slot_path(spelling, "mount")
            == "/v1/drives/a:mount"
        )


def test_drive_slot_path_allows_softiec():
    """S: route_drives.cc:95 PATH_PARAM_ENUM("drive", "a,b,softiec").

    The validation was narrower than the firmware, so the IEC file
    system could not be addressed at all.
    """
    assert (
        Ultimate64Client._drive_slot_path("softiec", "mount")
        == "/v1/drives/softiec:mount"
    )


def test_drive_slot_path_still_rejects_nonsense():
    for bad in ("c", "", "a b", "../etc"):
        with pytest.raises(ValueError):
            Ultimate64Client._drive_slot_path(bad, "mount")


def test_mount_disk_routes_through_the_shared_builder():
    """Regression guard for the class of bug, not the instance.

    #167 was one hand-rolled path construction left behind when its
    sibling was fixed. Asserting the shared builder is actually used
    means a future hand-rolled site fails here rather than on hardware.
    """
    seen = []
    real = Ultimate64Client._drive_slot_path

    def spy(drive, action):
        seen.append((drive, action))
        return real(drive, action)

    mock, _ = _capture(b"")
    c = Ultimate64Client("h")
    with patch.object(
        Ultimate64Client, "_drive_slot_path", staticmethod(spy)
    ), patch("urllib.request.urlopen", mock):
        c.mount_disk("a", b"x", "d64")
    assert seen == [("a", "mount")]


def test_mount_disk_validates_mode():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.mount_disk("a", b"x", "d64", mode="bogus")


def test_unmount_disk_url():
    """The `:unmount` endpoint this used to assert has never existed.

    It 404s on 3.14, 3.15 and 1.1.0 alike; `:remove` is the unmount verb,
    and the slot takes the plain letter (a percent-encoded `b:` draws a
    400 "Invalid Drive"). See `test_unmount_disk_targets_the_remove_endpoint`.
    """
    mock, captured = _capture(b"")
    c = Ultimate64Client("h", write_mem_query_threshold=48)
    with patch("urllib.request.urlopen", mock):
        c.unmount_disk("b")
    url = captured[0][0].get_full_url()
    assert url == "http://h/v1/drives/b:remove"


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


# ---------------------------------------------------------------- drive control
def test_drive_on_off_reset_urls():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.drive_on("a")
        c.drive_off("a")
        c.drive_reset("b")
    urls = [r[0].get_full_url() for r in captured]
    assert urls == [
        "http://h/v1/drives/a:on",
        "http://h/v1/drives/a:off",
        "http://h/v1/drives/b:reset",
    ]
    assert all(r[0].get_method() == "PUT" for r in captured)
    assert all(r[0].data is None for r in captured)


def test_drive_remove_disk_and_unlink_urls():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.drive_remove_disk("a")
        c.drive_unlink("b")
    urls = [r[0].get_full_url() for r in captured]
    assert urls == [
        "http://h/v1/drives/a:remove",
        "http://h/v1/drives/b:unlink",
    ]


def test_drive_set_mode_url_and_query():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.drive_set_mode("a", "1581")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.get_full_url() == "http://h/v1/drives/a:set_mode?mode=1581"
    assert req.data is None


def test_drive_set_mode_rejects_invalid_mode():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.drive_set_mode("a", "1551")


def test_drive_methods_reject_invalid_drive():
    c = Ultimate64Client("h")
    for fn in (c.drive_on, c.drive_off, c.drive_reset, c.drive_remove_disk, c.drive_unlink):
        with pytest.raises(ValueError):
            fn("c")
    with pytest.raises(ValueError):
        c.drive_set_mode("c", "1541")


def test_drive_load_rom_with_bytes_uses_multipart_put():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.drive_load_rom("a", b"\xaa\xbb\xcc")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.get_full_url() == "http://h/v1/drives/a:load_rom"
    ct = req.get_header("Content-type")
    assert ct.startswith("multipart/form-data; boundary=")
    assert b'name="file"' in req.data
    assert b"\xaa\xbb\xcc" in req.data


def test_drive_load_rom_with_str_uses_file_query():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.drive_load_rom("b", "/Roms/dos1541.rom")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.data is None
    url = req.get_full_url()
    assert url == "http://h/v1/drives/b:load_rom?file=/Roms/dos1541.rom"


def test_drive_load_rom_rejects_bad_type():
    c = Ultimate64Client("h")
    with pytest.raises(TypeError):
        c.drive_load_rom("a", 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------- files
def test_file_info_url_and_parse():
    body = b'{"size":174848,"extension":"d64"}'
    mock, captured = _capture(body)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        result = c.file_info("Usb0/Disks/foo.d64")
    req = captured[0][0]
    assert req.get_method() == "GET"
    assert req.get_full_url() == "http://h/v1/files/Usb0/Disks/foo.d64:info"
    assert result == {"size": 174848, "extension": "d64"}


def test_file_info_rejects_empty():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.file_info("")


def test_create_d64_default_tracks_and_query():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.create_d64("Usb0/new.d64", diskname="MYDISK")
    req = captured[0][0]
    assert req.get_method() == "PUT"
    url = req.get_full_url()
    assert url.startswith("http://h/v1/files/Usb0/new.d64:create_d64?")
    assert "tracks=35" in url
    assert "diskname=MYDISK" in url


def test_create_d64_accepts_40_tracks():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.create_d64("foo.d64", tracks=40)
    assert "tracks=40" in captured[0][0].get_full_url()


def test_create_d64_rejects_bad_tracks():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.create_d64("foo.d64", tracks=42)


def test_create_d71_url():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.create_d71("foo.d71", diskname="X")
    url = captured[0][0].get_full_url()
    assert url == "http://h/v1/files/foo.d71:create_d71?diskname=X"


def test_create_d81_url():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.create_d81("foo.d81", diskname="Y")
    url = captured[0][0].get_full_url()
    assert url == "http://h/v1/files/foo.d81:create_d81?diskname=Y"


def test_create_dnp_default_tracks_and_query():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.create_dnp("foo.dnp", tracks=10, diskname="N")
    url = captured[0][0].get_full_url()
    assert url.startswith("http://h/v1/files/foo.dnp:create_dnp?")
    assert "tracks=10" in url
    assert "diskname=N" in url


def test_create_dnp_rejects_out_of_range_tracks():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.create_dnp("foo.dnp", tracks=0)
    with pytest.raises(ValueError):
        c.create_dnp("foo.dnp", tracks=256)


# ---------------------------------------------------------------- debug / measure
def test_get_debug_register_int_response():
    mock, captured = _capture(b"42")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        v = c.get_debug_register()
    req = captured[0][0]
    assert req.get_method() == "GET"
    assert req.get_full_url() == "http://h/v1/machine:debugreg"
    assert v == 42


def test_get_debug_register_json_value_response():
    mock, _ = _capture(b'{"value":"0xAB"}')
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        assert c.get_debug_register() == 0xAB


def test_set_debug_register_url_and_query():
    mock, captured = _capture(b"")
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        c.set_debug_register(0x7F)
    req = captured[0][0]
    assert req.get_method() == "PUT"
    assert req.get_full_url() == "http://h/v1/machine:debugreg?value=127"
    assert req.data is None


def test_set_debug_register_rejects_out_of_range():
    c = Ultimate64Client("h")
    with pytest.raises(ValueError):
        c.set_debug_register(-1)
    with pytest.raises(ValueError):
        c.set_debug_register(256)


def test_measure_bus_timing_returns_raw_bytes():
    vcd = b"$date\n  Mon\n$end\n#0\n0!\n#1\n1!\n"
    mock, captured = _capture(vcd)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", mock):
        data = c.measure_bus_timing()
    assert data == vcd
    req = captured[0][0]
    assert req.get_method() == "GET"
    assert req.get_full_url() == "http://h/v1/machine:measure"


# ---------------------------------------------------------------- batch config
def test_set_config_items_batch_posts_json_body():
    mock, captured = _capture(b"{}")
    c = Ultimate64Client("h")
    updates = {
        "Drive A Settings": {"Drive Bus ID": 8, "Drive Type": "1581"},
        "U64 Specific Settings": {"CPU Speed": " 8"},
    }
    with patch("urllib.request.urlopen", mock):
        c.set_config_items_batch(updates)
    assert len(captured) == 1
    req = captured[0][0]
    assert req.get_method() == "POST"
    assert req.get_full_url() == "http://h/v1/configs"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data.decode("utf-8")) == updates


def test_set_config_items_batch_validates_inputs():
    c = Ultimate64Client("h")
    with pytest.raises(TypeError):
        c.set_config_items_batch("nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        c.set_config_items_batch({"": {"x": 1}})
    with pytest.raises(TypeError):
        c.set_config_items_batch({"cat": "not-a-dict"})  # type: ignore[dict-item]


# ---------------------------------------------------------------- send_text
def test_send_text_appends_return():
    """send_text() PETSCII-encodes the text and writes a trailing 0x0D."""
    c = Ultimate64Client("h")
    writes: list[tuple[int, bytes]] = []
    reads: list[tuple[int, int]] = []

    def fake_read(addr: int, length: int) -> bytes:
        reads.append((addr, length))
        return b"\x00"  # buffer always empty

    def fake_write(addr: int, data: bytes) -> None:
        writes.append((addr, bytes(data)))

    with patch.object(c, "read_mem", side_effect=fake_read), \
         patch.object(c, "write_mem", side_effect=fake_write):
        c.send_text("AB")

    # Two writes per chunk: payload to $0277, count byte to $00C6.
    payload_writes = [w for w in writes if w[0] == Ultimate64Client.KEYBUF_ADDR]
    assert len(payload_writes) == 1
    payload = payload_writes[0][1]
    # "AB" + CR
    assert payload == bytes([0x41, 0x42, 0x0D])
    count_writes = [w for w in writes if w[0] == Ultimate64Client.KEYBUF_COUNT_ADDR]
    assert count_writes[-1][1] == bytes([3])


def test_send_text_no_return_when_disabled():
    """finish_with_return=False omits the trailing 0x0D byte."""
    c = Ultimate64Client("h")
    writes: list[tuple[int, bytes]] = []

    def fake_read(addr: int, length: int) -> bytes:
        return b"\x00"

    def fake_write(addr: int, data: bytes) -> None:
        writes.append((addr, bytes(data)))

    with patch.object(c, "read_mem", side_effect=fake_read), \
         patch.object(c, "write_mem", side_effect=fake_write):
        c.send_text("AB", finish_with_return=False)

    payload_writes = [w for w in writes if w[0] == Ultimate64Client.KEYBUF_ADDR]
    assert len(payload_writes) == 1
    assert payload_writes[0][1] == bytes([0x41, 0x42])
    assert 0x0D not in payload_writes[0][1]


def test_send_text_waits_for_empty_buffer_before_writing():
    """send_text must not top up a partially-full buffer.

    The read-$C6 / write-chunk / write-$C6 sequence is three HTTP
    round-trips ~100 ms apart while the KERNAL dequeues from the front
    at 50/60 Hz — writing at a non-zero offset races the dequeue and
    produces garbage keystrokes.  The fix: poll $C6 until it reads 0,
    then write the chunk at offset 0 with a single count publication.
    """
    c = Ultimate64Client("h")
    # Simulate the KERNAL draining an in-flight buffer between polls:
    # $C6 reads 4, then 2, then 0 (empty), then 0 for any later polls.
    counts = iter([4, 2, 0])
    events: list[tuple[str, int, bytes]] = []

    def fake_read(addr: int, length: int) -> bytes:
        assert addr == Ultimate64Client.KEYBUF_COUNT_ADDR
        value = next(counts, 0)
        events.append(("read", addr, bytes([value])))
        return bytes([value])

    def fake_write(addr: int, data: bytes) -> None:
        events.append(("write", addr, bytes(data)))

    with patch.object(c, "read_mem", side_effect=fake_read), \
         patch.object(c, "write_mem", side_effect=fake_write):
        c.send_text("AB")

    writes = [e for e in events if e[0] == "write"]
    # No write may happen until the buffer polled empty (three reads first).
    reads_before_first_write = events[: events.index(writes[0])]
    assert [e[2][0] for e in reads_before_first_write] == [4, 2, 0]
    # Chunk lands at offset 0 ($0277 exactly), never $0277+current.
    assert writes[0] == ("write", Ultimate64Client.KEYBUF_ADDR, bytes([0x41, 0x42, 0x0D]))
    # Count write publishes exactly the chunk length, not current+len.
    assert writes[1] == ("write", Ultimate64Client.KEYBUF_COUNT_ADDR, bytes([3]))
    assert len(writes) == 2


def test_send_text_long_string_chunks_start_at_offset_zero():
    """Strings past KEYBUF_MAX are split into full-buffer chunks, each
    written at $0277 offset 0 after the previous chunk drains to empty."""
    c = Ultimate64Client("h")
    # 12 chars + CR = 13 codes -> chunks of 10 and 3.
    text = "ABCDEFGHIJKL"
    # Poll sequence: empty (write chunk 1), still draining (5), empty
    # (write chunk 2).
    counts = iter([0, 5, 0])
    writes: list[tuple[int, bytes]] = []

    def fake_read(addr: int, length: int) -> bytes:
        return bytes([next(counts, 0)])

    def fake_write(addr: int, data: bytes) -> None:
        writes.append((addr, bytes(data)))

    with patch.object(c, "read_mem", side_effect=fake_read), \
         patch.object(c, "write_mem", side_effect=fake_write):
        c.send_text(text)

    payload_writes = [w for w in writes if w[0] == Ultimate64Client.KEYBUF_ADDR]
    count_writes = [w for w in writes if w[0] == Ultimate64Client.KEYBUF_COUNT_ADDR]
    assert len(payload_writes) == 2
    assert payload_writes[0][1] == bytes([0x41 + i for i in range(10)])
    assert payload_writes[1][1] == bytes([0x4B, 0x4C, 0x0D])
    assert [w[1] for w in count_writes] == [bytes([10]), bytes([3])]
    # Every payload write targets the buffer base — no offset writes at all.
    assert all(w[0] == Ultimate64Client.KEYBUF_ADDR for w in payload_writes)


# ---------------------------------------------------------------- run_prg fallback
def test_run_prg_falls_back_on_404(caplog):
    """On 404, run_prg sideloads via write_mem + send_text("SYS <addr>")."""
    import logging

    c = Ultimate64Client("h")
    # PRG load address $0360 = 864 (the canonical "tape buffer" trampoline).
    prg = bytes([0x60, 0x03]) + b"\xAA\xBB\xCC"

    write_mem_calls: list[tuple[int, bytes]] = []
    send_text_calls: list[tuple[str, bool]] = []

    def fake_post_binary(path, data, query=None):
        # Mimic the firmware 404 by raising the matching client exception.
        raise Ultimate64Error(f"POST {path} returned HTTP 404", status=404)

    def fake_write_mem(addr: int, data: bytes) -> None:
        write_mem_calls.append((addr, bytes(data)))

    def fake_send_text(text: str, *, finish_with_return: bool = True) -> None:
        send_text_calls.append((text, finish_with_return))

    with patch.object(c, "_post_binary", side_effect=fake_post_binary), \
         patch.object(c, "write_mem", side_effect=fake_write_mem), \
         patch.object(c, "send_text", side_effect=fake_send_text), \
         caplog.at_level(logging.WARNING, logger="c64_test_harness.backends.ultimate64_client"):
        c.run_prg(prg)

    assert write_mem_calls == [(0x0360, b"\xAA\xBB\xCC")]
    assert send_text_calls == [("SYS 864", True)]
    assert any("404" in r.message and "fallback" not in r.message.lower() or
               "writemem" in r.message for r in caplog.records)


def test_run_prg_falls_back_on_404_basic_stub_types_run(caplog):
    """A $0801 BASIC-stub PRG must be triggered with RUN, not SYS 2049.

    SYS 2049 would execute the BASIC line-link bytes as 6502 opcodes and
    corrupt the machine; the real runner endpoint's semantics are
    load-and-RUN.
    """
    import logging

    c = Ultimate64Client("h")
    # Canonical BASIC stub: 10 SYS 2062 — load address $0801.
    stub_body = bytes([
        0x0B, 0x08,              # link to next line ($080B)
        0x0A, 0x00,              # line number 10
        0x9E,                    # SYS token
        0x32, 0x30, 0x36, 0x32,  # "2062"
        0x00,                    # end of line
        0x00, 0x00,              # end of program
    ])
    prg = bytes([0x01, 0x08]) + stub_body

    write_mem_calls: list[tuple[int, bytes]] = []
    send_text_calls: list[tuple[str, bool]] = []

    def fake_post_binary(path, data, query=None):
        raise Ultimate64Error(f"POST {path} returned HTTP 404", status=404)

    def fake_write_mem(addr: int, data: bytes) -> None:
        write_mem_calls.append((addr, bytes(data)))

    def fake_send_text(text: str, *, finish_with_return: bool = True) -> None:
        send_text_calls.append((text, finish_with_return))

    with patch.object(c, "_post_binary", side_effect=fake_post_binary), \
         patch.object(c, "write_mem", side_effect=fake_write_mem), \
         patch.object(c, "send_text", side_effect=fake_send_text), \
         caplog.at_level(logging.WARNING, logger="c64_test_harness.backends.ultimate64_client"):
        c.run_prg(prg)

    assert write_mem_calls == [(0x0801, stub_body)]
    # RUN, never "SYS 2049".
    assert send_text_calls == [("RUN", True)]
    # The warning names the trigger path that was taken.
    assert any("'RUN'" in r.getMessage() for r in caplog.records)


def test_run_prg_fallback_disabled():
    """fallback_on_404=False surfaces the 404 instead of side-loading."""
    c = Ultimate64Client("h")
    prg = bytes([0x60, 0x03]) + b"\xAA"

    def fake_post_binary(path, data, query=None):
        raise Ultimate64Error("POST 404", status=404)

    sent_texts: list[str] = []

    def fake_send_text(*a, **k):
        sent_texts.append("called")

    with patch.object(c, "_post_binary", side_effect=fake_post_binary), \
         patch.object(c, "send_text", side_effect=fake_send_text):
        with pytest.raises(Ultimate64Error) as ei:
            c.run_prg(prg, fallback_on_404=False)
    assert ei.value.status == 404
    assert sent_texts == []


# ---------------------------------------------------------------- write_mem threshold
def test_write_mem_threshold_autodetect_3_14d():
    """fw 3.14d auto-detects to 128, not the conservative 48 default."""
    mock, _ = _capture(b'{"firmware_version":"V3.14d","product":"Ultimate 64"}')
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h")
        # The property is lazy; resolve it inside the patch context.
        assert c.write_mem_query_threshold == 128


def test_write_mem_threshold_autodetect_older_firmware_is_protected():
    """Firmware below 3.15 lacks the Temp-folder fix, whatever the version.

    This used to assert 48 for anything that was not literally `3.14*`,
    which handed the permissive threshold to every older build and to the
    whole CBM line. The rule is now "has the fix", not "is 3.14".
    """
    mock, _ = _capture(b'{"firmware_version":"V3.13","product":"Ultimate 64"}')
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h")
        assert c.write_mem_query_threshold == 128


def test_write_mem_threshold_kwarg_override():
    """Explicit kwarg wins over autodetect (no probe issued)."""
    mock, captured = _capture(b'{"firmware_version":"V3.14d"}')
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", write_mem_query_threshold=64)
        # Reading the threshold must not consume HTTP traffic.
        assert c.write_mem_query_threshold == 64
    assert captured == []


def test_write_mem_threshold_probe_failure_is_conservative():
    """A failed probe must assume the fix is absent, not present.

    Guessing "present" puts small writes back on the leaking POST path;
    guessing "absent" only costs a higher PUT threshold.
    """
    def _raise(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=_raise):
        c = Ultimate64Client("h")
        # Construction must not raise; resolving the property must not raise.
        assert c.write_mem_query_threshold == 128
        assert c.capabilities.writemem_post_safe is False


# ------------------------------------------- drive unmount endpoint (audit #3)
def test_unmount_disk_targets_the_remove_endpoint():
    """`:unmount` has never existed on any firmware.

    3.15's `software/api/route_drives.cc` registers mount / reset / remove /
    on / off / unlink / load_rom / set_mode. `remove` is the unmount verb;
    `unmount` 404s on 3.14, 3.15 and 1.1.0 alike.
    """
    mock, captured = _capture()
    c = Ultimate64Client("h", write_mem_query_threshold=48)
    with patch("urllib.request.urlopen", mock):
        c.unmount_disk("a")
    assert captured[0][0].full_url == "http://h/v1/drives/a:remove"


def test_unmount_disk_does_not_percent_encode_the_slot():
    """`/v1/drives/a%3A:remove` answers 400 "Invalid Drive 'a:'".

    The slot takes the plain letter (verified live 2026-07-28).
    """
    mock, captured = _capture()
    c = Ultimate64Client("h", write_mem_query_threshold=48)
    with patch("urllib.request.urlopen", mock):
        c.unmount_disk("a:")
    assert "%3A" not in captured[0][0].full_url
    assert captured[0][0].full_url == "http://h/v1/drives/a:remove"


def test_unmount_disk_rejects_an_unknown_slot():
    c = Ultimate64Client("h", write_mem_query_threshold=48)
    with pytest.raises(ValueError):
        c.unmount_disk("z")


# --------------------------------- write_mem threshold via capabilities (#1)
def test_write_mem_threshold_autodetect_3_15_uses_post_sooner():
    """3.15 carries the Temp-folder fix, so the POST path is safe again."""
    mock, _ = _capture(b'{"firmware_version":"3.15","product":"Ultimate 64 Elite"}')
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h")
        assert c.write_mem_query_threshold == 48


def test_write_mem_threshold_autodetect_c64u_1_1_0_is_protected():
    """Regression: 1.1.0 is not `3.14*`, so the old string match left the
    C64U on the permissive threshold despite predating the fix."""
    mock, _ = _capture(b'{"firmware_version":"1.1.0"}')
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h")
        assert c.write_mem_query_threshold == 128


def test_client_exposes_capabilities():
    mock, _ = _capture(b'{"firmware_version":"3.15"}')
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h")
        assert c.capabilities.writemem_post_safe is True
        assert c.capabilities.runner_wedge_possible is False
