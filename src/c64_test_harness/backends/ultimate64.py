"""Ultimate 64 transport — C64Transport protocol over the REST API.

Wraps :class:`Ultimate64Client` so the Ultimate 64 hardware can be used
anywhere the test harness expects a :class:`C64Transport`.

Unlike :class:`BinaryViceTransport`, there is no breakpoint/register
protocol on the U64.  CPU inspection methods (``read_registers``,
``set_registers``, checkpoint management, :func:`jsr`,
:func:`wait_for_pc`, :func:`set_breakpoint`) are VICE-only — design
U64 tests to self-report results via memory reads.  ``read_registers``
is intentionally **not** part of :class:`C64Transport`; consult
``BinaryViceTransport`` directly when you need it.
"""
from __future__ import annotations

import logging
import socket
import time

from .hardware import HardwareTransportBase
from .u64_socket_dma import SocketDMAClient
from .u64_video_capture import VIC_PALETTE, DEFAULT_VIDEO_PORT, VideoCapture
from .ultimate64_client import Ultimate64Client, Ultimate64Error

_log = logging.getLogger(__name__)

# DMAWRITE chunk ceiling: ``SocketDMAClient`` caps a single command payload at
# 0xFFFF bytes, but the 6510 address is 16-bit, so we also need every chunk's
# *start* address to stay <= 0xFFFF.  Splitting on 32 KiB boundaries satisfies
# both: a full 64 KiB restore at $0000 becomes exactly two chunks ($0000 and
# $8000), the second still a legal DMAWRITE address.
_SOCKET_DMA_CHUNK = 0x8000

#: How long write_memory polls the tail read-back before declaring a
#: SocketDMA verify mismatch.  The protocol is fire-and-forget: the device
#: applies the DMA asynchronously after consuming the TCP stream, so an
#: immediate single read races the transfer (observed live on C64U fw
#: 1.1.0: 16 KiB lands in ~100-150 ms).
_SOCKET_DMA_VERIFY_TIMEOUT = 2.0


class Ultimate64Transport(HardwareTransportBase):
    """C64Transport implementation backed by Ultimate 64 REST API.

    All memory I/O goes through the device's DMA-backed ``readmem``/
    ``writemem`` endpoints, so no CPU pause is required — reads and
    writes happen concurrently with normal execution.
    """

    def __init__(
        self,
        host: str,
        password: str | None = None,
        port: int = 80,
        timeout: float = 10.0,
        screen_base: int = 0x0400,
        keybuf_addr: int = 0x0277,
        keybuf_count_addr: int = 0x00C6,
        keybuf_max: int = 10,
        cols: int = 40,
        rows: int = 25,
        client: Ultimate64Client | None = None,
        memory_policy: "MemoryPolicy | None" = None,
        *,
        socket_dma: bool = False,
        socket_dma_min_bytes: int = 8192,
    ) -> None:
        super().__init__(screen_cols=cols, screen_rows=rows)
        if client is None:
            client = Ultimate64Client(
                host=host,
                password=password,
                port=port,
                timeout=timeout,
            )
        self._client = client
        self._screen_base = screen_base
        self._keybuf_addr = keybuf_addr
        self._keybuf_count_addr = keybuf_count_addr
        self._keybuf_max = keybuf_max

        # Opt-in SocketDMA (TCP/64) fast path for bulk writes.  ``socket_dma``
        # is the master switch; writes of >= ``socket_dma_min_bytes`` are
        # eligible.  Both are plain public attributes — flip them at any time.
        self.socket_dma: bool = bool(socket_dma)
        self.socket_dma_min_bytes: int = int(socket_dma_min_bytes)
        #: Tail-verify poll budget (seconds); see _SOCKET_DMA_VERIFY_TIMEOUT.
        self.socket_dma_verify_timeout: float = _SOCKET_DMA_VERIFY_TIMEOUT
        self._socket_dma_client: SocketDMAClient | None = None
        # Latched True after a connect failure so we stop paying the connect
        # timeout on every subsequent write (verify mismatches do NOT latch).
        self._socket_dma_unusable: bool = False

        from ..memory_policy import MemoryPolicy as _MemoryPolicy
        self._memory_policy: _MemoryPolicy = memory_policy or _MemoryPolicy.permissive()

    @property
    def client(self) -> "Ultimate64Client":
        """Return the underlying Ultimate64Client for low-level operations not yet wrapped on the transport."""
        return self._client

    @property
    def memory_policy(self) -> "MemoryPolicy":
        """Active :class:`MemoryPolicy` for this transport.

        Set this to enforce allow-list/deny-list checks on every
        :meth:`write_memory` call.  The default is permissive (every
        write passes).
        """
        return self._memory_policy

    @memory_policy.setter
    def memory_policy(self, policy: "MemoryPolicy") -> None:
        from ..memory_policy import MemoryPolicy as _MemoryPolicy
        if not isinstance(policy, _MemoryPolicy):
            raise TypeError(
                f"memory_policy must be a MemoryPolicy, got {type(policy).__name__}"
            )
        self._memory_policy = policy

    # ----- C64Transport protocol -----

    def read_memory(self, addr: int, length: int) -> bytes:
        """Read *length* bytes from C64 memory via DMA."""
        if length <= 0:
            return b""
        return self._client.read_mem(addr, length)

    def write_memory(
        self,
        addr: int,
        data: bytes | list[int],
        *,
        override: str | None = None,
    ) -> None:
        """Write *data* bytes to C64 memory via DMA.

        Routes through ``self.memory_policy.check_write`` before any byte
        crosses the wire — a violating write raises
        :class:`MemoryPolicyError`.  Pass ``override="<reason>"`` to
        bypass for a single call (logged at WARNING).  The default
        policy is permissive, so existing callers see no behaviour
        change.
        """
        if isinstance(data, list):
            data = bytes(data)
        if not data:
            return
        if not self._memory_policy.is_permissive():
            self._memory_policy.check_write(addr, len(data), override=override)
        # SocketDMA fast path is reachable only for policy-approved writes.
        if (
            self.socket_dma
            and not self._socket_dma_unusable
            and len(data) >= self.socket_dma_min_bytes
            and self._socket_dma_write(addr, bytes(data))
        ):
            return
        self._client.write_mem(addr, data)

    def _ensure_socket_dma_client(self) -> SocketDMAClient:
        """Return the lazily-created, connection-reusing SocketDMA client.

        Host and password are inherited from the REST client; the port is
        the SocketDMA default (TCP/64), independent of the REST port.
        """
        if self._socket_dma_client is None:
            self._socket_dma_client = SocketDMAClient(
                host=self._client.host,
                password=self._client.password,
            )
        return self._socket_dma_client

    def _socket_dma_write(self, addr: int, data: bytes) -> bool:
        """Attempt the SocketDMA fast path for one write.

        Returns ``True`` when the payload was sent and the tail verified via
        the REST read path; ``False`` (with a WARNING logged) when the caller
        should fall back to the REST ``write_mem`` for this write.  A connect
        failure additionally latches the fast path off for this transport's
        lifetime; send failures and verify mismatches do not latch.
        """
        client = self._ensure_socket_dma_client()

        # Establish (or reuse) the connection.  ``__enter__`` runs the
        # idempotent connect + optional authenticate; a no-op if already open.
        try:
            client.__enter__()
        except Ultimate64Error as exc:
            _log.warning(
                "SocketDMA connect to %s:64 failed (%s); latching fast path "
                "off and falling back to REST write_mem",
                self._client.host,
                exc,
            )
            self._socket_dma_unusable = True
            return False

        try:
            for offset in range(0, len(data), _SOCKET_DMA_CHUNK):
                chunk = data[offset:offset + _SOCKET_DMA_CHUNK]
                client.dma_write(addr + offset, chunk)
        except Ultimate64Error as exc:
            _log.warning(
                "SocketDMA send failed at %#06x (%s); falling back to REST "
                "write_mem for this write",
                addr,
                exc,
            )
            return False

        # Fire-and-forget protocol has no ack, so poll the tail back over REST
        # until it matches — the only confirmation the payload landed.  A
        # single immediate read races the device-side DMA application (the
        # TCP stream is consumed before the bytes hit C64 RAM).
        tail_len = min(16, len(data))
        expected = data[len(data) - tail_len:]
        deadline = time.monotonic() + self.socket_dma_verify_timeout
        while True:
            actual = self._client.read_mem(addr + len(data) - tail_len, tail_len)
            if actual == expected:
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        _log.warning(
            "SocketDMA verify mismatch at %#06x (wrote %d bytes; tail "
            "expected %r, read %r after %.1fs); falling back to REST "
            "write_mem for this write",
            addr,
            len(data),
            expected,
            actual,
            self.socket_dma_verify_timeout,
        )
        return False

    def read_screen_codes(self) -> list[int]:
        """Read raw screen codes (cols * rows values) from screen memory."""
        total = self._screen_cols * self._screen_rows
        raw = self.read_memory(self._screen_base, total)
        return list(raw)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        """Inject PETSCII codes into the KERNAL keyboard buffer at $0277.

        The C64 keyboard buffer is 10 bytes at $0277 with the pending-count
        byte at $00C6.  Each chunk writes up to ``(keybuf_max - current_count)``
        bytes, updates the count, and polls for the buffer to drain before
        writing the next chunk.

        Because U64 memory I/O is DMA-backed, no CPU pause is needed.
        """
        if not petscii_codes:
            return

        remaining = list(petscii_codes)
        # Safety bound: single iterations are bounded by keybuf_max.
        max_iters = len(remaining) * 4 + 16
        iters = 0
        while remaining:
            iters += 1
            if iters > max_iters:
                raise RuntimeError(
                    "inject_keys: keyboard buffer never drained "
                    f"(still {len(remaining)} keys pending)"
                )

            count_byte = self.read_memory(self._keybuf_count_addr, 1)
            current = count_byte[0] if count_byte else 0
            free = self._keybuf_max - current
            if free <= 0:
                # Buffer full — wait for KERNAL to consume keys.
                continue

            chunk = remaining[:free]
            remaining = remaining[free:]

            # Write into the buffer at (buf_addr + current), then update count.
            self.write_memory(
                self._keybuf_addr + current,
                bytes(chunk),
            )
            self.write_memory(
                self._keybuf_count_addr,
                bytes([current + len(chunk)]),
            )

    def inject_joystick(self, port: int, value: int) -> None:
        """Inject joystick state on U64 by writing CIA1 ports via REST.

        SocketDMA (TCP/64) has no dedicated joystick opcode, and the REST API
        has no joystick endpoint.  The standard out-of-band technique is to
        DMA-write CIA1's data ports directly: ``$DC01`` is read as joystick
        port 1, ``$DC00`` as joystick port 2.  Bits 0-4 are
        up/down/left/right/fire; the C64 joystick is **active-low** at the
        hardware level, but this method preserves the caller-supplied
        ``value`` byte verbatim — convert active-high/active-low conventions
        in the caller, not here.

        Note: writes are one-shot.  CIA1 will hold the value until the next
        keyboard scan (the KERNAL writes ``$DC00`` ~60 Hz), so for sustained
        input the caller must rewrite periodically or pause the C64 first.
        """
        if port == 1:
            cia_addr = 0xDC01
        elif port == 2:
            cia_addr = 0xDC00
        else:
            raise ValueError(f"inject_joystick: port must be 1 or 2, got {port}")
        if not (0 <= value <= 0xFF):
            raise ValueError(f"inject_joystick: value {value:#x} out of byte range")
        self._client.write_mem(cia_addr, bytes([value & 0xFF]))

    def read_framebuffer(
        self,
        *,
        listen_port: int = DEFAULT_VIDEO_PORT,
        timeout: float = 2.0,
    ) -> dict:
        """Capture one VIC-II frame from the U64 video stream.

        Returns a dict matching the :class:`BinaryViceTransport`
        ``read_framebuffer`` shape::

            {
                "debug_rect": (0, 0, W, H),       # full frame rect
                "inner_rect": (0, 0, W, H),       # U64 stream has no
                                                  # debug border, so inner
                                                  # == debug here
                "bpp": 8,                         # we unpack to 1 byte/px
                "palette": 0,                     # palette id (0 = VIC)
                "bytes": <pixels>,                # W*H bytes, colour
                                                  # indices 0-15
            }

        Implementation: starts the U64 video UDP stream, captures one
        complete frame, then stops the stream.  Latency is roughly one
        frame time (~20 ms on PAL) plus stream-start overhead — callers
        that need many frames should drive ``VideoCapture`` directly
        from :mod:`c64_test_harness.backends.u64_video_capture`.

        Raises ``TransportError`` if no complete frame arrives within
        ``timeout`` seconds (typically means the device cannot reach the
        host on UDP — a firewall, or wrong source IP).
        """
        import time

        from ..transport import TransportError

        # Discover the local IP the U64 can reach us on (same trick
        # render_wav_u64 uses — UDP connect picks the right interface
        # without sending traffic).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
            _s.connect((self._client.host, self._client.port))
            local_ip = _s.getsockname()[0]
        destination = f"{local_ip}:{listen_port}"

        capture = VideoCapture(port=listen_port)
        capture.start()
        stream_started = False
        try:
            self._client.stream_video_start(destination)
            stream_started = True

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if capture.frames_completed >= 1:
                    break
                time.sleep(0.01)
        finally:
            if stream_started:
                try:
                    self._client.stream_video_stop()
                except Exception:
                    pass
            result = capture.stop()

        if not result.frames:
            raise TransportError(
                f"read_framebuffer: no complete frame received from "
                f"{self._client.host} within {timeout}s "
                f"(packets_received={result.packets_received}, "
                f"frames_dropped={result.frames_dropped}). "
                f"Check that the device can reach {destination} on UDP."
            )

        frame = result.frames[0]
        return {
            "debug_rect": (0, 0, frame.width, frame.height),
            "inner_rect": (0, 0, frame.width, frame.height),
            "bpp": 8,
            "palette": 0,
            "bytes": frame.pixels,
        }

    def read_palette(self) -> list[tuple[int, int, int]]:
        """Return the active VIC palette as a list of 16 RGB triples.

        The U64 REST API does not expose the live palette, so this
        returns the canonical VIC-II palette
        (:data:`~c64_test_harness.backends.u64_video_capture.VIC_PALETTE`)
        — the same indices the U64 video stream uses to encode pixels.
        Matches the return shape of
        :meth:`BinaryViceTransport.read_palette`.
        """
        return [tuple(rgb) for rgb in VIC_PALETTE]

    def resume(self) -> None:
        """Resume the emulated CPU (after an external pause)."""
        self._client.resume()

    # ----- protocol: speed control ------------------------------------------

    def set_speed(self, multiplier: int | None) -> None:
        """Backend-agnostic CPU-speed control on Ultimate 64.

        Wraps :func:`ultimate64_helpers.set_turbo_mhz`:

        * ``multiplier=1`` — turbo off (1 MHz native).
        * ``multiplier=N`` where N is a supported U64 CPU-Speed enum
          (2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 24, 32, 40, 48) —
          set Turbo Control to ``"Manual"`` at that MHz.
        * ``multiplier=None`` — max available speed (48 MHz).

        :raises ValueError: integer is not one of the supported MHz steps.
        """
        from .ultimate64_helpers import set_turbo_mhz
        if multiplier is None:
            set_turbo_mhz(self._client, 48)
            return
        if multiplier == 1:
            set_turbo_mhz(self._client, None)
            return
        # set_turbo_mhz validates against the device enum and raises
        # ValueError for unsupported speeds.
        set_turbo_mhz(self._client, multiplier)

    def get_speed(self) -> int | None:
        """Return the current CPU-speed multiplier.

        Returns ``1`` when turbo is off (native 1 MHz), the integer MHz
        when turbo is active at a known step, or ``None`` if the device
        is in turbo mode but the underlying CPU-Speed enum is missing
        (treated the same as VICE warp: faster-than-native, exact rate
        unknown).
        """
        from .ultimate64_helpers import get_turbo_enabled, get_turbo_mhz
        if not get_turbo_enabled(self._client):
            return 1
        return get_turbo_mhz(self._client)

    # ----- protocol: reset --------------------------------------------------

    def reset(self, scope: str = "cpu", *, drive: str | int | None = None) -> None:
        """Reset the machine.  See :meth:`C64Transport.reset` for semantics.

        * ``scope="cpu"`` — :meth:`Ultimate64Client.reset` (soft 6510).
        * ``scope="machine"`` — :meth:`Ultimate64Client.reboot` (FPGA
          full reinit; ~8 s before the device is reachable again).
        * ``scope="drive"`` — :meth:`Ultimate64Client.drive_reset`;
          ``drive`` must be ``"a"``, ``"b"`` (or ``0`` / ``1``).
        """
        if scope == "cpu":
            self._client.reset()
            return
        if scope == "machine":
            self._client.reboot()
            return
        if scope == "drive":
            if drive is None:
                raise ValueError(
                    "reset(scope='drive') requires drive='a' or 'b'"
                )
            if isinstance(drive, bool):
                raise ValueError(
                    f"drive must be 'a'/'b' or 0/1, got bool {drive!r}"
                )
            if isinstance(drive, int):
                if drive == 0:
                    slot = "a"
                elif drive == 1:
                    slot = "b"
                else:
                    raise ValueError(
                        f"drive index must be 0 or 1 (slot a/b); got {drive}"
                    )
            elif isinstance(drive, str):
                slot = drive.lower()
                if slot not in ("a", "b"):
                    raise ValueError(
                        f"drive slot must be 'a' or 'b'; got {drive!r}"
                    )
            else:
                raise ValueError(
                    f"drive must be 'a'/'b' or 0/1; got {drive!r}"
                )
            self._client.drive_reset(slot)
            return
        raise ValueError(
            f"scope must be 'cpu', 'machine', or 'drive'; got {scope!r}"
        )

    def close(self) -> None:
        """Release client resources.

        The REST client is stateless, but the SocketDMA fast-path client (if
        one was ever created) holds an open TCP/64 socket that must be closed.
        """
        if self._socket_dma_client is not None:
            try:
                self._socket_dma_client.close()
            finally:
                self._socket_dma_client = None
        self._client.close()
