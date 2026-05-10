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
    "Ultimate64UnsafeOperationError",
    "Ultimate64UnreachableError",
    "Ultimate64RunnerStuckError",
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


class Ultimate64UnsafeOperationError(Ultimate64Error):
    """Raised when a destructive call needs an explicit caller-confirmation
    kwarg and didn't get one.

    Reserved for operations whose effect cannot be undone via the network
    API -- specifically ``poweroff``, which leaves the device unreachable
    until someone physically power-cycles it.  ``reset`` and ``reboot``
    are also DESTRUCTIVE but recoverable over the wire (~8s for reboot,
    instant for reset), so they don't require this gate.
    """


class Ultimate64UnreachableError(Ultimate64Error):
    """Raised when the device is unreachable after recovery attempts.

    Used by ``ultimate64_helpers.recover()`` when both the soft reset and
    (optionally) the full reboot escalations failed to bring the device
    back to a probe-reachable state. Caller is expected to escalate to a
    human / physical power-cycle -- the network API has no further
    recovery primitive (``poweroff`` is irrecoverable, not a recovery).
    """


class Ultimate64RunnerStuckError(Ultimate64Error):
    """Raised when the firmware's runner subsystem is wedged.

    Signature: ``run_prg`` (or similar runner endpoint) returns the
    "Cannot open file" error from the device even though the device is
    otherwise reachable (HTTP works, ``/v1/version`` responds). The
    runner state machine is stuck and refuses new programs.

    ``recover()`` (which issues a soft reset and optionally a reboot)
    typically clears this. Do NOT call ``poweroff()`` -- that's
    irrecoverable over the network.
    """


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
        *,
        write_mem_query_threshold: int | None = None,
    ) -> None:
        """Construct an Ultimate64 REST client.

        :param host: device hostname or IP.
        :param password: ``X-Password`` header value (optional).
        :param port: HTTP port (default 80).
        :param timeout: per-request socket timeout in seconds.
        :param write_mem_query_threshold: payload-size cutoff (in bytes)
            at which :meth:`write_mem` switches from the legacy
            ``PUT ?data=<hex>`` form to the ``POST`` raw-byte form. If
            ``None`` (the default), the threshold is auto-detected from
            the device's firmware version on first construction:

            * fw ``3.14*`` (incl. 3.14d) → **128**. The 48..127 range over
              the POST path occasionally wedges the runner on this fw,
              so the higher threshold pushes everything below 128 onto
              the reliable PUT-with-hex path.
            * any other / unknown fw → **48** (conservative legacy default).

            If the firmware probe (``get_info()``) fails for any reason,
            the threshold falls back to 48 silently — construction never
            raises on probe failure.
        """
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

        if write_mem_query_threshold is not None:
            self.write_mem_query_threshold = int(write_mem_query_threshold)
        else:
            self.write_mem_query_threshold = self._autodetect_write_mem_threshold()

    def close(self) -> None:
        """No-op — the client is stateless (uses a fresh connection per call)."""
        return None

    #: Bounded timeout (seconds) for the construct-time firmware probe
    #: used by :meth:`_autodetect_write_mem_threshold`. Decoupled from
    #: the per-request ``timeout`` so an unreachable host doesn't stall
    #: ``__init__`` for the full default.
    _AUTODETECT_PROBE_TIMEOUT: float = 0.5

    def _autodetect_write_mem_threshold(self) -> int:
        original = self.timeout
        self.timeout = min(self._AUTODETECT_PROBE_TIMEOUT, original)
        try:
            info = self.get_info()
        except Exception:
            return self.WRITE_MEM_QUERY_THRESHOLD
        finally:
            self.timeout = original
        if not isinstance(info, dict):
            return self.WRITE_MEM_QUERY_THRESHOLD
        fw = info.get("firmware_version")
        if not isinstance(fw, str):
            return self.WRITE_MEM_QUERY_THRESHOLD
        normalised = fw.lstrip("Vv").lower()
        if normalised.startswith("3.14"):
            return 128
        return self.WRITE_MEM_QUERY_THRESHOLD

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

    def poweroff(self, *, confirm_irrecoverable: bool = False) -> None:
        """PUT /v1/machine:poweroff — power off the C64 side (DESTRUCTIVE).

        UNSAFE WITHOUT PHYSICAL ACCESS.  After this call the device drops
        off the network entirely (no ICMP, no TCP, no HTTP) and only a
        manual power-cycle restores it.  Multiple agents have called this
        thinking it was a benign reset and then mis-diagnosed the
        unreachable state as a "hung device" -- producing wasted
        troubleshooting cycles each time.

        For "the device looks stuck, recover it" scenarios, prefer:
            * ``reset()``   — soft 6510 reset, instant
            * ``reboot()``  — full FPGA reinit, ~8s, recovers REU/DMA state

        Pass ``confirm_irrecoverable=True`` only if you (a) intend to
        leave the device off and (b) have physical access to power-cycle
        it later.  Without that explicit confirmation, this method
        raises ``Ultimate64UnsafeOperationError`` rather than firing the
        request.
        """
        if not confirm_irrecoverable:
            raise Ultimate64UnsafeOperationError(
                "Ultimate64Client.poweroff() requires "
                "confirm_irrecoverable=True. After poweroff, the device "
                "is unreachable until someone physically power-cycles "
                "it -- use reboot() (~8s) or reset() (instant) for "
                "recovery scenarios."
            )
        self._put_no_body("/v1/machine:poweroff")

    def menu_button(self) -> None:
        """PUT /v1/machine:menu_button — press the Ultimate menu button (DESTRUCTIVE)."""
        self._put_no_body("/v1/machine:menu_button")

    # ------------------------------------------------------------ streams
    def stream_audio_start(self, destination: str) -> None:
        """PUT /v1/streams/audio:start — start streaming audio to *destination*.

        *destination* is an IP address or hostname, optionally with ``:port``
        suffix.  The device sends 16-bit stereo PCM at ~48 kHz over UDP.
        Default multicast destination is ``239.0.1.65:11001``.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty string")
        self._put_no_body("/v1/streams/audio:start", query={"ip": destination})

    def stream_audio_stop(self) -> None:
        """PUT /v1/streams/audio:stop — stop the audio stream."""
        self._put_no_body("/v1/streams/audio:stop")

    def stream_video_start(self, destination: str) -> None:
        """PUT /v1/streams/video:start — start streaming video to *destination*.

        *destination* is an IP address or hostname, optionally with ``:port``
        suffix.  The device sends video frames over UDP to the given address.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty string")
        self._put_no_body("/v1/streams/video:start", query={"ip": destination})

    def stream_video_stop(self) -> None:
        """PUT /v1/streams/video:stop — stop the video stream."""
        self._put_no_body("/v1/streams/video:stop")

    def stream_debug_start(self, destination: str) -> None:
        """PUT /v1/streams/debug:start — start streaming debug data to *destination*.

        *destination* is an IP address or hostname, optionally with ``:port``
        suffix.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty string")
        self._put_no_body("/v1/streams/debug:start", query={"ip": destination})

    def stream_debug_stop(self) -> None:
        """PUT /v1/streams/debug:stop — stop the debug stream."""
        self._put_no_body("/v1/streams/debug:stop")

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

    #: Class-level fallback for the raw-byte threshold above which
    #: :meth:`write_mem` switches from the legacy ``PUT ?data=<hex>`` form
    #: to the ``POST`` raw-byte form. Per-instance ``write_mem_query_threshold``
    #: (set in ``__init__``) takes precedence; this attribute is retained
    #: for backwards compatibility with callers that poke the class.
    WRITE_MEM_QUERY_THRESHOLD: int = 48

    def write_mem(self, address: int, data: bytes) -> None:
        """Write bytes to C64 memory via DMA (DESTRUCTIVE).

        Uses one of two wire forms depending on payload size:

        * **Small payloads**
          (``len(data) <= self.write_mem_query_threshold``, default 48
          bytes; auto-bumped to 128 on firmware 3.14*) —
          ``PUT /v1/machine:writemem?address=0xNNNN&data=<hex>``.  Kept
          for backwards compatibility with existing callers/mocks.
        * **Large payloads** — ``POST /v1/machine:writemem?address=0xNNNN``
          with the raw bytes as the request body
          (``Content-Type: application/octet-stream``). Required for
          anything past the device's 128-hex-char cap on the ``data=``
          query param (firmware 3.14 responds
          ``"Maximum length of 128 bytes exceeded. Consider using POST
          method with attachment."``).

        Both forms are functionally equivalent for supported sizes; the
        POST form has no upper bound verified at 2048 bytes.
        """
        if not isinstance(address, int) or address < 0 or address > 0xFFFF:
            raise ValueError(f"address out of range 0..0xFFFF: {address}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        if not data:
            return
        payload = bytes(data)
        if len(payload) <= self.write_mem_query_threshold:
            query = {
                "address": "0x%04X" % address,
                "data": payload.hex().upper(),
            }
            self._request("PUT", "/v1/machine:writemem", query=query)
        else:
            # POST with raw-byte attachment — no data= query, body carries payload.
            self._request(
                "POST",
                "/v1/machine:writemem",
                body=payload,
                content_type="application/octet-stream",
                query={"address": "0x%04X" % address},
            )

    # ------------------------------------------------------------ keyboard
    #: KERNAL keyboard buffer base address ($0277).
    KEYBUF_ADDR: int = 0x0277
    #: KERNAL keyboard buffer fill-count byte ($00C6).
    KEYBUF_COUNT_ADDR: int = 0x00C6
    #: C64 keyboard buffer hardware capacity (10 bytes).
    KEYBUF_MAX: int = 10

    def send_text(self, text: str, *, finish_with_return: bool = True) -> None:
        """Inject *text* as a sequence of keystrokes into the C64 keyboard buffer.

        Convenience wrapper that PETSCII-encodes *text* and writes the
        bytes into the KERNAL keyboard buffer at ``$0277`` (with the
        fill-count byte at ``$00C6``).  When *finish_with_return* is
        True (the default), a trailing CR (PETSCII ``0x0D``) is
        appended — the canonical pattern for triggering a BASIC command
        such as ``"SYS 864"`` after :meth:`run_prg` lands at READY.

        Two trigger patterns for U64 PRGs:

        1. **Hijack an existing parking JMP** in the PRG — no
           ``send_text`` needed; ``run_prg`` alone fires the entry.
        2. **Type a SYS** — call ``send_text("SYS <trampoline>")`` after
           ``run_prg`` returns BASIC to READY.  This is the canonical
           shape for library-only PRGs that have no native main loop.

        For longer strings the buffer's 10-byte hardware limit is
        respected: the call polls ``$00C6`` and waits for the KERNAL
        scan loop to drain the buffer before pushing the next chunk.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        # Local import keeps the encoding module out of the import-time graph.
        from ..encoding.petscii import char_to_petscii

        codes = [char_to_petscii(ch) for ch in text]
        if finish_with_return:
            codes.append(0x0D)
        if not codes:
            return

        remaining = list(codes)
        max_iters = len(remaining) * 4 + 16
        iters = 0
        while remaining:
            iters += 1
            if iters > max_iters:
                raise Ultimate64Error(
                    "send_text: keyboard buffer never drained "
                    f"(still {len(remaining)} keys pending)"
                )
            count_byte = self.read_mem(self.KEYBUF_COUNT_ADDR, 1)
            current = count_byte[0] if count_byte else 0
            free = self.KEYBUF_MAX - current
            if free <= 0:
                continue
            chunk = remaining[:free]
            remaining = remaining[free:]
            self.write_mem(self.KEYBUF_ADDR + current, bytes(chunk))
            self.write_mem(self.KEYBUF_COUNT_ADDR, bytes([current + len(chunk)]))

    # ------------------------------------------------------------ code runners
    def load_prg(self, data: bytes) -> None:
        """POST /v1/runners:load_prg — load a PRG into memory (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).
        """
        self._post_binary("/v1/runners:load_prg", data)

    def run_prg(self, data: bytes, *, fallback_on_404: bool = True) -> None:
        """POST /v1/runners:run_prg — load and RUN a PRG (DESTRUCTIVE).

        Firmware 3.14 requires POST (PUT returns 400).

        Failure modes:

        * **Normal failure** (HTTP 4xx with informative body) — raised
          as ``Ultimate64Error`` with the device's error message in
          ``.body``. Caller can retry, fix the PRG, etc.
        * **Stuck-runner state** — device returns the firmware's
          ``"Cannot open file"`` signature (either as the body of a 4xx
          or in a JSON ``errors`` array). The device is alive (HTTP and
          ``/v1/version`` still work) but its runner state machine is
          wedged and refuses new programs. Detect via
          ``ultimate64_helpers.runner_health_check()``; clear via
          ``ultimate64_helpers.recover()`` (soft reset, escalates to
          ``reboot()`` if needed).
        * **HTTP 404 on fw 3.14d** — after certain non-PRG load
          sequences the runner endpoint starts returning 404 until the
          device is rebooted.  When ``fallback_on_404=True`` (the
          default), the call transparently retries by sideloading the
          PRG body via :meth:`write_mem` (using the load address from
          the PRG's first two header bytes, little-endian) and then
          triggering with :meth:`send_text` (``"SYS <addr>\\r"``).  A
          ``logging.warning`` is emitted when the fallback fires.  Pass
          ``fallback_on_404=False`` to surface the 404 as a plain
          :class:`Ultimate64Error`.
        * **Device unreachable** — raised as ``Ultimate64TimeoutError``.
          Use ``recover()`` first; if that raises
          ``Ultimate64UnreachableError`` the device needs a physical
          power-cycle.

        Do NOT call ``poweroff()`` to clear a stuck runner -- it leaves
        the device unreachable until someone physically power-cycles it.
        ``reboot()`` (via ``recover()``) is the correct escalation.
        """
        try:
            self._post_binary("/v1/runners:run_prg", data)
        except Ultimate64Error as exc:
            if not (fallback_on_404 and exc.status == 404):
                raise
            if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
                raise
            load_addr = data[0] | (data[1] << 8)
            body = bytes(data[2:])
            _log.warning(
                "run_prg got HTTP 404 from /v1/runners:run_prg; "
                "falling back to writemem+SYS sideload at $%04X "
                "(fw 3.14d wedged-runner workaround)",
                load_addr,
            )
            if body:
                self.write_mem(load_addr, body)
            self.send_text(f"SYS {load_addr}", finish_with_return=True)

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

    @staticmethod
    def _drive_slot_path(drive: str, action: str) -> str:
        if drive not in ("a", "b"):
            raise ValueError(f"drive must be 'a' or 'b', got {drive!r}")
        slot = drive + ":"
        return f"/v1/drives/{_encode(slot)}:{action}"

    def drive_on(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:on — power on a drive slot (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "on"))

    def drive_off(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:off — power off a drive slot (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "off"))

    def drive_reset(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:reset — reset a drive slot (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "reset"))

    def drive_remove_disk(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:remove — remove the disk from a drive (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "remove"))

    def drive_unlink(self, drive: str) -> None:
        """PUT /v1/drives/<drive>:unlink — unlink the mounted image (DESTRUCTIVE)."""
        self._put_no_body(self._drive_slot_path(drive, "unlink"))

    def drive_set_mode(self, drive: str, mode: str) -> None:
        """PUT /v1/drives/<drive>:set_mode?mode=<mode> — set drive mode (DESTRUCTIVE).

        `mode` is one of "1541", "1571", "1581".
        """
        if mode not in ("1541", "1571", "1581"):
            raise ValueError(f"mode must be '1541', '1571', or '1581', got {mode!r}")
        self._put_no_body(self._drive_slot_path(drive, "set_mode"), query={"mode": mode})

    def drive_load_rom(self, drive: str, rom_path_or_data: bytes | bytearray | str) -> None:
        """Load a custom ROM into a drive slot (DESTRUCTIVE).

        If *rom_path_or_data* is a ``bytes``-like object, the ROM is uploaded
        as a multipart body via PUT /v1/drives/<drive>:load_rom (mirrors the
        ``mount_disk`` shape).  If it is a ``str``, it is treated as a
        filename on the device's filesystem and passed via PUT
        /v1/drives/<drive>:load_rom?file=<path>.
        """
        path = self._drive_slot_path(drive, "load_rom")
        if isinstance(rom_path_or_data, (bytes, bytearray)):
            boundary = "----U64ClientBoundary" + uuid.uuid4().hex
            body = _build_multipart(
                boundary,
                fields={},
                file_field="file",
                file_name="drive.rom",
                file_bytes=bytes(rom_path_or_data),
            )
            self._request(
                "PUT",
                path,
                body=body,
                content_type=f"multipart/form-data; boundary={boundary}",
            )
        elif isinstance(rom_path_or_data, str):
            if not rom_path_or_data:
                raise ValueError("rom_path_or_data must be a non-empty string")
            self._put_no_body(path, query={"file": rom_path_or_data})
        else:
            raise TypeError("rom_path_or_data must be bytes or str")

    # ----------------------------------------------------------------- files
    def file_info(self, path: str) -> dict:
        """GET /v1/files/<path>:info — return size/extension info for a file.

        Path segments are URL-encoded individually so embedded slashes
        survive as path delimiters.
        """
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        encoded = "/".join(_encode(seg) for seg in path.lstrip("/").split("/"))
        return self._get_json(f"/v1/files/{encoded}:info")

    def _files_create_path(self, path: str, action: str) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        encoded = "/".join(_encode(seg) for seg in path.lstrip("/").split("/"))
        return f"/v1/files/{encoded}:{action}"

    def create_d64(self, path: str, tracks: int = 35, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_d64 — create an empty .d64 image (DESTRUCTIVE).

        `tracks` must be 35 or 40.
        """
        if tracks not in (35, 40):
            raise ValueError(f"tracks must be 35 or 40, got {tracks!r}")
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_d64"),
            query={"tracks": tracks, "diskname": diskname},
        )

    def create_d71(self, path: str, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_d71 — create an empty .d71 image (DESTRUCTIVE)."""
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_d71"),
            query={"diskname": diskname},
        )

    def create_d81(self, path: str, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_d81 — create an empty .d81 image (DESTRUCTIVE)."""
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_d81"),
            query={"diskname": diskname},
        )

    def create_dnp(self, path: str, tracks: int = 1, diskname: str = "") -> None:
        """PUT /v1/files/<path>:create_dnp — create an empty .dnp image (DESTRUCTIVE).

        `tracks` must be 1..255 inclusive.
        """
        if not isinstance(tracks, int) or tracks < 1 or tracks > 255:
            raise ValueError(f"tracks must be 1..255, got {tracks!r}")
        if not isinstance(diskname, str):
            raise TypeError("diskname must be a string")
        self._put_no_body(
            self._files_create_path(path, "create_dnp"),
            query={"tracks": tracks, "diskname": diskname},
        )

    # ------------------------------------------------------------ debug / measure
    def get_debug_register(self) -> int:
        """GET /v1/machine:debugreg — return the byte at $D7FF."""
        _, data = self._request("GET", "/v1/machine:debugreg")
        text = data.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "value" in payload:
            return int(payload["value"], 0) if isinstance(payload["value"], str) else int(payload["value"])
        if isinstance(payload, int):
            return payload
        if text:
            return int(text, 0)
        raise Ultimate64ProtocolError("empty debugreg response")

    def set_debug_register(self, value: int) -> None:
        """PUT /v1/machine:debugreg?value=<value> — write the byte at $D7FF (DESTRUCTIVE).

        `value` must be 0..255 inclusive.
        """
        if not isinstance(value, int) or value < 0 or value > 255:
            raise ValueError(f"value must be 0..255, got {value!r}")
        self._put_no_body("/v1/machine:debugreg", query={"value": value})

    def measure_bus_timing(self) -> bytes:
        """GET /v1/machine:measure — return raw VCD bytes from a bus-timing capture."""
        _, data = self._request("GET", "/v1/machine:measure")
        return data

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

    def set_config_items_batch(self, updates: dict[str, dict[str, Any]]) -> None:
        """POST /v1/configs — apply many config items in a single request (DESTRUCTIVE).

        `updates` is a mapping of category -> {item: value, ...}.  Sends a
        single JSON body to the device, so all writes are atomic from the
        caller's perspective.  Use :meth:`set_config_items` for the
        per-item PUT fan-out form.
        """
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict")
        for category, items in updates.items():
            if not isinstance(category, str) or not category:
                raise ValueError("category keys must be non-empty strings")
            if not isinstance(items, dict):
                raise TypeError(f"updates[{category!r}] must be a dict")
        body = json.dumps(updates).encode("utf-8")
        self._request("POST", "/v1/configs", body=body, content_type="application/json")

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
