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
from .ultimate64_baseline import apply_factory_baseline, baseline_on_entry_enabled
from .vice_lifecycle import ViceConfig
from .vice_manager import ViceInstanceManager

try:
    from .device_lock import (
        DeviceLock,
        DeviceLockTimeout,
        suppress_unlocked_warning,
    )

    _HAS_DEVICE_LOCK = True
except ImportError:  # pragma: no cover
    _HAS_DEVICE_LOCK = False
    DeviceLockTimeout = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

#: The DEBUG line ``_LockedU64Manager.acquire`` logs once it holds the
#: lock; a test that needs proof its log capture saw the lane matches
#: this (``tests/test_unlocked_notice_live.py``).
LANE_LOCKED_PHRASE = "with cross-process lock"


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

    __test__ = False  # not a pytest test class, despite the name

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
    baseline_on_entry:
        U64 only (issue #227).  ``True`` runs
        :func:`~c64_test_harness.backends.ultimate64_baseline.apply_factory_baseline`
        on every ``acquire()`` — right after the ``DeviceLock`` is taken
        and before the transport is handed out — resetting the covered
        config categories to the firmware's factory defaults and
        asserting ``current == default`` per item.  ``None`` (the
        default) reads ``U64_BASELINE_ON_ENTRY``; unset means off, and
        off means no requests at all.  ``HarnessConfig.u64_baseline_on_entry``
        is the TOML/env form — pass it here.  Requires ``DeviceLock``
        to be importable; the manager refuses to run the reset unlocked.
    """

    def __init__(
        self,
        backend: str = "auto",
        vice_config: ViceConfig | None = None,
        vice_kwargs: dict[str, Any] | None = None,
        u64_hosts: str | list[str] | None = None,
        u64_password: str | None = None,
        lock_timeout: float = 60.0,
        memory_policy: "MemoryPolicy | None" = None,
        baseline_on_entry: bool | None = None,
    ) -> None:
        self._backend = self._resolve_backend(backend)
        self._manager: BackendManager
        self._device_lock: Any = None
        if baseline_on_entry is None:
            baseline_on_entry = baseline_on_entry_enabled()
        self._baseline_on_entry = bool(baseline_on_entry)
        # When set, the policy is stamped onto every transport this
        # manager hands out via :meth:`acquire` / :meth:`instance`.
        # ``None`` keeps the transport's existing (permissive) policy
        # untouched, which is the migration default.
        self._memory_policy: MemoryPolicy | None = memory_policy

        if self._backend == "vice":
            kw = dict(vice_kwargs or {})
            self._manager = ViceInstanceManager(
                config=vice_config, **kw,
            )
        elif self._backend == "u64":
            self._manager = self._build_u64_manager(
                u64_hosts, u64_password, lock_timeout=lock_timeout,
                baseline_on_entry=self._baseline_on_entry,
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
        if self._memory_policy is not None:
            instance.transport.memory_policy = self._memory_policy
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
        if self._memory_policy is not None:
            instance.transport.memory_policy = self._memory_policy
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
        baseline_on_entry: bool = False,
    ) -> Any:
        """Build an Ultimate64InstanceManager from host/password config.

        When :class:`DeviceLock` is available, wraps the manager with
        ``_LockedU64Manager`` for cross-process queueing via flock.  The
        entry reset (*baseline_on_entry*, issue #227) lives in that
        wrapper because it must run inside the lock; without
        ``DeviceLock`` it is refused rather than run unlocked.
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
            return _LockedU64Manager(
                inner, lock_timeout=lock_timeout,
                baseline_on_entry=baseline_on_entry,
            )

        if baseline_on_entry:
            raise RuntimeError(
                "baseline_on_entry requires DeviceLock (the entry reset runs "
                "inside the device lock, never on a bare client); device_lock "
                "could not be imported on this host"
            )
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
            3. baseline_on_entry: apply_factory_baseline(client)
               → reset the covered config stores to factory default and
                 assert it (issue #227); on failure release 2 and 1
            4. return instance

        release(instance):
            1. DeviceLock.release()
            2. inner.release(instance)

    """

    def __init__(
        self,
        inner: Any,
        lock_timeout: float = 60.0,
        baseline_on_entry: bool = False,
    ) -> None:
        self._inner = inner
        self._lock_timeout = lock_timeout
        self._baseline_on_entry = bool(baseline_on_entry)
        # Map instance id → DeviceLock so release() can find the right lock.
        self._locks: dict[int, DeviceLock] = {}
        self._map_lock = __import__("threading").Lock()

    def acquire(self) -> Any:
        """Acquire a device with cross-process locking.

        On lock-timeout, raises :class:`DeviceLockTimeout` (a
        ``TimeoutError`` subclass) with structured diagnostics —
        holder PID, liveness, lockfile age, REST reachability — so
        callers can distinguish "queued behind a healthy holder" from
        "device wedged/unreachable" without guessing.
        """
        # The inner pool builds the transport (and with it the
        # Ultimate64Client) before we can know which host to lock, so
        # the client is constructed a moment *before* the lock is taken.
        # Left alone, that would fire the unlocked-client notice on the
        # one path that does the locking correctly — a false positive on
        # exactly the careful lane issue #194 is about.  Suppression is
        # thread-scoped, so an ad-hoc unlocked client built on another
        # thread still gets its notice.
        # (This class is only instantiated when device_lock imported.)
        with suppress_unlocked_warning():
            instance = self._inner.acquire()
        device_host = instance.device.host
        # allow_nested: a caller that already holds this device's lock
        # (a pytest fixture, a bench tool wrapping its whole run) would
        # otherwise queue behind itself for the full lock_timeout and
        # then fail.  The inner manager still guarantees one in-process
        # user per device, so joining the existing hold is safe.
        lock = DeviceLock(device_host, allow_nested=True)
        try:
            lock.acquire_or_raise(timeout=self._lock_timeout)
        except BaseException:
            # Couldn't get cross-process lock — return device to pool.
            self._inner.release(instance)
            raise
        with self._map_lock:
            self._locks[id(instance)] = lock
        logger.debug(
            "Acquired U64 %s " + LANE_LOCKED_PHRASE + " (pid=%d)",
            device_host,
            os.getpid(),
        )
        if self._baseline_on_entry:
            # Inside the lock, before the transport is handed out: the
            # previous lane's leftovers are cleared here, and a reset
            # that does not take fails the acquire rather than the test.
            try:
                report = apply_factory_baseline(instance.transport.client)
            except BaseException:
                with self._map_lock:
                    self._locks.pop(id(instance), None)
                lock.release()
                self._inner.release(instance)
                raise
            logger.info(
                "U64 %s at factory baseline on entry: %s",
                device_host, report.summary(),
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
        ``vice_config``, ``vice_kwargs``, ``u64_hosts``, ``u64_password``,
        ``baseline_on_entry`` (U64 reset-on-entry to factory default,
        issue #227; ``None`` reads ``U64_BASELINE_ON_ENTRY``, off by
        default).
    """
    return UnifiedManager(backend=backend, lock_timeout=lock_timeout, **kwargs)
