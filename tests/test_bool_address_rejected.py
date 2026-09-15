"""A ``bool`` is not a C64 address (#340).

``bool`` subclasses ``int``, so ``True``/``False`` passed every
``isinstance(address, int)`` range check and went out as ``address=0001`` /
``address=0000``.  ``$0001`` is the 6510 processor port: a flag passed
positionally where an address was expected silently rewrote the memory map
with HTTP 200.  Same family as #251 -- a malformed argument reported as
success.

Every harness entry point that takes a U64 address now refuses a ``bool``
with the error a bad address already raises (``ValueError``), before any
request.  Each is driven with a recording ``_request`` (and, for the
SocketDMA fast path, a fake socket client) so "no request" is observed, not
inferred.  Positive controls: the real ints ``0`` and ``1`` still go out.
No device.
"""
from __future__ import annotations

import pytest

from c64_test_harness.backends import ultimate64 as transport_mod
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    _wire_hex16,
)

_BOOLS = [True, False]


class _Recorder:
    """Stands in for ``Ultimate64Client._request``; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, *, body=None, content_type=None, query=None, **kw):
        self.calls.append((method, path, query))
        if path == "/v1/machine:readmem":
            return 200, bytes(int(query["length"]))
        return 200, b""


class _FakeSocketDMA:
    """In-process stand-in for SocketDMAClient; records DMA writes."""

    writes: list[tuple[int, bytes]] = []

    def __init__(self, **kw) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        pass

    def dma_write(self, address, data) -> None:
        _FakeSocketDMA.writes.append((address, bytes(data)))

    def identify(self) -> dict:
        return {"title": "FAKE"}


@pytest.fixture
def client():
    c = Ultimate64Client("h", write_mem_query_threshold=48, warn_unlocked=False,
                         temp_hygiene=False)
    rec = _Recorder()
    c._request = rec  # type: ignore[method-assign]
    return c, rec


@pytest.fixture
def fake_socket(monkeypatch):
    _FakeSocketDMA.writes = []
    monkeypatch.setattr(transport_mod, "SocketDMAClient", _FakeSocketDMA)
    return _FakeSocketDMA


# --------------------------------------------------------------------------- #
# The formatter                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", _BOOLS)
def test_wire_hex16_refuses_a_bool(flag):
    with pytest.raises(ValueError):
        _wire_hex16(flag)


# --------------------------------------------------------------------------- #
# The client                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", _BOOLS)
@pytest.mark.parametrize("size", [1, 48, 49], ids=lambda n: f"{n}B")
def test_write_mem_refuses_a_bool_before_any_request(client, flag, size):
    c, rec = client
    with pytest.raises(ValueError):
        c.write_mem(flag, b"\x37" * size)
    assert rec.calls == []


@pytest.mark.parametrize("flag", _BOOLS)
@pytest.mark.parametrize("data", [b"", "not bytes"], ids=["empty", "non-bytes"])
def test_write_mem_refuses_a_bool_before_looking_at_data(client, flag, data):
    """A bool address is refused like a bad int address is: first, whatever
    the payload.  ``write_mem(-1, b"")`` already raises ValueError; without
    its own guard ``write_mem(True, b"")`` returned silently (the formatter
    that would catch it is never reached) and ``write_mem(True, "x")``
    raised a data TypeError instead."""
    c, rec = client
    with pytest.raises(ValueError, match="bool"):
        c.write_mem(flag, data)  # type: ignore[arg-type]
    assert rec.calls == []


@pytest.mark.parametrize("data", [b"", "not bytes"], ids=["empty", "non-bytes"])
def test_a_bad_int_address_is_refused_first_too(client, data):
    """Control for the ordering claim above."""
    c, _ = client
    with pytest.raises(ValueError):
        c.write_mem(-1, data)  # type: ignore[arg-type]


@pytest.mark.parametrize("flag", _BOOLS)
def test_read_mem_refuses_a_bool_before_any_request(client, flag):
    c, rec = client
    with pytest.raises(ValueError):
        c.read_mem(flag, 1)
    assert rec.calls == []


@pytest.mark.parametrize("address, wire", [(1, "0001"), (0, "0000")])
def test_the_real_ints_still_go_out(client, address, wire):
    """Positive control: rejecting bool must not reject the port or $0000."""
    c, rec = client
    c.write_mem(address, b"\x37")
    c.read_mem(address, 1)
    assert [q["address"] for _, _, q in rec.calls] == [wire, wire]


# --------------------------------------------------------------------------- #
# The transport (both write paths, and read)                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", _BOOLS)
@pytest.mark.parametrize("socket_dma", [False, True], ids=["rest", "socketdma"])
def test_transport_write_memory_refuses_a_bool_on_every_path(client, fake_socket, flag, socket_dma):
    c, rec = client
    t = Ultimate64Transport(host="h", client=c, socket_dma=socket_dma,
                            socket_dma_min_bytes=1)
    with pytest.raises(ValueError):
        t.write_memory(flag, b"\x37")
    assert rec.calls == []
    assert fake_socket.writes == []


@pytest.mark.parametrize("flag", _BOOLS)
def test_transport_read_memory_refuses_a_bool(client, flag):
    c, rec = client
    t = Ultimate64Transport(host="h", client=c)
    with pytest.raises(ValueError):
        t.read_memory(flag, 1)
    assert rec.calls == []


def test_transport_socketdma_positive_control(client, fake_socket):
    """The fake really is on the path: an int address reaches it."""
    c, rec = client
    t = Ultimate64Transport(host="h", client=c, socket_dma=True, socket_dma_min_bytes=1)
    t.write_memory(1, b"\x37")
    assert fake_socket.writes == [(1, b"\x37")]
