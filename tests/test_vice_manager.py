"""Tests for ViceInstanceManager — mocked VICE, no emulator required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_manager import (
    PortAllocator,
    ViceInstance,
    ViceInstanceManager,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig


@pytest.fixture
def _mock_vice():
    """Patch ViceProcess and ViceTransport so no real VICE is needed."""
    with (
        patch("c64_test_harness.backends.vice_manager.ViceProcess") as MockProc,
        patch("c64_test_harness.backends.vice_manager.ViceTransport") as MockTrans,
    ):
        MockProc.return_value.wait_for_monitor.return_value = True
        MockProc.return_value.stop = MagicMock()
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
        MockProc.return_value.wait_for_monitor.assert_called_once()
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

    def test_failed_wait_for_monitor_releases_port(self, _mock_vice):
        MockProc, _ = _mock_vice
        MockProc.return_value.wait_for_monitor.return_value = False
        mgr = ViceInstanceManager(port_range_start=18600, port_range_end=18605)
        with pytest.raises(RuntimeError, match="did not become ready"):
            mgr.acquire()
        assert mgr.active_count == 0
        assert len(mgr._allocator.allocated_ports) == 0


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
