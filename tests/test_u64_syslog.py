"""Unit tests for :class:`U64SyslogListener`.

These tests bind the listener on a kernel-assigned port and send UDP
packets from a sender socket in the same process — no real Ultimate 64
required.  The :meth:`U64SyslogListener.configure_device` path is
covered with a ``MagicMock`` client, since it is a thin wrapper around
``client.set_config_item``.
"""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.u64_syslog import U64SyslogListener


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send(payload: bytes, addr: tuple[str, int]) -> None:
    """Send a single UDP datagram to ``addr`` and close the sender."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(payload, addr)
    finally:
        s.close()


def _send_to(listener: U64SyslogListener, payload: bytes) -> None:
    """Send ``payload`` to the listener's bound port on 127.0.0.1."""
    _, port = listener.address
    _send(payload, ("127.0.0.1", port))


# ---------------------------------------------------------------------------
# Context manager / address
# ---------------------------------------------------------------------------


def test_address_unavailable_outside_context() -> None:
    listener = U64SyslogListener(listen_host="127.0.0.1", listen_port=0)
    with pytest.raises(RuntimeError):
        _ = listener.address


def test_kernel_assigned_port_is_nonzero() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        host, port = listener.address
        assert host == "127.0.0.1"
        assert port > 0


def test_socket_closed_on_exit() -> None:
    listener = U64SyslogListener(listen_host="127.0.0.1", listen_port=0)
    with listener:
        assert listener._sock is not None
    assert listener._sock is None


def test_lines_outside_context_raises() -> None:
    listener = U64SyslogListener(listen_host="127.0.0.1", listen_port=0)
    with pytest.raises(RuntimeError):
        # Trigger the iterator body, not just construct it.
        next(iter(listener.lines(timeout=0.05)))


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------


def test_collect_returns_empty_when_no_traffic() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        assert listener.collect() == []


def test_collect_drains_buffered_lines() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"hello\n")
        _send_to(listener, b"world\n")
        # Give the kernel a moment to enqueue the datagrams.
        time.sleep(0.05)
        got = listener.collect()
        assert got == ["hello", "world"]
        # Second call sees nothing more.
        assert listener.collect() == []


def test_collect_handles_multiline_datagram() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"alpha\nbeta\ngamma\n")
        time.sleep(0.05)
        assert listener.collect() == ["alpha", "beta", "gamma"]


def test_collect_holds_partial_line_until_completed() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"first half ")
        time.sleep(0.05)
        # No newline yet -> nothing complete.
        assert listener.collect() == []
        _send_to(listener, b"second half\n")
        time.sleep(0.05)
        assert listener.collect() == ["first half second half"]


def test_collect_normalizes_crlf() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"with-crlf\r\nplain\n")
        time.sleep(0.05)
        assert listener.collect() == ["with-crlf", "plain"]


def test_collect_decodes_utf8_with_replacement() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        # Invalid UTF-8 byte 0xff in the middle of an otherwise ASCII line.
        _send_to(listener, b"good-\xff-bad\n")
        time.sleep(0.05)
        got = listener.collect()
        assert len(got) == 1
        # The replacement char (U+FFFD) should be present where 0xff was.
        assert "good-" in got[0]
        assert "-bad" in got[0]
        assert "�" in got[0]


# ---------------------------------------------------------------------------
# lines()
# ---------------------------------------------------------------------------


def test_lines_yields_already_buffered_first() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"early\n")
        time.sleep(0.05)
        # Drain into the internal deque via a non-blocking collect
        # cycle... actually use lines() which should yield it.
        it = listener.lines(timeout=0.2)
        assert next(it) == "early"


def test_lines_terminates_on_timeout_with_no_traffic() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        start = time.monotonic()
        result = list(listener.lines(timeout=0.1))
        elapsed = time.monotonic() - start
        assert result == []
        # Should not block much beyond the timeout.
        assert elapsed < 1.0


def test_lines_preserves_ordering_across_datagrams() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        for i in range(5):
            _send_to(listener, f"line-{i}\n".encode())
        time.sleep(0.1)
        got = list(listener.lines(timeout=0.1))
        assert got == [f"line-{i}" for i in range(5)]


def test_lines_streams_late_arriving_datagram() -> None:
    """A datagram arriving after lines() starts iterating should still appear."""
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _, port = listener.address

        def delayed_send() -> None:
            time.sleep(0.1)
            _send(b"late\n", ("127.0.0.1", port))

        t = threading.Thread(target=delayed_send, daemon=True)
        t.start()
        try:
            it = listener.lines(timeout=2.0)
            assert next(it) == "late"
        finally:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# wait_for()
# ---------------------------------------------------------------------------


def test_wait_for_matches_buffered_line() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"noise\n")
        _send_to(listener, b"PANIC: oops\n")
        _send_to(listener, b"more noise\n")
        time.sleep(0.05)
        got = listener.wait_for(lambda line: "PANIC" in line, timeout=1.0)
        assert got == "PANIC: oops"


def test_wait_for_discards_non_matching_lines() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"skip-me\n")
        _send_to(listener, b"target\n")
        _send_to(listener, b"after\n")
        time.sleep(0.05)
        got = listener.wait_for(lambda line: line == "target", timeout=1.0)
        assert got == "target"
        # The "skip-me" line was consumed.  "after" should still be
        # available via collect().
        remaining = listener.collect()
        assert remaining == ["after"]


def test_wait_for_times_out_when_nothing_matches() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _send_to(listener, b"nope\n")
        time.sleep(0.05)
        with pytest.raises(TimeoutError):
            listener.wait_for(lambda line: "PANIC" in line, timeout=0.2)


def test_wait_for_times_out_with_no_traffic() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        with pytest.raises(TimeoutError):
            listener.wait_for(lambda line: True, timeout=0.1)


def test_wait_for_picks_up_late_match() -> None:
    with U64SyslogListener(listen_host="127.0.0.1", listen_port=0) as listener:
        _, port = listener.address

        def delayed_send() -> None:
            time.sleep(0.1)
            _send(b"early\n", ("127.0.0.1", port))
            time.sleep(0.05)
            _send(b"FOUND\n", ("127.0.0.1", port))

        t = threading.Thread(target=delayed_send, daemon=True)
        t.start()
        try:
            got = listener.wait_for(lambda line: line == "FOUND", timeout=2.0)
            assert got == "FOUND"
        finally:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# configure_device()
# ---------------------------------------------------------------------------


def test_configure_device_calls_set_config_item() -> None:
    listener = U64SyslogListener()
    client = MagicMock()
    listener.configure_device(client, host="10.0.0.5", port=12345)
    client.set_config_item.assert_called_once_with(
        "Network", "REMOTE_SYSLOG_SERVER", "10.0.0.5:12345"
    )


def test_configure_device_default_port_514() -> None:
    listener = U64SyslogListener()
    client = MagicMock()
    listener.configure_device(client, host="192.168.1.10")
    client.set_config_item.assert_called_once_with(
        "Network", "REMOTE_SYSLOG_SERVER", "192.168.1.10:514"
    )


def test_configure_device_rejects_empty_host() -> None:
    listener = U64SyslogListener()
    client = MagicMock()
    with pytest.raises(ValueError):
        listener.configure_device(client, host="", port=514)
    client.set_config_item.assert_not_called()


@pytest.mark.parametrize("bad_port", [0, -1, 65536, "514"])
def test_configure_device_rejects_bad_port(bad_port) -> None:
    listener = U64SyslogListener()
    client = MagicMock()
    with pytest.raises(ValueError):
        listener.configure_device(client, host="1.2.3.4", port=bad_port)  # type: ignore[arg-type]
    client.set_config_item.assert_not_called()
