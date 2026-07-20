"""Unit tests for the opt-in SocketDMA fast path in Ultimate64Transport.

Mock-only: the REST client is a MagicMock and the SocketDMAClient is
replaced by an in-process fake, so no network or hardware is touched.  These
tests cover the write-routing decision, chunking, MemoryPolicy ordering, and
the connect-failure / verify-mismatch fallback behaviour.  Live behaviour of
the real SocketDMAClient framing lives in ``test_u64_socket_dma.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness import MemoryPolicy, MemoryPolicyError, MemoryRegion
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Error


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSocketDMAClient:
    """Records dma_write calls and lets a test script connect/send failures."""

    def __init__(self) -> None:
        self.init_kwargs: dict | None = None
        self.enter_count = 0
        self.close_count = 0
        self.dma_calls: list[tuple[int, bytes]] = []
        # Test hooks:
        self.connect_error = False          # raise Ultimate64Error on __enter__
        self.send_error_after: int | None = None  # raise on Nth dma_write (0-based)

    def __enter__(self) -> "FakeSocketDMAClient":
        self.enter_count += 1
        if self.connect_error:
            raise Ultimate64Error("fake connect refused")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.close_count += 1

    def dma_write(self, address: int, data: bytes) -> None:
        if (
            self.send_error_after is not None
            and len(self.dma_calls) >= self.send_error_after
        ):
            raise Ultimate64Error("fake send failed")
        self.dma_calls.append((address, bytes(data)))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.host = "192.0.2.1"
    client.password = None
    client.read_mem.return_value = b""
    return client


@pytest.fixture
def install_fake(monkeypatch: pytest.MonkeyPatch):
    """Install a FakeSocketDMAClient in place of the real class.

    Returns ``(fake, state)`` where ``state["constructed"]`` records whether
    the transport ever asked for a SocketDMAClient.
    """
    fake = FakeSocketDMAClient()
    state = {"constructed": False}

    def factory(**kwargs: object) -> FakeSocketDMAClient:
        state["constructed"] = True
        fake.init_kwargs = dict(kwargs)
        return fake

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64.SocketDMAClient", factory
    )
    return fake, state


def _payload(n: int) -> bytes:
    return bytes(i % 256 for i in range(n))


# ---------------------------------------------------------------------------
# Constructor / attribute API
# ---------------------------------------------------------------------------


def test_defaults_off(mock_client: MagicMock) -> None:
    t = Ultimate64Transport(host="h", client=mock_client)
    assert t.socket_dma is False
    assert t.socket_dma_min_bytes == 8192


def test_constructor_sets_attrs(mock_client: MagicMock) -> None:
    t = Ultimate64Transport(
        host="h", client=mock_client, socket_dma=True, socket_dma_min_bytes=4096
    )
    assert t.socket_dma is True
    assert t.socket_dma_min_bytes == 4096


def test_attrs_settable_at_runtime(mock_client: MagicMock) -> None:
    t = Ultimate64Transport(host="h", client=mock_client)
    t.socket_dma = True
    t.socket_dma_min_bytes = 100
    assert t.socket_dma is True
    assert t.socket_dma_min_bytes == 100


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_default_off_large_write_uses_rest(
    mock_client: MagicMock, install_fake
) -> None:
    fake, state = install_fake
    t = Ultimate64Transport(host="h", client=mock_client)  # socket_dma default off
    data = _payload(16384)
    t.write_memory(0x2000, data)
    mock_client.write_mem.assert_called_once_with(0x2000, data)
    assert state["constructed"] is False
    assert fake.dma_calls == []


def test_enabled_below_threshold_uses_rest(
    mock_client: MagicMock, install_fake
) -> None:
    fake, state = install_fake
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)
    data = _payload(100)  # below default 8192
    t.write_memory(0x2000, data)
    mock_client.write_mem.assert_called_once_with(0x2000, data)
    assert state["constructed"] is False
    assert fake.dma_calls == []


def test_enabled_at_threshold_uses_dma(
    mock_client: MagicMock, install_fake
) -> None:
    fake, _ = install_fake
    data = _payload(8192)  # exactly the threshold → eligible (>=)
    mock_client.read_mem.return_value = data[-16:]  # verify tail matches
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x3000, data)

    # Single chunk (8192 < 32 KiB), correct address + payload.
    assert fake.dma_calls == [(0x3000, data)]
    # Tail verified over REST read.
    mock_client.read_mem.assert_called_once_with(0x3000 + 8192 - 16, 16)
    # REST write path NOT used.
    mock_client.write_mem.assert_not_called()
    # Password / host inherited from the REST client; SocketDMA uses TCP/64.
    assert fake.init_kwargs == {"host": "192.0.2.1", "password": None}


def test_chunking_full_ram_restore(
    mock_client: MagicMock, install_fake
) -> None:
    fake, _ = install_fake
    data = _payload(65536)  # full 64 KiB at $0000
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x0000, data)

    # Two 32 KiB chunks with advancing 16-bit addresses.
    assert len(fake.dma_calls) == 2
    assert fake.dma_calls[0] == (0x0000, data[:0x8000])
    assert fake.dma_calls[1] == (0x8000, data[0x8000:])
    mock_client.write_mem.assert_not_called()


# ---------------------------------------------------------------------------
# MemoryPolicy ordering
# ---------------------------------------------------------------------------


def test_policy_denial_before_any_dma(
    mock_client: MagicMock, install_fake
) -> None:
    fake, state = install_fake
    policy = MemoryPolicy(
        reserved_regions=(MemoryRegion(0xC000, 0xD000, "TCP_BUF"),),
    )
    t = Ultimate64Transport(
        host="h", client=mock_client, socket_dma=True, memory_policy=policy
    )
    data = _payload(8192)  # eligible size, but lands in a reserved range

    with pytest.raises(MemoryPolicyError):
        t.write_memory(0xC000, data)

    # Fast path must be unreachable for a denied write.
    assert state["constructed"] is False
    assert fake.dma_calls == []
    mock_client.write_mem.assert_not_called()


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------


def test_connect_failure_falls_back_and_latches(
    mock_client: MagicMock, install_fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake, _ = install_fake
    fake.connect_error = True
    data = _payload(8192)
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    with caplog.at_level("WARNING"):
        t.write_memory(0x4000, data)
    assert any("connect" in r.message.lower() for r in caplog.records)
    # Fell back to REST for this write.
    mock_client.write_mem.assert_called_once_with(0x4000, data)
    assert fake.dma_calls == []
    assert fake.enter_count == 1

    # Second eligible write must NOT re-attempt SocketDMA (latched off).
    mock_client.write_mem.reset_mock()
    t.write_memory(0x5000, data)
    mock_client.write_mem.assert_called_once_with(0x5000, data)
    assert fake.enter_count == 1  # no second connect attempt


def test_send_failure_falls_back_no_latch(
    mock_client: MagicMock, install_fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake, _ = install_fake
    fake.send_error_after = 0  # raise on the first dma_write
    data = _payload(8192)
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    with caplog.at_level("WARNING"):
        t.write_memory(0x4000, data)
    assert any("send" in r.message.lower() for r in caplog.records)
    mock_client.write_mem.assert_called_once_with(0x4000, data)

    # Send failure does not latch — a later write attempts SocketDMA again.
    fake.send_error_after = None
    mock_client.read_mem.return_value = data[-16:]
    mock_client.write_mem.reset_mock()
    t.write_memory(0x6000, data)
    assert fake.dma_calls == [(0x6000, data)]
    mock_client.write_mem.assert_not_called()


def test_verify_mismatch_falls_back_no_latch(
    mock_client: MagicMock, install_fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake, _ = install_fake
    data = _payload(8192)
    mock_client.read_mem.return_value = b"\x00" * 16  # tail does NOT match
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)
    # Keep the verify poll from burning its full live-hardware budget on a
    # mock that can never match.
    t.socket_dma_verify_timeout = 0.05

    with caplog.at_level("WARNING"):
        t.write_memory(0x4000, data)
    assert any("mismatch" in r.message.lower() for r in caplog.records)
    # DMA was attempted, but we still fall back to REST for this write.
    assert fake.dma_calls == [(0x4000, data)]
    mock_client.write_mem.assert_called_once_with(0x4000, data)

    # Verify mismatch does NOT latch — next eligible write attempts SocketDMA.
    mock_client.read_mem.return_value = data[-16:]  # now matches
    mock_client.write_mem.reset_mock()
    t.write_memory(0x7000, data)
    assert fake.dma_calls[-1] == (0x7000, data)
    mock_client.write_mem.assert_not_called()


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_close_closes_socket_dma_client(
    mock_client: MagicMock, install_fake
) -> None:
    fake, _ = install_fake
    data = _payload(8192)
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)
    t.write_memory(0x3000, data)  # forces lazy client creation

    t.close()
    assert fake.close_count >= 1
    mock_client.close.assert_called_once_with()


def test_close_without_socket_dma_client_is_safe(
    mock_client: MagicMock,
) -> None:
    t = Ultimate64Transport(host="h", client=mock_client)
    t.close()  # never created a SocketDMA client
    mock_client.close.assert_called_once_with()
