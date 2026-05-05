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
    """Thread-safe allocator for TCP monitor ports."""

    def __init__(
        self,
        port_range_start: int = 6510,
        port_range_end: int = 6520,
    ) -> None:
        self._start = port_range_start
        self._end = port_range_end
        self._allocated: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self, allow_in_use: bool = False) -> int:
        """Return the next free port (not allocated and, by default, no TCP listener).

        If *allow_in_use* is True, ports with active listeners are still
        eligible (used when reusing existing VICE instances).

        Raises ``RuntimeError`` if all ports are exhausted.
        """
        with self._lock:
            for port in range(self._start, self._end):
                if port in self._allocated:
                    continue
                if not allow_in_use and self.is_port_in_use(port):
                    continue
                self._allocated.add(port)
                return port
            raise RuntimeError(
                f"No free ports in range {self._start}-{self._end - 1}"
            )

    def release(self, port: int) -> None:
        """Mark *port* as available for reuse."""
        with self._lock:
            self._allocated.discard(port)

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


@dataclass
class ViceInstance:
    """Handle to a single running VICE instance."""

    port: int
    process: ViceProcess | None
    transport: ViceTransport
    managed: bool = True

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
            extra_args=list(self._base_config.extra_args),
        )
        proc = ViceProcess(cfg)
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
