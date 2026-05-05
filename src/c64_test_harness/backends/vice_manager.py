"""Multi-instance VICE management — port allocation and instance lifecycle.

Provides ``PortAllocator`` for thread-safe port management,
``ViceInstance`` as a lightweight handle to one running VICE,
and ``ViceInstanceManager`` for acquiring/releasing multiple
concurrent emulator instances.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .vice import ViceTransport
from .vice_lifecycle import ViceConfig, ViceProcess


class PortAllocator:
    """Thread-safe, cross-process-safe allocator for TCP monitor ports.

    Allocated ports are held at the OS level via ``bind()`` so that
    concurrent processes cannot claim the same port.  The reservation
    socket is kept open until the caller retrieves it with
    ``take_socket()`` (to close it just before VICE starts) or until
    ``release()`` is called.
    """

    def __init__(
        self,
        port_range_start: int = 6510,
        port_range_end: int = 6520,
    ) -> None:
        self._start = port_range_start
        self._end = port_range_end
        self._allocated: set[int] = set()
        self._held_sockets: dict[int, socket.socket] = {}
        self._lock = threading.Lock()

    def allocate(self, allow_in_use: bool = False) -> int:
        """Return the next free port, reserved at the OS level.

        The port is held via ``bind()`` so other processes cannot grab it.
        If *allow_in_use* is True, ports with active listeners are still
        eligible (used when reusing existing VICE instances); no
        reservation socket is held for those ports.

        Raises ``RuntimeError`` if all ports are exhausted.
        """
        with self._lock:
            for port in range(self._start, self._end):
                if port in self._allocated:
                    continue
                if allow_in_use and self.is_port_in_use(port):
                    self._allocated.add(port)
                    return port
                sock = self._try_bind(port)
                if sock is None:
                    continue
                self._allocated.add(port)
                self._held_sockets[port] = sock
                return port
            raise RuntimeError(
                f"No free ports in range {self._start}-{self._end - 1}"
            )

    def take_socket(self, port: int) -> socket.socket | None:
        """Remove and return the reservation socket for *port*.

        The caller should close it immediately before starting VICE so
        the emulator can bind to the port.  Returns ``None`` if no
        socket is held (e.g. adopted ports).
        """
        with self._lock:
            return self._held_sockets.pop(port, None)

    def release(self, port: int) -> None:
        """Mark *port* as available for reuse and close any held socket."""
        with self._lock:
            self._allocated.discard(port)
            sock = self._held_sockets.pop(port, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def allocated_ports(self) -> frozenset[int]:
        """Snapshot of currently allocated ports."""
        with self._lock:
            return frozenset(self._allocated)

    @staticmethod
    def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
        """Return True if *port* has an active TCP listener."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect((host, port))
            s.close()
            return True
        except (OSError, ConnectionRefusedError):
            return False

    @staticmethod
    def _try_bind(port: int, host: str = "127.0.0.1") -> socket.socket | None:
        """Try to bind a TCP socket to *port*.

        Returns the bound socket on success, ``None`` if the port is
        already in use.  The socket is set to ``SO_REUSEADDR`` so that
        VICE can rebind immediately after the socket is closed.
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(1)
            return s
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            return None


@dataclass
class ViceInstance:
    """Handle to a single running VICE instance."""

    port: int
    process: ViceProcess | None
    transport: ViceTransport
    managed: bool = True

    @property
    def pid(self) -> int | None:
        """PID of the underlying VICE process, or ``None`` if adopted."""
        return self.process.pid if self.process else None

    def stop(self) -> None:
        """Close transport and stop the process (if managed)."""
        try:
            self.transport.close()
        except Exception:
            pass
        if self.managed and self.process is not None:
            self.process.stop()


class ViceInstanceManager:
    """Manage multiple concurrent VICE instances.

    Usage::

        with ViceInstanceManager(config) as mgr:
            with mgr.instance() as inst:
                transport = inst.transport
                ...
    """

    def __init__(
        self,
        config: ViceConfig | None = None,
        port_range_start: int = 6510,
        port_range_end: int = 6520,
        reuse_existing: bool = False,
    ) -> None:
        self._base_config = config or ViceConfig()
        self._allocator = PortAllocator(port_range_start, port_range_end)
        self._reuse_existing = reuse_existing
        self._instances: list[ViceInstance] = []
        self._lock = threading.Lock()

    def __enter__(self) -> ViceInstanceManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    @property
    def active_count(self) -> int:
        """Number of currently active instances."""
        with self._lock:
            return len(self._instances)

    def acquire(self) -> ViceInstance:
        """Allocate a port, start VICE, and return a ready instance.

        If ``reuse_existing`` is True and a listener already exists on the
        allocated port, the manager adopts it (``managed=False``) instead
        of launching a new process.

        Raises ``RuntimeError`` if the monitor port never becomes ready.
        """
        port = self._allocator.allocate(allow_in_use=self._reuse_existing)
        try:
            instance = self._start_or_adopt(port)
        except Exception:
            self._allocator.release(port)
            raise
        with self._lock:
            self._instances.append(instance)
        return instance

    def release(self, instance: ViceInstance) -> None:
        """Stop an instance and free its port."""
        with self._lock:
            try:
                self._instances.remove(instance)
            except ValueError:
                pass
        instance.stop()
        self._allocator.release(instance.port)

    def shutdown(self) -> None:
        """Stop all active instances."""
        with self._lock:
            instances = list(self._instances)
            self._instances.clear()
        for inst in instances:
            inst.stop()
            self._allocator.release(inst.port)

    @contextmanager
    def instance(self) -> Iterator[ViceInstance]:
        """Context manager: acquire an instance, auto-release on exit."""
        inst = self.acquire()
        try:
            yield inst
        finally:
            self.release(inst)

    # ------------------------------------------------------------------

    def _start_or_adopt(self, port: int) -> ViceInstance:
        """Start a new VICE or adopt an existing listener on *port*."""
        if self._reuse_existing and PortAllocator.is_port_in_use(port):
            transport = ViceTransport(port=port)
            return ViceInstance(
                port=port, process=None, transport=transport, managed=False,
            )

        cfg = ViceConfig(
            executable=self._base_config.executable,
            prg_path=self._base_config.prg_path,
            port=port,
            warp=self._base_config.warp,
            ntsc=self._base_config.ntsc,
            sound=self._base_config.sound,
            minimize=self._base_config.minimize,
            extra_args=list(self._base_config.extra_args),
        )
        proc = ViceProcess(cfg)

        # Release the OS-level port reservation just before VICE starts
        # so the emulator can bind to it.
        reservation = self._allocator.take_socket(port)
        if reservation is not None:
            reservation.close()

        proc.start()

        if not proc.wait_for_monitor(timeout=30.0):
            proc.stop()
            raise RuntimeError(
                f"VICE monitor on port {port} did not become ready"
            )

        transport = ViceTransport(port=port)
        return ViceInstance(
            port=port, process=proc, transport=transport, managed=True,
        )
