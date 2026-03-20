"""Tests for PortAllocator — no VICE required."""

import socket
import threading

import pytest

from c64_test_harness.backends.vice_manager import PortAllocator


class TestPortAllocator:
    def test_sequential_allocation(self):
        alloc = PortAllocator(port_range_start=17000, port_range_end=17003)
        p1 = alloc.allocate()
        p2 = alloc.allocate()
        p3 = alloc.allocate()
        assert {p1, p2, p3} == {17000, 17001, 17002}
        # Clean up held sockets
        alloc.release(p1)
        alloc.release(p2)
        alloc.release(p3)

    def test_exhaustion_raises(self):
        alloc = PortAllocator(port_range_start=17100, port_range_end=17102)
        p1 = alloc.allocate()
        p2 = alloc.allocate()
        with pytest.raises(RuntimeError, match="No free ports"):
            alloc.allocate()
        alloc.release(p1)
        alloc.release(p2)

    def test_release_frees_port(self):
        alloc = PortAllocator(port_range_start=17200, port_range_end=17201)
        p = alloc.allocate()
        assert p == 17200
        alloc.release(p)
        p2 = alloc.allocate()
        assert p2 == 17200
        alloc.release(p2)

    def test_allocated_ports_snapshot(self):
        alloc = PortAllocator(port_range_start=17300, port_range_end=17303)
        p1 = alloc.allocate()
        p2 = alloc.allocate()
        snap = alloc.allocated_ports
        assert isinstance(snap, frozenset)
        assert len(snap) == 2
        alloc.release(p1)
        alloc.release(p2)

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
            alloc.release(p)
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
        for p in results:
            alloc.release(p)

    def test_allocate_holds_socket(self):
        """Allocated port is held at the OS level via bind()."""
        alloc = PortAllocator(port_range_start=17700, port_range_end=17701)
        p = alloc.allocate()
        assert p == 17700
        # Another bind attempt on the same port should fail
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 17700))
            probe.close()
            pytest.fail("Expected bind to fail — port should be held")
        except OSError:
            pass  # Expected: port is held by the allocator
        finally:
            probe.close()
            alloc.release(p)

    def test_take_socket_returns_reservation(self):
        """take_socket() returns the held socket and removes it."""
        alloc = PortAllocator(port_range_start=17710, port_range_end=17711)
        p = alloc.allocate()
        sock = alloc.take_socket(p)
        assert sock is not None
        assert sock.fileno() != -1  # still open
        # Second call returns None
        assert alloc.take_socket(p) is None
        sock.close()
        alloc.release(p)

    def test_release_closes_held_socket(self):
        """release() closes the reservation socket if not taken."""
        alloc = PortAllocator(port_range_start=17720, port_range_end=17721)
        p = alloc.allocate()
        alloc.release(p)
        # Port should be free for binding again
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 17720))
        finally:
            probe.close()

    def test_cross_process_safety(self):
        """Two allocators on the same range get different ports."""
        alloc1 = PortAllocator(port_range_start=17730, port_range_end=17732)
        alloc2 = PortAllocator(port_range_start=17730, port_range_end=17732)
        p1 = alloc1.allocate()
        p2 = alloc2.allocate()
        assert p1 != p2
        alloc1.release(p1)
        alloc2.release(p2)

    def test_allocate_acquires_file_lock(self):
        """Allocated port has a file lock held by the allocator."""
        from c64_test_harness.backends.port_lock import PortLock
        alloc = PortAllocator(port_range_start=17740, port_range_end=17741)
        p = alloc.allocate()
        # Another PortLock on the same port should fail
        rival = PortLock(p)
        assert not rival.acquire()
        alloc.release(p)
        # After release, the lock should be available
        assert rival.acquire()
        rival.release()

    def test_take_lock_returns_and_removes(self):
        """take_lock() returns the held lock and removes it."""
        alloc = PortAllocator(port_range_start=17750, port_range_end=17751)
        p = alloc.allocate()
        lock = alloc.take_lock(p)
        assert lock is not None
        assert lock.held
        # Second call returns None
        assert alloc.take_lock(p) is None
        lock.release()
        alloc.release(p)

    def test_release_releases_file_lock(self):
        """release() releases the file lock."""
        from c64_test_harness.backends.port_lock import PortLock
        alloc = PortAllocator(port_range_start=17760, port_range_end=17761)
        p = alloc.allocate()
        alloc.release(p)
        # Lock should be free now
        lock = PortLock(p)
        assert lock.acquire()
        lock.release()

    def test_file_lock_blocks_concurrent_allocator(self):
        """A port held by file lock is skipped by another allocator."""
        from c64_test_harness.backends.port_lock import PortLock
        # Hold a file lock on the first port manually
        lock = PortLock(17770)
        lock.acquire()
        try:
            alloc = PortAllocator(port_range_start=17770, port_range_end=17772)
            p = alloc.allocate()
            assert p == 17771  # Should skip 17770 (file-locked)
            alloc.release(p)
        finally:
            lock.release()
