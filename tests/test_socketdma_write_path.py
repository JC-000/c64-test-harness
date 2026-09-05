"""Unit tests for the opt-in SocketDMA fast path in Ultimate64Transport.

Mock-only: the REST client is a MagicMock and the SocketDMAClient is
replaced by an in-process fake, so no network or hardware is touched.  These
tests cover the write-routing decision, chunking, MemoryPolicy ordering, the
in-band IDENTIFY completion barrier, and the connect-failure /
verify-mismatch fallback behaviour.  Live behaviour of
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
    """Records dma_write/identify calls and lets a test script failures.

    ``events`` is a shared ordered log (``"dma_write"`` / ``"identify"``
    entries; tests may append their own markers, e.g. for REST reads) so
    barrier-ordering assertions can see the full call sequence.
    """

    def __init__(self) -> None:
        self.init_kwargs: dict | None = None
        self.enter_count = 0
        self.close_count = 0
        self.dma_calls: list[tuple[int, bytes]] = []
        self.identify_calls = 0
        self.events: list[str] = []
        # Test hooks:
        self.connect_error = False          # raise Ultimate64Error on __enter__
        self.send_error_after: int | None = None  # raise on Nth dma_write (0-based)
        self.identify_error = False         # raise Ultimate64Error on identify()
        # Issue #223 hooks: fail only the first N calls, the way a connection
        # the firmware closed while idle fails once and a fresh one works.
        self.identify_fail_first = 0        # identify() raises this many times
        self.dma_fail_first = 0             # dma_write() raises this many times
        self.dma_attempts = 0

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
        self.dma_attempts += 1
        if self.dma_attempts <= self.dma_fail_first:
            raise Ultimate64Error("fake send failed: [Errno 32] Broken pipe")
        if (
            self.send_error_after is not None
            and len(self.dma_calls) >= self.send_error_after
        ):
            raise Ultimate64Error("fake send failed")
        self.dma_calls.append((address, bytes(data)))
        self.events.append("dma_write")

    def identify(self) -> dict:
        self.identify_calls += 1
        self.events.append("identify")
        if self.identify_calls <= self.identify_fail_first:
            raise Ultimate64Error("SocketDMA connection closed by peer (fake)")
        if self.identify_error:
            raise Ultimate64Error("fake identify failed")
        return {"title": "FAKE ULTIMATE 64"}


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
# Completion barrier (in-band IDENTIFY)
# ---------------------------------------------------------------------------


def test_barrier_required_even_when_tail_already_matches(
    mock_client: MagicMock, install_fake
) -> None:
    """Regression: a tail read-back is not a completion barrier.

    Simulates RAM whose tail ALREADY equals the payload tail (zero
    padding / re-writing the same buffer / snapshot restores whose last
    bytes rarely change) while the head bytes are still stale in flight.
    The pre-fix code returned success off the immediate tail match
    without any completion barrier; the fixed code must issue the
    in-band IDENTIFY barrier BEFORE any REST tail read.
    """
    fake, _ = install_fake
    data = _payload(8192)

    def read_mem(addr: int, length: int) -> bytes:
        fake.events.append("read_mem")
        return data[-16:]  # tail matches from the very first read

    mock_client.read_mem.side_effect = read_mem
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x2000, data)

    # The barrier must have run, exactly once, after the DMA send and
    # before the (now sanity-only) REST tail read.
    assert fake.identify_calls == 1
    assert fake.events == ["dma_write", "identify", "read_mem"]
    mock_client.write_mem.assert_not_called()


def test_barrier_once_after_all_chunks(
    mock_client: MagicMock, install_fake
) -> None:
    """Chunked writes get ONE barrier, after the last DMAWRITE chunk."""
    fake, _ = install_fake
    data = _payload(65536)  # full 64 KiB → two 32 KiB chunks
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x0000, data)

    assert fake.identify_calls == 1
    assert fake.events == ["dma_write", "dma_write", "identify"]


def test_barrier_failure_falls_back_no_latch(
    mock_client: MagicMock, install_fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake, _ = install_fake
    fake.identify_error = True
    data = _payload(8192)
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    with caplog.at_level("WARNING"):
        t.write_memory(0x4000, data)
    assert any("barrier" in r.message.lower() for r in caplog.records)
    # Fell back to REST for this write; no tail read was attempted.
    mock_client.write_mem.assert_called_once_with(0x4000, data)
    mock_client.read_mem.assert_not_called()
    # The connection is dropped so a later attempt starts clean.
    assert fake.close_count >= 1

    # Barrier failure does not latch — a later write attempts SocketDMA.
    fake.identify_error = False
    mock_client.read_mem.return_value = data[-16:]
    mock_client.write_mem.reset_mock()
    t.write_memory(0x6000, data)
    assert fake.dma_calls[-1] == (0x6000, data)
    mock_client.write_mem.assert_not_called()


def test_barrier_recv_timeout_scaled_with_payload(
    mock_client: MagicMock, install_fake
) -> None:
    """The barrier stretches the socket recv timeout by payload size at
    the worst-observed drain rate (~4 KiB/s floor), then restores it —
    the same scaling ``SocketDMAClient.reu_write`` applies."""

    class _FakeSock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    fake, _ = install_fake
    fake._sock = _FakeSock()
    fake._timeout = 5.0
    data = _payload(65536)
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x0000, data)

    assert fake._sock.timeouts == [5.0 + 65536 / 4096.0, 5.0]


# ---------------------------------------------------------------------------
# Issue #223: one retry on a fresh connection before the REST fallback
# ---------------------------------------------------------------------------
#
# Measured on the U64E (fw 3.15 fork, 2026-09-05, scratchpad exp223.py): the
# firmware closes a SocketDMA connection idle for >1 s, the first DMAWRITE
# into that socket is never read (the data was in RAM 0/50 times), and the
# barrier then fails with "connection closed by peer" 50/50 -- idle and
# under a 100 ms REST poll alike -- while 0.2 s gaps never failed (0/50).
# The fix is to retry the whole send + barrier once on a fresh connection;
# these tests were red on the pre-fix transport, which fell straight back
# to REST.


def test_barrier_closed_by_peer_retries_once_on_fresh_connection(
    mock_client: MagicMock, install_fake, caplog: pytest.LogCaptureFixture
) -> None:
    fake, _ = install_fake
    fake.identify_fail_first = 1
    data = _payload(8192)
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    with caplog.at_level("WARNING"):
        t.write_memory(0x4000, data)

    # Same bytes re-sent to the same address, barrier run again, and the
    # connection dropped + reopened in between.
    assert fake.events == ["dma_write", "identify", "dma_write", "identify"]
    assert fake.dma_calls == [(0x4000, data), (0x4000, data)]
    assert fake.close_count == 1
    assert fake.enter_count == 2
    # The retry succeeded: no REST fallback, tail verified.
    mock_client.write_mem.assert_not_called()
    mock_client.read_mem.assert_called()
    assert any("retrying once" in r.message for r in caplog.records)


def test_send_failure_on_reused_connection_retries_once(
    mock_client: MagicMock, install_fake
) -> None:
    """A DMAWRITE into a socket the device already closed can fail at the
    send (EPIPE) instead of at the barrier; that is retried the same way."""
    fake, _ = install_fake
    fake.dma_fail_first = 1
    data = _payload(8192)
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x5000, data)

    assert fake.events == ["dma_write", "identify"]
    assert fake.dma_calls == [(0x5000, data)]
    assert fake.dma_attempts == 2
    assert fake.close_count == 1
    mock_client.write_mem.assert_not_called()


def test_retry_is_exactly_one_then_rest_fallback(
    mock_client: MagicMock, install_fake, caplog: pytest.LogCaptureFixture
) -> None:
    """Two failures in a row -> REST fallback; no third attempt, no latch."""
    fake, _ = install_fake
    fake.identify_error = True
    data = _payload(8192)
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    with caplog.at_level("WARNING"):
        t.write_memory(0x4000, data)

    assert fake.identify_calls == 2
    assert fake.dma_calls == [(0x4000, data), (0x4000, data)]
    mock_client.write_mem.assert_called_once_with(0x4000, data)
    assert t._socket_dma_unusable is False
    assert any("on the retry as well" in r.message for r in caplog.records)


def test_retry_reconnect_failure_falls_back_without_latch(
    mock_client: MagicMock, install_fake
) -> None:
    """If the fresh connection cannot be opened, this write goes to REST
    and the fast path is NOT latched off (the device may just be busy)."""
    fake, _ = install_fake
    fake.identify_fail_first = 1
    data = _payload(8192)
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    real_enter = fake.__enter__

    def enter_once_then_refuse():
        if fake.enter_count >= 1:
            fake.enter_count += 1
            raise Ultimate64Error("fake reconnect refused")
        return real_enter()

    fake.__enter__ = enter_once_then_refuse  # type: ignore[method-assign]
    t.write_memory(0x4000, data)

    assert fake.identify_calls == 1
    mock_client.write_mem.assert_called_once_with(0x4000, data)
    assert t._socket_dma_unusable is False


def test_barrier_timeout_restore_survives_a_socket_closed_by_the_client(
    mock_client: MagicMock, install_fake
) -> None:
    """Live-caught (U64E, 2026-09-05): when the peer has closed the
    connection the client closes its socket inside identify(), and the
    barrier's timeout restore then hits a dead descriptor.  That OSError
    must not escape the fast path -- the retry has to run."""

    class _ClosingSock:
        def __init__(self) -> None:
            self.closed = False

        def settimeout(self, value: float) -> None:
            if self.closed:
                raise OSError(9, "Bad file descriptor")

    fake, _ = install_fake
    sock = _ClosingSock()
    fake._sock = sock
    fake._timeout = 5.0
    real_identify = fake.identify

    def identify_closing_on_first_failure() -> dict:
        if fake.identify_calls == 0:
            fake.identify_calls += 1
            fake.events.append("identify")
            sock.closed = True                      # what _recv_exact does
            fake._sock = _ClosingSock()             # ...and the reconnect
            raise Ultimate64Error("SocketDMA connection closed by peer")
        return real_identify()

    fake.identify = identify_closing_on_first_failure  # type: ignore[method-assign]
    data = _payload(8192)
    mock_client.read_mem.return_value = data[-16:]
    t = Ultimate64Transport(host="h", client=mock_client, socket_dma=True)

    t.write_memory(0x4000, data)          # must not raise OSError

    assert fake.dma_calls == [(0x4000, data), (0x4000, data)]
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
