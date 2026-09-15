"""Live integration tests for Ultimate64Transport.

Gated by the ``U64_HOST`` env var — e.g.:

    U64_HOST=<device> U64_ALLOW_MUTATE=1 python3 -m pytest tests/test_ultimate64_transport_live.py -v

Most tests are read-only and run on ``U64_HOST`` alone. The
``TestSetSpeed`` class and the device-touching ``TestResetScopes`` tests
added for PR #122 coverage exercise the protocol's ``set_speed`` /
``get_speed`` / ``reset(scope=...)`` surface on real hardware; they write
CPU speed and therefore also need ``U64_ALLOW_MUTATE=1`` (#268), and they
request the module-scoped ``speed_baseline`` fixture.

**Known state on entry, not on exit (#276).** ``speed_baseline``
reconciles the device when it starts, through the merged entry-baseline
mechanism: ``resolve_baseline_on_entry`` for the device's generation, then
``apply_factory_baseline`` -- on by default for the Ultimate line (U64E),
off for the C64 Ultimate and for a device that cannot be identified.  This
module is stricter than the manager in one respect: it **never** resets a
device that is not graded ``ultimate``, not even when
``U64_BASELINE_ON_ENTRY=1`` asks, because ``apply_factory_baseline`` has no
generation gate of its own.  It then sets 1 MHz, the one item these tests
own, on every generation.  At exit it writes ``CPU Speed`` and ``Turbo
Control`` back to the ``default`` each reported at entry (#360: ``set_speed(1)``
alone left ``CPU Speed`` behind); that restore and the
``DeviceLock`` release are attempted on every path a live process survives
-- an exception at the ``yield``, a raising ``close()``, a raising
constructor -- and a restore that fails is raised, not swallowed.  Neither
runs on SIGKILL: on the U64E the next run's entry reset clears what a killed
run left; **on a C64 Ultimate nothing does**, so after an abnormal exit
read ``U64 Specific Settings`` / ``Turbo Control`` and ``CPU Speed`` with
``get_config_item`` (``current`` against ``default``; bodyless GETs) before
trusting a timing measurement.  Pinned offline by
``tests/test_transport_live_fixture_teardown.py``.  Each test still
restores 1 MHz in its own ``finally`` as well.

The ``reset(scope='machine')`` case triggers a full FPGA reboot (~8 s
to recover) and is therefore gated by an additional ``U64_DESTRUCTIVE=1``
env var. When that variable is unset the test skips cleanly so
module-scoped state is never disturbed by default.
"""
from __future__ import annotations

import logging
import os
import time

import pytest

from c64_test_harness import apply_factory_baseline
from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_baseline import resolve_baseline_on_entry
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_U64_SPECIFIC,
    get_turbo_mhz,
    max_cpu_speed_mhz,
)
from c64_test_harness.backends.ultimate64_probe import is_u64_reachable
from c64_test_harness.transport import C64Transport

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_DESTRUCTIVE = os.environ.get("U64_DESTRUCTIVE") == "1"

_ALLOW_MUTATE = bool(os.environ.get("U64_ALLOW_MUTATE"))

pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="U64_HOST not set — live Ultimate device tests disabled",
)

#: Tests that write CPU speed (every ``set_speed`` call is a config PUT)
#: need ``U64_ALLOW_MUTATE`` as well (#268).
_requires_mutate = pytest.mark.skipif(
    not _ALLOW_MUTATE,
    reason="U64_ALLOW_MUTATE not set — this test writes CPU speed",
)


_log = logging.getLogger(__name__)


def _teardown_steps(steps):
    """Attempt every step; return ``(label, exception)`` for each that raised.

    Nothing stops early: a step that raises is logged at WARNING and the
    next one still runs (#276 -- a raising ``close()`` used to orphan the
    ``DeviceLock``).
    """
    failures = []
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 -- collected, raised below
            _log.warning("teardown step %s failed: %r", label, exc)
            failures.append((label, exc))
    return failures


def _raise_teardown_failures(what, failures):
    """A restore that did not happen says so."""
    if failures:
        detail = "; ".join(f"{label} failed: {exc!r}" for label, exc in failures)
        raise RuntimeError(f"{what}: {detail}") from failures[0][1]


def _locked_transport(host, password):
    """Lock, construct, yield; then close and release -- the lock last, always.

    The ``try`` opens immediately after the lock is taken, so a constructor
    that raises, an exception arriving at the ``yield`` (``throw``,
    ``GeneratorExit``) and a raising ``close()`` all still release it.  A
    failed ``close()`` is raised after the release when nothing else is
    already propagating.
    """
    lock = DeviceLock(host)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {host}")
    t = None
    failures = []
    try:
        t = Ultimate64Transport(host=host, password=password, timeout=8.0)
        yield t
    finally:
        try:
            if t is not None:
                failures = _teardown_steps([("transport.close()", t.close)])
        finally:
            lock.release()
    _raise_teardown_failures("transport teardown", failures)


def _reconcile_on_entry(client):
    """Entry-time reconciliation through the merged mechanism (#276, #285).

    ``resolve_baseline_on_entry`` decides exactly as the manager's acquire
    does -- the generation default (on for ``ultimate`` only), overridden
    by ``U64_BASELINE_ON_ENTRY`` -- and ``apply_factory_baseline`` performs
    it.  One refusal the manager does not make: a device not graded
    ``ultimate`` is never reset here, even when the override asks, because
    ``apply_factory_baseline`` itself has no generation gate and the C64
    Ultimate must never be reset at entry by an unattended run.
    """
    try:
        generation = client.capabilities.generation
    except Exception as exc:  # noqa: BLE001 -- an ungradeable device resolves off
        _log.info("entry baseline: generation unreadable (%r), grading unknown", exc)
        generation = "unknown"
    if not isinstance(generation, str):
        generation = "unknown"
    enabled, why = resolve_baseline_on_entry(generation)
    if generation != "ultimate":
        if enabled:
            _log.warning(
                "entry baseline REFUSED on %s (generation=%s) although %s: this "
                "module never resets a device not graded 'ultimate' at entry",
                _HOST, generation, why,
            )
        else:
            _log.info(
                "entry baseline skipped on %s (generation=%s): %s",
                _HOST, generation, why,
            )
        return None
    if not enabled:
        _log.info("entry baseline skipped on %s (generation=%s): %s", _HOST, generation, why)
        return None
    report = apply_factory_baseline(client)
    _log.info("entry baseline ran on %s (%s): %s", _HOST, why, report.summary())
    return report


#: The two config items the speed tests write, in the order they are
#: restored -- ``CPU Speed`` before ``Turbo Control``, the order
#: ``set_turbo_mhz`` writes them in.
_SPEED_ITEMS = ("CPU Speed", "Turbo Control")


def _speed_item_defaults(client):
    """``{item: default}`` for :data:`_SPEED_ITEMS`, read at entry (#360).

    Bodyless GETs.  An item whose ``current`` already differs from its
    ``default`` is logged at WARNING -- on a C64 Ultimate, where the entry
    reset never runs, that is a predecessor's residue this module is about
    to clear.  A map with no usable ``default`` raises: the exit restore
    could not honour ``current == default``, so the tests do not start.
    """
    defaults = {}
    for item in _SPEED_ITEMS:
        entry = client.get_config_item(CAT_U64_SPECIFIC, item)
        default = entry.get("default") if isinstance(entry, dict) else None
        if not isinstance(default, str) or not default:
            raise RuntimeError(
                f"{CAT_U64_SPECIFIC} / {item}: no default in {entry!r}; "
                "cannot restore it at exit"
            )
        if entry.get("current") != default:
            _log.warning(
                "%s / %s drifted at entry: current=%r default=%r; it will be "
                "written to its default at exit",
                CAT_U64_SPECIFIC, item, entry.get("current"), default,
            )
        defaults[item] = default
    return defaults


def _speed_session(t):
    """Reconcile at entry, set 1 MHz, restore both speed items on every exit.

    ``set_speed(1)`` writes only ``Turbo Control = Off`` and leaves the
    ``CPU Speed`` the tests set behind (#360), so the exit writes each of
    :data:`_SPEED_ITEMS` back to the ``default`` read at entry -- not the
    entry ``current``, which on a C64 Ultimate may be a killed run's
    residue.  Each item is its own step, so one failed PUT does not skip
    the other.
    """
    _reconcile_on_entry(t.client)
    defaults = _speed_item_defaults(t.client)
    t.set_speed(1)
    failures = []
    try:
        yield t
    finally:
        failures = _teardown_steps([
            (
                f"restore {item}={value!r}",
                lambda item=item, value=value: t.client.set_config_item(
                    CAT_U64_SPECIFIC, item, value
                ),
            )
            for item, value in defaults.items()
        ])
    _raise_teardown_failures("CPU speed restore", failures)


@pytest.fixture(scope="module")
def transport():
    """The module's locked transport; read-only tests use it on its own."""
    yield from _locked_transport(_HOST, _PW)


@pytest.fixture(scope="module")
def speed_baseline(transport):
    """For the tests that write CPU speed: gate, reconcile at entry, restore.

    Entry reconciliation is the guarantee -- a SIGKILLed predecessor's
    residue (CPU Speed 8 on a freshly flashed U64E, #276) is cleared by the
    *next* run.  The exit restore is a courtesy that SIGKILL skips.
    """
    if not _ALLOW_MUTATE:
        pytest.skip("U64_ALLOW_MUTATE not set — CPU-speed tests write device config")
    yield from _speed_session(transport)


def test_protocol_conformance(transport: Ultimate64Transport) -> None:
    assert isinstance(transport, C64Transport)


def test_dimensions(transport: Ultimate64Transport) -> None:
    assert transport.screen_cols == 40
    assert transport.screen_rows == 25


def test_read_memory_screen_area(transport: Ultimate64Transport) -> None:
    data = transport.read_memory(0x0400, 1000)
    assert isinstance(data, bytes)
    assert len(data) == 1000


def test_read_memory_small_range(transport: Ultimate64Transport) -> None:
    data = transport.read_memory(0xA000, 16)  # BASIC ROM area
    assert isinstance(data, bytes)
    assert len(data) == 16


def test_read_screen_codes(transport: Ultimate64Transport) -> None:
    codes = transport.read_screen_codes()
    assert isinstance(codes, list)
    assert len(codes) == 1000
    assert all(isinstance(c, int) and 0 <= c <= 255 for c in codes)


def test_read_registers_removed_from_protocol(transport: Ultimate64Transport) -> None:
    """``read_registers`` is not part of ``C64Transport`` — VICE-only.

    The Ultimate64 transport must not advertise the attribute at all
    (so that ``hasattr`` checks in cross-backend helpers can dispatch
    cleanly).
    """
    assert not hasattr(transport, "read_registers")


def test_read_palette(transport: Ultimate64Transport) -> None:
    """``read_palette`` returns the canonical 16-entry VIC palette."""
    palette = transport.read_palette()
    assert len(palette) == 16
    assert palette[0] == (0x00, 0x00, 0x00)
    assert palette[1] == (0xFF, 0xFF, 0xFF)


def test_read_framebuffer_returns_one_frame(transport: Ultimate64Transport) -> None:
    """Capturing one frame should produce a dict matching the VICE shape.

    Requires the device to be able to reach the host on UDP
    ``DEFAULT_VIDEO_PORT`` (11000).  Skips with a clear message if the
    stream cannot be received (firewall, NAT, etc.).
    """
    from c64_test_harness.backends.ultimate64_client import Ultimate64Error
    from c64_test_harness.transport import TransportError

    try:
        fb = transport.read_framebuffer(timeout=3.0)
    except Ultimate64Error as exc:
        if exc.status == 500:
            # Observed live 2026-07-28: C64U fw 1.1.0 answered HTTP 500
            # "No Operational Network Interface" to video:start with a
            # routed capture host. May be topology-dependent — the skip
            # reason carries the device's actual error body.
            pytest.skip(
                f"video stream start rejected by this firmware: {exc}"
            )
        raise
    except TransportError as exc:
        pytest.skip(f"U64 video stream not reachable from this host: {exc}")

    assert set(fb.keys()) == {"debug_rect", "inner_rect", "bpp", "palette", "bytes"}
    dx, dy, dw, dh = fb["debug_rect"]
    assert dx == 0 and dy == 0
    assert dw > 0 and dh > 0
    ix, iy, iw, ih = fb["inner_rect"]
    assert (iw, ih) == (dw, dh)  # U64 stream has no debug border
    assert fb["bpp"] == 8
    assert isinstance(fb["bytes"], bytes)
    # 1 byte per pixel after unpacking.
    assert len(fb["bytes"]) == dw * dh


# ---------------------------------------------------------------------------
# PR #122 coverage — set_speed / get_speed against real hardware
# ---------------------------------------------------------------------------


@_requires_mutate
@pytest.mark.usefixtures("speed_baseline")
class TestSetSpeed:
    """``set_speed`` / ``get_speed`` round-trip through real U64 turbo state.

    Every test restores 1 MHz in ``finally`` so the module-scoped
    transport leaves the device at the native clock for downstream
    tests (e.g. CIA-timer measurements).
    """

    def test_set_speed_1_disables_turbo(
        self, transport: Ultimate64Transport
    ) -> None:
        try:
            transport.set_speed(1)
            assert transport.get_speed() == 1
            assert get_turbo_mhz(transport.client) is None
        finally:
            transport.set_speed(1)

    def test_set_speed_none_selects_max(
        self, transport: Ultimate64Transport
    ) -> None:
        """``set_speed(None)`` enables turbo at the device's probed max.

        Since PR #143 the max is probed from the device's CPU-Speed
        presets (64 on a C64 Ultimate fw 1.1.0, 48 on a U64 Elite fw
        3.14; 48 fallback on an inconclusive probe), so the expectation
        is ``max_cpu_speed_mhz(client)`` rather than a literal 48.

        Note the asymmetry with VICE (where ``set_speed(None)`` ⇒ warp on,
        and ``get_speed()`` returns ``None``): on U64, ``set_speed(None)``
        maps to ``set_turbo_mhz(client, <max>)``, which sets Turbo Control
        to ``"Manual"``, so ``get_speed()`` reads back the max (not
        ``None``). ``get_speed()`` only returns ``None`` when turbo is on
        but the CPU-Speed enum is unrecognised — a state ``set_speed``
        does not produce.
        """
        device_max = max_cpu_speed_mhz(transport.client)
        try:
            transport.set_speed(None)
            assert transport.get_speed() == device_max
            assert get_turbo_mhz(transport.client) == device_max
        finally:
            transport.set_speed(1)

    def test_set_speed_4(self, transport: Ultimate64Transport) -> None:
        try:
            transport.set_speed(4)
            assert transport.get_speed() == 4
            assert get_turbo_mhz(transport.client) == 4
        finally:
            transport.set_speed(1)

    def test_set_speed_round_trip(
        self, transport: Ultimate64Transport
    ) -> None:
        """Capture, change to two other values, restore — reads agree with sets."""
        try:
            original = transport.get_speed()
            transport.set_speed(2)
            assert transport.get_speed() == 2
            assert get_turbo_mhz(transport.client) == 2

            transport.set_speed(8)
            assert transport.get_speed() == 8
            assert get_turbo_mhz(transport.client) == 8

            transport.set_speed(original)
            assert transport.get_speed() == original
        finally:
            transport.set_speed(1)

    def test_set_speed_unsupported_raises(
        self, transport: Ultimate64Transport
    ) -> None:
        """Speeds not in the device CPU-Speed enum raise ``ValueError``.

        ``set_turbo_mhz`` validates locally via ``cpu_speed_enum`` so no
        request hits the wire and device state is untouched.
        """
        try:
            with pytest.raises(ValueError):
                transport.set_speed(7)  # 7 MHz is not a supported step
            assert transport.get_speed() == 1
        finally:
            transport.set_speed(1)


# ---------------------------------------------------------------------------
# PR #122 coverage — reset(scope=..., drive=...) against real hardware
# ---------------------------------------------------------------------------


class TestResetScopes:
    """``reset(scope=...)`` dispatches to the right REST endpoint.

    ``scope='cpu'`` and ``scope='drive'`` are exercised by default.
    ``scope='machine'`` triggers a full FPGA reboot and is gated by
    ``U64_DESTRUCTIVE=1`` so default test runs are not disrupted.
    """

    @_requires_mutate
    @pytest.mark.usefixtures("speed_baseline")
    def test_reset_scope_cpu_keeps_device_responsive(
        self, transport: Ultimate64Transport
    ) -> None:
        """Soft 6510 reset must leave the device reachable & memory I/O alive."""
        try:
            transport.reset(scope="cpu")
            time.sleep(1.0)
            assert is_u64_reachable(_HOST, password=_PW)
            data = transport.read_memory(0xA000, 16)
            assert isinstance(data, bytes) and len(data) == 16
            # Settle: the C64's boot sequence walks RAM upward (BASIC
            # memory sizing) for a few seconds after reset. Leaving
            # mid-boot pollutes whatever runs next — e.g. a SocketDMA
            # write to $4000 gets clobbered by the walker (observed
            # live on U64E fw 3.14, 2026-07-28).
            time.sleep(3.0)
        finally:
            transport.set_speed(1)

    @_requires_mutate
    @pytest.mark.usefixtures("speed_baseline")
    def test_reset_scope_default_is_cpu(
        self, transport: Ultimate64Transport
    ) -> None:
        """Calling ``reset()`` with no kwargs must behave like ``scope='cpu'``."""
        try:
            transport.reset()
            time.sleep(1.0)
            assert is_u64_reachable(_HOST, password=_PW)
            # Same boot-settle as test_reset_scope_cpu_keeps_device_responsive:
            # don't hand a mid-boot machine to whatever runs next.
            time.sleep(3.0)
        finally:
            transport.set_speed(1)

    @_requires_mutate
    @pytest.mark.usefixtures("speed_baseline")
    def test_reset_scope_drive_a(
        self, transport: Ultimate64Transport
    ) -> None:
        """``reset(scope='drive', drive='a')`` must dispatch cleanly."""
        try:
            transport.reset(scope="drive", drive="a")
            assert is_u64_reachable(_HOST, password=_PW)
        finally:
            transport.set_speed(1)

    def test_reset_scope_drive_missing_drive_kwarg(
        self, transport: Ultimate64Transport
    ) -> None:
        """``scope='drive'`` without a ``drive=`` kwarg raises ``ValueError``.

        Verified at the protocol layer; no request hits the device.
        """
        with pytest.raises(ValueError, match="drive"):
            transport.reset(scope="drive")

    def test_reset_scope_unknown_raises(
        self, transport: Ultimate64Transport
    ) -> None:
        """Unknown scopes are rejected client-side — device untouched."""
        with pytest.raises(ValueError):
            transport.reset(scope="everything")

    @pytest.mark.skipif(
        not _DESTRUCTIVE,
        reason=(
            "reset(scope='machine') triggers an ~8s reboot — "
            "set U64_DESTRUCTIVE=1 to opt in"
        ),
    )
    @_requires_mutate
    @pytest.mark.usefixtures("speed_baseline")
    def test_reset_scope_machine_reboots_and_recovers(
        self, transport: Ultimate64Transport
    ) -> None:
        """Full FPGA reboot — device must come back reachable within ~15s.

        Destructive: the reboot disrupts any other activity on the device.
        Placed last in the class so the cheaper cases run first.
        """
        try:
            transport.reset(scope="machine")
            deadline = time.monotonic() + 15.0
            recovered = False
            while time.monotonic() < deadline:
                if is_u64_reachable(_HOST, password=_PW):
                    recovered = True
                    break
                time.sleep(0.5)
            assert recovered, (
                f"U64 at {_HOST} did not become reachable within 15s "
                f"after reset(scope='machine')"
            )
            data = transport.read_memory(0xA000, 16)
            assert isinstance(data, bytes) and len(data) == 16
        finally:
            try:
                transport.set_speed(1)
            except Exception:
                pass
