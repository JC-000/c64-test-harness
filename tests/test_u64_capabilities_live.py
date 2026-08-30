"""Behavioural capability probes against a real Ultimate device.

**Staged, not yet run.** These are the probes that settle the capabilities
:class:`DeviceCapabilities` reports as ``None`` — the ones that landed after
the "Bump to 3.15" commit, so every build on that line reports the same
version string whether or not it carries them.

Gated by ``U64_HOST`` like the other live suites, so they skip cleanly until
a device is deliberately pointed at.

Everything here is **read-only** except the tests in ``TestSocketLifetime``,
which reset the C64 and so additionally require ``U64_ALLOW_MUTATE``.

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
    yield Ultimate64Client(host=_HOST or "", password=password, timeout=10.0)
    lock.release()


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
