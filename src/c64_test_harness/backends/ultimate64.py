"""Ultimate 64 transport — C64Transport protocol over the REST API.

Wraps :class:`Ultimate64Client` so the Ultimate 64 hardware can be used
anywhere the test harness expects a :class:`C64Transport`.

Unlike :class:`BinaryViceTransport`, there is no breakpoint/register
protocol on the U64.  Methods that depend on CPU inspection —
:meth:`read_registers`, :meth:`set_registers`, checkpoint management,
:func:`jsr`, :func:`wait_for_pc`, :func:`set_breakpoint` — are all
unavailable on hardware.  Design tests to self-report results via
memory reads.
"""
from __future__ import annotations

from .hardware import HardwareTransportBase
from .ultimate64_client import Ultimate64Client


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

    @property
    def client(self) -> "Ultimate64Client":
        """Return the underlying Ultimate64Client for low-level operations not yet wrapped on the transport."""
        return self._client

    # ----- C64Transport protocol -----

    def read_memory(self, addr: int, length: int) -> bytes:
        """Read *length* bytes from C64 memory via DMA."""
        if length <= 0:
            return b""
        return self._client.read_mem(addr, length)

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        """Write *data* bytes to C64 memory via DMA."""
        if isinstance(data, list):
            data = bytes(data)
        if not data:
            return
        self._client.write_mem(addr, data)

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

    def read_registers(self) -> dict[str, int]:
        """Not supported on Ultimate 64 hardware.

        The REST API does not expose CPU registers.  :func:`jsr`,
        :func:`wait_for_pc`, and :func:`set_breakpoint` are therefore
        unavailable — design tests to self-report results via memory.
        """
        raise NotImplementedError(
            "Ultimate 64 REST API does not expose CPU registers. "
            "jsr(), wait_for_pc(), and set_breakpoint() are unavailable "
            "on hardware. Design tests to self-report results via memory."
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

    def read_framebuffer(self) -> dict:
        """Return raw framebuffer bytes plus geometry. Backend-specific layout — see backend docs."""
        raise NotImplementedError(
            "U64 hardware does not expose framebuffer reads directly via REST."
        )

    def read_palette(self) -> list[tuple[int, int, int]]:
        """Return the active VIC palette as RGB triples."""
        raise NotImplementedError(
            "U64 hardware does not expose the VIC palette directly via REST."
        )

    def resume(self) -> None:
        """Resume the emulated CPU (after an external pause)."""
        self._client.resume()

    def close(self) -> None:
        """Release client resources (no-op for the stateless REST client)."""
        self._client.close()
