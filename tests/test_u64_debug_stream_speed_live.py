"""Live investigation: does the U64E debug stream deliver a complete bus
trace at all turbo speeds, or only at 1 MHz?

Hypothesis (to prove/disprove):
    The Ultimate 64 Elite's UDP debug stream carries a complete cycle-accurate
    trace at 1 MHz, but at higher turbo speeds (4/8/16/24/48 MHz) the FPGA
    streamer can't keep up with the CPU's cycle rate and delivers a
    significantly truncated / undercounted trace.  A 48 MHz run should look
    qualitatively different from a 1 MHz run.

How this test measures "completeness":
    For each CPU speed ``mhz`` we put the U64 in ``6510 Only`` debug-stream
    mode, start a ``DebugCapture`` on the host, tell the U64 to stream, sleep
    exactly ``T_CAPTURE_SECONDS`` of wall-clock, and stop.  The expected
    number of CPU cycles over that window is ``T_CAPTURE_SECONDS * mhz * 1e6``
    (6510 runs at the nominal turbo rate; ``6510 Only`` filters out VIC
    cycles at the source).  The *delivery ratio* is

        ratio = delivered_cycles / expected_cycles

    At 1 MHz the stream bandwidth (~ 4 MB/s if every cycle is emitted) is
    comfortably within any reasonable UDP link, so ``ratio ≈ 1.0`` is the
    prediction.  At 48 MHz the naive cycle rate (~192 MB/s) exceeds typical
    100/1000 BASE-T capacity and, more to the point, the U64E's own FPGA
    UDP transmit path — so the stream is expected to be sparsely populated
    relative to reality.

Hypothesis is confirmed when, after a single contiguous sweep:
    * 1 MHz run delivers ratio >= MIN_RATIO_1MHZ  (near-complete)
    * 48 MHz run delivers ratio <= MAX_RATIO_48MHZ  (clearly incomplete)
    * ratios are monotonically non-increasing with MHz (higher speed →
      no more cycles delivered per wall-clock second than the streamer's
      fixed budget allows)

The test records a metrics table for every speed in the sweep and prints
it at the end regardless of outcome — the investigative value is in the
numbers, not just the pass/fail line.

Gates:
    * ``U64_HOST`` — required.  This is a live-hardware test.
    * ``U64_ALLOW_MUTATE=1`` — required.  The test changes Turbo Control
      and Debug Stream Mode on the device; it restores both on teardown,
      but the mutation gate is the repo convention.

Example::

    U64_HOST=192.168.1.81 U64_ALLOW_MUTATE=1 \\
        python3 -m pytest tests/test_u64_debug_stream_speed_live.py -v -s
"""
from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.u64_debug_capture import DebugCapture
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    DEBUG_MODE_6510,
    get_debug_stream_mode,
    get_turbo_mhz,
    set_debug_stream_mode,
    set_turbo_mhz,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment gates
# ---------------------------------------------------------------------------

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(
        not _HOST,
        reason="U64_HOST not set -- live Ultimate 64 tests disabled",
    ),
    pytest.mark.skipif(
        not _ALLOW_MUTATE,
        reason=(
            "U64_ALLOW_MUTATE not set -- this test changes Turbo Control and "
            "Debug Stream Mode on the device (restored on teardown)"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Tunables (module-level constants; change here to re-tune the experiment)
# ---------------------------------------------------------------------------

#: Wall-clock seconds to capture at each speed.  Needs to be large enough that
#: per-packet jitter averages out (>= 1s) but short enough that the full sweep
#: completes in well under a minute.  2.0s gives a solid statistical base at
#: 1 MHz (~2 M cycles expected) while keeping the sweep total under 30s.
T_CAPTURE_SECONDS: float = 2.0

#: Settling delay after ``set_turbo_mhz`` / ``set_debug_stream_mode`` before
#: starting a capture.  The U64 config endpoints return before the FPGA has
#: visibly latched the new clock divider on every firmware.
SETTLE_SECONDS: float = 0.5

#: Post-stop drain.  UDP packets may still be in flight when the REST stop
#: command returns; we give the background thread a moment to drain them
#: before calling ``cap.stop()``.
DRAIN_SECONDS: float = 0.3

#: Speeds to sweep, in ascending order.  Deliberately spans from the native
#: 1 MHz through the midrange (4, 16) to the 48 MHz extreme.  Intermediate
#: speeds can be added later if the four-point shape suggests a transition
#: band worth characterising.
SPEEDS_MHZ: tuple[int, ...] = (1, 4, 16, 48)

#: Hypothesis thresholds.  Tightened versions of "ratio near 1" and "ratio
#: well below 1".  Values are conservative so the test only fails when the
#: behaviour is qualitatively off — not on minor run-to-run jitter.
MIN_RATIO_1MHZ: float = 0.90
MAX_RATIO_48MHZ: float = 0.50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_ip() -> str:
    """Detect the local IP address that can reach the U64 (UDP peek trick)."""
    assert _HOST is not None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((_HOST, 80))
        return s.getsockname()[0]
    finally:
        s.close()


@dataclass
class _SpeedSample:
    """One row of the speed-vs-completeness investigation."""

    mhz: int
    expected_cycles: int
    delivered_cycles: int
    packets_received: int
    packets_dropped: int
    duration_seconds: float
    ratio: float = field(init=False)

    def __post_init__(self) -> None:
        self.ratio = (
            self.delivered_cycles / self.expected_cycles
            if self.expected_cycles > 0
            else 0.0
        )


def _capture_one_speed(
    client: Ultimate64Client,
    local: str,
    mhz: int,
) -> _SpeedSample:
    """Measure debug-stream delivery at a single CPU speed.

    Sets turbo to *mhz*, settles, then runs a ``DebugCapture`` for
    ``T_CAPTURE_SECONDS`` wall-clock seconds with the U64's debug stream
    pointed at ``local:11002``.  Returns a :class:`_SpeedSample`.
    """
    set_turbo_mhz(client, mhz)
    time.sleep(SETTLE_SECONDS)

    expected_cycles = int(T_CAPTURE_SECONDS * mhz * 1_000_000)

    cap = DebugCapture(port=11002)
    cap.start()
    started_stream = False
    try:
        client.stream_debug_start(f"{local}:11002")
        started_stream = True
        # Sleep the wall-clock capture window.  Using monotonic sleep rather
        # than an event-loop tick because we want the elapsed time reported
        # by DebugCaptureResult.duration_seconds to be close to T_CAPTURE.
        time.sleep(T_CAPTURE_SECONDS)
    finally:
        if started_stream:
            try:
                client.stream_debug_stop()
            except Exception:  # noqa: BLE001
                # Stopping is best-effort: if the U64 REST is momentarily
                # unresponsive the capture-stop path still cleans up.
                pass
        time.sleep(DRAIN_SECONDS)
        result = cap.stop()

    return _SpeedSample(
        mhz=mhz,
        expected_cycles=expected_cycles,
        delivered_cycles=result.total_cycles,
        packets_received=result.packets_received,
        packets_dropped=result.packets_dropped,
        duration_seconds=result.duration_seconds,
    )


def _format_samples(samples: list[_SpeedSample]) -> str:
    """Render the per-speed samples as a readable fixed-width table."""
    header = (
        f"{'MHz':>4}  {'duration':>9}  {'delivered':>12}  {'expected':>14}  "
        f"{'ratio':>6}  {'pkts':>7}  {'dropped':>8}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for s in samples:
        lines.append(
            f"{s.mhz:>4}  {s.duration_seconds:>9.3f}  "
            f"{s.delivered_cycles:>12,}  {s.expected_cycles:>14,}  "
            f"{s.ratio:>6.3f}  {s.packets_received:>7,}  "
            f"{s.packets_dropped:>8,}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    """Acquire the device lock and yield an Ultimate64Client for the module.

    Uses ``DeviceLock`` per the repo's U64 concurrency rule: multiple Claude
    agents sharing a U64 will corrupt each other's tests without it.
    """
    assert _HOST is not None
    lock = DeviceLock(_HOST)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    c = Ultimate64Client(host=_HOST, password=_PW, timeout=10.0)
    yield c
    lock.release()


@pytest.fixture(scope="module")
def original_state(client: Ultimate64Client):
    """Snapshot and restore turbo + debug-stream-mode around the sweep.

    We don't use :func:`snapshot_state` / :func:`restore_state` here because
    only two settings are in play and we want a targeted restore (the broad
    snapshot helpers can churn unrelated config on some firmwares).
    """
    orig_mhz = get_turbo_mhz(client)  # int | None
    orig_mode = get_debug_stream_mode(client)

    set_debug_stream_mode(client, DEBUG_MODE_6510)
    time.sleep(SETTLE_SECONDS)

    try:
        yield
    finally:
        # Best-effort restore: turbo first (back to 1 MHz or original), then
        # the debug stream mode.  Ignore individual failures so one broken
        # restore doesn't prevent the other from running.
        try:
            set_turbo_mhz(client, orig_mhz)
        except Exception:  # noqa: BLE001
            logger.exception("failed to restore turbo to %r", orig_mhz)
        try:
            if orig_mode:
                set_debug_stream_mode(client, orig_mode)
        except Exception:  # noqa: BLE001
            logger.exception("failed to restore debug stream mode to %r", orig_mode)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_debug_stream_completeness_vs_cpu_speed(
    client: Ultimate64Client,
    original_state: None,  # noqa: ARG001 -- fixture used for setup/teardown
) -> None:
    """Sweep CPU speeds and verify the "1 MHz only" hypothesis.

    Captures one sample per speed in ``SPEEDS_MHZ``.  Always prints the
    full measured table (even on failure) so the run's investigative value
    survives any one threshold assertion.
    """
    local = _local_ip()
    logger.info(
        "speed-sweep debug-stream investigation starting: "
        "local=%s, speeds=%s, T=%.2fs",
        local, SPEEDS_MHZ, T_CAPTURE_SECONDS,
    )

    samples: list[_SpeedSample] = []
    for mhz in SPEEDS_MHZ:
        sample = _capture_one_speed(client, local, mhz)
        samples.append(sample)
        logger.info(
            "mhz=%d delivered=%d expected=%d ratio=%.3f "
            "packets=%d dropped=%d duration=%.2fs",
            sample.mhz, sample.delivered_cycles, sample.expected_cycles,
            sample.ratio, sample.packets_received, sample.packets_dropped,
            sample.duration_seconds,
        )

    # Print the full table unconditionally so CI logs / `-s` runs keep the
    # data even when one of the downstream asserts fails.
    table = _format_samples(samples)
    print("\nDebug-stream completeness vs CPU speed:\n" + table)
    logger.info("speed-sweep complete:\n%s", table)

    # ---- Hypothesis checks -------------------------------------------------
    by_mhz: dict[int, _SpeedSample] = {s.mhz: s for s in samples}

    if 1 in by_mhz:
        s1 = by_mhz[1]
        assert s1.ratio >= MIN_RATIO_1MHZ, (
            f"Baseline broken: at 1 MHz the debug stream should be nearly "
            f"complete, but ratio={s1.ratio:.3f} < MIN_RATIO_1MHZ="
            f"{MIN_RATIO_1MHZ:.2f}.  This invalidates the experiment; the "
            f"stream is not delivering even at 1x.  Full table:\n{table}"
        )

    if 48 in by_mhz:
        s48 = by_mhz[48]
        assert s48.ratio <= MAX_RATIO_48MHZ, (
            f"Hypothesis NOT confirmed: at 48 MHz the debug stream delivered "
            f"ratio={s48.ratio:.3f} >= MAX_RATIO_48MHZ={MAX_RATIO_48MHZ:.2f}; "
            f"the stream appears to keep up even at the highest turbo speed. "
            f"Full table:\n{table}"
        )

    # Monotonic non-increasing ratio with MHz.  A small amount of jitter is
    # fine — we allow each step to increase by at most MONO_EPSILON before
    # treating it as a regression in the trend.
    MONO_EPSILON = 0.05
    for prev, curr in zip(samples, samples[1:]):
        assert curr.ratio <= prev.ratio + MONO_EPSILON, (
            f"Non-monotonic: ratio went UP from {prev.mhz} MHz "
            f"({prev.ratio:.3f}) to {curr.mhz} MHz ({curr.ratio:.3f}); "
            f"expected higher MHz => lower ratio (stream falling behind). "
            f"Full table:\n{table}"
        )
