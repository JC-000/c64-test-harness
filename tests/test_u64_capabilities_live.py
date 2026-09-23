"""Behavioural capability probes against a real Ultimate device.

**Staged, not yet run.** These are the probes that settle the capabilities
:class:`DeviceCapabilities` reports as ``None`` — the ones that landed after
the "Bump to 3.15" commit, so every build on that line reports the same
version string whether or not it carries them.

Gated by ``U64_HOST`` like the other live suites, so they skip cleanly until
a device is deliberately pointed at.

Everything here is **read-only** except two classes.  ``TestSocketLifetime``
resets the C64; it also skips without ``U64_ALLOW_MUTATE``: stricter than the
contract, which covers config changes only (#333).  ``TestSocketReadCeiling``
runs UCI routines -- RAM writes through ``transport.write_memory`` and a typed
``SYS`` -- which #333 allows on ``U64_HOST`` alone.  It writes no config: it
skips unless ``Command Interface`` is already Enabled, and it runs only on a
post-safe Ultimate-line device (the U64E), never the C64U.

What each probe distinguishes
-----------------------------
=========================  ====================================  ============
Capability                 Probe                                 Upstream PR
=========================  ====================================  ============
readmem_rejects_zero_len   ``readmem?length=0`` 400 vs 200       #760
uci_socket_read_multiblock ``READ_SOCKET`` accepts len > 893     #802/#806
uci_sockets_close_on_reset socket handle invalid after reset     #808
=========================  ====================================  ============

Recording a result
------------------
Feed a confirmed probe back into the capability set rather than re-probing::

    caps = DeviceCapabilities.from_info(
        client.get_info(),
        overrides={"readmem_rejects_zero_length": True},
    )
"""
from __future__ import annotations

import os
import socket
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_helpers import (
    restore_speed_defaults,
    set_turbo_mhz,
)
from c64_test_harness.uci_network import (
    NET_MAX_SOCKET_READ,
    _DATA_ADDR,
    _RESP_LEN_ADDR,
    _execute_uci_routine,
    _read_status_string,
    build_socket_read,
    get_uci_enabled,
    uci_socket_close,
    uci_socket_read,
    uci_socket_write,
    uci_udp_connect,
)
from live_fixture_teardown import raise_teardown_failures, teardown_then_release

_HOST = os.environ.get("U64_HOST")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set — skipping live U64 tests"
)


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    password = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(_HOST or "")
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    client = None
    failures: list = []
    try:
        client = Ultimate64Client(host=_HOST or "", password=password, timeout=10.0)
        yield client
    finally:
        steps = [] if client is None else [("client.close()", client.close)]
        failures = teardown_then_release(steps, lock.release)
    raise_teardown_failures('capabilities client teardown', failures)


# --------------------------------------------------------------- version read
class TestVersionDerived:
    """The half of the capability set the version string does settle."""

    def test_capabilities_match_the_reported_firmware(
        self, client: Ultimate64Client
    ) -> None:
        caps = client.capabilities
        assert caps.firmware_version, "device reported no firmware_version"
        assert caps.generation in ("ultimate", "cbm")
        # writemem_post_safe is version-derivable on both lines.
        assert isinstance(caps.writemem_post_safe, bool)
        assert caps.runner_wedge_possible is not caps.writemem_post_safe

    def test_threshold_follows_the_capability_not_the_string(
        self, client: Ultimate64Client
    ) -> None:
        caps = client.capabilities
        expected = 48 if caps.writemem_post_safe else 128
        assert client.write_mem_query_threshold == expected


# ------------------------------------------------- readmem bounds (#760)
class TestReadmemBounds:
    """``length=0`` became 400 when readmem stopped using ``new[]``.

    The old handler answered 200 with an empty body. ``malloc_allocate``
    returns NULL for a zero-size request, so keeping length=0 valid would
    turn a well-formed request into a spurious 500 — hence the 400.
    """

    def test_zero_length_readmem_is_rejected(
        self, client: Ultimate64Client
    ) -> None:
        with pytest.raises((Ultimate64Error, ValueError)):
            client.read_mem(0x0400, 0)

    def test_probe_records_the_capability(
        self, client: Ultimate64Client
    ) -> None:
        """Whatever the device does, it must be recordable as a capability."""
        try:
            client.read_mem(0x0400, 0)
            observed = False
        except (Ultimate64Error, ValueError):
            observed = True
        caps = DeviceCapabilities.from_info(
            client.get_info(),
            overrides={"readmem_rejects_zero_length": observed},
        )
        assert caps.readmem_rejects_zero_length is observed


# --------------------------------------- UCI socket read length ceiling (#802)
class TestSocketReadCeiling:
    """3.15 raised the accepted read length to 1472 and split the reply.

    Before #802 a ``READ_SOCKET`` for more than one reply block's worth was
    refused; after it, the payload comes back over Data More blocks with the
    *total* length in the first block's header.  ``uci_socket_read`` drains
    those blocks above 253 bytes (#420); these two tests are its device
    check.  U64E only: see the module docstring.
    """

    @pytest.fixture
    def uci(self, client: Ultimate64Client) -> Ultimate64Transport:
        caps = client.capabilities
        if caps.generation != "ultimate" or not caps.writemem_post_safe:
            pytest.skip(
                f"U64E only: {caps.generation} fw {caps.firmware_version} is "
                "not a post-safe Ultimate-line device"
            )
        if not get_uci_enabled(client):
            pytest.skip(
                "Command Interface is Disabled; this suite writes no config "
                "-- enable it deliberately first"
            )
        return Ultimate64Transport(host=_HOST or "", client=client)

    @pytest.mark.parametrize("size", [100, 253, 894, NET_MAX_SOCKET_READ])
    def test_read_socket_accepts_a_length_above_one_block(
        self, uci: Ultimate64Transport, size: int
    ) -> None:
        """A datagram of *size* comes back whole through ``uci_socket_read``.

        100 and 253 take the single-block routine (the regression control);
        894 is one byte into a continuation block and 1472 two blocks
        (893 + 579).  Assumes a 1500-byte MTU on every hop between host and
        device (1472 is the largest unfragmented IPv4 UDP payload), and a C64
        at 1 MHz with Turbo Control Off -- the bench baseline -- since the
        routine is the plain one, not ``turbo_safe``.
        """
        datagram = _pattern(size)
        assert _round_trip(uci, datagram, size, turbo_safe=False) == datagram

    @pytest.mark.skipif(
        not _ALLOW_MUTATE,
        reason="U64_ALLOW_MUTATE not set -- this test changes CPU speed",
    )
    def test_a_1472_byte_read_at_48_mhz_with_turbo_safe(
        self, uci: Ultimate64Transport, client: Ultimate64Client
    ) -> None:
        """The fenced multi-block routine at 48 MHz (#479 finding 3 is the
        1 MHz timeout; this is the speed the fence exists for).  Writes
        ``Turbo Control`` / ``CPU Speed`` and puts both back to the device's
        defaults afterwards.  Same MTU assumption as above."""
        datagram = _pattern(NET_MAX_SOCKET_READ)
        set_turbo_mhz(client, 48)
        try:
            time.sleep(0.3)
            got = _round_trip(uci, datagram, NET_MAX_SOCKET_READ, turbo_safe=True)
        finally:
            restore_speed_defaults(client)
        assert got == datagram

    def test_the_firmware_refuses_a_read_above_its_ceiling(
        self, uci: Ultimate64Transport
    ) -> None:
        """1473 draws ``82,PARAMETER(S) OUT OF RANGE`` and an empty reply.

        ``uci_socket_read`` refuses 1473 before touching the device, so this
        drives the routine directly.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((_HOST, 80))
            host_ip = probe.getsockname()[0]
        sid = uci_udp_connect(uci, host_ip, 9)
        try:
            uci.write_memory(_DATA_ADDR, bytes([sid]))
            code = build_socket_read(
                _DATA_ADDR, max_len=NET_MAX_SOCKET_READ + 1, multi_block=True,
            )
            _execute_uci_routine(uci, code)
            drained = uci.read_memory(_RESP_LEN_ADDR, 2)
            status = _read_status_string(uci)
        finally:
            uci_socket_close(uci, sid)
        assert status.startswith("82"), status
        assert drained[0] | (drained[1] << 8) == 0


def _pattern(n: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


def _round_trip(uci: Ultimate64Transport, datagram: bytes, max_len: int, *,
                turbo_safe: bool) -> bytes:
    """Send *datagram* from the host to a UCI UDP socket and read it back.

    The C64 writes one small datagram first, so the host learns which port
    to answer; the reply is then read with one ``uci_socket_read``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((_HOST, 80))
        host_ip = probe.getsockname()[0]
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("", 0))
    listener.settimeout(5.0)
    sid = None
    try:
        sid = uci_udp_connect(uci, host_ip, listener.getsockname()[1],
                              turbo_safe=turbo_safe)
        uci_socket_write(uci, sid, b"hello", turbo_safe=turbo_safe)
        _, c64_addr = listener.recvfrom(64)
        listener.sendto(datagram, c64_addr)
        time.sleep(0.5)
        return uci_socket_read(uci, sid, max_len, turbo_safe=turbo_safe)
    finally:
        if sid is not None:
            uci_socket_close(uci, sid, turbo_safe=turbo_safe)
        listener.close()


# ----------------------------------------- UCI socket lifetime (#808)
@pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — socket-lifetime probes reset the C64",
)
class TestSocketLifetime:
    """#808 bounded the socket table and closes it on C64 reset.

    Two behaviours changed at once:

    * ``NET_MAX_SOCKETS`` is 4, and opening past it closes the oldest first,
      so an ``OPEN_*`` always succeeds rather than failing once lwip's eight
      UDP control blocks are exhausted;
    * a C64 reset closes every socket the target opened for its client,
      because the program that owned them is gone.

    Any harness flow that resets and then reuses a handle is now broken by
    design — four live UCI suites call ``client.reset()``.
    """

    @pytest.mark.skip(reason="staged — awaiting device all-clear")
    def test_sockets_do_not_survive_a_c64_reset(self) -> None:
        raise NotImplementedError(
            "Open a UDP socket, reset the C64, settle >=3s for the boot "
            "RAM-walk, then assert a READ_SOCKET on the old handle answers "
            "EBADF rather than reading."
        )

    @pytest.mark.skip(reason="staged — awaiting device all-clear")
    def test_opening_past_the_table_evicts_the_oldest(self) -> None:
        raise NotImplementedError(
            "Open NET_MAX_SOCKETS + 1 sockets; assert every OPEN succeeds and "
            "the first handle is the one that stopped working."
        )
