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


# SID I/O register window — 32 bytes at $D400-$D41F.  On real 6581/8580
# hardware (and the UltiSID emulations the U64 ships with), 28 of these
# 32 registers are write-only: reading $D400-$D418 and $D41D-$D41F
# returns open-bus garbage.  The only readable registers are $D419 (POTX),
# $D41A (POTY), $D41B (OSC3), and $D41C (ENV3).
#
# That breaks snapshot extraction from U64 hardware: we cannot ask the
# device "what's the current voice 1 frequency?" the way we can on VICE
# (where reads return last-written values).  The remedy is a host-side
# "shadow" copy maintained by Ultimate64Transport: every byte that lands
# in $D400-$D41F via write_memory() is recorded, and the snapshot
# extractor reads from this shadow instead of the wire when the
# transport exposes it.
_SID_BASE = 0xD400
_SID_LEN = 32  # $D400..$D41F


class Ultimate64Transport(HardwareTransportBase):
    """C64Transport implementation backed by Ultimate 64 REST API.

    All memory I/O goes through the device's DMA-backed ``readmem``/
    ``writemem`` endpoints, so no CPU pause is required — reads and
    writes happen concurrently with normal execution.

    Maintains a host-side shadow of the SID register file ($D400-$D41F)
    so :func:`extract_snapshot` can capture the SID state even though
    real-hardware SID readback returns open-bus garbage for the
    write-only registers.  See :attr:`sid_shadow`.
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

        from ..memory_policy import MemoryPolicy as _MemoryPolicy
        self._memory_policy: _MemoryPolicy = memory_policy or _MemoryPolicy.permissive()

        # Host-side shadow of $D400-$D41F.  Initialised to all-zero —
        # matches the SID register file at C64 power-on.  Updated on
        # every write_memory() call that overlaps the SID window.
        self._sid_shadow: bytearray = bytearray(_SID_LEN)

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

    @property
    def sid_shadow(self) -> bytes:
        """Host-side shadow of the SID register file ($D400-$D41F).

        Returns a 32-byte snapshot of every byte written to the SID
        through this transport since construction (or since the last
        :meth:`reset_sid_shadow` call).  Used by
        :func:`c64_test_harness.snapshot.extract_snapshot` to capture
        SID state without hitting open-bus reads on real hardware.

        The shadow tracks writes only; it does NOT reflect:

        * Writes that bypass ``write_memory`` (e.g. SocketDMA opcodes
          issued through :class:`U64SocketDMATransport`).
        * SID register updates the running C64 program makes via 6502
          stores — those happen entirely inside the device.

        For programs that drive the SID from the C64 side, the shadow
        will diverge from the device's true register state.  For tests
        whose SID writes all originate host-side, the shadow is exact.
        """
        return bytes(self._sid_shadow)

    def reset_sid_shadow(self) -> None:
        """Clear the host-side SID shadow back to all zeros.

        Call this after a full machine reset (e.g. ``client.reboot()``)
        which clears the SID register file on the hardware.  A CPU soft
        reset (``client.reset()``) does NOT clear the SID — the SID is
        on its own clock domain on real hardware and only a full
        machine reset / power-cycle wipes the register file — so this
        method is deliberately NOT called automatically by
        ``client.reset()``.  Call it explicitly when you know the SID
        was cleared on the device side.
        """
        self._sid_shadow = bytearray(_SID_LEN)

    # ----- C64Transport protocol -----

    def read_memory(self, addr: int, length: int) -> bytes:
        """Read *length* bytes from C64 memory via DMA."""
        if length <= 0:
            return b""
        return self._client.read_mem(addr, length)

    def write_memory(
        self,
        addr: int,
        data: bytes | list[int] | bytearray,
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

        After the policy check (and any logging), the bytes that
        overlap $D400-$D41F are mirrored into :attr:`sid_shadow` so the
        snapshot extractor can capture SID state on a backend whose
        readback returns open-bus.  Writes that don't touch the SID
        window leave the shadow untouched.
        """
        # Normalise data into bytes — accept bytes, bytearray, or list[int].
        if isinstance(data, list):
            data = bytes(data)
        elif isinstance(data, bytearray):
            data = bytes(data)
        if not data:
            return
        if not self._memory_policy.is_permissive():
            self._memory_policy.check_write(addr, len(data), override=override)

        # Update the SID shadow if any byte of this write lands in the
        # $D400-$D41F window.  Handle writes that straddle the SID range
        # on either end — only the portion inside the window is mirrored.
        end = addr + len(data)
        if addr < _SID_BASE + _SID_LEN and end > _SID_BASE:
            lo = max(addr, _SID_BASE)
            hi = min(end, _SID_BASE + _SID_LEN)
            src_start = lo - addr
            src_end = hi - addr
            dst_start = lo - _SID_BASE
            dst_end = hi - _SID_BASE
            self._sid_shadow[dst_start:dst_end] = data[src_start:src_end]

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
