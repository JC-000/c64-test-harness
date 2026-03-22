"""Tests for ViceInstanceManager — mocked VICE, no emulator required."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from c64_test_harness.backends.vice_manager import (
    PortAllocator,
    ViceInstance,
    ViceInstanceManager,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig


@pytest.fixture
def _mock_vice():
    """Patch ViceProcess and BinaryViceTransport so no real VICE is needed."""
    with (
        patch("c64_test_harness.backends.vice_manager.ViceProcess") as MockProc,
        patch("c64_test_harness.backends.vice_manager.BinaryViceTransport") as MockTrans,
    ):
        MockProc.return_value.stop = MagicMock()
        MockProc.return_value.pid = 12345
        # Simulate process still running (poll returns None)
        mock_popen = MagicMock()
        mock_popen.poll.return_value = None
        MockProc.return_value._proc = mock_popen
        # get_listener_pid returns None by default (skip PID verification)
        MockProc.get_listener_pid = MagicMock(return_value=None)
        MockTrans.return_value.close = MagicMock()
        yield MockProc, MockTrans


class TestViceInstanceManager:
    def test_acquire_starts_process(self, _mock_vice):
        MockProc, MockTrans = _mock_vice
        mgr = ViceInstanceManager(port_range_start=18000, port_range_end=18005)
        inst = mgr.acquire()
        assert inst.port == 18000
        assert inst.managed is True
        MockProc.return_value.start.assert_called_once()
        # Binary transport should be connected
        MockTrans.assert_called()
        assert mgr.active_count == 1
        mgr.release(inst)
        assert mgr.active_count == 0

    def test_multiple_acquire(self, _mock_vice):
        mgr = ViceInstanceManager(port_range_start=18100, port_range_end=18105)
        i1 = mgr.acquire()
        i2 = mgr.acquire()
        assert i1.port != i2.port
        assert mgr.active_count == 2
        mgr.shutdown()
        assert mgr.active_count == 0

    def test_shutdown_stops_all(self, _mock_vice):
        MockProc, MockTrans = _mock_vice
        mgr = ViceInstanceManager(port_range_start=18200, port_range_end=18205)
        mgr.acquire()
        mgr.acquire()
        mgr.shutdown()
        assert mgr.active_count == 0

    def test_context_manager(self, _mock_vice):
        with ViceInstanceManager(port_range_start=18300, port_range_end=18305) as mgr:
            inst = mgr.acquire()
            assert mgr.active_count == 1
        # __exit__ calls shutdown
        assert mgr.active_count == 0

    def test_instance_context_manager(self, _mock_vice):
        mgr = ViceInstanceManager(port_range_start=18400, port_range_end=18405)
        with mgr.instance() as inst:
            assert inst.port == 18400
            assert mgr.active_count == 1
        assert mgr.active_count == 0

    def test_reuse_existing_adopts_listener(self, _mock_vice):
        MockProc, MockTrans = _mock_vice
        with patch.object(PortAllocator, "is_port_in_use", return_value=True):
            mgr = ViceInstanceManager(
                port_range_start=18500, port_range_end=18505,
                reuse_existing=True,
            )
            inst = mgr.acquire()
            assert inst.managed is False
            assert inst.process is None
            MockProc.return_value.start.assert_not_called()
        mgr.shutdown()

    def test_failed_binary_connect_releases_port(self, _mock_vice):
        MockProc, MockTrans = _mock_vice
        # Binary transport always fails to connect
        MockTrans.side_effect = ConnectionError("refused")
        # Process exits immediately to break retry loop
        MockProc.return_value._proc.poll.return_value = 1
        mgr = ViceInstanceManager(port_range_start=18600, port_range_end=18605)
        with pytest.raises(RuntimeError, match="exited during"):
            mgr.acquire()
        assert mgr.active_count == 0
        assert len(mgr._allocator.allocated_ports) == 0

    def test_acquire_retries_on_failure(self, _mock_vice):
        MockProc, MockTrans = _mock_vice
        # First attempt: process exits; second: succeeds
        call_count = [0]
        original_poll = MockProc.return_value._proc.poll

        def poll_side_effect():
            call_count[0] += 1
            # First call for first acquire attempt - process exits
            if call_count[0] <= 2:
                return 1  # exited
            return None  # running

        MockProc.return_value._proc.poll.side_effect = poll_side_effect
        mgr = ViceInstanceManager(
            port_range_start=18700, port_range_end=18710, max_retries=3,
        )
        inst = mgr.acquire()
        assert inst.port in range(18700, 18710)
        mgr.release(inst)

    def test_acquire_exhausts_retries(self, _mock_vice):
        MockProc, MockTrans = _mock_vice
        # Process always exits immediately
        MockProc.return_value._proc.poll.return_value = 1
        MockTrans.side_effect = ConnectionError("refused")
        mgr = ViceInstanceManager(
            port_range_start=18800, port_range_end=18810, max_retries=2,
        )
        with pytest.raises(RuntimeError):
            mgr.acquire()
        assert mgr.active_count == 0

    def test_pid_mismatch_raises(self, _mock_vice):
        MockProc, _ = _mock_vice
        MockProc.return_value.pid = 1234
        with patch(
            "c64_test_harness.backends.vice_manager.ViceProcess.get_listener_pid",
            return_value=5678,
        ):
            mgr = ViceInstanceManager(
                port_range_start=18900, port_range_end=18910, max_retries=1,
            )
            with pytest.raises(RuntimeError, match="PID mismatch"):
                mgr.acquire()

    def test_instance_holds_port_lock(self, _mock_vice):
        mgr = ViceInstanceManager(port_range_start=19000, port_range_end=19010)
        inst = mgr.acquire()
        assert inst._port_lock is not None
        assert inst._port_lock.held
        mgr.release(inst)

    def test_instance_stop_releases_port_lock(self, _mock_vice):
        mgr = ViceInstanceManager(port_range_start=19100, port_range_end=19110)
        inst = mgr.acquire()
        lock = inst._port_lock
        assert lock is not None
        inst.stop()
        assert not lock.held


class TestViceInstance:
    def test_stop_closes_transport_and_process(self):
        proc = MagicMock()
        transport = MagicMock()
        inst = ViceInstance(port=9999, process=proc, transport=transport, managed=True)
        inst.stop()
        transport.close.assert_called_once()
        proc.stop.assert_called_once()

    def test_stop_unmanaged_skips_process(self):
        transport = MagicMock()
        inst = ViceInstance(port=9999, process=None, transport=transport, managed=False)
        inst.stop()
        transport.close.assert_called_once()

    def test_stop_releases_port_lock(self):
        from c64_test_harness.backends.port_lock import PortLock
        transport = MagicMock()
        lock = MagicMock(spec=PortLock)
        inst = ViceInstance(
            port=9999, process=MagicMock(), transport=transport,
            managed=True, _port_lock=lock,
        )
        inst.stop()
        lock.release.assert_called_once()
