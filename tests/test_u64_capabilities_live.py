"""Behavioural capability probes against a real Ultimate device.

These are the probes that settle the capabilities
:class:`DeviceCapabilities` reports as ``None`` — the ones that landed after
the "Bump to 3.15" commit, so every build on that line reports the same
version string whether or not it carries them.

Gated by ``U64_HOST`` like the other live suites, so they skip cleanly until
a device is deliberately pointed at.

Everything here is **read-only** except the tests in ``TestSocketLifetime``
(run on the U64E at bce4535e, 2026-09-23), which enable the Command
Interface and reset the C64.  They also skip without ``U64_ALLOW_MUTATE``: stricter
than the contract, which covers config changes only (#333).

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

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
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
    not answerable; after it, the payload comes back over Data More blocks
    with the *total* length in the first block's header.

    The harness drain is still single-block and 8-bit indexed
    (``SOCKET_READ_MAX_BYTES`` = 253), so this suite only establishes what
    the firmware accepts. Draining the continuation blocks is the follow-up
    work; until it lands, ``uci_socket_read`` logs a warning and returns the
    first block when the header reports more than arrived.
    """

    @pytest.mark.skip(
        reason="needs the 16-bit multi-block drain; see audit finding #4"
    )
    def test_read_socket_accepts_a_length_above_one_block(self) -> None:
        raise NotImplementedError(
            "Open a UDP socket, send a datagram larger than "
            "NET_FIRST_BLOCK_PAYLOAD (893), request it in one READ_SOCKET, "
            "and assert the concatenated blocks equal the datagram."
        )

    @pytest.mark.skip(
        reason="needs the 16-bit multi-block drain; see audit finding #4"
    )
    def test_oversized_read_is_rejected_not_truncated(self) -> None:
        raise NotImplementedError(
            "A READ_SOCKET length above NET_MAX_SOCKET_READ (1472) must draw "
            "the param-out-of-range status, not a silently clamped read."
        )


# ----------------------------------------- UCI socket lifetime (#808)
#: A handle the network target never handed out.  lwip numbers its sockets
#: from 0 up to MEMP_NUM_NETCONN (16 in the firmware's lwipopts.h), so no
#: OPEN can return this; a READ_SOCKET on it takes the ``owns_socket``
#: refusal -- the control the reset case is compared against.
_NEVER_OPENED = 0xC8

_UCI_CATEGORY = "C64 and Cartridge Settings"
_UCI_ITEM = "Command Interface"


@pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — socket-lifetime probes reset the C64",
)
class TestSocketLifetime:
    """#808 made the UCI network target own its sockets.

    At bce4535e (``software/io/network/network_target.cc``, PR #814) the
    target keeps a table of the sockets it opened, sized to every socket lwip
    can create (``NET_MAX_SOCKETS`` 16 = ``MEMP_NUM_NETCONN``); a C64 reset
    closes them all (``c64_reset`` -> ``close_all_sockets``), and a READ,
    WRITE or CLOSE on a handle not in the table answers ``EBADF`` without
    touching lwip.  There is no eviction: an OPEN past lwip's limit fails
    with ``85,ERROR OPENING SOCKET``.  Exhausting lwip is not probed here --
    the device's own HTTP server needs a socket per REST request.

    So a handle does not outlive a C64 reset, and the reset that
    ``_execute_uci_routine`` issues after a routine timeout (#313) ends every
    socket the caller held.  U64E only: the C64U is not probed.
    """

    @pytest.fixture(scope="class")
    def uci(self, client: Ultimate64Client):
        """``(transport, host_ip)`` with the Command Interface on and no sockets."""
        import socket as pysocket
        import time

        from c64_test_harness import uci_network as un
        from c64_test_harness.backends.ultimate64 import Ultimate64Transport
        from live_fixture_teardown import (
            attempt_steps,
            read_restore_defaults,
            restore_default_steps,
        )

        info = client.get_info()
        if info.get("product") != "Ultimate 64 Elite":
            pytest.skip(f"U64E only; device reports {info.get('product')!r}")
        plan = read_restore_defaults(client, {_UCI_CATEGORY: [_UCI_ITEM]})
        transport = Ultimate64Transport(host=_HOST or "", client=client)
        probe = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM)
        try:
            probe.connect((_HOST, 80))
            host_ip = probe.getsockname()[0]
        finally:
            probe.close()
        failures: list = []
        try:
            un.enable_uci(client)
            client.reset()          # also closes any socket a previous lane left
            time.sleep(3.0)
            yield transport, host_ip
        finally:
            failures = attempt_steps([
                ("client.reset()", client.reset),
                *restore_default_steps(client, plan),
            ])
        raise_teardown_failures("TestSocketLifetime uci teardown", failures)

    @staticmethod
    def _listener():
        import socket as pysocket

        s = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        s.settimeout(1.5)
        return s

    @staticmethod
    def _received(listener) -> bytes | None:
        import socket as pysocket

        try:
            return listener.recv(64)
        except pysocket.timeout:
            return None

    def test_sockets_do_not_survive_a_c64_reset(self, client, uci) -> None:
        import time

        from c64_test_harness import uci_network as un

        transport, host_ip = uci
        listener = self._listener()
        try:
            port = listener.getsockname()[1]
            sock = un.uci_udp_connect(transport, host_ip, port)
            un.uci_socket_write(transport, sock, b"before")
            assert self._received(listener) == b"before", (
                "the freshly opened socket did not deliver: the probe has no "
                "positive control"
            )
            # The instrument must tell the two apart before it is trusted.
            assert un.uci_socket_read(transport, sock, 16) == b""
            with pytest.raises(un.UCISocketNotOwnedError):
                un.uci_socket_read(transport, _NEVER_OPENED, 16)

            client.reset()
            time.sleep(3.0)

            with pytest.raises(un.UCISocketNotOwnedError):
                un.uci_socket_read(transport, sock, 16)
            with pytest.raises(un.UCISocketNotOwnedError):
                un.uci_socket_write(transport, sock, b"after")
            assert self._received(listener) is None, (
                f"handle {sock} still delivered a datagram after a C64 reset"
            )
        finally:
            listener.close()

    def test_closing_one_socket_leaves_the_others_open(self, client, uci) -> None:
        from c64_test_harness import uci_network as un

        transport, host_ip = uci
        listener = self._listener()
        socks: list[int] = []
        try:
            port = listener.getsockname()[1]
            socks = [un.uci_udp_connect(transport, host_ip, port) for _ in range(3)]
            assert len(set(socks)) == 3, f"three OPENs returned {socks}"

            un.uci_socket_close(transport, socks[1])

            with pytest.raises(un.UCISocketNotOwnedError):
                un.uci_socket_read(transport, socks[1], 16)
            for n, sock in ((0, socks[0]), (2, socks[2])):
                payload = f"open{n}".encode()
                un.uci_socket_write(transport, sock, payload)
                assert self._received(listener) == payload, (
                    f"socket {n} (handle {sock}) stopped delivering when "
                    f"handle {socks[1]} was closed"
                )
        finally:
            for sock in (socks[0], socks[2]) if len(socks) == 3 else ():
                try:
                    un.uci_socket_close(transport, sock)
                except Exception:  # noqa: BLE001 -- the fixture's reset closes it anyway
                    pass
            listener.close()
