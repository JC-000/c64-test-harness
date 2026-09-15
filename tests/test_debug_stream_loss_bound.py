"""The debug-stream loss bound sits between the bench's network and a broken receiver.

``test_u64_streams_live.py::test_debug_stream_captures_cycles`` used to
require ``packets_dropped < 50`` and failed on master most runs (#356).  It
now bounds the *fraction* of the stream lost to sequence gaps by
``DEBUG_STREAM_LOSS_MAX``.  A live run cannot show that bound doing
anything, because a healthy capture never approaches it (raising the bound
to 1.0 survives every live run).  So this module pins it offline, with no
device, from both sides:

* **Below it:** the worst loss measured on the bench, 0.446
  (U64E fw 3.15 bce4535e, 2026-09-15, 28 captures, Wi-Fi link, kernel
  socket-buffer drops zero).  A measured figure, restated here, not derived.
* **Above it:** what the receiver's own accounting reports when the
  sequence number is read in the wrong byte order.  That is *measured here*,
  on loopback, through the real ``DebugCapture``: the packets carry
  consecutive sequence numbers byte-swapped, which is exactly what a
  byte-order bug in the parser sees.

The control sends a correctly ordered stream with one known gap and
requires the accounting to be exact, so a receiver that miscounts in either
direction fails here rather than on the bench.
"""
from __future__ import annotations

import socket
import struct
import time

import pytest

from c64_test_harness.backends.u64_debug_capture import (
    ENTRIES_PER_PACKET,
    ENTRY_SIZE,
    DebugCapture,
)
from test_u64_streams_live import DEBUG_STREAM_LOSS_MAX

#: Worst loss fraction measured on the bench (#356); see the module docstring.
MEASURED_NETWORK_LOSS_MAX = 0.446

_PAYLOAD = bytes(ENTRIES_PER_PACKET * ENTRY_SIZE)


def _capture(seqs: list[int]) -> tuple[int, int]:
    """Send one packet per sequence number to a loopback capture.

    Returns ``(packets_received, packets_dropped)``.  Paced so the 256 KiB
    socket buffer cannot overflow, and waits for every packet to be counted.
    """
    cap = DebugCapture(port=0, bind_addr="127.0.0.1")
    cap.start()
    try:
        dest = cap._sock.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for i, seq in enumerate(seqs):
                tx.sendto(struct.pack("<HH", seq, 0) + _PAYLOAD, dest)
                if i % 20 == 19:
                    time.sleep(0.005)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with cap._lock:
                if cap._packets_received >= len(seqs):
                    break
            time.sleep(0.01)
    finally:
        result = cap.stop()
    assert result.packets_received == len(seqs), (
        f"loopback lost packets ({result.packets_received}/{len(seqs)}); "
        "the measurement below would be meaningless"
    )
    return result.packets_received, result.packets_dropped


def _loss(received: int, dropped: int) -> float:
    return dropped / (received + dropped)


def test_the_bound_is_pinned() -> None:
    """Chosen, not derived: moving it must be a deliberate edit here too."""
    assert DEBUG_STREAM_LOSS_MAX == pytest.approx(0.75)


def test_the_receiver_counts_a_known_gap_exactly() -> None:
    """Control: 0..49 then 60..109 is 100 received and exactly 10 dropped."""
    seqs = list(range(0, 50)) + list(range(60, 110))
    received, dropped = _capture(seqs)
    assert (received, dropped) == (100, 10)


def test_the_bound_admits_the_worst_measured_network_loss() -> None:
    assert MEASURED_NETWORK_LOSS_MAX < DEBUG_STREAM_LOSS_MAX


def test_the_bound_rejects_a_byte_swapped_sequence_number() -> None:
    """Consecutive sequence numbers, as a wrong-byte-order parser reads them.

    Each +1 step becomes a +256 jump, so the capture reports 255 drops per
    packet.  The bound must sit below that, with the network max below it.
    """
    swapped = [struct.unpack("<H", struct.pack(">H", n))[0] for n in range(1, 101)]
    received, dropped = _capture(swapped)
    loss = _loss(received, dropped)
    assert loss > 0.99, (received, dropped)
    assert MEASURED_NETWORK_LOSS_MAX < DEBUG_STREAM_LOSS_MAX < loss
