"""UDP debug stream capture from Ultimate 64 bus trace.

The U64 streams 6510/VIC bus traces over UDP. Each packet: 4-byte
header (2-byte LE sequence + 2 reserved) followed by 360 × 4-byte LE
entries, each representing one bus cycle.

**Rate cap (measured, not a bug in this receiver)**

The U64E FPGA emits the debug stream at a fixed rate of roughly
**~850,000 entries per second** (≈ 2,400 UDP packets/sec) regardless
of the CPU's actual turbo speed. This matches the 6510's native rate
at 1 MHz, so at 1 MHz you get an essentially complete cycle-accurate
trace. At higher turbo speeds you get a **uniformly sampled 1/N view**
of the real bus: at 4 MHz you see ~1/4 of cycles, at 48 MHz ~1/48.
The FPGA does NOT attempt to send everything and drop on overflow
(sequence-number gaps stay at zero across speeds); it rate-limits at
the source. See ``tests/test_u64_debug_stream_speed_live.py`` for
the measurement.

**Practical implication**: if your test needs a complete trace
(call-graph, exact cycle count, bus-state transitions), set
``set_turbo_mhz(client, 1)`` for the capture window. If you only need
aggregate statistics (PC distribution, frequency maps), turbo-speed
capture is fine because the sampling is uniform.

**FPGA degradation under sustained workload (issue #81)**

Independent of the rate cap above, the U64E FPGA's debug-stream
emitter exhibits *delivery-rate degradation* over time when the
device is under sustained adjacent workload. Observed delivery
drops to **30-90% of the configured rate** after a long run, with
``packets_received`` falling well below the expected ~2,400/sec
even at 1 MHz. Recovery requires a full :meth:`Ultimate64Client.reboot`
(FPGA reinit, ~8s); a soft :meth:`Ultimate64Client.reset` is
**insufficient**. For multi-routine benches, prefer constructing
the capture via :meth:`DebugCapture.with_fresh_fpga` so each
routine starts on a fresh emitter.

**Cycle-counting on real silicon: use a tolerance window**

The CIA 6526's timer arming has 1-2 cycle phase variance against
the CPU clock, which adds up to roughly **~43 cycles of inherent
non-determinism** per measured interval on real hardware (VICE does
not model this jitter). Anyone using ``DebugCapture`` to verify a
cycle count on a real U64 should compare against a *tolerance window*
rather than an exact match. See commits ``38f2708`` and ``406c863``
in ``c64-ChaCha20-Poly1305`` for a worked example of the tolerance
pattern.

Public API
----------
- ``DebugCapture`` — background-thread UDP receiver
- ``DebugCaptureResult`` — result dataclass
- ``BusCycle`` — parsed bus cycle entry
- ``DEFAULT_DEBUG_PORT`` — 11002
- ``ENTRIES_PER_PACKET`` — 360
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .ultimate64_client import Ultimate64Client

__all__ = [
    "DebugCapture",
    "DebugCaptureResult",
    "BusCycle",
    "DEFAULT_DEBUG_PORT",
    "ENTRIES_PER_PACKET",
]

_log = logging.getLogger(__name__)

DEFAULT_DEBUG_PORT = 11002
ENTRIES_PER_PACKET = 360
ENTRY_SIZE = 4           # 4 bytes per bus cycle entry
HEADER_SIZE = 4          # 2-byte LE sequence number + 2 reserved


@dataclass(frozen=True)
class BusCycle:
    """A single 6510/VIC bus cycle parsed from a 32-bit debug stream word.

    Bit layout (6510/VIC)::

        Bit 31:    PHI2 (1=6510 access, 0=VIC access)
        Bit 30:    GAME# (active low)
        Bit 29:    EXROM# (active low)
        Bit 28:    BA (Bus Available)
        Bit 27:    IRQ# (active low)
        Bit 26:    ROM# (active low)
        Bit 25:    NMI# (active low)
        Bit 24:    R/W# (1=read, 0=write)
        Bits 23-16: Data bus (8-bit)
        Bits 15-0:  Address bus (16-bit)
    """

    raw: int

    @property
    def phi2(self) -> bool:
        """PHI2 clock phase — True for 6510 access, False for VIC access."""
        return bool(self.raw & (1 << 31))

    @property
    def is_cpu(self) -> bool:
        """True if this is a 6510 CPU cycle (PHI2 high)."""
        return self.phi2

    @property
    def is_vic(self) -> bool:
        """True if this is a VIC access cycle (PHI2 low)."""
        return not self.phi2

    @property
    def game(self) -> bool:
        """GAME# signal — True when asserted (active low, bit=0)."""
        return not bool(self.raw & (1 << 30))

    @property
    def exrom(self) -> bool:
        """EXROM# signal — True when asserted (active low, bit=0)."""
        return not bool(self.raw & (1 << 29))

    @property
    def ba(self) -> bool:
        """Bus Available signal."""
        return bool(self.raw & (1 << 28))

    @property
    def irq(self) -> bool:
        """IRQ# signal — True when asserted (active low, bit=0)."""
        return not bool(self.raw & (1 << 27))

    @property
    def rom(self) -> bool:
        """ROM# signal — True when asserted (active low, bit=0)."""
        return not bool(self.raw & (1 << 26))

    @property
    def nmi(self) -> bool:
        """NMI# signal — True when asserted (active low, bit=0)."""
        return not bool(self.raw & (1 << 25))

    @property
    def rw(self) -> bool:
        """R/W# line — True for read, False for write."""
        return bool(self.raw & (1 << 24))

    @property
    def is_read(self) -> bool:
        """True if this cycle is a read."""
        return self.rw

    @property
    def is_write(self) -> bool:
        """True if this cycle is a write."""
        return not self.rw

    @property
    def data(self) -> int:
        """Data bus value (8-bit)."""
        return (self.raw >> 16) & 0xFF

    @property
    def address(self) -> int:
        """Address bus value (16-bit)."""
        return self.raw & 0xFFFF


@dataclass
class DebugCaptureResult:
    """Outcome of a debug stream capture session."""

    trace: list[BusCycle]
    duration_seconds: float
    packets_received: int
    packets_dropped: int
    total_cycles: int


class DebugCapture:
    """Background-thread UDP receiver for U64 debug bus traces.

    Usage::

        cap = DebugCapture(port=11002)
        cap.start()
        # ... run code on the C64 ...
        result = cap.stop()

    The receiver runs in a daemon thread. ``start()`` begins capturing
    packets into an internal buffer. ``stop()`` halts capture, parses
    the raw data into ``BusCycle`` objects, and returns a
    ``DebugCaptureResult``.

    Sequence numbers are tracked for gap detection. Gaps are logged
    but captured data is simply the concatenation of received payloads
    in order.

    Raw bytes are accumulated during capture and parsed into BusCycle
    objects only on ``stop()`` to keep the recv loop fast at ~32 Mbps.

    .. note::

       The U64E debug stream is rate-capped at ~850k entries/sec at the
       FPGA source (see the module docstring). At CPU turbo speeds you
       receive a uniformly sampled ``1/N`` view of the bus, not a
       dropped-during-send slice. ``packets_dropped`` stays at zero
       regardless of turbo speed because the rate limit is applied
       before emission. Drop to 1 MHz if you need a complete trace.
    """

    def __init__(
        self,
        port: int = DEFAULT_DEBUG_PORT,
        bind_addr: str = "",
        multicast_group: str | None = None,
        recv_buf_size: int = 262144,
        max_bytes: int | None = None,
        filter: Callable[[int], bool] | None = None,
    ) -> None:
        """
        Args:
            port: UDP port to listen on.
            bind_addr: Address to bind to (empty = all interfaces).
            multicast_group: If set, join this multicast group.
            recv_buf_size: SO_RCVBUF size hint (larger for ~32 Mbps stream).
            max_bytes: If set, cap retained raw entry bytes to the last
                ``max_bytes`` (rolling window). Older chunks are evicted
                FIFO in the recv thread. Default None = unbounded.
            filter: If set, called with each 32-bit raw word in the recv
                thread; return True to keep the entry, False to drop.
                Lets the caller restrict capture to e.g. CPU cycles in a
                given PC range. Default None = keep everything.
        """
        self._port = port
        self._bind_addr = bind_addr
        self._multicast_group = multicast_group
        self._recv_buf_size = recv_buf_size
        self._max_bytes = max_bytes
        self._filter = filter

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Accumulated raw entry data (no headers)
        self._raw_chunks: list[bytes] = []
        self._raw_bytes_total = 0
        self._packets_received = 0
        self._packets_dropped = 0
        self._last_seq: int | None = None
        self._started = False
        self._start_time = 0.0

    @classmethod
    def with_fresh_fpga(
        cls,
        client: "Ultimate64Client",
        *,
        capture_kwargs: dict | None = None,
        reboot_settle_seconds: float = 12.0,
    ) -> "DebugCapture":
        """Reboot the U64 FPGA, then construct a fresh DebugCapture ready to start.

        The U64E FPGA's debug-stream emitter degrades over time under
        sustained workload — observed delivery drops to 30-90% of the
        configured rate after a long run, and only a full
        :meth:`Ultimate64Client.reboot` restores it. A soft
        :meth:`Ultimate64Client.reset` is insufficient (see issue #81).

        Use this constructor before each routine in a multi-routine bench::

            for routine in routines:
                cap = DebugCapture.with_fresh_fpga(client)
                cap.start()
                run_routine()
                result = cap.stop()

        If you need fine-grained control of the reboot sequencing relative
        to other test fixtures, do the reboot+settle yourself and
        instantiate :class:`DebugCapture` the normal way.

        This helper NEVER calls :meth:`Ultimate64Client.poweroff` —
        ``poweroff`` is irrecoverable over the network and requires
        physical access to power-cycle. ``reboot()`` is the right
        primitive for clearing FPGA state.

        :param client: Connected Ultimate64 client.
        :param capture_kwargs: Forwarded as ``**kwargs`` to the
            :class:`DebugCapture` constructor (e.g. ``port``,
            ``multicast_group``, ``max_bytes``, ``filter``). Default
            ``None`` is treated as ``{}``.
        :param reboot_settle_seconds: Sleep after ``reboot()`` before
            returning. Default 12.0s — matches the value used by
            :func:`ultimate64_helpers.recover` (PR #75).
        :returns: A freshly-constructed :class:`DebugCapture`. The caller
            still has to call ``.start()``.
        """
        client.reboot()
        time.sleep(reboot_settle_seconds)
        return cls(**(capture_kwargs or {}))

    def start(self) -> None:
        """Begin capturing debug packets in a background thread."""
        if self._started:
            raise RuntimeError("DebugCapture already started")

        self._stop_event.clear()
        self._raw_chunks = []
        self._raw_bytes_total = 0
        self._packets_received = 0
        self._packets_dropped = 0
        self._last_seq = None

        # Create and bind UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._recv_buf_size)
        except OSError:
            pass  # best-effort buffer size
        self._sock.bind((self._bind_addr, self._port))

        # Join multicast group if requested
        if self._multicast_group:
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(self._multicast_group),
                socket.inet_aton("0.0.0.0"),
            )
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        self._sock.settimeout(0.5)  # so recv loop can check stop_event

        self._thread = threading.Thread(
            target=self._recv_loop,
            name="u64-debug-capture",
            daemon=True,
        )
        self._started = True
        self._start_time = time.monotonic()
        self._thread.start()
        _log.info("DebugCapture started on port %d", self._port)

    def _recv_loop(self) -> None:
        """Receive UDP packets until stop_event is set."""
        assert self._sock is not None
        expected_payload = HEADER_SIZE + ENTRIES_PER_PACKET * ENTRY_SIZE
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(expected_payload + 64)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                raise

            if len(data) <= HEADER_SIZE:
                continue  # runt packet

            seq = struct.unpack_from("<H", data, 0)[0]
            entry_payload = data[HEADER_SIZE:]

            # Optional per-entry filtering: done outside the lock since it
            # only reads local bytes. Keeps the protected section short.
            if self._filter is not None:
                n = len(entry_payload) // ENTRY_SIZE
                kept = bytearray()
                for i in range(n):
                    word = struct.unpack_from("<I", entry_payload, i * ENTRY_SIZE)[0]
                    if self._filter(word):
                        kept += entry_payload[i * ENTRY_SIZE : (i + 1) * ENTRY_SIZE]
                entry_payload = bytes(kept)

            with self._lock:
                # Gap detection (always on raw packet sequence)
                if self._last_seq is not None:
                    expected = (self._last_seq + 1) & 0xFFFF
                    if seq != expected:
                        gap = (seq - expected) & 0xFFFF
                        if gap < 0x8000:  # forward gap (not reorder)
                            self._packets_dropped += gap
                            _log.warning(
                                "Debug stream gap: expected seq %d, got %d (%d packets dropped)",
                                expected, seq, gap,
                            )

                self._last_seq = seq
                self._packets_received += 1

                if entry_payload:
                    self._raw_chunks.append(entry_payload)
                    self._raw_bytes_total += len(entry_payload)

                    # Rolling-window trim: evict oldest chunks FIFO until
                    # the retained total fits in max_bytes. The tail chunk
                    # is never partially sliced — we tolerate overshoot of
                    # at most one chunk (a single UDP payload, ~1.4KB).
                    if self._max_bytes is not None:
                        while (
                            self._raw_bytes_total > self._max_bytes
                            and len(self._raw_chunks) > 1
                        ):
                            evicted = self._raw_chunks.pop(0)
                            self._raw_bytes_total -= len(evicted)

    def stop(self) -> DebugCaptureResult:
        """Stop capturing and parse the accumulated bus trace.

        Returns:
            DebugCaptureResult with parsed BusCycle trace and statistics.
        """
        if not self._started:
            raise RuntimeError("DebugCapture not started")

        elapsed = time.monotonic() - self._start_time
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        with self._lock:
            raw_data = b"".join(self._raw_chunks)
            packets_received = self._packets_received
            packets_dropped = self._packets_dropped

        # Parse raw bytes into BusCycle objects
        total_words = len(raw_data) // ENTRY_SIZE
        trace: list[BusCycle] = []
        for i in range(total_words):
            word = struct.unpack_from("<I", raw_data, i * ENTRY_SIZE)[0]
            trace.append(BusCycle(raw=word))

        _log.info(
            "DebugCapture stopped: %d cycles, %d packets, %d dropped, %.2fs",
            len(trace), packets_received, packets_dropped, elapsed,
        )

        self._started = False

        return DebugCaptureResult(
            trace=trace,
            duration_seconds=elapsed,
            packets_received=packets_received,
            packets_dropped=packets_dropped,
            total_cycles=len(trace),
        )

    @property
    def is_capturing(self) -> bool:
        """True if the capture thread is running."""
        return self._started and not self._stop_event.is_set()

    @property
    def packets_received(self) -> int:
        """Number of packets received so far (thread-safe)."""
        with self._lock:
            return self._packets_received
