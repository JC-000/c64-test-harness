"""Tests for the unified backend manager (no real VICE or U64 needed)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call, patch

import pytest

from c64_test_harness.backends.unified_manager import (
    BackendManager,
    TestTarget,
    UnifiedManager,
    _LockedU64Manager,
    create_manager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_vice_instance(pid: int = 1234) -> MagicMock:
    """Build a mock that looks like a ViceInstance."""
    inst = MagicMock()
    inst.pid = pid
    inst.transport = MagicMock()
    return inst


def _make_mock_u64_instance(host: str = "192.168.1.81") -> MagicMock:
    """Build a mock that looks like an Ultimate64Instance."""
    inst = MagicMock()
    inst.pid = None
    inst.transport = MagicMock()
    inst.device.host = host
    return inst


# ---------------------------------------------------------------------------
# BackendManager protocol
# ---------------------------------------------------------------------------

class TestBackendManagerProtocol:
    """Verify BackendManager is a structural protocol."""

    def test_mock_satisfies_protocol(self) -> None:
        mgr = MagicMock()
        mgr.acquire = MagicMock()
        mgr.release = MagicMock()
        mgr.shutdown = MagicMock()
        assert isinstance(mgr, BackendManager)


# ---------------------------------------------------------------------------
# TestTarget
# ---------------------------------------------------------------------------

class TestTestTarget:
    def test_fields(self) -> None:
        transport = MagicMock()
        target = TestTarget(transport=transport, backend="vice", pid=42)
        assert target.transport is transport
        assert target.backend == "vice"
        assert target.pid == 42

    def test_pid_defaults_to_none(self) -> None:
        target = TestTarget(transport=MagicMock(), backend="u64")
        assert target.pid is None


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

class TestBackendResolution:
    def test_auto_defaults_to_vice(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "C64_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            assert UnifiedManager._resolve_backend("auto") == "vice"

    def test_auto_reads_env(self) -> None:
        with patch.dict(os.environ, {"C64_BACKEND": "u64"}):
            assert UnifiedManager._resolve_backend("auto") == "u64"

    def test_auto_reads_env_case_insensitive(self) -> None:
        with patch.dict(os.environ, {"C64_BACKEND": "U64"}):
            assert UnifiedManager._resolve_backend("auto") == "u64"

    def test_explicit_vice(self) -> None:
        assert UnifiedManager._resolve_backend("vice") == "vice"

    def test_explicit_u64(self) -> None:
        assert UnifiedManager._resolve_backend("u64") == "u64"

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown backend"):
            UnifiedManager._resolve_backend("c128")


# ---------------------------------------------------------------------------
# U64 host parsing
# ---------------------------------------------------------------------------

class TestU64HostParsing:
    def test_single_host_string(self) -> None:
        assert UnifiedManager._parse_u64_hosts("192.168.1.81") == ["192.168.1.81"]

    def test_comma_separated(self) -> None:
        result = UnifiedManager._parse_u64_hosts("10.0.0.1, 10.0.0.2, 10.0.0.3")
        assert result == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_list_passthrough(self) -> None:
        hosts = ["a.local", "b.local"]
        assert UnifiedManager._parse_u64_hosts(hosts) == hosts

    def test_none_reads_env(self) -> None:
        with patch.dict(os.environ, {"U64_HOST": "192.168.1.81"}):
            assert UnifiedManager._parse_u64_hosts(None) == ["192.168.1.81"]

    def test_none_no_env_returns_empty(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "U64_HOST"}
        with patch.dict(os.environ, env, clear=True):
            assert UnifiedManager._parse_u64_hosts(None) == []

    def test_empty_string(self) -> None:
        assert UnifiedManager._parse_u64_hosts("") == []

    def test_multiple_hosts_from_env(self) -> None:
        with patch.dict(os.environ, {"U64_HOST": "10.0.0.1,10.0.0.2"}):
            result = UnifiedManager._parse_u64_hosts(None)
            assert result == ["10.0.0.1", "10.0.0.2"]


# ---------------------------------------------------------------------------
# Factory: VICE backend
# ---------------------------------------------------------------------------

class TestViceBackend:
    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_creates_vice_manager(self, mock_vim_cls: MagicMock) -> None:
        mgr = UnifiedManager(backend="vice")
        mock_vim_cls.assert_called_once()
        assert mgr.backend == "vice"

    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_acquire_delegates(self, mock_vim_cls: MagicMock) -> None:
        mock_inst = _make_mock_vice_instance(pid=5678)
        mock_vim_cls.return_value.acquire.return_value = mock_inst

        mgr = UnifiedManager(backend="vice")
        target = mgr.acquire()

        assert target.backend == "vice"
        assert target.pid == 5678
        assert target.transport is mock_inst.transport

    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_shutdown_delegates(self, mock_vim_cls: MagicMock) -> None:
        mgr = UnifiedManager(backend="vice")
        mgr.shutdown()
        mock_vim_cls.return_value.shutdown.assert_called_once()

    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_context_manager_shuts_down(self, mock_vim_cls: MagicMock) -> None:
        with UnifiedManager(backend="vice") as mgr:
            assert mgr.backend == "vice"
        mock_vim_cls.return_value.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# Factory: U64 backend
# ---------------------------------------------------------------------------

class TestU64Backend:
    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_creates_u64_manager(self, mock_build: MagicMock) -> None:
        mock_build.return_value = MagicMock()
        mgr = UnifiedManager(backend="u64", u64_hosts="10.0.0.1")
        mock_build.assert_called_once_with("10.0.0.1", None, lock_timeout=60.0)
        assert mgr.backend == "u64"

    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_acquire_u64_has_no_pid(self, mock_build: MagicMock) -> None:
        mock_inst = _make_mock_u64_instance()
        mock_mgr = MagicMock()
        mock_mgr.acquire.return_value = mock_inst
        mock_build.return_value = mock_mgr

        mgr = UnifiedManager(backend="u64", u64_hosts="10.0.0.1")
        target = mgr.acquire()

        assert target.backend == "u64"
        assert target.pid is None

    def test_u64_no_host_raises(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "U64_HOST"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="at least one host"):
                UnifiedManager(backend="u64")

    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_u64_password_from_env(self, mock_build: MagicMock) -> None:
        mock_build.return_value = MagicMock()
        with patch.dict(os.environ, {"U64_PASSWORD": "secret"}):
            UnifiedManager(backend="u64", u64_hosts="10.0.0.1")
        mock_build.assert_called_once_with("10.0.0.1", None, lock_timeout=60.0)


# ---------------------------------------------------------------------------
# Context manager: instance()
# ---------------------------------------------------------------------------

class TestInstanceContextManager:
    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_instance_yields_target(self, mock_vim_cls: MagicMock) -> None:
        mock_inst = _make_mock_vice_instance()
        mock_vim_cls.return_value.acquire.return_value = mock_inst

        mgr = UnifiedManager(backend="vice")
        with mgr.instance() as target:
            assert isinstance(target, TestTarget)
            assert target.backend == "vice"

        # Release was called with the raw instance
        mock_vim_cls.return_value.release.assert_called_once_with(mock_inst)

    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_instance_releases_on_exception(self, mock_vim_cls: MagicMock) -> None:
        mock_inst = _make_mock_vice_instance()
        mock_vim_cls.return_value.acquire.return_value = mock_inst

        mgr = UnifiedManager(backend="vice")
        with pytest.raises(RuntimeError):
            with mgr.instance() as target:
                raise RuntimeError("boom")

        mock_vim_cls.return_value.release.assert_called_once_with(mock_inst)


# ---------------------------------------------------------------------------
# create_manager factory
# ---------------------------------------------------------------------------

class TestCreateManager:
    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_creates_vice_by_default(self, mock_vim_cls: MagicMock) -> None:
        env = {k: v for k, v in os.environ.items() if k != "C64_BACKEND"}
        with patch.dict(os.environ, env, clear=True):
            mgr = create_manager()
        assert mgr.backend == "vice"

    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_creates_u64_from_env(self, mock_build: MagicMock) -> None:
        mock_build.return_value = MagicMock()
        with patch.dict(os.environ, {"C64_BACKEND": "u64", "U64_HOST": "10.0.0.1"}):
            mgr = create_manager()
        assert mgr.backend == "u64"

    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_kwargs_forwarded(self, mock_vim_cls: MagicMock) -> None:
        from c64_test_harness.backends.vice_lifecycle import ViceConfig
        cfg = ViceConfig()
        mgr = create_manager(backend="vice", vice_config=cfg)
        mock_vim_cls.assert_called_once_with(config=cfg)
        assert mgr.backend == "vice"


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

class TestPublicExports:
    def test_importable_from_package(self) -> None:
        from c64_test_harness import (
            BackendManager,
            TestTarget,
            UnifiedManager,
            create_manager,
        )
        assert TestTarget is not None
        assert BackendManager is not None
        assert UnifiedManager is not None
        assert create_manager is not None


# ---------------------------------------------------------------------------
# _LockedU64Manager — cross-process queueing wrapper
# ---------------------------------------------------------------------------

class TestLockedU64Manager:
    """Tests for _LockedU64Manager cross-process locking wrapper."""

    def _make_inner(self, instances: list[MagicMock] | None = None) -> MagicMock:
        """Build a mock Ultimate64InstanceManager."""
        inner = MagicMock()
        if instances:
            inner.acquire.side_effect = instances
        else:
            inner.acquire.return_value = _make_mock_u64_instance()
        return inner

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_acquire_delegates_and_locks(self, MockDeviceLock: MagicMock) -> None:
        """acquire() gets an instance from inner, then acquires DeviceLock."""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.held = True
        MockDeviceLock.return_value = mock_lock

        inst = _make_mock_u64_instance("10.0.0.1")
        inner = self._make_inner([inst])
        mgr = _LockedU64Manager(inner)

        result = mgr.acquire()

        assert result is inst
        inner.acquire.assert_called_once()
        MockDeviceLock.assert_called_once_with("10.0.0.1")
        mock_lock.acquire.assert_called_once_with(timeout=60.0)

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_release_unlocks_and_delegates(self, MockDeviceLock: MagicMock) -> None:
        """release() releases DeviceLock then delegates to inner."""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.held = True
        MockDeviceLock.return_value = mock_lock

        inst = _make_mock_u64_instance()
        inner = self._make_inner([inst])
        mgr = _LockedU64Manager(inner)

        acquired = mgr.acquire()
        mgr.release(acquired)

        mock_lock.release.assert_called_once()
        inner.release.assert_called_once_with(inst)

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_acquire_timeout_returns_to_pool_and_raises(
        self, MockDeviceLock: MagicMock,
    ) -> None:
        """When DeviceLock.acquire() returns False, instance goes back to pool."""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_lock.held = False
        MockDeviceLock.return_value = mock_lock

        inst = _make_mock_u64_instance("10.0.0.5")
        inner = self._make_inner([inst])
        mgr = _LockedU64Manager(inner, lock_timeout=5.0)

        with pytest.raises(RuntimeError, match="Timed out.*10.0.0.5.*5.0s"):
            mgr.acquire()

        # Instance was returned to the pool exactly once
        inner.release.assert_called_once_with(inst)

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_shutdown_releases_all_locks(self, MockDeviceLock: MagicMock) -> None:
        """shutdown() releases every held lock and delegates to inner."""
        locks = []
        def make_lock(host):
            lock = MagicMock()
            lock.acquire.return_value = True
            lock.held = True
            locks.append(lock)
            return lock
        MockDeviceLock.side_effect = make_lock

        inst_a = _make_mock_u64_instance("10.0.0.1")
        inst_b = _make_mock_u64_instance("10.0.0.2")
        inner = self._make_inner([inst_a, inst_b])
        mgr = _LockedU64Manager(inner)

        mgr.acquire()
        mgr.acquire()

        mgr.shutdown()

        for lock in locks:
            lock.release.assert_called_once()
        inner.shutdown.assert_called_once()

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_multiple_devices_get_independent_locks(
        self, MockDeviceLock: MagicMock,
    ) -> None:
        """Each device gets its own DeviceLock keyed by host."""
        created_locks: dict[str, MagicMock] = {}
        def make_lock(host):
            lock = MagicMock()
            lock.acquire.return_value = True
            lock.held = True
            created_locks[host] = lock
            return lock
        MockDeviceLock.side_effect = make_lock

        inst_a = _make_mock_u64_instance("10.0.0.1")
        inst_b = _make_mock_u64_instance("10.0.0.2")
        inner = self._make_inner([inst_a, inst_b])
        mgr = _LockedU64Manager(inner)

        mgr.acquire()
        mgr.acquire()

        assert "10.0.0.1" in created_locks
        assert "10.0.0.2" in created_locks
        assert len(created_locks) == 2

        # Release only first — second lock stays held
        mgr.release(inst_a)
        created_locks["10.0.0.1"].release.assert_called_once()
        created_locks["10.0.0.2"].release.assert_not_called()

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    @patch(
        "c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
        create=True,
    )
    @patch(
        "c64_test_harness.backends.unified_manager.Ultimate64Device",
        create=True,
    )
    def test_build_u64_manager_uses_locked_when_available(
        self,
        MockDevice: MagicMock,
        MockU64Mgr: MagicMock,
        MockDeviceLock: MagicMock,
    ) -> None:
        """_build_u64_manager wraps with _LockedU64Manager when _HAS_DEVICE_LOCK is True."""
        with patch(
            "c64_test_harness.backends.unified_manager._HAS_DEVICE_LOCK", True,
        ):
            result = UnifiedManager._build_u64_manager(["10.0.0.1"], None)

        assert isinstance(result, _LockedU64Manager)


# ---------------------------------------------------------------------------
# TestTarget.client accessor (issue #76)
# ---------------------------------------------------------------------------

class TestTargetClientAccessor:
    """The public client property is U64-only."""

    def test_target_client_accessor_u64(self) -> None:
        from c64_test_harness.backends.ultimate64 import Ultimate64Transport

        transport = MagicMock(spec=Ultimate64Transport)
        sentinel_client = MagicMock(name="Ultimate64Client")
        transport._client = sentinel_client

        target = TestTarget(transport=transport, backend="u64", pid=None)
        assert target.client is sentinel_client

    def test_target_client_accessor_vice_raises(self) -> None:
        # A bare MagicMock is not an Ultimate64Transport instance.
        target = TestTarget(transport=MagicMock(), backend="vice", pid=42)
        with pytest.raises(
            AttributeError,
            match="client accessor is U64-only; this target is VICE-backed",
        ):
            target.client


# ---------------------------------------------------------------------------
# create_manager threads lock_timeout (issue #77)
# ---------------------------------------------------------------------------

class TestCreateManagerLockTimeout:
    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_create_manager_threads_lock_timeout(
        self, mock_build: MagicMock,
    ) -> None:
        mock_build.return_value = MagicMock()
        create_manager(backend="u64", u64_hosts="10.0.0.1", lock_timeout=1234.5)
        mock_build.assert_called_once_with(
            "10.0.0.1", None, lock_timeout=1234.5,
        )

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    @patch(
        "c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
        create=True,
    )
    @patch(
        "c64_test_harness.backends.unified_manager.Ultimate64Device",
        create=True,
    )
    def test_lock_timeout_reaches_locked_manager(
        self,
        MockDevice: MagicMock,
        MockU64Mgr: MagicMock,
        MockDeviceLock: MagicMock,
    ) -> None:
        with patch(
            "c64_test_harness.backends.unified_manager._HAS_DEVICE_LOCK", True,
        ):
            result = UnifiedManager._build_u64_manager(
                ["10.0.0.1"], None, lock_timeout=999.0,
            )
        assert isinstance(result, _LockedU64Manager)
        assert result._lock_timeout == 999.0


# ---------------------------------------------------------------------------
# Bare acquire/release round-trip (issue #79)
# ---------------------------------------------------------------------------

class TestBareAcquireReleaseRoundTrip:
    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_bare_acquire_release_round_trip_vice(
        self, mock_vim_cls: MagicMock,
    ) -> None:
        mock_inst = _make_mock_vice_instance()
        mock_vim_cls.return_value.acquire.return_value = mock_inst

        mgr = UnifiedManager(backend="vice")
        target = mgr.acquire()
        # Must NOT raise — regression for #79.
        mgr.release(target)

        mock_vim_cls.return_value.release.assert_called_once_with(mock_inst)

    @patch("c64_test_harness.backends.unified_manager.UnifiedManager._build_u64_manager")
    def test_bare_acquire_release_round_trip_u64(
        self, mock_build: MagicMock,
    ) -> None:
        mock_inst = _make_mock_u64_instance()
        mock_mgr = MagicMock()
        mock_mgr.acquire.return_value = mock_inst
        mock_build.return_value = mock_mgr

        mgr = UnifiedManager(backend="u64", u64_hosts="10.0.0.1")
        target = mgr.acquire()
        mgr.release(target)

        mock_mgr.release.assert_called_once_with(mock_inst)

    @patch("c64_test_harness.backends.unified_manager.ViceInstanceManager")
    def test_instance_context_manager_unaffected(
        self, mock_vim_cls: MagicMock,
    ) -> None:
        mock_inst = _make_mock_vice_instance()
        mock_vim_cls.return_value.acquire.return_value = mock_inst

        mgr = UnifiedManager(backend="vice")
        with mgr.instance() as target:
            assert isinstance(target, TestTarget)
            assert target.backend == "vice"

        mock_vim_cls.return_value.release.assert_called_once_with(mock_inst)
