"""The hand-written wire format in the mocks must match a real VICE frame.

``test_vice_binary_unit.py`` feeds the transport synthetic responses
built by a local helper::

    struct.pack("<BBIBBI", STX, API_VERSION, len(body),
                response_type, error_code, request_id)

Nineteen call sites in that module rest on the one line (twenty
repo-wide, counting this module's own), and the transport parses the
frames back at fixed offsets.

**A slip in the builder alone is caught.** Swapping ``response_type`` and
``error_code`` in the mock only leaves 13 of that module's tests failing
-- builder and transport disagree, and the suite notices. That control
matters: it is what makes the finding below specific rather than a blanket
claim that mocks prove nothing.

**A slip they share is not caught.** Make the same swap in the
transport's parse *and* the mock's build and they agree perfectly, as
does every test between them. Measured at this commit:

* ``test_vice_binary_unit.py`` -- 40 passed
* this module -- failed

So the honest figure is **40 mocked tests certifying a frame layout VICE
does not use** -- the tests that actually feed bytes through the builder.

Scope note, because it is easy to inflate this number: the 85 tests in
``test_vice_binary_resource.py`` also pass under that mutation, but they
pass under *no* mutation too. They inject already-parsed ``_Response``
tuples, so they sit above the wire format and a layout error is invisible
to them by construction -- they are decode-logic tests, not wire tests,
and counting them here would be padding. (That module's own
``_build_response`` helper was dead code and has been removed.)

So this module talks to a real binary monitor over a raw socket, captures
genuine response bytes, and checks that the mock's builder reproduces
them exactly.

Raw sockets on purpose: going through ``BinaryViceTransport`` would
validate the format against the same code that defines it, which is the
circularity this module exists to break.

**What this module cannot see.** It distinguishes fields by comparing
their *values*, so any two fields that happen to hold the same value are
indistinguishable to it:

* ``STX`` and ``API_VERSION`` are **both 0x02**, so transposing those two
  request fields is byte-identical on the wire. Measured: 125 mocked
  tests and all three tests here pass with the transposition applied to
  the parser, the request builder and the mock together. No black-box
  test can find it while they coincide — VICE ``continue``s past both a
  bad STX and an out-of-range api_version *without replying*
  (S ``monitor_binary.c:1913``), so the only signal is silence. It is
  declared instead, as a tripwire:
  ``test_vice_binary_unit.py::TestFieldsTheWireTestsCannotSeparate``.
* ``response_type`` and ``error_code`` are separable only while they
  differ for the command being captured. RESOURCE_GET gives 0x51 against
  0x00, and :func:`test_the_mock_builder_reproduces_a_real_frame_exactly`
  now asserts that rather than relying on it.

The general rule, since this is the third instance: a test naming a
*structural* property — field order, identity, which register holds
what — whose oracle is a *value* comparison will go quiet the day those
values coincide. Where no black-box test can exist, say so out loud. A
guard that declares "I cannot see this" is worth more than one that
silently cannot.
"""
from __future__ import annotations

import socket
import struct

import pytest

from c64_test_harness.backends.vice_binary import (
    API_VERSION,
    CMD_RESOURCE_GET,
    REQUEST_HEADER_SIZE,
    RESPONSE_HEADER_SIZE,
    STX,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess

# The builder under test, imported from the module it serves so that a
# change to it is caught here.
from test_vice_binary_unit import _build_response_bytes

pytestmark = pytest.mark.vice_live


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def raw_monitor():
    """A raw TCP socket speaking to a real VICE binary monitor."""
    cfg = ViceConfig(port=free_port(), warp=True, sound=False, console=True)
    proc = ViceProcess(cfg)
    proc.start()
    sock = None
    try:
        deadline = __import__("time").monotonic() + 30.0
        while __import__("time").monotonic() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", cfg.port), timeout=5.0)
                break
            except OSError:
                __import__("time").sleep(0.5)
        if sock is None:
            pytest.fail("VICE binary monitor never accepted a connection")
        sock.settimeout(10.0)
        yield sock
    finally:
        if sock is not None:
            sock.close()
        proc.stop()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError(f"monitor closed after {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def capture_real_response(sock: socket.socket, resource: str = b"Speed") -> bytes:
    """Send a RESOURCE_GET and return the complete raw response frame.

    Skips asynchronous events, which VICE may interleave and which carry
    the reserved request id ``0xFFFFFFFF``.
    """
    req_id = 0x1234ABCD
    body = bytes([len(resource)]) + resource
    header = struct.pack("<BBII", STX, API_VERSION, len(body), req_id)
    sock.sendall(header + bytes([CMD_RESOURCE_GET]) + body)

    for _ in range(20):
        head = _recv_exact(sock, RESPONSE_HEADER_SIZE)
        body_len = struct.unpack_from("<I", head, 2)[0]
        payload = _recv_exact(sock, body_len) if body_len else b""
        if struct.unpack_from("<I", head, 8)[0] == req_id:
            return head + payload
    raise AssertionError("no response carrying our request id arrived")


def test_a_real_response_frame_is_available(raw_monitor):
    """Guard the guard: a frame we cannot capture proves nothing.

    Every assertion below is vacuous if ``capture_real_response`` quietly
    returned something empty or malformed, so pin the premises first.
    """
    frame = capture_real_response(raw_monitor)
    assert len(frame) > RESPONSE_HEADER_SIZE, "captured no body at all"
    assert frame[0] == STX, f"first byte {frame[0]:#x} is not STX"
    assert frame[1] == API_VERSION, f"API version byte is {frame[1]:#x}"
    assert struct.unpack_from("<I", frame, 8)[0] == 0x1234ABCD


def test_the_mock_builder_reproduces_a_real_frame_exactly(raw_monitor):
    """The hand-written builder must be byte-identical to VICE's own.

    This is the assertion the mocked layer has been resting on unstated.
    Field order, field widths and endianness are all pinned at once: any
    of them wrong and the rebuilt frame diverges.
    """
    frame = capture_real_response(raw_monitor)
    body_len = struct.unpack_from("<I", frame, 2)[0]
    response_type = frame[6]
    error_code = frame[7]
    # This test can only see a response_type/error_code swap while the two
    # differ.  RESOURCE_GET replies 0x51 with error 0x00, so they do -- but
    # nothing enforces that, and a command whose response type were 0x00
    # would make the swap invisible here.  Pinned so the dependency is a
    # stated property rather than a lucky choice of command.
    assert response_type != error_code, (
        f"response_type and error_code are both {error_code:#04x}; this "
        f"frame cannot distinguish them, so the check below would pass "
        f"under a swap. Capture a different command."
    )
    request_id = struct.unpack_from("<I", frame, 8)[0]
    body = frame[RESPONSE_HEADER_SIZE:]

    assert error_code == 0x00, f"VICE reported error {error_code:#x} for Speed"
    assert len(body) == body_len

    rebuilt = _build_response_bytes(response_type, body, request_id, error_code)
    assert rebuilt == frame, (
        "test_vice_binary_unit._build_response_bytes does not reproduce a "
        "real VICE frame — every test that module feeds through it is "
        "asserting against a wire format VICE does not use"
    )


def test_the_request_header_size_constant_matches_the_wire(raw_monitor):
    """``REQUEST_HEADER_SIZE`` is asserted nowhere against a real monitor.

    It is 11 because the header is ``<BBII`` (10 bytes) plus the command
    byte.  A wrong value here desynchronises every request, so prove VICE
    accepts a frame built to it — a reply carrying our request id is only
    possible if the monitor parsed the header we sent.
    """
    assert REQUEST_HEADER_SIZE == struct.calcsize("<BBII") + 1
    frame = capture_real_response(raw_monitor)
    assert struct.unpack_from("<I", frame, 8)[0] == 0x1234ABCD, (
        "VICE did not echo our request id, so it did not parse our header"
    )
