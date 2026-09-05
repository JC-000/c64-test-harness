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

#: Worst-case SocketDMA drain rate used to scale the IDENTIFY
#: completion-barrier recv timeout with payload size.  Same figure as
#: ``u64_socket_dma._REU_DRAIN_FLOOR_BPS`` (live-measured on C64U fw
#: 1.1.0: bursts drain as slowly as ~5 KiB/s under firmware load; 4 KiB/s
#: adds margin).  This is a timeout ceiling, not a wait — the barrier
#: returns as soon as the IDENTIFY reply arrives.
_SOCKET_DMA_DRAIN_FLOOR_BPS = 4096.0

#: The 6510's I/O window.  A DMAWRITE that touches it is a register write
#: with side effects, so the fast path never re-sends such a span after a
#: failure (issue #223 review).
_IO_WINDOW_START = 0xD000
_IO_WINDOW_END = 0xDFFF


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

    def _socket_dma_barrier(self, client: SocketDMAClient, payload_len: int) -> None:
        """In-band ``IDENTIFY`` completion barrier for fire-and-forget sends.

        The firmware services SocketDMA commands on a connection strictly
        in order, so once the ``IDENTIFY`` reply arrives every preceding
        ``DMAWRITE`` has been consumed and applied — the same barrier
        :meth:`SocketDMAClient.reu_write` uses for ``REUWRITE``.  The recv
        timeout is scaled by *payload_len* at the worst-observed drain
        rate (:data:`_SOCKET_DMA_DRAIN_FLOOR_BPS`) because drain time is
        erratic under firmware load; the barrier returns as soon as the
        reply arrives.

        :raises Ultimate64Error: when the barrier send/recv fails — the
            preceding writes may not have been applied.
        """
        # The timeout scaling pokes the client's socket the same way
        # reu_write does internally; guarded getattr keeps test fakes
        # (no ``_sock``) working with the plain client timeout.
        sock = getattr(client, "_sock", None)
        base_timeout = getattr(client, "_timeout", 5.0)
        if sock is not None:
            sock.settimeout(
                base_timeout + payload_len / _SOCKET_DMA_DRAIN_FLOOR_BPS
            )
        try:
            client.identify()
        finally:
            # The client closes its socket when the peer has closed the
            # connection (issue #223), so the object we stretched may be
            # dead by now; restoring the timeout on it is then a no-op,
            # not an error to leak past the Ultimate64Error handling.
            if sock is not None:
                try:
                    sock.settimeout(base_timeout)
                except OSError:
                    pass

    def _socket_dma_send_and_barrier(
        self, client: SocketDMAClient, addr: int, data: bytes
    ) -> None:
        """Send *data* in DMAWRITE chunks, then run the IDENTIFY barrier.

        :raises Ultimate64Error: on a send failure or a barrier failure;
            the message names the phase.
        """
        try:
            for offset in range(0, len(data), _SOCKET_DMA_CHUNK):
                chunk = data[offset:offset + _SOCKET_DMA_CHUNK]
                client.dma_write(addr + offset, chunk)
        except Ultimate64Error as exc:
            raise Ultimate64Error(f"send failed: {exc}") from exc
        # DMAWRITE is fire-and-forget with no per-command ack, so finish with
        # the in-band IDENTIFY completion barrier (FIFO ordering means the
        # reply proves every chunk above was consumed and applied).  A tail
        # read-back alone is NOT a completion barrier: if the tail happens to
        # match pre-existing RAM (zero padding, re-writing the same buffer,
        # snapshot restores whose last bytes rarely change) it reports success
        # while the bulk DMA is still in flight, which can then clobber
        # subsequent REST writes (e.g. restore_snapshot's $0000/$0001
        # CPU-port writes).
        try:
            self._socket_dma_barrier(client, len(data))
        except Ultimate64Error as exc:
            raise Ultimate64Error(
                f"completion barrier (IDENTIFY) failed: {exc}"
            ) from exc

    def _socket_dma_write(self, addr: int, data: bytes) -> bool:
        """Attempt the SocketDMA fast path for one write.

        Returns ``True`` when the payload was sent, the in-band
        ``IDENTIFY`` completion barrier confirmed the device consumed
        every chunk, and the tail verified via the REST read path;
        ``False`` (with a WARNING logged) when the caller should fall
        back to the REST ``write_mem`` for this write.  A connect
        failure additionally latches the fast path off for this
        transport's lifetime; send failures, barrier failures and verify
        mismatches do not latch.

        A send or barrier failure on a **reused** connection is retried
        once on a fresh connection before falling back (issue #223).
        Firmware from fdb521a5 on (v3.15 and the U64E's fork; v3.14d and
        the C64U's 1.1.0 have no socket timeout) closes a SocketDMA
        connection idle for >1 s, and a DMAWRITE written into that closed
        socket is never read, so the barrier fails with "closed by peer"
        and the DMA was not applied (U64E fw 3.15, 2026-09-05: 50/50
        barrier failures at a 1.5 s inter-write gap, 0/50 at 0.2 s, idle
        and under REST load alike; the failed write's bytes were in RAM
        0/50 times).  The client's own idle reconnect normally reopens
        the socket in time; the retry covers a connection the device
        dropped anyway.  Re-sending is safe for RAM: the same bytes go to
        the same address, and the earlier DMA either never happened or
        wrote a prefix of them.  It is **not** safe for the I/O window --
        a second DMA into ``$D000-$DFFF`` is a second register write with
        side effects -- so a span touching that window is never re-sent:
        its first failure goes straight to the REST fallback.
        """
        client = self._ensure_socket_dma_client()
        touches_io = addr <= _IO_WINDOW_END and addr + len(data) - 1 >= _IO_WINDOW_START

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

        for attempt in (1, 2):
            try:
                self._socket_dma_send_and_barrier(client, addr, data)
                break
            except Ultimate64Error as exc:
                # A failed barrier can leave its reply unread on the wire
                # and a failed send leaves the stream mid-command; drop the
                # connection so the next attempt starts clean.
                client.close()
                if attempt == 1 and touches_io:
                    _log.warning(
                        "SocketDMA write at %#06x: %s; the span touches the "
                        "I/O window $D000-$DFFF so it is not re-sent over "
                        "DMA; falling back to REST write_mem for this write",
                        addr,
                        exc,
                    )
                    return False
                if attempt == 1:
                    _log.warning(
                        "SocketDMA write at %#06x: %s; retrying once on a "
                        "fresh connection",
                        addr,
                        exc,
                    )
                    try:
                        client.__enter__()
                    except Ultimate64Error as exc2:
                        _log.warning(
                            "SocketDMA reconnect to %s:64 failed (%s); "
                            "falling back to REST write_mem for this write",
                            self._client.host,
                            exc2,
                        )
                        return False
                    continue
                _log.warning(
                    "SocketDMA write at %#06x: %s on the retry as well; "
                    "falling back to REST write_mem for this write",
                    addr,
                    exc,
                )
                return False

        # Post-barrier sanity check: the DMA has been applied, so the tail
        # must read back immediately; the short poll budget only covers REST
        # read latency jitter.
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

    def socket_dma_reu_write(self, offset: int, data: bytes) -> None:
        """Write *data* into REU expansion memory via SocketDMA ``REUWRITE``.

        Routes through the transport's managed (lazily-connected,
        teardown-closed) SocketDMA client, the same one the ``write_memory``
        fast path uses, so the TCP/64 connection is reused across calls.
        Payloads above the per-command ceiling are chunked by
        :meth:`SocketDMAClient.reu_write` (~65 KiB per command, 24-bit
        offsets advancing automatically).

        Unlike the ``write_memory`` fast path this does **not** require the
        ``socket_dma`` master switch: there is no REST fallback for REU
        memory (the firmware has no REU write endpoint, and no read-back
        endpoint either), so an unusable SocketDMA service is a hard error,
        never a silent skip or degrade.  The transport's connect-failure
        latch is respected in both directions: a latched fast path fails
        immediately here, and a connect failure here latches the fast path
        off for the transport's lifetime.

        ``REUWRITE`` has no per-command ack, so the client finishes with
        an in-band ``IDENTIFY`` completion barrier — when this method
        returns, the firmware has applied every chunk (a read-back
        started immediately afterwards is safe).  REU contents still
        cannot be read back over REST; byte-fidelity verification, when
        needed, goes through the snapshot staging-window extract
        (:func:`c64_test_harness.snapshot.extract_reu_contents`).

        :raises Ultimate64Error: if SocketDMA is latched off, the connect
            fails (which also latches), or a send fails mid-transfer.
        """
        if not data:
            return
        unavailable_hint = (
            f"SocketDMA (TCP {self._client.host}:64) is required for REU "
            "writes and there is NO REST fallback. Enable 'Ultimate DMA "
            "Service' in the device's Network Settings and ensure TCP port "
            "64 is reachable, then retry."
        )
        if self._socket_dma_unusable:
            raise Ultimate64Error(
                "SocketDMA is latched off after an earlier connect failure; "
                f"cannot write REU memory. {unavailable_hint}"
            )
        client = self._ensure_socket_dma_client()
        try:
            client.__enter__()
        except Ultimate64Error as exc:
            self._socket_dma_unusable = True
            raise Ultimate64Error(
                f"SocketDMA connect failed ({exc}); cannot write REU memory. "
                f"{unavailable_hint}"
            ) from exc
        # Send failures propagate: there is nothing to fall back to.
        client.reu_write(offset, bytes(data))

    def read_screen_codes(self) -> list[int]:
        """Read raw screen codes (cols * rows values) from screen memory."""
        total = self._screen_cols * self._screen_rows
        raw = self.read_memory(self._screen_base, total)
        return list(raw)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        """Inject PETSCII codes into the KERNAL keyboard buffer at $0277.

        The C64 keyboard buffer is 10 bytes at $0277 with the pending-count
        byte at $00C6.  Each chunk waits for the buffer to fully drain
        (``$C6 == 0``), then writes up to ``keybuf_max`` codes at offset 0
        and sets the count.

        Topping up a *partially* drained buffer is a race: the KERNAL
        shifts the remaining buffer bytes down and decrements ``$C6``
        concurrently with our DMA writes, so a write placed at
        ``buf + count`` can land at a stale offset (dropping or
        duplicating keys).  Waiting for a fully drained buffer and always
        writing at offset 0 makes the write placement race-free (the same
        convention :meth:`Ultimate64Client.send_text` uses).

        Because U64 memory I/O is DMA-backed, no CPU pause is needed.
        """
        if not petscii_codes:
            return

        remaining = list(petscii_codes)
        # Safety bound: covers the drain polls between chunks (a full
        # 10-key buffer drains in ~166 ms at the KERNAL's ~60 Hz scan;
        # each poll below sleeps 20 ms when the buffer is non-empty).
        max_iters = len(remaining) * 4 + 100
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
            if current != 0:
                # KERNAL is still consuming keys — writing now would race
                # its shift-down of the buffer.  Wait for a full drain.
                time.sleep(0.02)
                continue

            chunk = remaining[:self._keybuf_max]
            remaining = remaining[len(chunk):]

            # Buffer is empty — write at offset 0, then set the count.
            self.write_memory(
                self._keybuf_addr,
                bytes(chunk),
            )
            self.write_memory(
                self._keybuf_count_addr,
                bytes([len(chunk)]),
            )

    def inject_joystick(self, port: int, value: int) -> None:
        """Inject joystick state on U64 by writing CIA1 ports via REST.

        SocketDMA (TCP/64) has no dedicated joystick opcode, and the REST API
        has no joystick endpoint.  The standard out-of-band technique is to
        DMA-write CIA1's data ports directly: ``$DC01`` is read as joystick
        port 1, ``$DC00`` as joystick port 2.

        ``value`` follows the protocol convention (:meth:`C64Transport.
        inject_joystick`): **active-high**, bit set = direction/button
        pressed, bits 0-4 = up/down/left/right/fire — the same convention
        VICE's ``JOYPORT_SET`` uses.  The CIA data ports are active-low at
        the hardware level, so this method inverts bits 0-4 before the
        write (bits 5-7 pass through verbatim).

        The write routes through :meth:`write_memory` with
        ``override="inject-joystick"`` so it stays visible to the
        transport's :class:`MemoryPolicy` (the override is logged at
        WARNING when a non-permissive policy is active) — CIA registers
        are I/O, not consumer RAM, so the policy is bypassed rather than
        consulted.

        Persistence caveat (differs from VICE): U64 writes are
        **one-shot**.  CIA1 holds the value only until the next keyboard
        scan (the KERNAL rewrites the ports at ~60 Hz), so for sustained
        input the caller must rewrite periodically or pause the C64
        first.  VICE's ``JOYPORT_SET`` holds the state until changed.
        """
        if port == 1:
            cia_addr = 0xDC01
        elif port == 2:
            cia_addr = 0xDC00
        else:
            raise ValueError(f"inject_joystick: port must be 1 or 2, got {port}")
        if not (0 <= value <= 0xFF):
            raise ValueError(f"inject_joystick: value {value:#x} out of byte range")
        # Protocol value is active-high; CIA lines are active-low with
        # pull-ups, so invert the five joystick bits.
        cia_value = value ^ 0x1F
        self.write_memory(
            cia_addr, bytes([cia_value & 0xFF]), override="inject-joystick"
        )

    def read_framebuffer(
        self,
        *,
        listen_port: int = DEFAULT_VIDEO_PORT,
        timeout: float = 2.0,
    ) -> dict:
        """Capture one VIC-II frame from the U64 video stream.

        Generation caveat (observed live 2026-07-28): a C64 Ultimate
        (firmware 1.1.0) answered the video-stream start with HTTP 500
        "No Operational Network Interface" when the capture host was on
        a routed subnet — this raises :class:`Ultimate64Error` there.
        Whether C64U streaming works with an on-subnet capture host is
        still unverified; on the U64 Elite (fw 3.14) the stream works.

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
        * ``multiplier=N`` where N is a supported CPU-Speed enum step
          (2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 24, 32, 40, 48, 64) —
          set Turbo Control to ``"Manual"`` at that MHz.  The enum is
          the cross-generation superset: the U64 Elite (fw 3.14) lacks
          64, the C64 Ultimate (fw 1.1.0) lacks 5.  The device's actual
          preset list is probed once (cached) and a generation-foreign
          speed raises :class:`ValueError` locally; when the probe is
          inconclusive the firmware still rejects it with HTTP 400
          before turbo is enabled.
        * ``multiplier=None`` — max available speed as probed from the
          device's CPU-Speed presets (64 MHz on a C64 Ultimate, 48 MHz
          on a U64 Elite; falls back to 48 when the probe is
          inconclusive).

        :raises ValueError: integer is not one of the supported MHz
            steps, or not supported by this device generation.
        """
        from .ultimate64_helpers import max_cpu_speed_mhz, set_turbo_mhz
        if multiplier is None:
            set_turbo_mhz(self._client, max_cpu_speed_mhz(self._client))
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
