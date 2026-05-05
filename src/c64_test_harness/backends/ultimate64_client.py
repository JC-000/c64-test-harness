"""REST API client for the Ultimate 64 / Ultimate II+ family.

Targets the device's HTTP v1 API (plain HTTP, no TLS). Zero runtime
dependencies — `urllib.request` only.

Response shape for config queries is always:
    { "<Category Name>": { ...items... }, "errors": [] }

The client does NOT auto-unwrap the category key — callers inspect the
raw response. `errors` is always passed through; non-empty `errors`
arrays should be treated as a soft failure by the caller.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

__all__ = [
    "Ultimate64Client",
    "Ultimate64Error",
    "Ultimate64AuthError",
    "Ultimate64TimeoutError",
    "Ultimate64ProtocolError",
]

_log = logging.getLogger(__name__)


class Ultimate64Error(Exception):
    """Base exception for Ultimate64Client failures."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class Ultimate64AuthError(Ultimate64Error):
    """Raised on HTTP 401/403 — bad or missing X-Password."""


class Ultimate64TimeoutError(Ultimate64Error):
    """Raised when the HTTP request times out or the device is unreachable."""


class Ultimate64ProtocolError(Ultimate64Error):
    """Raised when a JSON response cannot be parsed."""


def _encode(value: str) -> str:
    """URL-encode a single path segment (including spaces and colons)."""
    return urllib.parse.quote(value, safe="")


class Ultimate64Client:
    """HTTP REST client for Ultimate 64 / Ultimate II+ devices.

    All methods either return parsed JSON / raw bytes, or raise
    :class:`Ultimate64Error` (or a subclass) on failure.

    The client is stateless between calls — each call opens a fresh
    TCP connection via ``urllib.request.urlopen``.
    """

    def __init__(
        self,
        host: str,
        password: str | None = None,
        port: int = 80,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if port <= 0 or port > 65535:
            raise ValueError(f"port out of range: {port}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._base = f"http://{host}:{port}" if port != 80 else f"http://{host}"

    def close(self) -> None:
        """No-op — the client is stateless (uses a fresh connection per call)."""
        return None

    # ----------------------------------------------------------------- internal
    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._base + path

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> tuple[int, bytes]:
        url = self._url(path)
        if query:
            # Preserve caller-formatted values (e.g. "0x0400") by stringifying as-is
            qs = "&".join(f"{urllib.parse.quote(str(k))}={urllib.parse.quote(str(v))}" for k, v in query.items())
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, data=body, method=method)
        if self.password:
            req.add_header("X-Password", self.password)
        if content_type:
            req.add_header("Content-Type", content_type)
        _log.debug("Ultimate64 %s %s (body=%s bytes)", method, url, len(body) if body else 0)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                data = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                data = e.read() if e.fp else b""
            except Exception:
                data = b""
            _log.debug("Ultimate64 %s %s -> %d (error body=%s bytes)", method, url, status, len(data))
            self._raise_for_status(status, data, method, url)
            return status, data  # unreachable
        except socket.timeout as e:
            raise Ultimate64TimeoutError(f"timeout after {self.timeout}s: {method} {url}") from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, socket.timeout):
                raise Ultimate64TimeoutError(f"timeout after {self.timeout}s: {method} {url}") from e
            raise Ultimate64TimeoutError(f"connection failed: {method} {url}: {reason}") from e

        _log.debug("Ultimate64 %s %s -> %d (%d bytes)", method, url, status, len(data))
        if status < 200 or status >= 300:
            self._raise_for_status(status, data, method, url)
        return status, data

    @staticmethod
    def _raise_for_status(status: int, data: bytes, method: str, url: str) -> None:
        body_text = data.decode("utf-8", errors="replace") if data else ""
        msg = f"{method} {url} returned HTTP {status}"
        if body_text:
            msg += f": {body_text[:256]}"
        if status in (401, 403):
            raise Ultimate64AuthError(msg, status=status, body=body_text)
        raise Ultimate64Error(msg, status=status, body=body_text)

    def _get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        _, data = self._request("GET", path, query=query)
        return self._parse_json(data)

    def _put_no_body(self, path: str, query: dict[str, Any] | None = None) -> None:
        self._request("PUT", path, query=query)

    def _put_json(self, path: str, payload: Any) -> Any:
        body = json.dumps(payload).encode("utf-8")
        status, data = self._request("PUT", path, body=body, content_type="application/json")
        if data:
            try:
                return self._parse_json(data)
            except Ultimate64ProtocolError:
                return None
        return None

    @staticmethod
    def _parse_json(data: bytes) -> Any:
        if not data:
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Ultimate64ProtocolError(f"invalid JSON from device: {e}") from e

    # ----------------------------------------------------------------- identity
    def get_version(self) -> dict:
        """GET /v1/version — REST API version info."""
        return self._get_json("/v1/version")

    def get_info(self) -> dict:
        """GET /v1/info — product, firmware_version, fpga_version, etc."""
        return self._get_json("/v1/info")

    def list_configs(self) -> list[str]:
        """GET /v1/configs — returns the list of config category names."""
        payload = self._get_json("/v1/configs")
        if not isinstance(payload, dict):
            raise Ultimate64ProtocolError(f"expected object from /v1/configs, got {type(payload).__name__}")
        cats = payload.get("categories", [])
        if not isinstance(cats, list):
            raise Ultimate64ProtocolError("categories field is not a list")
        return [str(c) for c in cats]

    def get_config_category(self, category: str) -> dict:
        """GET /v1/configs/<category> — all items in a category.

        Returns the raw response, including the ``<Category>`` wrapper key
        and the ``errors`` array.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        return self._get_json(f"/v1/configs/{_encode(category)}")

    def get_config_item(self, category: str, item: str) -> dict:
        """GET /v1/configs/<category>/<item> — single item with enum/range info."""
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(item, str) or not item:
            raise ValueError("item must be a non-empty string")
        return self._get_json(f"/v1/configs/{_encode(category)}/{_encode(item)}")

    def list_drives(self) -> dict:
        """GET /v1/drives — enumerates all drive slots."""
        return self._get_json("/v1/drives")

    # ------------------------------------------------------------ machine ctrl
    def reset(self) -> None:
        """PUT /v1/machine:reset — soft reset the C64 (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:reset")

    def reboot(self) -> None:
        """PUT /v1/machine:reboot — full reboot of the Ultimate device (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:reboot")

    def pause(self) -> None:
        """PUT /v1/machine:pause — halt the emulated CPU (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:pause")

    def resume(self) -> None:
        """PUT /v1/machine:resume — resume the emulated CPU."""
        self._put_no_body("/v1/machine:resume")

    def poweroff(self) -> None:
        """PUT /v1/machine:poweroff — power off the C64 side (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:poweroff")

    def menu_button(self) -> None:
        """PUT /v1/machine:menu_button — press the Ultimate menu button (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:menu_button")

    # ----------------------------------------------------------------- memory
    def read_mem(self, address: int, length: int) -> bytes:
        """GET /v1/machine:readmem — read `length` bytes from C64 memory via DMA.

        Returns the raw byte payload. Address is formatted as 0xNNNN.
        """
        if not isinstance(address, int) or address < 0 or address > 0xFFFF:
            raise ValueError(f"address out of range 0..0xFFFF: {address}")
        if not isinstance(length, int) or length <= 0:
            raise ValueError(f"length must be positive, got {length}")
        query = {"address": "0x%04X" % address, "length": "%d" % length}
        _, data = self._request("GET", "/v1/machine:readmem", query=query)
        return data

    def write_mem(self, address: int, data: bytes) -> None:
        """PUT /v1/machine:writemem — write bytes to C64 memory via DMA (DESTRUCTIVE).

        The device expects the payload as a hex-encoded ``data=`` query
        parameter (not an HTTP body). Each byte becomes two uppercase
        hex nibbles with no prefix or separator, e.g. ``414243`` for
        ``b"ABC"``.
        """
        if not isinstance(address, int) or address < 0 or address > 0xFFFF:
            raise ValueError(f"address out of range 0..0xFFFF: {address}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        if not data:
            return
        query = {
            "address": "0x%04X" % address,
            "data": bytes(data).hex().upper(),
        }
        self._request("PUT", "/v1/machine:writemem", query=query)

    # ------------------------------------------------------------ code runners
    def load_prg(self, data: bytes) -> None:
        """POST /v1/runners:load_prg — load a PRG into memory (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).
        """
        self._post_binary("/v1/runners:load_prg", data)

    def run_prg(self, data: bytes) -> None:
        """POST /v1/runners:run_prg — load and RUN a PRG (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).
        """
        self._post_binary("/v1/runners:run_prg", data)

    def run_crt(self, data: bytes) -> None:
        """POST /v1/runners:run_crt — start a cartridge image (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).
        """
        self._post_binary("/v1/runners:run_crt", data)

    def sid_play(self, data: bytes, songnr: int = 0) -> None:
        """POST /v1/runners:sidplay — play a .sid tune (DESTRUCTIVE).

        Firmware 3.14 exposes this as POST to ``sidplay`` (no underscore);
        the PUT/``sid_play`` form returns HTTP 404.
        """
        if not isinstance(songnr, int) or songnr < 0:
            raise ValueError(f"songnr must be >= 0, got {songnr}")
        self._post_binary("/v1/runners:sidplay", data, query={"songnr": "%d" % songnr})

    def mod_play(self, data: bytes) -> None:
        """POST /v1/runners:modplay — play a .mod file (DESTRUCTIVE).

        Firmware 3.14 exposes this as POST to ``modplay`` (no underscore);
        the PUT/``mod_play`` form returns HTTP 404.
        """
        self._post_binary("/v1/runners:modplay", data)

    def _put_binary(
        self,
        path: str,
        data: bytes,
        query: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        self._request(
            "PUT",
            path,
            body=bytes(data),
            content_type="application/octet-stream",
            query=query,
        )

    def _post_binary(
        self,
        path: str,
        data: bytes,
        query: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        self._request(
            "POST",
            path,
            body=bytes(data),
            content_type="application/octet-stream",
            query=query,
        )

    # ----------------------------------------------------------------- drives
    def mount_disk(
        self,
        drive: str,
        image: bytes,
        image_type: str,
        mode: str = "readwrite",
    ) -> None:
        """PUT /v1/drives/<drive>:mount — mount a disk image (DESTRUCTIVE).

        `drive` is the slot id (e.g. "a", "b"). The trailing colon used by
        the drives endpoint is added automatically. `image_type` is e.g.
        "d64", "d71", "d81", "g64". `mode` is "readwrite", "readonly",
        or "unlinked".
        """
        if not isinstance(drive, str) or not drive:
            raise ValueError("drive must be a non-empty string")
        if not isinstance(image, (bytes, bytearray)):
            raise TypeError("image must be bytes")
        if not isinstance(image_type, str) or not image_type:
            raise ValueError("image_type must be a non-empty string")
        if mode not in ("readwrite", "readonly", "unlinked"):
            raise ValueError(f"mode must be readwrite/readonly/unlinked, got {mode!r}")

        # Normalise "a" -> "a:" — URL-encode the full segment including colon.
        slot = drive if drive.endswith(":") else drive + ":"
        path = f"/v1/drives/{_encode(slot)}:mount"

        boundary = "----U64ClientBoundary" + uuid.uuid4().hex
        body = _build_multipart(
            boundary,
            fields={"mode": mode, "type": image_type},
            file_field="file",
            file_name=f"image.{image_type}",
            file_bytes=bytes(image),
        )
        self._request(
            "PUT",
            path,
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def unmount_disk(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:unmount — unmount a drive (DESTRUCTIVE)."""
        if not isinstance(drive, str) or not drive:
            raise ValueError("drive must be a non-empty string")
        slot = drive if drive.endswith(":") else drive + ":"
        self._put_no_body(f"/v1/drives/{_encode(slot)}:unmount")

    # -------------------------------------------------------------- config write
    def set_config_item(self, category: str, item: str, value: Any) -> None:
        """PUT /v1/configs/<category>/<item>?value=<value> — set a single
        config item (DESTRUCTIVE).

        The device expects the new value as a ``value=`` query
        parameter, not as a JSON body.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(item, str) or not item:
            raise ValueError("item must be a non-empty string")
        path = f"/v1/configs/{_encode(category)}/{_encode(item)}"
        self._put_no_body(path, query={"value": value})

    def set_config_items(self, category: str, updates: dict) -> None:
        """Set multiple config items in *category* (DESTRUCTIVE).

        Issues one PUT per item because the device firmware does not
        accept a JSON-object batch body on ``PUT /v1/configs/<category>``
        (it returns HTTP 400 ``"Function none requires parameter value"``).
        Items are applied in dict insertion order; on failure, earlier
        writes are left in place.

        `updates` is a mapping of item name -> new value.
        """
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict")
        for item, value in updates.items():
            self.set_config_item(category, item, value)

    def save_config_to_flash(self) -> None:
        """PUT /v1/configs:save_to_flash — persist config to flash (DESTRUCTIVE)."""
        self._put_no_body("/v1/configs:save_to_flash")

    def load_config_from_flash(self) -> None:
        """PUT /v1/configs:load_from_flash — reload config from flash (DESTRUCTIVE)."""
        self._put_no_body("/v1/configs:load_from_flash")

    def reset_config_to_default(self) -> None:
        """PUT /v1/configs:reset_to_default — reset all config (DESTRUCTIVE)."""
        self._put_no_body("/v1/configs:reset_to_default")


def _build_multipart(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
) -> bytes:
    """Build an RFC 2388 multipart/form-data body.

    Order: simple fields first, file last. Line endings are CRLF.
    """
    crlf = b"\r\n"
    out = bytearray()
    b = boundary.encode("ascii")
    for name, value in fields.items():
        out += b"--" + b + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode("utf-8") + crlf
        out += crlf
        out += value.encode("utf-8") + crlf
    out += b"--" + b + crlf
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"'
        .encode("utf-8")
        + crlf
    )
    out += b"Content-Type: application/octet-stream" + crlf
    out += crlf
    out += file_bytes + crlf
    out += b"--" + b + b"--" + crlf
    return bytes(out)
