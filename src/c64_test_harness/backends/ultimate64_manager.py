"""Multi-device Ultimate 64 hardware instance management.

Provides ``Ultimate64Device`` (device configuration), ``Ultimate64Instance``
(handle to an acquired device + transport), and
``Ultimate64InstanceManager`` for pool-based acquisition of multiple
physical U64 units.

This mirrors the :class:`ViceInstanceManager` pattern but allocates from
a fixed list of configured devices (identified by host/IP) rather than a
TCP port range: each device is a physical hardware unit, and concurrency
is bounded by how many units you have wired up.

Compatibility with :func:`run_parallel`
---------------------------------------
``run_parallel`` (see ``c64_test_harness.parallel``) expects a manager
that exposes ``acquire() -> instance`` and ``release(instance)`` and
where each instance exposes a ``.transport`` attribute and a ``.pid``
attribute.  This manager matches that protocol verbatim:

* :meth:`Ultimate64InstanceManager.acquire` returns an
  :class:`Ultimate64Instance` with a live
  :class:`~c64_test_harness.backends.ultimate64.Ultimate64Transport`.
* :meth:`Ultimate64InstanceManager.release` closes the transport and
  returns the device to the pool.
* :class:`Ultimate64Instance` exposes ``.transport`` (the live
  transport) and ``.pid`` (always ``None`` — hardware has no OS PID).

As a result, ``run_parallel(u64_manager, tests)`` works without any
modifications to ``parallel.py``.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .ultimate64 import Ultimate64Transport
from .ultimate64_probe import probe_u64

_log = logging.getLogger(__name__)


class Ultimate64ManagerError(Exception):
    """Base class for Ultimate64 manager errors."""


class Ultimate64PoolExhaustedError(Ultimate64ManagerError):
    """Raised when no device becomes available within the acquire timeout."""


@dataclass(frozen=True)
class Ultimate64Device:
    """Configuration for one Ultimate 64 hardware unit.

    Parameters
    ----------
    host:
        Hostname or IP address of the unit (REST endpoint host).
    password:
        Optional password if the unit requires authentication.
    port:
        HTTP port of the REST API (default 80).
    timeout:
        Per-request HTTP timeout in seconds (default 10.0).
    name:
        Optional human-readable label.  Defaults to ``host`` when empty.
    """

    host: str
    password: str | None = None
    port: int = 80
    timeout: float = 10.0
    name: str = ""

    @property
    def label(self) -> str:
        """Human-readable label for this device (falls back to host)."""
        return self.name or self.host


@dataclass
class Ultimate64Instance:
    """An acquired Ultimate 64 device plus its live transport.

    Analogous to :class:`ViceInstance`.  The ``pid`` property always
    returns ``None`` (hardware has no OS-level process).
    """

    device: Ultimate64Device
    transport: Ultimate64Transport
    _stopped: bool = field(default=False, repr=False)

    @property
    def pid(self) -> int | None:
        """Hardware devices have no OS PID; always ``None``.

        Provided for :func:`run_parallel` compatibility.
        """
        return None

    def stop(self) -> None:
        """Close the transport.  Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self.transport.close()
        except Exception:
            pass


class Ultimate64InstanceManager:
    """Pool-based manager for multiple Ultimate 64 devices.

    Analogous to :class:`ViceInstanceManager`, but allocates from a
    fixed list of configured devices rather than a port range.  Each
    device represents one physical hardware unit.

    Thread-safe: multiple threads may call :meth:`acquire` concurrently
    and will each receive a distinct device.  If all devices are in
    use, ``acquire()`` blocks (using ``threading.Condition.wait``) up
    to ``acquire_timeout`` seconds for one to free up, after which it
    raises :class:`Ultimate64PoolExhaustedError`.

    No network calls are performed in ``__init__``; transports are
    created lazily on :meth:`acquire`.

    Usage::

        devices = [Ultimate64Device(host="10.0.0.10"),
                   Ultimate64Device(host="10.0.0.11")]
        with Ultimate64InstanceManager(devices) as mgr:
            with mgr.instance() as inst:
                inst.transport.write_memory(0x0400, b"HELLO")
    """

    def __init__(
        self,
        devices: list[Ultimate64Device],
        acquire_timeout: float = 30.0,
    ) -> None:
        if not devices:
            raise ValueError("Ultimate64InstanceManager requires at least one device")
        # Defensive copy — caller can't mutate the pool.
        self._devices: list[Ultimate64Device] = list(devices)
        self._acquire_timeout = acquire_timeout
        # Available devices (pool).  Items are removed on acquire,
        # returned on release.
        self._available: list[Ultimate64Device] = list(self._devices)
        # All currently in-flight instances (for shutdown).
        self._in_flight: list[Ultimate64Instance] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._shutdown = False

    def __enter__(self) -> Ultimate64InstanceManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    @property
    def pool_size(self) -> int:
        """Total number of devices configured in the pool."""
        return len(self._devices)

    @property
    def available_count(self) -> int:
        """Number of devices currently free (not acquired)."""
        with self._lock:
            return len(self._available)

    @property
    def active_count(self) -> int:
        """Number of currently in-flight (acquired) instances."""
        with self._lock:
            return len(self._in_flight)

    def acquire(self) -> Ultimate64Instance:
        """Acquire the next available device.

        Before creating a transport, the device is probed (ping + TCP)
        to confirm reachability.  If a device fails its probe, it is
        returned to the *end* of the available list and the next device
        is tried.  If ALL available devices fail probing, raises
        :class:`Ultimate64PoolExhaustedError` with the probe errors.

        Blocks up to ``acquire_timeout`` seconds waiting for a device
        to become free.  Raises :class:`Ultimate64PoolExhaustedError`
        on timeout, or :class:`Ultimate64ManagerError` if the manager
        has been shut down.
        """
        with self._cond:
            if self._shutdown:
                raise Ultimate64ManagerError(
                    "Cannot acquire from a shut-down manager"
                )
            # Wait until a device is available or timeout elapses.
            if not self._available:
                got = self._cond.wait_for(
                    lambda: bool(self._available) or self._shutdown,
                    timeout=self._acquire_timeout,
                )
                if self._shutdown:
                    raise Ultimate64ManagerError(
                        "Manager was shut down while waiting for a device"
                    )
                if not got or not self._available:
                    raise Ultimate64PoolExhaustedError(
                        f"No Ultimate64 device became available within "
                        f"{self._acquire_timeout}s (pool size={len(self._devices)})"
                    )

            # Try each available device with a liveness probe.
            probe_errors: list[str] = []
            tried = 0
            total = len(self._available)
            while tried < total:
                device = self._available.pop(0)
                result = probe_u64(
                    device.host,
                    port=device.port,
                    password=device.password,
                    skip_api=True,
                )
                if result.reachable:
                    transport = Ultimate64Transport(
                        host=device.host,
                        password=device.password,
                        port=device.port,
                        timeout=device.timeout,
                    )
                    instance = Ultimate64Instance(device=device, transport=transport)
                    self._in_flight.append(instance)
                    return instance
                # Probe failed — log, record, and push device to the end.
                _log.warning(
                    "Probe failed for %s: %s", device.label, result.error
                )
                probe_errors.append(f"{device.label}: {result.error}")
                self._available.append(device)
                tried += 1

            # All devices failed probing.
            raise Ultimate64PoolExhaustedError(
                f"All {total} device(s) failed liveness probe: "
                + "; ".join(probe_errors)
            )

    def release(self, instance: Ultimate64Instance) -> None:
        """Return *instance*'s device to the pool.

        Closes the transport and makes the device reusable.  Safe to
        call multiple times on the same instance (double-release is a
        no-op).
        """
        with self._cond:
            try:
                self._in_flight.remove(instance)
            except ValueError:
                # Already released — no-op for idempotency.
                return
            self._available.append(instance.device)
            self._cond.notify()
        # Close transport outside the lock — it may do network I/O.
        instance.stop()

    @contextmanager
    def instance(self) -> Iterator[Ultimate64Instance]:
        """Context manager: acquire then auto-release on exit."""
        inst = self.acquire()
        try:
            yield inst
        finally:
            self.release(inst)

    def shutdown(self) -> None:
        """Release all in-flight instances and mark manager shut down.

        After shutdown, :meth:`acquire` raises
        :class:`Ultimate64ManagerError`.  Safe to call multiple times.
        """
        with self._cond:
            self._shutdown = True
            instances = list(self._in_flight)
            self._in_flight.clear()
            self._available.clear()
            self._cond.notify_all()
        # Close transports outside the lock.
        for inst in instances:
            inst.stop()
