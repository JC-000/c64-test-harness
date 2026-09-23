"""A socket call on a handle the network target does not own must raise (#428).

Since #808 (PR #814, in the U64E's bce4535e) the UCI network target keeps a
table of the sockets it opened and closes them all on a C64 reset.  A READ,
WRITE or CLOSE on a handle not in that table sets ``errno = EBADF`` and
answers with a status the harness used to drop:

* ``READ_SOCKET`` -- header ``FF FF`` (``lwip_recvmsg``'s ``-1``) and
  ``"02,NO DATA: 9"``;
* ``WRITE_SOCKET`` -- ``"12,SEND ERROR: 9"``;
* ``CLOSE_SOCKET`` -- ``"12,ERROR ON CLOSE: 9"``.

Measured on the U64E, 2026-09-23, for a handle closed by ``client.reset()``
and for one never opened; a live handle with nothing queued reads
``"02,NO DATA: 11"`` (``EAGAIN``, the 40 ms receive timeout).  So a reset --
including the one ``_execute_uci_routine`` issues after a routine timeout
(#313) -- turned every later read into ``b""`` and every write into a no-op,
with a "reply spans blocks" warning for the ``FF FF`` header.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from c64_test_harness import uci_network as un

READ_EBADF = b"02,NO DATA: 9"
READ_EAGAIN = b"02,NO DATA: 11"
SEND_EBADF = b"12,SEND ERROR: 9"
CLOSE_EBADF = b"12,ERROR ON CLOSE: 9"


def _transport(status: bytes, reply: bytes = b"\xff\xff") -> MagicMock:
    """A routine that completed with *reply* at ``$C200`` and *status* at ``$C300``."""
    t = MagicMock()

    def read_memory(addr: int, length: int) -> bytes:
        if addr == un._SENTINEL_ADDR:
            return bytes([un._SENTINEL_DONE])
        if addr == un._ERROR_ADDR:
            return b"\x00"
        if addr == un._RESP_LEN_ADDR:
            return bytes([len(reply), 0])[:length]
        if addr == un._STAT_LEN_ADDR:
            return bytes([len(status), 0])[:length]
        if un._STATUS_ADDR <= addr < un._STATUS_ADDR + max(len(status), 1):
            off = addr - un._STATUS_ADDR
            return status[off:off + length]
        if un._RESP_ADDR <= addr < un._RESP_ADDR + len(reply):
            off = addr - un._RESP_ADDR
            return reply[off:off + length]
        return bytes(length)

    t.read_memory.side_effect = read_memory
    return t


def test_read_on_a_handle_not_owned_raises() -> None:
    with pytest.raises(un.UCISocketNotOwnedError, match="02,NO DATA: 9"):
        un.uci_socket_read(_transport(READ_EBADF), 6, 16, timeout=1.0)


def test_read_with_nothing_queued_returns_empty_without_a_warning(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="c64_test_harness.uci_network")
    assert un.uci_socket_read(_transport(READ_EAGAIN), 6, 16, timeout=1.0) == b""
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_read_that_arrived_is_returned() -> None:
    t = _transport(b"00,OK", reply=b"\x03\x00abc")
    assert un.uci_socket_read(t, 6, 16, timeout=1.0) == b"abc"


@pytest.mark.parametrize("stale", [b"\xff\xff", b"\x03\x00", b""],
                         ids=["ff-ff", "03-00", "empty"])
def test_write_on_a_handle_not_owned_raises(stale: bytes) -> None:
    """``build_socket_write`` drains only the status, never ``$C200``.

    Whatever is at ``$C200`` was left by an earlier routine, so it must not
    decide the outcome: the status alone does.
    """
    with pytest.raises(un.UCISocketNotOwnedError, match="12,SEND ERROR: 9"):
        un.uci_socket_write(_transport(SEND_EBADF, reply=stale), 6, b"x", timeout=1.0)


@pytest.mark.parametrize("stale", [b"\xff\xff", b"\x03\x00"], ids=["ff-ff", "03-00"])
def test_write_that_was_sent_does_not_raise(stale: bytes) -> None:
    un.uci_socket_write(_transport(b"00,OK", reply=stale), 6, b"x", timeout=1.0)


@pytest.mark.parametrize("status", [b"04,DATAGRAM TRUNCATED: 9", b"00,OK: 9"],
                         ids=["truncated", "ok"])
def test_a_status_ending_in_9_that_is_not_an_error_does_not_raise(status: bytes) -> None:
    """Only the ``02``/``12`` error codes carry an errno; ``: 9`` alone is not EBADF."""
    un.uci_socket_close(_transport(status, reply=b""), 6, timeout=1.0)


def test_close_on_a_handle_not_owned_raises() -> None:
    with pytest.raises(un.UCISocketNotOwnedError, match="12,ERROR ON CLOSE: 9"):
        un.uci_socket_close(_transport(CLOSE_EBADF, reply=b""), 6, timeout=1.0)


def test_the_error_is_a_uci_error() -> None:
    assert issubclass(un.UCISocketNotOwnedError, un.UCIError)
