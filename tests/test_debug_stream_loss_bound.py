"""The debug-stream loss bound sits between the bench's network and a broken receiver.

``test_u64_streams_live.py::test_debug_stream_captures_cycles`` used to
require ``packets_dropped < 50`` and failed on master most runs (#356).  It
now bounds the *fraction* of the stream lost to sequence gaps,
``_debug_loss_fraction(result)``, by ``DEBUG_STREAM_LOSS_MAX``.  A live run
cannot show that bound doing anything, because a healthy capture never
approaches it (raising the bound to 1.0 survives every live run).  So this
module pins it offline, with no device, from both sides:

* **Below it:** the worst loss measured on the bench, 0.446
  (U64E fw 3.15 bce4535e, 2026-09-15, 28 captures, Wi-Fi link, kernel
  socket-buffer drops zero).  A measured figure, restated from the comment
  above ``DEBUG_STREAM_LOSS_MAX`` in the live module, not derived.
* **Above it:** what the receiver's own accounting reports when the
  sequence number is read in the wrong byte order.  That is *measured here*,
  on loopback, through the real ``DebugCapture``: the packets carry
  consecutive sequence numbers byte-swapped, which is exactly what a
  byte-order bug in the parser sees.

Two controls send correctly ordered streams whose accounting is known exactly
-- one gap, and one backward step -- so a receiver that miscounts in either
direction, or counts a reorder as a near-65,536-packet gap, fails here rather
than on the bench.  The fraction helper is imported from the live module, so
the arithmetic checked here is the arithmetic the live assertion runs.
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
    DebugCaptureResult,
)
from test_u64_streams_live import (
    DEBUG_STREAM_LOSS_MAX,
    _debug_loss_fraction,
    _debug_loss_within_bound,
)

#: Worst loss fraction measured on the bench (#356).  Restates the "max 0.446"
#: in the comment above ``DEBUG_STREAM_LOSS_MAX`` in
#: ``test_u64_streams_live.py``; change both together.
MEASURED_NETWORK_LOSS_MAX = 0.446

_PAYLOAD = bytes(ENTRIES_PER_PACKET * ENTRY_SIZE)


def _capture(seqs: list[int]) -> DebugCaptureResult:
    """Send one packet per sequence number to a loopback capture.

    Paced so the 256 KiB socket buffer cannot overflow, and waits (on the
    public ``packets_received``) for every packet to be counted.
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
        while time.monotonic() < deadline and cap.packets_received < len(seqs):
            time.sleep(0.01)
    finally:
        result = cap.stop()
    assert result.packets_received == len(seqs), (
        f"loopback lost packets ({result.packets_received}/{len(seqs)}); "
        "the measurement below would be meaningless"
    )
    return result


def test_the_bound_is_pinned() -> None:
    """Chosen, not derived: moving it must be a deliberate edit here too."""
    assert DEBUG_STREAM_LOSS_MAX == pytest.approx(0.75)


def test_the_receiver_counts_a_known_gap_exactly() -> None:
    """Control: 0..49 then 60..109 is 100 received and exactly 10 dropped.

    The loss fraction is 10/110 -- the denominator counts dropped packets
    too, so a fraction over received packets only (10/100) fails here.
    """
    result = _capture(list(range(0, 50)) + list(range(60, 110)))
    assert (result.packets_received, result.packets_dropped) == (100, 10)
    assert _debug_loss_fraction(result) == pytest.approx(10 / 110)
    assert _debug_loss_within_bound(result) is True


def test_a_backward_step_is_not_counted_as_a_huge_gap() -> None:
    """Control: 0..49, 48, 50..99 is 101 received and exactly 1 dropped.

    The step back to 48 is a duplicate.  Without the ``gap < 0x8000`` guard,
    49 -> 48 is read as a forward gap of 65,535 packets, a loss fraction near
    1.0; with it the step is ignored.

    The 1 is current receiver behaviour, pinned rather than endorsed:
    ``_recv_loop`` sets ``_last_seq`` to the backward sequence number, so the
    next packet (50, expected 49) counts as one drop.  A duplicate therefore
    overcounts ``packets_dropped`` by one; measured here, reported under #356.
    """
    result = _capture(list(range(0, 50)) + [48] + list(range(50, 100)))
    assert (result.packets_received, result.packets_dropped) == (101, 1)
    assert _debug_loss_fraction(result) == pytest.approx(1 / 102)
    assert _debug_loss_within_bound(result) is True


def test_the_bound_admits_the_worst_measured_network_loss() -> None:
    assert MEASURED_NETWORK_LOSS_MAX <= DEBUG_STREAM_LOSS_MAX


def test_the_bound_rejects_a_byte_swapped_sequence_number() -> None:
    """Consecutive sequence numbers, as a wrong-byte-order parser reads them.

    Each +1 step becomes a +256 jump, so the capture reports 255 drops per
    packet.  The bound must sit below that, with the network max below it,
    and the live assertion's own comparison must reject it.
    """
    swapped = [struct.unpack("<H", struct.pack(">H", n))[0] for n in range(1, 101)]
    result = _capture(swapped)
    loss = _debug_loss_fraction(result)
    assert loss > 0.99, (result.packets_received, result.packets_dropped)
    assert MEASURED_NETWORK_LOSS_MAX < DEBUG_STREAM_LOSS_MAX < loss
    assert _debug_loss_within_bound(result) is False
