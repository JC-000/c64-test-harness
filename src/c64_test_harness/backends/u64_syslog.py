"""UDP listener for Ultimate 64 raw-line syslog feed.

The Ultimate 64 firmware can be configured (via the
``CFG_NETWORK_REMOTE_SYSLOG_SERVER`` config resource, exposed in the
config tree as the ``Network`` / ``REMOTE_SYSLOG_SERVER`` item) to emit
diagnostic log lines to a remote UDP server on port 514.

The wire format is *raw line transmission* — each UDP datagram payload is
one (or sometimes several newline-separated) log line(s).  It is **not**
RFC 3164 / RFC 5424 syslog framing, so there is no priority header to
strip and no timestamp/hostname to parse.  See ``software/network/syslog.cc``
in the GideonZ/1541ultimate firmware tree for the producer side.

This module provides a small, stdlib-only listener that test code can
use as a context manager to capture device-side log output during a
test run.  Firmware errors that get swallowed by the REST surface
(silent config failures, watchdog reboots, etc.) are typically visible
here, so this is the primary diagnostic channel REST cannot supply.

Usage::

    with U64SyslogListener(listen_port=0) as listener:
        host, port = listener.address
        listener.configure_device(client, host=my_lan_ip, port=port)
        # ... drive the device ...
        for line in listener.lines(timeout=5.0):
            if "PANIC" in line:
                break

Privilege note: binding port 514 typically requires root on Unix.  The
public default is 514 to match the firmware's expectation, but tests
and ad-hoc captures should pass ``listen_port=0`` and tell the device
to send to the kernel-assigned ephemeral port instead.
"""

from __future__ import annotations

import socket
from collections import deque
from typing import TYPE_CHECKING, Callable, Iterator

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from .ultimate64_client import Ultimate64Client


__all__ = ["U64SyslogListener"]


# Firmware config item that selects the remote syslog destination.
# The CFG_NETWORK_REMOTE_SYSLOG_SERVER resource is surfaced on the
# device's REST config tree under category "Network".  The accepted
# value form is "ip[:port]" (port defaults to 514 on the device side
# when omitted).
_SYSLOG_CATEGORY = "Network"
_SYSLOG_ITEM = "REMOTE_SYSLOG_SERVER"

# Receive buffer size.  Datagrams from the firmware are short log lines;
# 8 KiB is generous and avoids truncation on chatty boots.
_RECV_BUFSIZE = 8192


class U64SyslogListener:
    """UDP listener for U64 raw-line syslog.  Use as a context manager.

    Parameters
    ----------
    listen_host:
        Address to bind on.  Default ``"0.0.0.0"`` to receive from any
        interface.  Use ``"127.0.0.1"`` for loopback-only tests.
    listen_port:
        UDP port to bind on.  Defaults to ``514`` to match the
        firmware's expectation, but binding 514 requires root on most
        Unix systems — pass ``0`` to let the kernel assign an ephemeral
        port (then read back :attr:`address` and tell the device to
        send there via :meth:`configure_device`).

    The listener does not spawn a background thread.  Buffering happens
    lazily on each ``recv`` cycle: every call to :meth:`lines`,
    :meth:`collect`, or :meth:`wait_for` drains whatever the kernel has
    queued, splits incoming payloads on newlines, and yields complete
    lines.  An internal ``deque`` holds lines that arrived in a
    multi-line datagram but haven't been consumed yet.
    """

    def __init__(self, listen_host: str = "0.0.0.0", listen_port: int = 514) -> None:
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._sock: socket.socket | None = None
        # Lines decoded from previous recv() calls but not yet yielded.
        self._buffer: deque[str] = deque()
        # Trailing partial line from the most recent datagram, if it
        # didn't end in '\n'.  Held back so multi-datagram lines can be
        # rejoined.  In practice the firmware sends one line per
        # datagram, but the parser is defensive.
        self._partial: str = ""

    # ------------------------------------------------------------------ context
    def __enter__(self) -> "U64SyslogListener":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._listen_host, self._listen_port))
        self._sock = sock
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ helpers
    @property
    def address(self) -> tuple[str, int]:
        """The actual ``(host, port)`` the socket is bound to.

        Useful when ``listen_port=0`` was passed; read this after
        ``__enter__`` to learn the kernel-assigned port.
        """
        if self._sock is None:
            raise RuntimeError("U64SyslogListener is not active (use 'with' block)")
        host, port = self._sock.getsockname()[:2]
        return host, port

    def _require_open(self) -> socket.socket:
        if self._sock is None:
            raise RuntimeError("U64SyslogListener is not active (use 'with' block)")
        return self._sock

    def _decode(self, data: bytes) -> list[str]:
        """Decode + split a datagram payload into complete lines.

        UTF-8 with ``errors='replace'`` because the firmware does not
        guarantee strict encoding.  Trailing partial line (no newline)
        is held in ``self._partial`` until the next datagram completes
        it.
        """
        text = data.decode("utf-8", errors="replace")
        text = self._partial + text
        # Normalize CRLF -> LF so split is uniform.
        text = text.replace("\r\n", "\n")
        # Note: bare '\r' line terminators are uncommon for this feed;
        # treat them as ordinary characters.  If a line ends in '\n',
        # the trailing element of split('\n') is "".
        parts = text.split("\n")
        # The last element is the new partial — empty if the datagram
        # ended in '\n'.
        self._partial = parts[-1]
        complete = parts[:-1]
        # Strip a trailing '\r' that might have survived odd framing.
        return [line.rstrip("\r") for line in complete]

    def _drain_socket(self, timeout: float | None) -> bool:
        """Pull at most one datagram from the socket into ``_buffer``.

        Returns True if a datagram was received (and thus *zero or more*
        lines were appended), False if the recv timed out.  A
        zero-length payload still counts as "received" (returns True)
        but appends nothing.
        """
        sock = self._require_open()
        sock.settimeout(timeout)
        try:
            data, _addr = sock.recvfrom(_RECV_BUFSIZE)
        except (socket.timeout, TimeoutError, BlockingIOError):
            # BlockingIOError is what a non-blocking socket
            # (settimeout(0)) raises when the receive queue is empty;
            # treat it as a clean "no datagram" outcome alongside the
            # blocking-timeout cases.
            return False
        for line in self._decode(data):
            self._buffer.append(line)
        return True

    # ------------------------------------------------------------------ public API
    def lines(self, timeout: float = 1.0) -> Iterator[str]:
        """Yield decoded log lines as they arrive.

        Each iteration step blocks for up to ``timeout`` seconds waiting
        for the next datagram; when the kernel queue drains and no new
        datagram arrives within ``timeout``, iteration ends.  Lines
        already in the buffer (e.g. from a prior multi-line datagram)
        are yielded first, without blocking.

        ``timeout`` of ``0`` makes this non-blocking — equivalent to
        :meth:`collect`, but as an iterator.
        """
        # Drain anything currently buffered first, no recv needed.
        while self._buffer:
            yield self._buffer.popleft()
        while True:
            got = self._drain_socket(timeout)
            if not got and not self._buffer:
                return
            while self._buffer:
                yield self._buffer.popleft()

    def collect(self, timeout: float = 1.0) -> list[str]:
        """Drain currently-buffered lines without further blocking.

        Performs a single non-blocking pass over the kernel queue,
        appending any complete lines from received datagrams to the
        internal buffer, then returns and clears the buffer.  The
        ``timeout`` parameter is accepted for symmetry with
        :meth:`lines` and :meth:`wait_for` but is currently a no-op:
        ``collect`` never blocks.  (It is kept in the signature so
        callers can pass a uniform timeout argument.)
        """
        del timeout  # accepted for API symmetry; collect() never blocks.
        # Non-blocking drain: keep recv'ing while datagrams are queued.
        while self._drain_socket(0.0):
            pass
        out = list(self._buffer)
        self._buffer.clear()
        return out

    def wait_for(self, predicate: Callable[[str], bool], timeout: float) -> str:
        """Block until a line matching ``predicate`` arrives, or raise.

        Lines that do *not* match are discarded (consumed but not
        returned).  If no matching line arrives within ``timeout``
        seconds total, raises :class:`TimeoutError`.

        ``timeout`` is the total wall-clock budget across all recv
        calls, not per-line.
        """
        import time

        deadline = time.monotonic() + timeout
        # Check anything already buffered first.
        while self._buffer:
            line = self._buffer.popleft()
            if predicate(line):
                return line
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no syslog line matched predicate within {timeout}s"
                )
            self._drain_socket(remaining)
            while self._buffer:
                line = self._buffer.popleft()
                if predicate(line):
                    return line

    def configure_device(
        self,
        client: "Ultimate64Client",
        host: str,
        port: int = 514,
    ) -> None:
        """Point the device's syslog feed at this listener.

        Convenience wrapper around
        ``client.set_config_item("Network", "REMOTE_SYSLOG_SERVER", "<host>:<port>")``.
        ``host`` is the address the *device* will send to (i.e. the
        listener's host as visible from the device's network), not the
        address the listener bound to — these usually differ when the
        listener bound on ``0.0.0.0``.

        DESTRUCTIVE: this writes a config item on the device, which
        persists until overwritten.  Tests that mutate this should
        restore the previous value (read it via
        ``client.get_config_category("Network")`` first).
        """
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if not isinstance(port, int) or port <= 0 or port > 65535:
            raise ValueError(f"port must be in 1..65535, got {port!r}")
        value = f"{host}:{port}"
        client.set_config_item(_SYSLOG_CATEGORY, _SYSLOG_ITEM, value)
