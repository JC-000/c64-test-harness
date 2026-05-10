"""Unit tests for Ultimate64Transport — the client is mocked, no network."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.transport import C64Transport


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.read_mem.return_value = b""
    return client


@pytest.fixture
def transport(mock_client: MagicMock) -> Ultimate64Transport:
    return Ultimate64Transport(host="192.0.2.1", client=mock_client)


def test_properties(transport: Ultimate64Transport) -> None:
    assert transport.screen_cols == 40
    assert transport.screen_rows == 25


def test_custom_dimensions(mock_client: MagicMock) -> None:
    t = Ultimate64Transport(host="h", client=mock_client, cols=80, rows=50)
    assert t.screen_cols == 80
    assert t.screen_rows == 50


def test_protocol_conformance(transport: Ultimate64Transport) -> None:
    assert isinstance(transport, C64Transport)


def test_client_property_returns_underlying_client(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    assert transport.client is mock_client
    assert transport.client is transport._client


def test_read_memory_delegates(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    mock_client.read_mem.return_value = b"\x01\x02\x03"
    result = transport.read_memory(0x1000, 3)
    mock_client.read_mem.assert_called_once_with(0x1000, 3)
    assert result == b"\x01\x02\x03"


def test_read_memory_zero_length(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    assert transport.read_memory(0x1000, 0) == b""
    mock_client.read_mem.assert_not_called()


def test_write_memory_bytes(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.write_memory(0x2000, b"\xaa\xbb")
    mock_client.write_mem.assert_called_once_with(0x2000, b"\xaa\xbb")


def test_write_memory_list_converted(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.write_memory(0x2000, [0x01, 0x02, 0x03])
    mock_client.write_mem.assert_called_once_with(0x2000, b"\x01\x02\x03")


def test_write_memory_empty_noop(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.write_memory(0x2000, b"")
    transport.write_memory(0x2000, [])
    mock_client.write_mem.assert_not_called()


def test_read_screen_codes(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    mock_client.read_mem.return_value = bytes(i % 256 for i in range(1000))
    codes = transport.read_screen_codes()
    mock_client.read_mem.assert_called_once_with(0x0400, 1000)
    assert len(codes) == 1000
    assert all(isinstance(c, int) for c in codes)


def test_read_screen_codes_custom_base(mock_client: MagicMock) -> None:
    mock_client.read_mem.return_value = bytes(1000)
    t = Ultimate64Transport(host="h", client=mock_client, screen_base=0x8400)
    t.read_screen_codes()
    mock_client.read_mem.assert_called_once_with(0x8400, 1000)


def test_inject_keys_empty(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.inject_keys([])
    mock_client.read_mem.assert_not_called()
    mock_client.write_mem.assert_not_called()


def test_inject_keys_simple(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    # Current count reads back as 0.
    mock_client.read_mem.return_value = b"\x00"
    transport.inject_keys([0x41, 0x42, 0x43])

    # Reads count once.
    assert mock_client.read_mem.call_count == 1
    mock_client.read_mem.assert_called_with(0x00C6, 1)
    # Writes buffer bytes, then count byte.
    assert mock_client.write_mem.call_count == 2
    calls = mock_client.write_mem.call_args_list
    assert calls[0].args == (0x0277, b"\x41\x42\x43")
    assert calls[1].args == (0x00C6, bytes([3]))


def test_inject_keys_respects_existing_count(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # Existing count is 4 — only 6 bytes of free space.
    mock_client.read_mem.return_value = b"\x04"
    transport.inject_keys([0x11, 0x22, 0x33])
    # Writes at (0x0277 + 4), then count becomes 4 + 3 = 7.
    calls = mock_client.write_mem.call_args_list
    assert calls[0].args == (0x0277 + 4, b"\x11\x22\x33")
    assert calls[1].args == (0x00C6, bytes([7]))


def test_inject_keys_chunks_when_over_max(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # First poll: count=0, second poll: count=0 again (drained), etc.
    mock_client.read_mem.return_value = b"\x00"
    keys = list(range(12))  # 12 keys — 10 + 2
    transport.inject_keys(keys)
    # Two chunks.
    write_calls = [c for c in mock_client.write_mem.call_args_list]
    assert len(write_calls) == 4  # 2 * (buffer, count)
    assert write_calls[0].args == (0x0277, bytes(range(10)))
    assert write_calls[1].args == (0x00C6, bytes([10]))
    assert write_calls[2].args == (0x0277, bytes([10, 11]))
    assert write_calls[3].args == (0x00C6, bytes([2]))


def test_inject_keys_waits_for_drain(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # Simulate buffer full for 2 polls, then drained.
    mock_client.read_mem.side_effect = [b"\x0a", b"\x0a", b"\x00"]
    transport.inject_keys([0x01])
    # Should have polled 3 times.
    assert mock_client.read_mem.call_count == 3
    # Single write pair after drain.
    assert mock_client.write_mem.call_count == 2


def test_read_registers_not_supported(transport: Ultimate64Transport) -> None:
    with pytest.raises(NotImplementedError, match="CPU registers"):
        transport.read_registers()


def test_resume_delegates(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.resume()
    mock_client.resume.assert_called_once_with()


def test_close_delegates(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.close()
    mock_client.close.assert_called_once_with()


def test_constructs_own_client_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64.Ultimate64Client",
        FakeClient,
    )
    Ultimate64Transport(host="10.0.0.5", password="pw", port=8080, timeout=3.0)
    assert created == {
        "host": "10.0.0.5",
        "password": "pw",
        "port": 8080,
        "timeout": 3.0,
    }
