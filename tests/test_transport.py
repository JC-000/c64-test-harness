"""Tests for the transport protocol and exception hierarchy (transport.py)."""
from __future__ import annotations

from c64_test_harness.transport import (
    C64Transport,
    ConnectionError,
    TimeoutError,
    TransportError,
)
from conftest import MockTransport


# -- Exception hierarchy -----------------------------------------------------

def test_transport_error_is_exception():
    assert issubclass(TransportError, Exception)


def test_connection_error_hierarchy():
    assert issubclass(ConnectionError, TransportError)
    err = ConnectionError("fail")
    assert isinstance(err, TransportError)
    assert isinstance(err, Exception)


def test_timeout_error_hierarchy():
    assert issubclass(TimeoutError, TransportError)
    err = TimeoutError("slow")
    assert isinstance(err, TransportError)


def test_exception_messages():
    assert str(TransportError("a")) == "a"
    assert str(ConnectionError("b")) == "b"
    assert str(TimeoutError("c")) == "c"


# -- Protocol conformance ---------------------------------------------------

def test_mock_transport_satisfies_protocol():
    t = MockTransport()
    assert isinstance(t, C64Transport)


def test_incomplete_object_fails_protocol():
    class Incomplete:
        pass

    assert not isinstance(Incomplete(), C64Transport)


def test_hardware_base_satisfies_protocol():
    """HardwareTransportBase implements all C64Transport methods."""
    from c64_test_harness.backends.hardware import HardwareTransportBase

    h = HardwareTransportBase()
    assert isinstance(h, C64Transport)
