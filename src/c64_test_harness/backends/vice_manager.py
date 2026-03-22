"""Multi-instance VICE management — port allocation and instance lifecycle.

Provides ``PortAllocator`` for thread-safe port management,
``ViceInstance`` as a lightweight handle to one running VICE,
and ``ViceInstanceManager`` for acquiring/releasing multiple
concurrent emulator instances.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .port_lock import PortLock
from .vice_binary import BinaryViceTransport
from .vice_lifecycle import ViceConfig, ViceProcess

logger = logging.getLogger(__name__)


class PortAllocator:
    """Thread-safe, cross-process-safe allocator for TCP monitor ports.

    Allocated ports are held at the OS level via ``bind()`` and a
    file-based ``flock()`` so that concurrent processes cannot claim the
    same port.  The reservation socket is kept open until the caller
    retrieves it with ``take_socket()`` (to close it just before VICE
    starts) or until ``release()`` is called.  The file lock persists
    across the socket-close→VICE-start window, closing the TOCTOU gap.
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
        self._held_locks: dict[int, PortLock] = {}
        self._lock = threading.Lock()
        PortLock.cleanup_stale()

    def allocate(self, allow_in_use: bool = False) -> int:
        """Return the next free port, reserved at the OS level.

        The port is held via ``bind()`` and a file lock so other
        processes cannot grab it.  If *allow_in_use* is True, ports with
        active listeners are still eligible (used when reusing existing
        VICE instances); no reservation socket is held for those ports.

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
                # Also acquire a file lock for cross-process safety
                port_lock = PortLock(port)
                if not port_lock.acquire():
                    sock.close()
                    continue
                self._allocated.add(port)
                self._held_sockets[port] = sock
                self._held_locks[port] = port_lock
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

    def take_lock(self, port: int) -> PortLock | None:
        """Remove and return the file lock for *port*.

        The caller becomes responsible for releasing it.  Returns
        ``None`` if no lock is held (e.g. adopted ports).
        """
        with self._lock:
            return self._held_locks.pop(port, None)

    def release(self, port: int) -> None:
        """Mark *port* as available for reuse and close any held socket/lock."""
        with self._lock:
            self._allocated.discard(port)
            sock = self._held_sockets.pop(port, None)
            lock = self._held_locks.pop(port, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if lock is not None:
            lock.release()

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
    transport: BinaryViceTransport
    managed: bool = True
    _port_lock: PortLock | None = field(default=None, repr=False)

    @property
    def pid(self) -> int | None:
        """PID of the underlying VICE process, or ``None`` if adopted."""
        return self.process.pid if self.process else None

    def stop(self) -> None:
        """Close transport, stop the process, and release the port lock."""
        try:
            self.transport.close()
        except Exception:
            pass
        if self.managed and self.process is not None:
            self.process.stop()
        if self._port_lock is not None:
            self._port_lock.release()
            self._port_lock = None


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
        max_retries: int = 3,
    ) -> None:
        self._base_config = config or ViceConfig()
        self._allocator = PortAllocator(port_range_start, port_range_end)
        self._reuse_existing = reuse_existing
        self._max_retries = max_retries
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

        Retries with exponential backoff on failure (up to
        ``max_retries`` attempts).

        Raises ``RuntimeError`` if all attempts fail.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                port = self._allocator.allocate(
                    allow_in_use=self._reuse_existing,
                )
            except RuntimeError:
                if attempt < self._max_retries:
                    time.sleep(0.1 * attempt)
                    continue
                raise
            try:
                instance = self._start_or_adopt(port)
            except Exception as exc:
                last_error = exc
                self._allocator.release(port)
                if attempt < self._max_retries:
                    logger.warning(
                        "VICE acquire attempt %d/%d on port %d failed: %s",
                        attempt, self._max_retries, port, exc,
                    )
                    time.sleep(0.1 * attempt)
                    continue
                raise
            with self._lock:
                self._instances.append(instance)
            return instance
        # Should not reach here, but just in case
        raise RuntimeError(
            f"Failed to acquire VICE instance after {self._max_retries} "
            f"attempts: {last_error}"
        )

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
            transport = BinaryViceTransport(port=port)
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

        # Take the file lock BEFORE closing the reservation socket.
        # The file lock bridges the gap between socket close and VICE
        # binding, preventing other processes from stealing the port.
        port_lock = self._allocator.take_lock(port)

        # Release the OS-level port reservation just before VICE starts
        # so the emulator can bind to it.
        reservation = self._allocator.take_socket(port)
        if reservation is not None:
            reservation.close()

        try:
            proc.start()

            # Connect binary transport with retries
            deadline_time = time.monotonic() + 30.0
            transport = None
            last_err = None
            while time.monotonic() < deadline_time:
                if proc._proc is not None and proc._proc.poll() is not None:
                    proc.stop()
                    raise RuntimeError(
                        f"VICE process exited during binary monitor connect on port {port}"
                    )
                try:
                    transport = BinaryViceTransport(port=port)
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(1)
            if transport is None:
                proc.stop()
                raise RuntimeError(
                    f"VICE binary monitor on port {port} did not become ready: {last_err}"
                )

            # Verify the listener is actually our VICE process
            listener_pid = ViceProcess.get_listener_pid(port)
            if listener_pid is not None and proc.pid is not None:
                if listener_pid != proc.pid:
                    proc.stop()
                    raise RuntimeError(
                        f"PID mismatch on port {port}: expected {proc.pid}, "
                        f"found {listener_pid}"
                    )

            if port_lock is not None:
                port_lock.update_vice_pid(proc.pid or 0)

        except Exception:
            if port_lock is not None:
                port_lock.release()
            raise

        return ViceInstance(
            port=port, process=proc, transport=transport, managed=True,
            _port_lock=port_lock,
        )
