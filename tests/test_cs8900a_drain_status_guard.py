"""``drain_status_addr`` may not land on the routine's other addresses (#487).

The drain stores its remaining budget with ``STX drain_status_addr``.  If
that address is ``result_addr`` the result overwrites it; inside a frame
buffer it corrupts the frame before it is copied to the chip; inside the
routine's own span the store patches the code that is running.  Found in
review of #488 and folded into #487; nothing refused any of it before.
"""
from __future__ import annotations

import pytest

import c64_test_harness.bridge_ping as bp

LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT, OK_STATUS = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300, 0x5310


def _tx(status: int) -> bytes:
    return bp.build_tx_code(LOAD, TX_BUF, 60, RESULT, drain_first=True, drain_status_addr=status)


def _ping(status: int) -> bytes:
    return bp.build_ping_and_wait_code(LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1, arp_frame_buf=ARP_BUF,
                                       drain_first=True, drain_status_addr=status)


def _tod(status: int) -> bytes:
    return bp.build_ping_and_wait_tod_code(LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1, arp_frame_buf=ARP_BUF,
                                           drain_first=True, drain_status_addr=status)


BUILDERS = {"build_tx_code": _tx, "build_ping_and_wait_code": _ping, "build_ping_and_wait_tod_code": _tod}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_a_status_clear_of_everything_builds(name: str) -> None:
    assert BUILDERS[name](OK_STATUS)


@pytest.mark.parametrize("name", sorted(BUILDERS))
@pytest.mark.parametrize("status, what", [
    (RESULT, "result_addr"),
    (TX_BUF, "frame"), (TX_BUF + 59, "frame"),
    (LOAD, "routine"), (LOAD + 40, "routine"),
])
def test_a_colliding_status_is_refused(name: str, status: int, what: str) -> None:
    with pytest.raises(ValueError, match=rf"^{name} drain_status_addr \${status:04X} overlaps .*{what}"):
        BUILDERS[name](status)


@pytest.mark.parametrize("name", ["build_ping_and_wait_code", "build_ping_and_wait_tod_code"])
def test_the_arp_frame_counts_as_a_frame(name: str) -> None:
    with pytest.raises(ValueError, match=r"overlaps .*frame"):
        BUILDERS[name](ARP_BUF + 10)


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_the_byte_after_each_span_is_allowed(name: str) -> None:
    """Half-open spans: the first byte past a frame or the routine is free."""
    code_len = len(BUILDERS[name](OK_STATUS))
    assert BUILDERS[name](LOAD + code_len)
    assert BUILDERS[name](TX_BUF + 60) if name == "build_tx_code" else True
