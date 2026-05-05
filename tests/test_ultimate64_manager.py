"""Unit tests for Ultimate64InstanceManager (no network)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.ultimate64_manager import (
    Ultimate64Device,
    Ultimate64Instance,
    Ultimate64InstanceManager,
    Ultimate64ManagerError,
    Ultimate64PoolExhaustedError,
)


@pytest.fixture
def mock_transport():
    """Patch Ultimate64Transport and probe_u64 in the manager module."""
    with patch(
        "c64_test_harness.backends.ultimate64_manager.Ultimate64Transport"
    ) as m, patch(
        "c64_test_harness.backends.ultimate64_manager.probe_u64"
    ) as mock_probe:
        m.side_effect = lambda **kwargs: MagicMock(name="Ultimate64Transport",
                                                    _kwargs=kwargs)
        # Default: all probes succeed.
        from c64_test_harness.backends.ultimate64_probe import ProbeResult
        mock_probe.side_effect = lambda host, **kw: ProbeResult(
            host=host, port=kw.get("port", 80), reachable=True,
            ping_ok=True, port_ok=True, api_ok=None,
            latency_ms=1.0, error=None,
        )
        yield m


def _devices(n: int) -> list[Ultimate64Device]:
    return [Ultimate64Device(host=f"10.0.0.{i+10}") for i in range(n)]


# --- Ultimate64Device ------------------------------------------------


def test_device_defaults():
    d = Ultimate64Device(host="1.2.3.4")
    assert d.host == "1.2.3.4"
    assert d.password is None
    assert d.port == 80
    assert d.timeout == 10.0
    assert d.name == ""
    assert d.label == "1.2.3.4"


def test_device_label_uses_name_when_set():
    d = Ultimate64Device(host="1.2.3.4", name="lab-unit-a")
    assert d.label == "lab-unit-a"


def test_device_is_frozen():
    d = Ultimate64Device(host="1.2.3.4")
    with pytest.raises(Exception):
        d.host = "5.6.7.8"  # type: ignore[misc]


# --- Instance.stop idempotency ---------------------------------------


def test_instance_stop_closes_transport(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    inst = mgr.acquire()
    inst.stop()
    inst.transport.close.assert_called_once()


def test_instance_stop_idempotent(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    inst = mgr.acquire()
    inst.stop()
    inst.stop()
    inst.stop()
    inst.transport.close.assert_called_once()


def test_instance_pid_always_none(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    inst = mgr.acquire()
    assert inst.pid is None


# --- Manager construction --------------------------------------------


def test_empty_devices_raises():
    with pytest.raises(ValueError):
        Ultimate64InstanceManager([])


def test_no_network_in_init(mock_transport):
    Ultimate64InstanceManager(_devices(3))
    mock_transport.assert_not_called()


def test_pool_size_and_initial_available(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(3))
    assert mgr.pool_size == 3
    assert mgr.available_count == 3
    assert mgr.active_count == 0


# --- Single-device acquire/release cycle -----------------------------


def test_acquire_creates_transport_with_device_kwargs(mock_transport):
    dev = Ultimate64Device(
        host="1.2.3.4", password="pw", port=8080, timeout=5.0,
    )
    mgr = Ultimate64InstanceManager([dev])
    inst = mgr.acquire()
    mock_transport.assert_called_once_with(
        host="1.2.3.4", password="pw", port=8080, timeout=5.0,
    )
    assert inst.device is dev


def test_acquire_release_cycle(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    inst1 = mgr.acquire()
    assert mgr.available_count == 0
    assert mgr.active_count == 1
    mgr.release(inst1)
    assert mgr.available_count == 1
    assert mgr.active_count == 0
    inst2 = mgr.acquire()
    assert inst2.device is inst1.device
    inst1.transport.close.assert_called_once()


# --- Multi-device pool -----------------------------------------------


def test_multi_device_pool_distributes(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(3))
    a = mgr.acquire()
    b = mgr.acquire()
    c = mgr.acquire()
    got = {a.device.host, b.device.host, c.device.host}
    assert got == {"10.0.0.10", "10.0.0.11", "10.0.0.12"}
    assert mgr.available_count == 0


def test_exhaustion_raises_with_timeout(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(3), acquire_timeout=0.2)
    mgr.acquire()
    mgr.acquire()
    mgr.acquire()
    t0 = time.monotonic()
    with pytest.raises(Ultimate64PoolExhaustedError):
        mgr.acquire()
    elapsed = time.monotonic() - t0
    assert 0.15 <= elapsed < 1.5


# --- Release makes device available ----------------------------------


def test_release_allows_reacquire(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(2), acquire_timeout=0.5)
    a = mgr.acquire()
    b = mgr.acquire()
    # Pool exhausted now.
    mgr.release(a)
    c = mgr.acquire()
    assert c.device is a.device
    mgr.release(b)
    mgr.release(c)


def test_double_release_safe(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    inst = mgr.acquire()
    mgr.release(inst)
    mgr.release(inst)  # no-op, no raise
    assert mgr.available_count == 1


# --- Context managers ------------------------------------------------


def test_manager_context_calls_shutdown(mock_transport):
    with Ultimate64InstanceManager(_devices(2)) as mgr:
        inst = mgr.acquire()
    # After exit, shutdown was called and transport closed.
    inst.transport.close.assert_called_once()
    # Post-shutdown acquire raises.
    with pytest.raises(Ultimate64ManagerError):
        mgr.acquire()


def test_instance_context_auto_releases(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    with mgr.instance() as inst:
        assert mgr.available_count == 0
        saved = inst
    assert mgr.available_count == 1
    saved.transport.close.assert_called_once()


def test_instance_context_releases_on_exception(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    with pytest.raises(RuntimeError):
        with mgr.instance():
            raise RuntimeError("boom")
    assert mgr.available_count == 1


# --- Concurrent acquisition ------------------------------------------


def test_concurrent_acquire_queues_until_release(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(2), acquire_timeout=2.0)
    barrier = threading.Barrier(4)
    peak_active = [0]
    peak_lock = threading.Lock()

    def worker():
        barrier.wait()
        inst = mgr.acquire()
        with peak_lock:
            peak_active[0] = max(peak_active[0], mgr.active_count)
        time.sleep(0.1)
        mgr.release(inst)
        return inst.device.host

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker) for _ in range(4)]
        results = [f.result(timeout=5.0) for f in futures]

    assert len(results) == 4
    # At no time should more than 2 be active concurrently.
    assert peak_active[0] <= 2
    assert peak_active[0] == 2  # must have hit the cap
    assert mgr.available_count == 2


def test_concurrent_acquire_blocks_then_unblocks(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1), acquire_timeout=2.0)
    a = mgr.acquire()

    acquired = threading.Event()
    result = []

    def waiter():
        inst = mgr.acquire()
        acquired.set()
        result.append(inst)

    t = threading.Thread(target=waiter)
    t.start()
    # Thread should be blocked waiting.
    assert not acquired.wait(timeout=0.2)
    mgr.release(a)
    assert acquired.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert len(result) == 1
    assert result[0].device is a.device
    mgr.release(result[0])


# --- Shutdown --------------------------------------------------------


def test_shutdown_closes_all_transports(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(3))
    a = mgr.acquire()
    b = mgr.acquire()
    mgr.shutdown()
    a.transport.close.assert_called_once()
    b.transport.close.assert_called_once()
    assert mgr.active_count == 0
    assert mgr.available_count == 0


def test_shutdown_idempotent(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(2))
    inst = mgr.acquire()
    mgr.shutdown()
    mgr.shutdown()
    inst.transport.close.assert_called_once()


def test_shutdown_unblocks_waiters(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1), acquire_timeout=5.0)
    mgr.acquire()  # exhaust pool

    exc: list[BaseException] = []

    def waiter():
        try:
            mgr.acquire()
        except BaseException as e:
            exc.append(e)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    mgr.shutdown()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert len(exc) == 1
    assert isinstance(exc[0], Ultimate64ManagerError)


def test_acquire_after_shutdown_raises(mock_transport):
    mgr = Ultimate64InstanceManager(_devices(1))
    mgr.shutdown()
    with pytest.raises(Ultimate64ManagerError):
        mgr.acquire()
