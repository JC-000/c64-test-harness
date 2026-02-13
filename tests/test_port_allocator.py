"""Tests for PortAllocator — no VICE required."""

import socket
import threading

from c64_test_harness.backends.vice_manager import PortAllocator


class TestPortAllocator:
    def test_sequential_allocation(self):
        alloc = PortAllocator(port_range_start=17000, port_range_end=17003)
        p1 = alloc.allocate()
        p2 = alloc.allocate()
        p3 = alloc.allocate()
        assert {p1, p2, p3} == {17000, 17001, 17002}

    def test_exhaustion_raises(self):
        alloc = PortAllocator(port_range_start=17100, port_range_end=17102)
        alloc.allocate()
        alloc.allocate()
        try:
            alloc.allocate()
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "No free ports" in str(e)

    def test_release_frees_port(self):
        alloc = PortAllocator(port_range_start=17200, port_range_end=17201)
        p = alloc.allocate()
        assert p == 17200
        alloc.release(p)
        p2 = alloc.allocate()
        assert p2 == 17200

    def test_allocated_ports_snapshot(self):
        alloc = PortAllocator(port_range_start=17300, port_range_end=17303)
        alloc.allocate()
        alloc.allocate()
        snap = alloc.allocated_ports
        assert isinstance(snap, frozenset)
        assert len(snap) == 2

    def test_is_port_in_use_with_listener(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 17400))
        srv.listen(1)
        try:
            assert PortAllocator.is_port_in_use(17400)
            assert not PortAllocator.is_port_in_use(17401)
        finally:
            srv.close()

    def test_skips_occupied_ports(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 17500))
        srv.listen(1)
        try:
            alloc = PortAllocator(port_range_start=17500, port_range_end=17502)
            p = alloc.allocate()
            assert p == 17501
        finally:
            srv.close()

    def test_thread_safety(self):
        alloc = PortAllocator(port_range_start=17600, port_range_end=17610)
        results: list[int] = []
        errors: list[Exception] = []

        def grab():
            try:
                results.append(alloc.allocate())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=grab) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 10
        assert len(set(results)) == 10
