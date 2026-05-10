"""Unified backend manager — select VICE or Ultimate 64 at runtime.

Provides ``TestTarget`` (a backend-agnostic handle), ``BackendManager``
(the protocol both managers already satisfy), and ``UnifiedManager``
which delegates to the appropriate underlying manager based on
configuration or environment variables.

Factory function ``create_manager()`` builds a ``UnifiedManager`` from
environment variables and optional keyword overrides.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol, runtime_checkable

from ..transport import C64Transport
from .vice_lifecycle import ViceConfig
from .vice_manager import ViceInstanceManager

try:
    from .device_lock import DeviceLock

    _HAS_DEVICE_LOCK = True
except ImportError:  # pragma: no cover
    _HAS_DEVICE_LOCK = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TestTarget — backend-agnostic handle
# ---------------------------------------------------------------------------

@dataclass
class TestTarget:
    """A backend-agnostic handle to a test target.

    Attributes
    ----------
    transport:
        The live transport (VICE binary monitor or Ultimate 64 REST).
    backend:
        ``"vice"`` or ``"u64"``.
    pid:
        VICE OS process PID, or ``None`` for hardware backends.
    """

    transport: C64Transport
    backend: str
    pid: int | None = None

    @property
    def client(self) -> "Ultimate64Client":  # type: ignore[name-defined]  # noqa: F821
        """Return the underlying Ultimate64Client (U64 backend only).

        Raises ``AttributeError`` on VICE-backed targets.
        """
        from .ultimate64 import Ultimate64Transport

        if not isinstance(self.transport, Ultimate64Transport):
            raise AttributeError(
                "client accessor is U64-only; this target is VICE-backed"
            )
        return self.transport._client


# ---------------------------------------------------------------------------
# BackendManager Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BackendManager(Protocol):
    """Protocol satisfied by both ViceInstanceManager and
    Ultimate64InstanceManager.

    Any object with ``acquire()``, ``release(instance)``, and
    ``shutdown()`` methods qualifies.
    """

    def acquire(self) -> Any: ...

    def release(self, instance: Any) -> None: ...

    def shutdown(self) -> None: ...


# ---------------------------------------------------------------------------
# UnifiedManager
# ---------------------------------------------------------------------------

class UnifiedManager:
    """Backend-agnostic manager that delegates to VICE or Ultimate 64.

    Parameters
    ----------
    backend:
        ``"vice"``, ``"u64"``, or ``"auto"``.  When ``"auto"`` the
        ``C64_BACKEND`` environment variable is consulted, defaulting to
        ``"vice"`` if unset.
    vice_config:
        Optional ``ViceConfig`` for VICE backends.
    vice_kwargs:
        Extra keyword arguments forwarded to ``ViceInstanceManager``.
    u64_hosts:
        Comma-separated hosts or list of hosts for Ultimate 64.
        Defaults to the ``U64_HOST`` environment variable.
    u64_password:
        Optional password for Ultimate 64 devices.  Defaults to the
        ``U64_PASSWORD`` environment variable.
    lock_timeout:
        Cross-process device-lock timeout in seconds (U64 only).
        Defaults to 60.0; long parallel benches typically pass
        ``lock_timeout=1800.0`` (30 min) or higher.
    """

    def __init__(
        self,
        backend: str = "auto",
        vice_config: ViceConfig | None = None,
        vice_kwargs: dict[str, Any] | None = None,
        u64_hosts: str | list[str] | None = None,
        u64_password: str | None = None,
        lock_timeout: float = 60.0,
    ) -> None:
        self._backend = self._resolve_backend(backend)
        self._manager: BackendManager
        self._device_lock: Any = None

        if self._backend == "vice":
            kw = dict(vice_kwargs or {})
            self._manager = ViceInstanceManager(
                config=vice_config, **kw,
            )
        elif self._backend == "u64":
            self._manager = self._build_u64_manager(
                u64_hosts, u64_password, lock_timeout=lock_timeout,
            )
        else:
            raise ValueError(
                f"Unknown backend {self._backend!r}; expected 'vice', 'u64', or 'auto'"
            )

        logger.info("UnifiedManager: using %s backend", self._backend)

    # -- public API ---------------------------------------------------------

    @property
    def backend(self) -> str:
        """The resolved backend name (``"vice"`` or ``"u64"``)."""
        return self._backend

    def acquire(self) -> TestTarget:
        """Acquire a test target from the underlying manager."""
        instance = self._manager.acquire()
        target = TestTarget(
            transport=instance.transport,
            backend=self._backend,
            pid=instance.pid,
        )
        # Stash so release() can delegate to the underlying manager.
        target._instance = instance  # type: ignore[attr-defined]
        return target

    def release(self, target: TestTarget) -> None:
        """Release a previously acquired test target."""
        # We need the original instance for the underlying manager.
        # The instance is stashed on the target so release can delegate.
        raw = getattr(target, "_instance", None)
        if raw is not None:
            self._manager.release(raw)
        else:
            # Fallback: build a lightweight shim that the underlying
            # manager can accept (transport + pid).
            self._manager.release(target)  # type: ignore[arg-type]

    def shutdown(self) -> None:
        """Shut down the underlying manager."""
        self._manager.shutdown()

    @contextmanager
    def instance(self) -> Iterator[TestTarget]:
        """Context manager: acquire a target, auto-release on exit."""
        instance = self._manager.acquire()
        target = TestTarget(
            transport=instance.transport,
            backend=self._backend,
            pid=instance.pid,
        )
        # Stash the raw instance so release() can delegate properly.
        target._instance = instance  # type: ignore[attr-defined]
        try:
            yield target
        finally:
            self._manager.release(instance)

    # -- context manager for the manager itself -----------------------------

    def __enter__(self) -> UnifiedManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        """Resolve ``"auto"`` to a concrete backend name."""
        if backend == "auto":
            backend = os.environ.get("C64_BACKEND", "vice").lower()
        if backend not in ("vice", "u64"):
            raise ValueError(
                f"Unknown backend {backend!r}; expected 'vice' or 'u64'"
            )
        return backend

    @staticmethod
    def _parse_u64_hosts(hosts: str | list[str] | None) -> list[str]:
        """Parse host specification into a list of host strings."""
        if hosts is None:
            hosts = os.environ.get("U64_HOST", "")
        if isinstance(hosts, str):
            hosts = [h.strip() for h in hosts.split(",") if h.strip()]
        return hosts

    @staticmethod
    def _build_u64_manager(
        hosts: str | list[str] | None,
        password: str | None,
        lock_timeout: float = 60.0,
    ) -> Any:
        """Build an Ultimate64InstanceManager from host/password config.

        When :class:`DeviceLock` is available, wraps the manager with
        ``_LockedU64Manager`` for cross-process queueing via flock.
        """
        from .ultimate64_manager import Ultimate64Device, Ultimate64InstanceManager

        parsed_hosts = UnifiedManager._parse_u64_hosts(hosts)
        if not parsed_hosts:
            raise ValueError(
                "U64 backend requires at least one host — set U64_HOST env "
                "var or pass u64_hosts"
            )

        if password is None:
            password = os.environ.get("U64_PASSWORD") or None

        devices = [
            Ultimate64Device(host=h, password=password)
            for h in parsed_hosts
        ]

        inner = Ultimate64InstanceManager(devices)

        if _HAS_DEVICE_LOCK:
            logger.debug("DeviceLock available — cross-process locking enabled")
            return _LockedU64Manager(inner, lock_timeout=lock_timeout)

        logger.debug("DeviceLock not available — in-process pooling only")
        return inner


# ---------------------------------------------------------------------------
# _LockedU64Manager — cross-process queueing for U64 devices
# ---------------------------------------------------------------------------

class _LockedU64Manager:
    """Wraps :class:`Ultimate64InstanceManager` with per-device file locks.

    The inner manager handles in-process thread safety via
    ``threading.Condition``.  This wrapper adds cross-process safety via
    :class:`DeviceLock` (``fcntl.flock``), so multiple independent agents
    queue for the same physical device automatically.

    Flow::

        acquire():
            1. inner.acquire()  → picks a device from the in-process pool
            2. DeviceLock(device.host).acquire(timeout)
               → blocks if another process holds this device
            3. return instance

        release(instance):
            1. DeviceLock.release()
            2. inner.release(instance)

    """

    def __init__(
        self,
        inner: Any,
        lock_timeout: float = 60.0,
    ) -> None:
        self._inner = inner
        self._lock_timeout = lock_timeout
        # Map instance id → DeviceLock so release() can find the right lock.
        self._locks: dict[int, DeviceLock] = {}
        self._map_lock = __import__("threading").Lock()

    def acquire(self) -> Any:
        """Acquire a device with cross-process locking."""
        instance = self._inner.acquire()
        device_host = instance.device.host
        lock = DeviceLock(device_host)
        if not lock.acquire(timeout=self._lock_timeout):
            # Couldn't get cross-process lock — return device to pool.
            self._inner.release(instance)
            raise RuntimeError(
                f"Timed out waiting for cross-process lock on {device_host!r} "
                f"after {self._lock_timeout}s"
            )
        with self._map_lock:
            self._locks[id(instance)] = lock
        logger.debug(
            "Acquired U64 %s with cross-process lock (pid=%d)",
            device_host,
            os.getpid(),
        )
        return instance

    def release(self, instance: Any) -> None:
        """Release device and its cross-process lock."""
        with self._map_lock:
            lock = self._locks.pop(id(instance), None)
        if lock is not None:
            lock.release()
            logger.debug(
                "Released cross-process lock for U64 %s",
                instance.device.host,
            )
        self._inner.release(instance)

    def shutdown(self) -> None:
        """Release all locks and shut down the inner manager."""
        with self._map_lock:
            locks = list(self._locks.values())
            self._locks.clear()
        for lock in locks:
            lock.release()
        self._inner.shutdown()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_manager(
    backend: str = "auto",
    *,
    lock_timeout: float = 60.0,
    **kwargs: Any,
) -> UnifiedManager:
    """Create a ``UnifiedManager`` from environment and keyword overrides.

    Parameters
    ----------
    backend:
        ``"vice"``, ``"u64"``, or ``"auto"`` (reads ``C64_BACKEND``).
    lock_timeout:
        Cross-process device-lock timeout in seconds (U64 only).
        Defaults to 60.0; long parallel benches typically pass
        ``lock_timeout=1800.0`` (30 min) or higher.
    **kwargs:
        Forwarded to ``UnifiedManager.__init__``.  Useful keys:
        ``vice_config``, ``vice_kwargs``, ``u64_hosts``, ``u64_password``.
    """
    return UnifiedManager(backend=backend, lock_timeout=lock_timeout, **kwargs)
